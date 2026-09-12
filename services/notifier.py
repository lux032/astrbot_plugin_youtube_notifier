"""通知服务：单频道检查 → 状态机/新投稿检测 → 渲染图片 → 推送给所有订阅会话。

数据源优先级（live_detect_mode）：
  data_api      默认。官方 Data API v3（仅需 API Key），稳定，支持 @handle。
  livebroadcasts  LiveBroadcasts API（OAuth）查直播 + Data API 查投稿；仅自己的频道。
  feed           legacy Atom feed（⚠️ 端点已不可靠，见 services/feed.py）。
  auto           优先 data_api，未配置 Key 或失败时回退 feed。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .data_api import (
    ApiKeyMissingError,
    InvalidApiKeyError,
    QuotaExceededError,
    YouTubeDataAPI,
)
from .feed import LegacyFeedClient
from .livebroadcasts import LiveBroadcastsClient
from .models import (
    ChannelState,
    FeedResult,
    LiveInfo,
    Notification,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from .state_machine import find_new_videos, process_live
from ..utils import utc_now_iso

_DEFAULT_ENABLED = {
    TYPE_LIVE_START: True,
    TYPE_LIVE_END: True,
    TYPE_NEW_VIDEO: True,
}


class NotificationService:
    def __init__(
        self,
        context,
        store,
        renderer,
        *,
        data_api: Optional[YouTubeDataAPI] = None,
        legacy_feed: Optional[LegacyFeedClient] = None,
        live_broadcasts: Optional[LiveBroadcastsClient] = None,
        live_detect_mode: str = "data_api",
        cover_download: bool = True,
        max_results: int = 5,
        image_dir: Path = Path("data/images"),
        enabled: Optional[dict] = None,
    ):
        self.context = context
        self.store = store
        self.renderer = renderer
        self.data_api = data_api
        self.legacy_feed = legacy_feed
        self.live_broadcasts = live_broadcasts
        self.live_detect_mode = live_detect_mode
        self.cover_download = cover_download
        self.max_results = max(1, min(50, int(max_results)))
        self.image_dir = Path(image_dir)
        self._enabled = dict(_DEFAULT_ENABLED)
        if enabled:
            self._enabled.update(enabled)
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._api_key_warned = False
        if not (data_api and data_api.configured):
            logger.warning(
                "[YT] 未配置 YouTube Data API Key —— 将退化为不稳定的 legacy feed 数据源，"
                "强烈建议在配置中填写 api_key"
            )

    # ------------------------------------------------------------ 主入口

    async def check_channel(self, channel_id: str) -> None:
        """单频道单轮检查：直播状态机 + 新投稿检测 → 渲染并推送。

        单频道串行化，避免 poller 与 WebSub 回调并发修改同一状态。
        """
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            state = self.store.get_channel_state(channel_id)
            if state is None:
                return
            try:
                await self._check_channel_locked(state)
            finally:
                await self.store.save()

    async def process_feed_result(self, channel_id: str, feed: FeedResult) -> None:
        """对一份已获取的快照做状态机 + 新投稿检测 + 推送（供 WebSub 推送复用）。"""
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            state = self.store.get_channel_state(channel_id)
            if state is None:
                logger.debug(f"[YT] 收到未订阅频道 {channel_id} 的推送，忽略")
                return
            notifications = self._apply_snapshot(state, feed)
            try:
                await self._dispatch_filtered(channel_id, notifications)
            finally:
                await self.store.save()

    # ------------------------------------------------------------ 检查流程

    async def _check_channel_locked(self, state: ChannelState) -> None:
        use_api = self.live_detect_mode in ("data_api", "auto") and bool(
            self.data_api and self.data_api.configured
        )
        use_livebroadcasts = self.live_detect_mode == "livebroadcasts"

        if use_livebroadcasts:
            notifications = await self._check_via_livebroadcasts(state)
        elif use_api or self.live_detect_mode in ("data_api", "auto"):
            if not use_api and self.live_detect_mode == "auto":
                logger.info(
                    f"[YT] channel={state.channel_id} 未配置 API Key，回退 legacy feed"
                )
            snapshot = await self._fetch_snapshot(state)
            notifications = self._apply_snapshot(state, snapshot) if snapshot else []
        else:
            # 显式 feed 模式
            snapshot = await self._fetch_legacy_feed(state)
            notifications = self._apply_snapshot(state, snapshot) if snapshot else []

        await self._dispatch_filtered(state.channel_id, notifications)

    async def _fetch_snapshot(self, state: ChannelState) -> Optional[FeedResult]:
        """用 Data API 拉取频道快照（必要时先解析并缓存 uploads 播放列表）。"""
        try:
            # 需要解析的情况：① 缺 uploads 播放列表；② 频道名来自抓页面
            # （可能是英文 og:title），拿到 API Key 后更正一次官方名称。
            if not state.uploads_playlist_id or not state.name_from_api:
                meta = await self.data_api.resolve_channel(state.channel_id)
                if meta.uploads_playlist_id:
                    state.uploads_playlist_id = meta.uploads_playlist_id
                if meta.title:
                    self.store.set_channel_name(state.channel_id, meta.title)
                    state.channel_name = meta.title
                state.name_from_api = True
                if not state.uploads_playlist_id:
                    logger.warning(
                        f"[YT] channel={state.channel_id} 未取到 uploads 播放列表"
                    )
                    return None
            return await self.data_api.fetch_snapshot(
                state.channel_id,
                state.uploads_playlist_id,
                max_results=self.max_results,
                channel_name=state.channel_name,
            )
        except QuotaExceededError as exc:
            logger.error(
                f"[YT] Data API 配额耗尽，本轮跳过 channel={state.channel_id}: {exc}"
            )
        except InvalidApiKeyError as exc:
            logger.error(f"[YT] API Key 无效，请检查配置: {exc}")
        except ApiKeyMissingError as exc:
            # 每轮都会走到这里，只报一次免得刷屏
            if not self._api_key_warned:
                self._api_key_warned = True
                logger.error(
                    f"[YT] {exc} —— 监控不会工作。"
                    "请在插件配置 basic.api_key 填入 YouTube Data API Key 后重载插件"
                )
            else:
                logger.debug(f"[YT] 仍未配置 API Key，跳过 channel={state.channel_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[YT] channel={state.channel_id} Data API 拉取失败: {exc!r}"
            )
        return None

    async def _fetch_legacy_feed(self, state: ChannelState) -> Optional[FeedResult]:
        if self.legacy_feed is None:
            return None
        feed = await self.legacy_feed.fetch_feed(state.channel_id)
        if feed is not None and feed.channel_name:
            self.store.set_channel_name(state.channel_id, feed.channel_name)
            state.channel_name = feed.channel_name
        return feed

    async def _check_via_livebroadcasts(self, state: ChannelState) -> list[Notification]:
        """LiveBroadcasts(OAuth) 查直播 + Data API 查投稿（仅自己的频道）。"""
        now_iso = utc_now_iso()
        notifications: list[Notification] = []
        snapshot: Optional[FeedResult] = None

        if self.live_broadcasts is not None:
            try:
                lives = await self.live_broadcasts.fetch_live_broadcasts()
                mine = [
                    lv
                    for lv in lives
                    if not state.channel_id or lv.channel_id == state.channel_id
                ]
                live = mine[0] if mine else None
                if live:
                    live.channel_name = state.channel_name
                notifications.extend(process_live(live, state, now_iso))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[YT] channel={state.channel_id} LiveBroadcasts 检测失败: {exc!r}"
                )

        # 投稿始终走 Data API（若可用），否则 legacy feed
        if self.data_api and self.data_api.configured:
            snapshot = await self._fetch_snapshot(state)
        else:
            snapshot = await self._fetch_legacy_feed(state)
        if snapshot is not None:
            notifications.extend(find_new_videos(snapshot.entries, state, now_iso))
        return notifications

    def _apply_snapshot(self, state: ChannelState, snapshot: FeedResult) -> list[Notification]:
        """对一份快照运行状态机与新投稿检测，返回通知列表。"""
        now_iso = utc_now_iso()
        if snapshot.channel_name:
            self.store.set_channel_name(state.channel_id, snapshot.channel_name)
            state.channel_name = snapshot.channel_name

        notifications: list[Notification] = []
        live_entry = snapshot.find_live()
        live: Optional[LiveInfo] = None
        if live_entry is not None:
            live = LiveInfo(
                live_id=live_entry.video_id,
                title=live_entry.title,
                channel_id=state.channel_id,
                channel_name=snapshot.channel_name or state.channel_name,
                thumbnail_url=live_entry.thumbnail_url,
                # 直播实际开始时间优先于发布时间
                start_time=live_entry.actual_start_time or live_entry.published_at,
                url=live_entry.url,
            )
        # 若上一次在直播、本轮已不在直播，尝试取该场的实际结束时间，算出准确时长
        ended_iso = ""
        if live is None and state.last_live_id:
            prev = next(
                (e for e in snapshot.entries if e.video_id == state.last_live_id), None
            )
            if prev is not None and prev.actual_end_time:
                ended_iso = prev.actual_end_time

        notifications.extend(
            process_live(live, state, now_iso, ended_at_iso=ended_iso)
        )
        notifications.extend(find_new_videos(snapshot.entries, state, now_iso))
        return notifications

    async def _dispatch_filtered(
        self, channel_id: str, notifications: list[Notification]
    ) -> None:
        if not notifications:
            return
        filtered = [n for n in notifications if self._enabled.get(n.type, True)]
        if not filtered:
            return
        await self.dispatch(channel_id, filtered)

    # ------------------------------------------------------------ 推送

    async def dispatch(self, channel_id: str, notifications: list[Notification]) -> None:
        sessions = self.store.sessions_for_channel(channel_id)
        if not sessions:
            logger.debug(f"[YT] channel={channel_id} 无订阅会话，跳过推送")
            return
        for n in notifications:
            path = await self._render_notification(n)
            if not path:
                logger.warning(f"[YT] 通知渲染失败 type={n.type} video={n.video_id}")
                continue
            for session in sessions:
                try:
                    await self.context.send_message(
                        session, MessageChain().file_image(path)
                    )
                    logger.info(
                        f"[YT] 已推送 {n.type} → session={session} "
                        f"channel={channel_id} video={n.video_id or '-'}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[YT] 推送失败 session={session} type={n.type}: {exc!r}"
                    )

    async def _render_notification(self, n: Notification) -> Optional[str]:
        data = n.to_dict()
        data["thumbnail_path"] = ""
        if self.cover_download and n.thumbnail_url and self.data_api is not None:
            thumb_path = self.image_dir / f"{n.video_id or 'thumb'}.jpg"
            ok = await self.data_api.download_image(n.thumbnail_url, thumb_path)
            if ok and thumb_path.exists():
                data["thumbnail_path"] = str(thumb_path)
        return await self.renderer.render(data)
