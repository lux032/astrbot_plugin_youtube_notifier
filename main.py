"""AstrBot YouTube 订阅提醒插件。

订阅 YouTube 频道，直播上播/下播与新投稿以「文生图」图片通知推送。
订阅按会话（unified_msg_origin）隔离。

指令：
    /yt订阅 <channel_id>      订阅频道
    /yt取消订阅 <channel_id>  取消订阅
    /yt列表                   查看本会话订阅
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .renderer import NotificationRenderer
from .services.data_api import (
    ApiKeyMissingError,
    ChannelNotFoundError,
    InvalidApiKeyError,
    QuotaExceededError,
    YouTubeDataAPI,
    parse_channel_input,
)
from .services.feed import LegacyFeedClient
from .services.livebroadcasts import LiveBroadcastsClient
from .services.models import TYPE_LIVE_END, TYPE_LIVE_START, TYPE_NEW_VIDEO
from .services.notifier import NotificationService
from .services.oauth import OAuthManager
from .services.poller import PollScheduler
from .services.state_machine import seed_channel_from_feed
from .services.store import SubscriptionStore
from .services.websub import WebSubManager
from .services.websub_server import WebSubCallbackServer
from .utils import format_time_zh

PLUGIN_NAME = "astrbot_plugin_youtube_notifier"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


@register(
    PLUGIN_NAME,
    "yuiasami",
    "订阅 YouTube 频道，直播上/下播与新投稿以图片形式推送。",
    "v1.0.0",
)
class YouTubeNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # AstrBotConfig 是 dict 子类；这里只做只读的 .get 取值，
        # 若框架未传配置则退化为空 dict（不构造 AstrBotConfig，避免误当作文件路径）。
        self.config = config if config is not None else {}
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)

        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy: str = ""
        self.store: Optional[SubscriptionStore] = None
        self.data_api: Optional[YouTubeDataAPI] = None
        self.notifier: Optional[NotificationService] = None
        self.poller: Optional[PollScheduler] = None
        self.websub: Optional[WebSubManager] = None
        self.websub_server: Optional[WebSubCallbackServer] = None

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        cfg = self.config
        basic = cfg.get("basic", {}) or {}
        oauth_cfg = cfg.get("oauth", {}) or {}
        websub_cfg = cfg.get("websub", {}) or {}
        notify_cfg = cfg.get("notify", {}) or {}
        render_cfg = cfg.get("render", {}) or {}

        logger.info(f"[YT] 正在初始化 {PLUGIN_NAME} ...")

        proxy = str(basic.get("proxy", "") or "")
        self._proxy = proxy
        self._session = aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "astrbot-youtube-notifier/1.0"},
        )
        oauth = OAuthManager(
            self._session,
            client_id=str(oauth_cfg.get("client_id", "") or ""),
            client_secret=str(oauth_cfg.get("client_secret", "") or ""),
            refresh_token=str(oauth_cfg.get("refresh_token", "") or ""),
        )
        self.data_api = YouTubeDataAPI(
            self._session, api_key=str(basic.get("api_key", "") or ""), proxy=proxy
        )
        legacy_feed = LegacyFeedClient(self._session, proxy=proxy)
        live_broadcasts = LiveBroadcastsClient(self._session, oauth, proxy=proxy)

        self.store = SubscriptionStore(self.data_dir)
        self.store.load()

        renderer = NotificationRenderer(
            image_width=int(render_cfg.get("image_width", 800) or 800),
            font_path=str(render_cfg.get("font_path", "") or ""),
            output_dir=self.data_dir / "images" / "notifications",
        )
        mode = str(basic.get("live_detect_mode", "data_api") or "data_api")
        self.notifier = NotificationService(
            self.context,
            self.store,
            renderer,
            data_api=self.data_api,
            legacy_feed=legacy_feed,
            live_broadcasts=live_broadcasts,
            live_detect_mode=mode,
            cover_download=bool(basic.get("cover_download", True)),
            max_results=int(basic.get("max_results", 5) or 5),
            image_dir=self.data_dir / "images" / "covers",
            enabled={
                TYPE_LIVE_START: bool(notify_cfg.get("live_start_enabled", True)),
                TYPE_LIVE_END: bool(notify_cfg.get("live_end_enabled", True)),
                TYPE_NEW_VIDEO: bool(notify_cfg.get("new_video_enabled", True)),
            },
        )

        self.poller = PollScheduler(
            self.store,
            self.notifier,
            interval_seconds=int(basic.get("poll_interval_seconds", 300) or 300),
        )
        self.poller.start()

        await self._start_websub(websub_cfg)

        api_state = "已配置" if self.data_api.configured else "❌ 未配置（数据源不可靠）"
        logger.info(
            f"[YT] 初始化完成: 模式={mode} API Key={api_state} "
            f"频道数={len(self.store.all_channel_ids())}"
        )

    async def _start_websub(self, websub_cfg: dict) -> None:
        if not bool(websub_cfg.get("enabled", False)):
            logger.info("[YT] WebSub 未启用，新投稿由轮询检测")
            return
        callback_url = str(websub_cfg.get("callback_url", "") or "")
        verify_token = str(websub_cfg.get("verify_token", "") or "")
        if not callback_url or not verify_token:
            logger.warning(
                "[YT] WebSub 已启用但缺少 callback_url/verify_token，已跳过"
            )
            return

        self.websub = WebSubManager(
            self._session, self.notifier, callback_url, verify_token
        )
        self.websub_server = WebSubCallbackServer(
            port=int(websub_cfg.get("callback_port", 8477) or 8477),
            on_verify=self.websub.handle_verification,
            on_push=self.websub.handle_push,
        )
        try:
            await self.websub_server.start()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[YT] WebSub 回调服务启动失败: {exc!r}")
            self.websub_server = None
            return

        # 为已有订阅补订阅
        for channel_id in self.store.all_channel_ids():
            await self.websub.subscribe(channel_id)

    async def terminate(self):
        logger.info(f"[YT] 正在停止 {PLUGIN_NAME} ...")
        if self.poller is not None:
            await self.poller.stop()
        if self.websub_server is not None:
            await self.websub_server.stop()
        if self.store is not None:
            await self.store.save()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        logger.info(f"[YT] {PLUGIN_NAME} 已停止")

    # ------------------------------------------------------------ 指令

    @filter.command("yt订阅", alias={"yt_subscribe", "youtube订阅"})
    async def subscribe(self, event: AstrMessageEvent, channel_id: str = ""):
        """订阅 YouTube 频道，格式: /yt订阅 <@handle 或 频道ID 或 频道URL>"""
        raw = (channel_id or "").strip()
        if not raw:
            yield event.plain_result(
                "用法: /yt订阅 <@handle 或 频道ID 或 频道URL>\n"
                "例如:\n"
                "  /yt订阅 @ukaisaki\n"
                "  /yt订阅 https://www.youtube.com/@ukaisaki\n"
                "  /yt订阅 UCxxxxxxxxxxxxxxxxxxxxxx"
            )
            return

        kind, value = parse_channel_input(raw)
        if not value:
            yield event.plain_result("频道标识为空，请检查输入")
            return

        session_id = event.unified_msg_origin

        try:
            meta, from_api = await self._resolve_channel(raw)
        except ChannelNotFoundError:
            yield event.plain_result(f"未找到频道: {raw}\n请确认 handle 或频道 ID 是否正确")
            return
        except QuotaExceededError:
            yield event.plain_result("YouTube API 配额已耗尽，请稍后再试")
            return
        except InvalidApiKeyError:
            yield event.plain_result("YouTube API Key 无效，请检查插件配置")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 订阅时解析频道失败 {raw}: {exc!r}")
            yield event.plain_result("解析频道失败，请稍后重试")
            return

        resolved_id = meta.channel_id
        if not resolved_id:
            yield event.plain_result(f"未能解析出频道 ID: {raw}")
            return

        if self.store.has_subscription(session_id, resolved_id):
            yield event.plain_result(
                f"本会话已订阅该频道: {meta.title or resolved_id}"
            )
            return

        self.store.add_subscription(session_id, resolved_id, meta.title)
        state = self.store.get_channel_state(resolved_id)
        if state is not None:
            if meta.uploads_playlist_id:
                state.uploads_playlist_id = meta.uploads_playlist_id
            if meta.handle:
                state.channel_handle = meta.handle
            elif kind not in ("id",):
                state.channel_handle = f"@{value}"
            # 记录名字来源：抓页面拿到的可能是其它语言，之后要用 API 更正一次
            state.name_from_api = from_api

        # 静默播种：避免刚订阅就把历史视频/正在进行的直播刷给用户
        snapshot = await self._fetch_snapshot_quiet(resolved_id, meta)
        if state is not None and snapshot is not None:
            seed_channel_from_feed(state, snapshot)
        await self.store.save()

        if self.websub is not None:
            await self.websub.subscribe(resolved_id)

        logger.info(
            f"[YT] 新订阅 session={session_id} channel={resolved_id} "
            f"name={meta.title} input={raw}"
        )

        # 数据源不可用时必须显式告知：否则订阅「成功」了但监控完全不工作，
        # 用户只会看到含糊的「暂无记录」，误以为一切正常。
        if not self._monitoring_ready():
            logger.error(
                f"[YT] 订阅已保存但监控不会工作：{self._monitoring_blocker()}"
            )
            yield event.plain_result(
                f"⚠️ 已保存订阅: {meta.title or resolved_id}\n"
                f"频道ID: {resolved_id}\n\n"
                f"但**监控尚未生效** —— {self._monitoring_blocker()}\n"
                "修好后即可正常推送，已保存的订阅会自动开始工作。"
            )
            return

        yield event.plain_result(
            f"已订阅频道: {meta.title or resolved_id}\n"
            f"频道ID: {resolved_id}\n"
            "直播上/下播与新投稿将以图片通知推送。"
        )

    @filter.command("yt取消订阅", alias={"yt_unsubscribe", "youtube取消订阅"})
    async def unsubscribe(self, event: AstrMessageEvent, channel_id: str = ""):
        """取消订阅 YouTube 频道，格式: /yt取消订阅 <@handle 或 频道ID>"""
        raw = (channel_id or "").strip()
        if not raw:
            yield event.plain_result("用法: /yt取消订阅 <@handle 或 频道ID>")
            return

        session_id = event.unified_msg_origin
        # 先按原样匹配，再尝试把 handle 解析成频道 ID 后匹配
        candidates = [raw]
        _, value = parse_channel_input(raw)
        if value:
            candidates.append(value)
        target = next(
            (
                cid
                for cid in self.store.get_session_channels(session_id)
                if cid in candidates
            ),
            None,
        )
        if target is None:
            # handle → channel_id 需要一次查询
            try:
                meta = await self._resolve_channel(raw)
                target = meta.channel_id if self.store.has_subscription(
                    session_id, meta.channel_id
                ) else None
            except Exception:  # noqa: BLE001
                target = None
        if target is None:
            yield event.plain_result(f"本会话未订阅该频道: {raw}")
            return

        self.store.remove_subscription(session_id, target)
        await self.store.save()

        # 若已无任何会话订阅该频道，向 hub 退订
        if self.websub is not None and not self.store.sessions_for_channel(target):
            await self.websub.unsubscribe(target)

        logger.info(f"[YT] 取消订阅 session={session_id} channel={target}")
        yield event.plain_result(f"已取消订阅: {target}")

    @filter.command("yt列表", alias={"yt_list", "youtube列表"})
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看本会话已订阅的频道"""
        session_id = event.unified_msg_origin
        channels = self.store.get_session_channels(session_id)
        if not channels:
            yield event.plain_result("本会话暂无订阅。\n用 /yt订阅 @handle 添加。")
            return

        lines = [f"本会话已订阅 {len(channels)} 个频道:"]
        for idx, (channel_id, meta) in enumerate(channels.items(), 1):
            state = self.store.get_channel_state(channel_id)
            name = (state.channel_name if state else "") or meta.get("channel_name") or ""
            handle = (state.channel_handle if state else "") or ""
            status = _status_text(state)
            lines.append(f"{idx}. {name or channel_id} {handle}".rstrip())
            lines.append(f"    ID: {channel_id}｜{status}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 数据源就绪检查

    def _monitoring_ready(self) -> bool:
        """当前配置下监控是否真的能工作。"""
        return self._monitoring_blocker() == ""

    def _monitoring_blocker(self) -> str:
        """返回阻碍监控的原因；一切就绪返回空串。"""
        mode = self.notifier.live_detect_mode if self.notifier else "data_api"
        api_ok = bool(self.data_api and self.data_api.configured)
        if mode == "data_api":
            if not api_ok:
                return (
                    "未配置 YouTube Data API Key（配置项 basic.api_key）。"
                    "请到 Google Cloud 免费申请后填入并重载插件"
                )
        elif mode == "livebroadcasts":
            if not (self.data_api and self.data_api.configured):
                return "livebroadcasts 模式需要 Data API Key 用于拉取投稿（basic.api_key 为空）"
            if not self._oauth_configured():
                return "livebroadcasts 模式需要配置 OAuth（oauth.client_id/secret/refresh_token）"
        # auto / feed 模式总会尝试，不算阻塞（feed 本身不稳定，仅告警）
        return ""

    def _oauth_configured(self) -> bool:
        oauth_cfg = (self.config.get("oauth", {}) or {})
        return all(
            str(oauth_cfg.get(k, "") or "").strip()
            for k in ("client_id", "client_secret", "refresh_token")
        )

    # ------------------------------------------------------------ 频道解析辅助

    async def _resolve_channel(self, raw: str):
        """解析频道标识：优先 Data API（官方），无 Key 时抓频道页兜底。

        Returns:
            (ChannelMeta, from_api) —— from_api 表示名称是否来自官方 API。
        """
        if self.data_api is not None and self.data_api.configured:
            return await self.data_api.resolve_channel(raw), True

        # 无 API Key：抓频道页解析
        from .services.scrape import resolve_channel_via_html

        meta = await resolve_channel_via_html(
            self._session, raw, proxy=self._proxy or None
        )
        if meta is None:
            raise ChannelNotFoundError(f"无法解析频道: {raw}")
        logger.warning(
            "[YT] 未配置 API Key，使用网页抓取解析频道（不稳定，建议配置 api_key）"
        )
        return meta, False

    async def _fetch_snapshot_quiet(self, channel_id: str, meta):
        """订阅时静默拉一次快照用于播种，失败不影响订阅本身。"""
        try:
            if not meta.uploads_playlist_id:
                return None
            return await self.data_api.fetch_snapshot(
                channel_id,
                meta.uploads_playlist_id,
                max_results=self.notifier.max_results,
                channel_name=meta.title,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 订阅播种快照失败 channel={channel_id}: {exc!r}")
            return None


def _status_text(state) -> str:
    """频道当前状态的简短描述。

    注意区分「尚未检测」与「已同步但确实没有投稿/直播」：
    主播型频道的上传列表全是直播存档，没有普通投稿，
    若笼统显示「暂无记录」会让人误以为插件没在工作。
    """
    if state is None:
        return "状态未知"
    if state.last_status == "live":
        return "🔴 直播中"
    if state.last_status == "ended":
        when = format_time_zh(state.last_live_end_at)
        title = state.last_live_title
        if len(title) > 18:
            title = title[:18] + "…"
        suffix = f"（{when}）" if when else ""
        return f"⚫ 上次直播已结束{suffix}" + (f": {title}" if title else "")
    if state.latest_video_title:
        title = state.latest_video_title
        if len(title) > 20:
            title = title[:20] + "…"
        return f"最近投稿: {title}"
    if state.video_seeded:
        return "✅ 已同步（暂无投稿或直播）"
    return "⏳ 尚未完成首次检测"
