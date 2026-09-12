"""AstrBot YouTube 订阅提醒插件。

订阅 YouTube 频道，直播上播/下播与新投稿以「文生图」图片通知推送。
订阅按会话（unified_msg_origin）隔离。

指令：
    /yt订阅 <channel_id>      订阅频道
    /yt取消订阅 <channel_id>  取消订阅
    /yt列表                   查看本会话订阅
    /yt直播测试 <目标>        抓目标直播并渲染推送一张测试图
    /yt视频测试 <目标>        抓目标最新视频并渲染推送一张测试图
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
from .services.models import (
    ChannelMeta,
    FeedResult,
    Notification,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from .services.notifier import NotificationService
from .services.oauth import OAuthManager
from .services.page_json import ChannelPageClient, parse_target
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
        self.page_json: Optional[ChannelPageClient] = None
        self.notifier: Optional[NotificationService] = None
        self.poller: Optional[PollScheduler] = None
        self.websub: Optional[WebSubManager] = None
        self.websub_server: Optional[WebSubCallbackServer] = None
        # 运行期降级原因（如「API Key 无效」）。配置检查发现不了这类问题 ——
        # 配了 Key 不等于 Key 能用，必须靠真实调用暴露出来。
        self._runtime_degraded: str = ""

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
        self.page_json = ChannelPageClient(
            self._session,
            proxy=proxy,
            min_interval=float(
                basic.get("page_fallback_min_interval_seconds", 60) or 60
            ),
        )

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
            page_json=self.page_json,
            live_detect_mode=mode,
            page_fallback_enabled=bool(basic.get("page_fallback_enabled", True)),
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

        api_state = "已配置" if self.data_api.configured else "未配置（走网页兜底）"
        fallback_state = "开" if self.notifier.page_fallback_enabled else "关"
        logger.info(
            f"[YT] 初始化完成: 模式={mode} API Key={api_state} "
            f"网页兜底={fallback_state} 频道数={len(self.store.all_channel_ids())}"
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

        notice = self._degraded_notice()
        if notice:
            yield event.plain_result(
                f"已订阅频道: {meta.title or resolved_id}\n"
                f"频道ID: {resolved_id}\n"
                "直播上/下播与新投稿将以图片通知推送。\n\n"
                f"⚠️ {notice}"
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
            # 降级必须可见（否则用户不知道数据来自网页兜底）
            degraded = self.notifier.degraded_reason(channel_id) if self.notifier else ""
            if degraded:
                lines.append(f"    ⚠️ 数据源已降级: {degraded}")
            elif self._runtime_degraded:
                lines.append(f"    ⚠️ 数据源已降级: {self._runtime_degraded}")

        notice = self._degraded_notice()
        if notice:
            lines.append("")
            lines.append(f"⚠️ {notice}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 测试指令

    @filter.command("yt直播测试", alias={"yt_live_test", "youtube直播测试"})
    async def live_test(self, event: AstrMessageEvent, target: str = ""):
        """抓取目标当前直播并渲染推送一张直播通知图（测试渲染与推送链路）

        用法: /yt直播测试 <@handle 或 频道ID 或 频道URL 或 视频URL>
        """
        yield event.plain_result(
            await self._run_test(event, target, want_live=True)
        )

    @filter.command("yt视频测试", alias={"yt_video_test", "youtube视频测试"})
    async def video_test(self, event: AstrMessageEvent, target: str = ""):
        """抓取目标最新视频并渲染推送一张新投稿通知图（测试渲染与推送链路）

        用法: /yt视频测试 <@handle 或 频道ID 或 频道URL 或 视频URL>
        """
        yield event.plain_result(
            await self._run_test(event, target, want_live=False)
        )

    _TEST_USAGE = (
        "用法: {cmd} <@handle 或 频道ID 或 频道URL 或 视频URL>\n"
        "例如:\n"
        "  {cmd} @NASA\n"
        "  {cmd} https://www.youtube.com/@MrBeast\n"
        "  {cmd} https://www.youtube.com/watch?v=gTKS8SAwUzE\n"
        "说明: 测试命令会真实抓取数据并渲染推送一张图"
        "（图上带「🧪 测试」标记），用于验证渲染与推送链路是否正常。"
    )

    async def _run_test(self, event: AstrMessageEvent, target: str, *, want_live: bool) -> str:
        """测试命令公共实现，返回给用户的文字说明。"""
        cmd = "/yt直播测试" if want_live else "/yt视频测试"
        raw = (target or "").strip()
        if not raw:
            return self._TEST_USAGE.format(cmd=cmd)

        if self.notifier is None or self.page_json is None:
            return "插件尚未初始化完成（或初始化失败），请稍后重试或查看日志。"

        kind, value = parse_target(raw)
        if not value:
            return f"无法识别的目标: {raw}\n\n" + self._TEST_USAGE.format(cmd=cmd)

        session = event.unified_msg_origin
        if kind == "video":
            return await self._test_from_video(session, value, want_live=want_live)
        return await self._test_from_channel(session, raw, want_live=want_live)

    async def _test_from_video(self, session: str, video_id: str, *, want_live: bool) -> str:
        """按视频 URL 生成测试通知（走观看页 ytInitialData）。"""
        data = await self.page_json.fetch_video(video_id, respect_throttle=False)
        if data is None:
            return (
                f"抓取视频失败: {video_id}\n"
                "可能原因：网络/代理不通、视频不可访问，或页面结构变化。请查看日志。"
            )

        entry = data.entry
        note = ""
        if want_live and not data.is_live_now:
            note = (
                "\n⚠️ 注意：该视频**当前不在直播**，这张图是借用它的信息生成的样例"
                "（真实推送只会在检测到直播时发出）。"
            )
        elif not want_live and data.is_live_now:
            note = "\n⚠️ 注意：该视频**正在直播**，新投稿通知图仅作渲染效果展示。"

        notification = Notification(
            type=TYPE_LIVE_START if want_live else TYPE_NEW_VIDEO,
            title=entry.title,
            channel_id=entry.channel_id,
            channel_name=entry.channel_name,
            thumbnail_url=entry.thumbnail_url,
            start_time=entry.actual_start_time if want_live else entry.published_at,
            url=entry.url,
            video_id=entry.video_id,
        )
        return await self._push_test(
            session, notification, source=f"视频 {video_id}", note=note
        )

    async def _test_from_channel(self, session: str, raw: str, *, want_live: bool) -> str:
        """按频道标识生成测试通知（自动挑选直播或最新投稿）。"""
        try:
            meta, _ = await self._resolve_channel(raw)
        except ChannelNotFoundError:
            return f"未找到频道: {raw}\n请确认 handle / 频道ID / 链接是否正确"
        except QuotaExceededError:
            return "YouTube API 配额已耗尽，且网页兜底也没取到数据，请稍后重试"
        except InvalidApiKeyError:
            return "YouTube API Key 无效，请检查插件配置 basic.api_key"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 测试命令解析频道失败 {raw}: {exc!r}")
            return "解析频道失败，请稍后重试或查看日志"

        snapshot = await self._fetch_test_snapshot(meta)
        if snapshot is None or not snapshot.entries:
            return (
                f"未取到频道 {meta.title or raw} 的视频数据。\n"
                "可能原因：网络/代理不通、频道无公开视频，或页面结构变化。请查看日志。"
            )

        entries = snapshot.entries
        channel_name = snapshot.channel_name or meta.title
        note = ""

        if want_live:
            chosen = snapshot.find_live()
            if chosen is None:
                # 没在直播：也没法可靠找到「最近一场直播」——
                # 网页数据里 /streams 只保留直播，往期直播存档与普通投稿
                # 无法区分（角标一样，见 services/page_json.py）。
                # 如实说明：这张图只是借用最新投稿验证渲染链路。
                chosen = entries[0]
                note = (
                    "\n⚠️ 该频道**当前没有直播**。网页数据无法可靠找出往期直播，"
                    f"这张图借用最新投稿（{chosen.title[:30]}）的信息生成，"
                    "仅用于验证渲染效果。"
                )
            start_time = chosen.actual_start_time or chosen.published_at
        else:
            regular = [e for e in entries if not e.live_state] or entries
            chosen = regular[0]
            start_time = chosen.published_at
            if chosen.was_live:
                note = "\n⚠️ 该频道最近只有直播内容，这张图借用直播存档生成。"

        notification = Notification(
            type=TYPE_LIVE_START if want_live else TYPE_NEW_VIDEO,
            title=chosen.title,
            channel_id=meta.channel_id,
            channel_name=chosen.channel_name or channel_name,
            thumbnail_url=chosen.thumbnail_url,
            start_time=start_time,
            url=chosen.url,
            video_id=chosen.video_id,
        )
        return await self._push_test(
            session, notification, source=f"频道 {channel_name or meta.channel_id}", note=note
        )

    async def _fetch_test_snapshot(self, meta: ChannelMeta) -> Optional[FeedResult]:
        """测试命令取快照：Data API 优先（数据准确），失败/无 Key 时用网页兜底。

        网页兜底在这里很划算 —— 测试命令不消耗 API 配额，没配 Key 也能用。
        """
        max_results = max(10, (self.notifier.max_results if self.notifier else 5))
        if self.data_api is not None and self.data_api.configured:
            try:
                return await self.data_api.fetch_snapshot(
                    meta.channel_id,
                    meta.uploads_playlist_id,
                    max_results=max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 测试命令 Data API 取快照失败，改用网页兜底: {exc!r}")
        if self.page_json is not None:
            return await self.page_json.fetch_snapshot(
                meta.channel_id,
                meta.handle or meta.channel_id,
                max_results=max_results,
                channel_name=meta.title,
                respect_throttle=False,  # 交互式命令：要当前真实数据
            )
        return None

    async def _push_test(
        self, session: str, notification: Notification, *, source: str, note: str = ""
    ) -> str:
        """渲染并推送测试图，返回给用户的文字说明。"""
        path = await self.notifier.dispatch_test(session, notification)
        if path is None:
            return (
                f"渲染或推送失败（{source}）。\n"
                "请检查日志：渲染失败通常是字体问题，推送失败通常是会话/适配器问题。"
            )
        return (
            f"🧪 测试通知已推送（{source}）\n"
            f"标题: {notification.title or '（无标题）'}\n"
            f"频道: {notification.channel_name or notification.channel_id or '未知'}\n"
            f"链接: {notification.url}\n"
            f"渲染链路与真实推送完全一致，仅图上多了「测试」标记。{note}"
        )

    # ------------------------------------------------------------ 数据源就绪检查

    def _monitoring_ready(self) -> bool:
        """当前配置下监控是否真的能工作。"""
        return self._monitoring_blocker() == ""

    def _page_fallback_ready(self) -> bool:
        """网页兜底是否可用（决定没配 Key 时监控还能不能工作）。"""
        if self.page_json is None or self.notifier is None:
            return False
        return bool(self.notifier.page_fallback_enabled)

    def _monitoring_blocker(self) -> str:
        """返回阻碍监控的原因；一切就绪（含可用降级）返回空串。

        语义约定（别轻易改，测试锁定了这套矩阵）：

          livebroadcasts  需要 API Key + OAuth，缺一即阻塞
          data_api        用户**显式要求官方 API**：没 Key 且网页兜底也关了
                          → 阻塞；网页兜底可用 → 不算阻塞，但由
                          `_degraded_notice()` 提示正在降级运行
          auto / feed     语义就是「尽力而为」，永不阻塞；实际走哪条链路
                          由 `_degraded_notice()` 如实说明

        「不阻塞」不等于「不提示」：能工作但降级时必须让用户知道，
        否则就是 CLAUDE.md 禁止的静默降级。
        """
        mode = self.notifier.live_detect_mode if self.notifier else "data_api"
        api_ok = bool(self.data_api and self.data_api.configured)

        if mode == "livebroadcasts":
            if not api_ok:
                return "livebroadcasts 模式需要 Data API Key 用于拉取投稿（basic.api_key 为空）"
            if not self._oauth_configured():
                return "livebroadcasts 模式需要配置 OAuth（oauth.client_id/secret/refresh_token）"
            return ""

        if mode in ("auto", "feed"):
            return ""

        # mode == data_api
        if api_ok or self._page_fallback_ready():
            return ""
        return (
            "未配置 YouTube Data API Key（配置项 basic.api_key），"
            "且网页兜底已被关闭（basic.page_fallback_enabled=false）。"
            "请到 Google Cloud 免费申请 API Key 后填入并重载插件"
        )

    def _degraded_notice(self) -> str:
        """降级运行说明；未降级返回空串。

        必须与 notifier 的实际降级链一致，否则就成了骗人的提示。
        运行期降级（如 Key 无效）优先于配置层面的判断 ——
        「配了 Key」不等于「Key 能用」。
        """
        if self.notifier is None:
            return ""
        if self._runtime_degraded:
            return (
                f"数据源已降级：{self._runtime_degraded}。"
                "已自动改用网页 JSON 兜底继续监控（数据可用但不够精确）。"
                "请检查 basic.api_key 配置"
            )
        mode = self.notifier.live_detect_mode
        if mode == "feed":
            return "当前使用 legacy Atom feed（该端点已不可靠，建议改用 Data API）"
        if mode == "livebroadcasts":
            return ""
        if self.data_api is not None and self.data_api.configured:
            return ""
        if self._page_fallback_ready():
            return (
                "当前**未配置 Data API Key**，监控走网页 JSON 兜底：实测可用，"
                "但比官方 API 脆弱 —— 没有精确时间戳、直播时长不准确，"
                "且每次检查抓 2 个页面（约 2.4MB）。建议配置 basic.api_key"
            )
        return (
            "当前**未配置 Data API Key**，且网页兜底已关闭，监控将回退到"
            "自 2025 年底起大面积 404 的 legacy Atom feed —— 大概率不会工作。"
            "请配置 basic.api_key，或打开 basic.page_fallback_enabled"
        )

    def _oauth_configured(self) -> bool:
        oauth_cfg = (self.config.get("oauth", {}) or {})
        return all(
            str(oauth_cfg.get(k, "") or "").strip()
            for k in ("client_id", "client_secret", "refresh_token")
        )

    # ------------------------------------------------------------ 频道解析辅助

    async def _resolve_channel(self, raw: str):
        """解析频道标识：Data API 优先，失败/无 Key 时抓频道页兜底。

        Returns:
            (ChannelMeta, from_api) —— from_api 表示名称是否来自官方 API。
        """
        if self.data_api is not None and self.data_api.configured:
            try:
                meta = await self.data_api.resolve_channel(raw)
                self._clear_runtime_degraded()
                return meta, True
            except ChannelNotFoundError:
                # 官方明确回答「没有这个频道」，不必再抓页面
                raise
            except QuotaExceededError as exc:
                logger.warning(f"[YT] Data API 配额耗尽，改用网页兜底: {exc}")
                self._note_runtime_degraded(_degrade_reason(exc))
            except InvalidApiKeyError as exc:
                logger.error(f"[YT] Data API Key 无效，改用网页兜底: {exc}")
                self._note_runtime_degraded(_degrade_reason(exc))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] Data API 请求失败，改用网页兜底: {exc!r}")
                self._note_runtime_degraded(_degrade_reason(exc))

        meta = await self._resolve_channel_via_page(raw)
        if meta is None:
            raise ChannelNotFoundError(f"无法解析频道: {raw}")
        return meta, False

    # ------------------------------------------------------------ 运行期降级痕迹

    def _note_runtime_degraded(self, reason: str) -> None:
        """记录「真实调用暴露出的」降级原因，供订阅回复与 /yt列表 展示。"""
        if self._runtime_degraded != reason:
            logger.warning(f"[YT] 数据源运行期降级: {reason}")
        self._runtime_degraded = reason

    def _clear_runtime_degraded(self) -> None:
        if self._runtime_degraded:
            logger.info("[YT] Data API 已恢复可用，清除降级状态")
        self._runtime_degraded = ""

    async def _resolve_channel_via_page(self, raw: str):
        """抓频道页解析频道标识（网页 JSON 客户端优先，其自带节流）。"""
        if self.page_json is not None:
            logger.warning(
                f"[YT] 使用网页抓取解析频道（非官方 API，脆弱）: {raw}"
            )
            # 交互式命令不参与节流：否则连跑两次同一频道会被误报「未找到频道」
            return await self.page_json.resolve_channel(
                raw, respect_throttle=False
            )

        from .services.scrape import resolve_channel_via_html

        logger.warning(
            f"[YT] 使用网页抓取解析频道（非官方 API，脆弱）: {raw}"
        )
        return await resolve_channel_via_html(
            self._session, raw, proxy=self._proxy or None
        )

    async def _fetch_snapshot_quiet(self, channel_id: str, meta):
        """订阅时静默拉一次快照用于播种，失败不影响订阅本身。

        播种同样要有降级链：否则配额耗尽时订阅「成功」却播种不到状态，
        频道会以「首次接入」的静默语义进入监控 —— 第一条视频被吞掉。
        """
        if self.data_api is not None and self.data_api.configured and meta.uploads_playlist_id:
            try:
                return await self.data_api.fetch_snapshot(
                    channel_id,
                    meta.uploads_playlist_id,
                    max_results=self.notifier.max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[YT] 订阅播种 Data API 失败，改用网页兜底 channel={channel_id}: {exc!r}"
                )
                if not isinstance(exc, ChannelNotFoundError):
                    self._note_runtime_degraded(_degrade_reason(exc))
        if self.page_json is not None:
            try:
                return await self.page_json.fetch_snapshot(
                    channel_id,
                    meta.handle or channel_id,
                    max_results=self.notifier.max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 订阅播种网页兜底失败 channel={channel_id}: {exc!r}")
        return None


def _degrade_reason(exc: BaseException) -> str:
    """把 Data API 异常映射为**给用户看**的简短降级原因。

    不要把异常 repr 拼进提示：`InvalidApiKeyError('API Key 无效: API key
    not valid. Please pass a valid API key.')` 这种文案对用户毫无价值。
    细节留在日志里。
    """
    if isinstance(exc, QuotaExceededError):
        return "Data API 配额耗尽"
    if isinstance(exc, InvalidApiKeyError):
        return "Data API Key 无效"
    if isinstance(exc, ApiKeyMissingError):
        return "未配置 Data API Key"
    return "Data API 不可用"


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
