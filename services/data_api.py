"""YouTube Data API v3 客户端（主数据源，仅需 API Key）。

为什么用 Data API 而不是 Atom feed：
    https://www.youtube.com/feeds/videos.xml 自 2025 年底起对自动化请求
    间歇性返回 404/500（含 YouTube 官方频道），已不可靠。官方 Data API
    稳定且仅需一个免费 API Key（无需 OAuth）。

配额（默认 10,000 单位/天）：
    channels.list      1 单位   handle/ID → 频道元数据（订阅时一次，后缓存）
    playlistItems.list 1 单位   上传播放列表 → 最新视频 ID
    videos.list        1 单位   视频详情（直播状态 + 实际起止时间）
所以每频道每轮轮询约 2 单位。

本模块不做重试以外的状态管理，纯数据访问。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger

from .models import (
    ChannelMeta,
    FeedEntry,
    FeedResult,
    LIVE_STATE_COMPLETED,
    LIVE_STATE_LIVE,
    LIVE_STATE_UPCOMING,
)
from ..utils import retry_async

API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNELS_URL = f"{API_BASE}/channels"
PLAYLIST_ITEMS_URL = f"{API_BASE}/playlistItems"
VIDEOS_URL = f"{API_BASE}/videos"

# 配额消耗表（单位）
QUOTA_COST = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
}

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class ApiKeyMissingError(Exception):
    """未配置 API Key。"""


class QuotaExceededError(Exception):
    """Data API 当日配额耗尽。"""


class ChannelNotFoundError(Exception):
    """频道/handle 不存在。"""


class InvalidApiKeyError(Exception):
    """API Key 无效。"""


# 语义性错误（调用方问题，重试不会变好）—— 命中即刻抛出，不做退避重试
_SEMANTIC_ERRORS = (
    ApiKeyMissingError,
    QuotaExceededError,
    ChannelNotFoundError,
    InvalidApiKeyError,
)


# ---------------------------------------------------------------- 输入解析


def parse_channel_input(raw: str) -> tuple[str, str]:
    """解析用户输入的频道标识。

    Returns:
        (kind, value)，kind ∈ {"id", "handle", "username"}

    支持：
        UCxxxxxxxxxxxxxxxxxxxxxx
        @ukaisaki
        ukaisaki
        https://www.youtube.com/@ukaisaki
        https://www.youtube.com/channel/UC...
        https://www.youtube.com/c/CustomName
        https://www.youtube.com/user/LegacyName
    """
    value = (raw or "").strip()
    if not value:
        return "handle", ""

    # 去掉 URL 前缀
    if "youtube.com/" in value or "youtu.be/" in value:
        value = value.split("youtube.com/", 1)[-1] if "youtube.com/" in value else value
        value = value.split("?", 1)[0]

    for prefix, kind in (
        ("channel/", "id"),
        ("user/", "username"),
        ("c/", "handle"),
        ("@", "handle"),
    ):
        if value.startswith(prefix):
            value = value[len(prefix):]
            value = value.split("/", 1)[0].split("?", 1)[0].strip()
            return kind, value

    value = value.split("/", 1)[0].split("?", 1)[0].strip().lstrip("@")
    if _CHANNEL_ID_RE.match(value):
        return "id", value
    return "handle", value


# ---------------------------------------------------------------- 客户端


class YouTubeDataAPI:
    def __init__(self, session, api_key: str, proxy: str = ""):
        self._session = session
        self.api_key = (api_key or "").strip()
        self._proxy = proxy or None
        self._quota_used = 0
        self._quota_day = self._today()
        self._quota_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _charge(self, endpoint: str) -> None:
        """记账并检查配额。跨天自动重置。"""
        today = self._today()
        if today != self._quota_day:
            self._quota_day = today
            self._quota_used = 0
        self._quota_used += QUOTA_COST.get(endpoint, 1)

    @property
    def quota_used_today(self) -> int:
        return self._quota_used

    # ------------------------------------------------------------ 底层请求

    async def _get(self, url: str, params: dict, endpoint: str, label: str) -> dict:
        if not self.configured:
            raise ApiKeyMissingError("未配置 YouTube Data API Key")
        self._charge(endpoint)
        query = {**params, "key": self.api_key}

        async def _do() -> dict:
            try:
                async with self._session.get(
                    url, params=query, proxy=self._proxy, timeout=20
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200:
                        return body
                    raise self._map_error(resp.status, body)
            except _SEMANTIC_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001 - 交给 retry
                raise RuntimeError(f"{label} 请求失败: {exc!r}") from exc

        # 语义性错误（Key 无效 / 配额耗尽 / 频道不存在）重试没有意义：
        # 重试只会白等十几秒才走到降级逻辑。只有网络错误与 5xx 才该重试。
        return await retry_async(
            _do, logger=logger, label=label, non_retryable=_SEMANTIC_ERRORS
        )

    @staticmethod
    def _map_error(status: int, body: dict) -> Exception:
        """把 API 错误响应映射为具体异常（便于上层区分处理）。"""
        error = (body or {}).get("error") or {}
        reason = ""
        for item in error.get("errors") or []:
            reason = item.get("reason", "") or reason
        message = error.get("message", "") or str(body)[:200]

        if reason == "quotaExceeded" or "quota" in message.lower():
            return QuotaExceededError(f"配额耗尽: {message}")
        if reason in ("keyInvalid", "badRequest") and "API key" in message:
            return InvalidApiKeyError(f"API Key 无效: {message}")
        if status == 404 or reason in ("channelNotFound", "handleNotFound", "notFound"):
            return ChannelNotFoundError(f"频道不存在: {message}")
        if status == 403 and "API key" in message:
            return InvalidApiKeyError(f"API Key 无效: {message}")
        return RuntimeError(f"Data API 返回 {status}: {message}")

    # ------------------------------------------------------------ 频道解析

    async def resolve_channel(self, raw_input: str) -> ChannelMeta:
        """把用户输入（@handle / URL / UC id）解析为频道元数据。"""
        kind, value = parse_channel_input(raw_input)
        if not value:
            raise ChannelNotFoundError("空的频道标识")

        if kind == "id":
            params = {"part": "snippet,contentDetails", "id": value}
        elif kind == "username":
            params = {"part": "snippet,contentDetails", "forUsername": value}
        else:
            params = {"part": "snippet,contentDetails", "forHandle": value}

        body = await self._get(
            CHANNELS_URL, params, "channels.list", f"resolve channel {raw_input}"
        )
        items = body.get("items") or []
        if not items:
            raise ChannelNotFoundError(f"未找到频道: {raw_input}")
        item = items[0]
        snippet = item.get("snippet") or {}
        uploads = (
            ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get(
                "uploads", ""
            )
        )
        return ChannelMeta(
            channel_id=item.get("id", ""),
            title=snippet.get("title", ""),
            handle=snippet.get("customUrl", "") or (f"@{value}" if kind == "handle" else ""),
            uploads_playlist_id=uploads,
        )

    # ------------------------------------------------------------ 频道快照

    async def fetch_snapshot(
        self,
        channel_id: str,
        uploads_playlist_id: str,
        max_results: int = 5,
        channel_name: str = "",
    ) -> FeedResult:
        """拉取频道最近视频 + 直播状态（2 次请求 ≈ 2 单位）。

        Args:
            uploads_playlist_id: channels.list 得到的 uploads 播放列表 ID。
            max_results: 取最近多少条（1-50）。
        """
        if not uploads_playlist_id:
            raise RuntimeError(f"频道 {channel_id} 缺少 uploads_playlist_id")

        max_results = max(1, min(50, int(max_results)))
        # ① 上传播放列表 → 最近视频 ID
        pl_body = await self._get(
            PLAYLIST_ITEMS_URL,
            {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": max_results,
            },
            "playlistItems.list",
            f"playlistItems {channel_id}",
        )
        pl_items = pl_body.get("items") or []
        if not pl_items:
            return FeedResult(channel_id, channel_name, [])

        # 收集 video id（contentDetails.videoId 是视频真实 ID）
        order: list[str] = []
        base: dict[str, FeedEntry] = {}
        for item in pl_items:
            details = item.get("contentDetails") or {}
            snippet = item.get("snippet") or {}
            vid = details.get("videoId") or (snippet.get("resourceId") or {}).get(
                "videoId", ""
            )
            if not vid or vid in base:
                continue
            order.append(vid)
            base[vid] = FeedEntry(
                video_id=vid,
                title=snippet.get("title", ""),
                channel_id=snippet.get("channelId", "") or channel_id,
                channel_name=snippet.get("channelTitle", "") or channel_name,
                published_at=details.get("videoPublishedAt")
                or snippet.get("publishedAt", ""),
                url=f"https://www.youtube.com/watch?v={vid}",
                thumbnail_url=self._pick_thumbnail(snippet.get("thumbnails") or {}),
            )

        # ② videos.list → 直播状态 + 实际起止时间
        v_body = await self._get(
            VIDEOS_URL,
            {
                "part": "snippet,liveStreamingDetails",
                "id": ",".join(order),
            },
            "videos.list",
            f"videos {channel_id}",
        )
        v_map = {i.get("id", ""): i for i in (v_body.get("items") or [])}

        entries: list[FeedEntry] = []
        for vid in order:
            entry = base[vid]
            detail = v_map.get(vid)
            if detail:
                self._apply_video_detail(entry, detail)
            entries.append(entry)

        return FeedResult(channel_id, channel_name, entries)

    @staticmethod
    def _apply_video_detail(entry: FeedEntry, detail: dict) -> None:
        """把 videos.list 的详情合并进 FeedEntry（容错缺失字段）。"""
        snippet = detail.get("snippet") or {}
        if snippet.get("title"):
            entry.title = snippet["title"]
        if snippet.get("channelTitle"):
            entry.channel_name = snippet["channelTitle"]
        if snippet.get("publishedAt"):
            entry.published_at = snippet["publishedAt"]
        thumb = YouTubeDataAPI._pick_thumbnail(snippet.get("thumbnails") or {})
        if thumb:
            entry.thumbnail_url = thumb

        live_content = (snippet.get("liveBroadcastContent") or "").strip().lower()
        lsd = detail.get("liveStreamingDetails") or {}
        entry.actual_start_time = lsd.get("actualStartTime", "") or ""
        entry.actual_end_time = lsd.get("actualEndTime", "") or ""

        if live_content == LIVE_STATE_LIVE:
            entry.live_state = LIVE_STATE_LIVE
        elif live_content == LIVE_STATE_UPCOMING:
            entry.live_state = LIVE_STATE_UPCOMING
        elif lsd:
            # 有 liveStreamingDetails 但已不在直播 → 已结束的直播存档
            entry.live_state = LIVE_STATE_COMPLETED
            if not entry.actual_start_time:
                entry.actual_start_time = lsd.get("scheduledStartTime", "") or ""
        else:
            entry.live_state = ""

    @staticmethod
    def _pick_thumbnail(thumbs: dict) -> str:
        """挑一个分辨率合适的封面（优先 medium/high，避免 maxres 过大）。"""
        for key in ("medium", "high", "standard", "default", "maxres"):
            url = (thumbs.get(key) or {}).get("url")
            if url:
                return url
        return ""

    # ------------------------------------------------------------ 图片下载

    async def download_image(self, url: str, dest_path: Path, timeout: int = 15) -> bool:
        """下载图片到 dest_path。失败返回 False，不抛异常。"""
        if not url:
            return False
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._session.get(url, proxy=self._proxy, timeout=timeout) as resp:
                if resp.status != 200:
                    return False
                data = await resp.read()
            if not data:
                return False
            dest_path.write_bytes(data)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 图片下载失败 {url}: {exc!r}")
            return False
