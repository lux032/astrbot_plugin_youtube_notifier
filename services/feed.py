"""Legacy Atom feed 数据源（⚠️ 降级方案，不推荐作为主数据源）。

背景：https://www.youtube.com/feeds/videos.xml 自 2025 年底起对自动化请求
间歇性返回 404/500（实测 YouTube 官方频道、MrBeast 等均为 404），已不可靠。
Google 未发布正式废弃公告，但普遍认为是收紧非官方数据访问所致。

因此本模块仅在以下情况作为兜底：
  1. 未配置 Data API Key，且用户接受不稳定的数据源；
  2. WebSub 推送回调（推送内容就是该 feed 的 Atom XML）——
     同样受端点失效影响，故 WebSub 默认关闭。

主数据源请使用 services/data_api.py（官方 Data API v3，仅需 API Key）。
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from typing import Optional

from astrbot.api import logger

from .models import (
    FeedEntry,
    FeedResult,
    LIVE_STATE_COMPLETED,
    LIVE_STATE_LIVE,
    LIVE_STATE_UPCOMING,
)
from ..utils import retry_async

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
FEED_MIN_INTERVAL = 30.0  # 每频道最小抓取间隔（秒）

_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"

_LIVE_STATES = {LIVE_STATE_LIVE, LIVE_STATE_UPCOMING, LIVE_STATE_COMPLETED}


class FeedUnavailableError(Exception):
    """feed 端点返回 404/500（该端点已不可靠）。"""


class LegacyFeedClient:
    """Atom feed 抓取（带每频道节流与重试）。"""

    def __init__(self, session, proxy: str = ""):
        self._session = session
        self._proxy = proxy or None
        self._feed_last_fetch: dict[str, float] = {}
        self._throttle = asyncio.Lock()

    def _cooling(self, channel_id: str) -> bool:
        last = self._feed_last_fetch.get(channel_id, 0.0)
        if time.monotonic() - last < FEED_MIN_INTERVAL:
            return True
        self._feed_last_fetch[channel_id] = time.monotonic()
        return False

    async def fetch_feed(
        self, channel_id: str, *, respect_throttle: bool = True
    ) -> Optional[FeedResult]:
        """抓取并解析频道 feed。端点失效或网络失败返回 None。"""
        if respect_throttle:
            async with self._throttle:
                if self._cooling(channel_id):
                    return None

        async def _do() -> FeedResult:
            async with self._session.get(
                FEED_URL,
                params={"channel_id": channel_id},
                proxy=self._proxy,
                timeout=15,
                headers={
                    # 该端点对缺少浏览器特征的请求更易返回 404
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    ),
                    "Accept": "application/atom+xml,application/xml,text/xml,*/*",
                },
            ) as resp:
                if resp.status in (404, 500, 502, 503):
                    raise FeedUnavailableError(
                        f"feed 端点返回 {resp.status}（该端点已不可靠，建议配置 API Key）"
                    )
                resp.raise_for_status()
                text = await resp.text()
            result = parse_feed(text, channel_id)
            if result is None or not result.channel_name:
                raise RuntimeError("feed 解析结果为空")
            return result

        try:
            return await retry_async(
                _do, logger=logger, label=f"legacy feed {channel_id}"
            )
        except FeedUnavailableError as exc:
            logger.warning(f"[YT] {exc} channel={channel_id}")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] legacy feed 拉取失败 channel={channel_id}: {exc!r}")
            return None


# ---------------------------------------------------------------- 解析


def parse_feed(xml_text: str, fallback_channel_id: str = "") -> Optional[FeedResult]:
    """解析 YouTube Atom feed XML → FeedResult。解析失败返回 None。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning(f"[YT] feed XML 解析失败: {exc!r}")
        return None

    channel_name = _feed_channel_name(root)
    channel_id = fallback_channel_id or _feed_channel_id(root)

    entries: list[FeedEntry] = []
    for node in root.findall(f"{_ATOM}entry"):
        entry = _parse_entry(node, channel_id, channel_name)
        if entry is not None:
            entries.append(entry)
    return FeedResult(channel_id=channel_id, channel_name=channel_name, entries=entries)


def _feed_channel_name(root: ET.Element) -> str:
    title_node = root.find(f"{_ATOM}title")
    if title_node is None or title_node.text is None:
        return ""
    title = title_node.text.strip()
    for prefix in ("Videos - ", "Videos — "):
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


def _feed_channel_id(root: ET.Element) -> str:
    """取 feed 所属频道 ID。

    ⚠️ 真实 feed 的坑：根元素的 <yt:channelId> **不含 UC 前缀**
    （实测为 22 字符的 'BR8-60-...'），而每个 <entry> 里的 <yt:channelId>
    是完整的 'UCBR8-60-...'。若直接采用根元素的值会得到错误的频道 ID，
    导致按 channel_id 索引的状态查不到（WebSub 推送会被静默丢弃）。

    因此优先从 <link rel="alternate" href=".../channel/UC..."> 取，
    再退回根元素并补回 UC 前缀。
    """
    for link in root.findall(f"{_ATOM}link"):
        href = link.get("href", "") or ""
        if "/channel/" in href:
            cid = href.split("/channel/", 1)[1].split("/")[0].strip()
            if cid:
                return cid

    node = root.find(f"{_YT}channelId")
    value = node.text.strip() if node is not None and node.text else ""
    if value and not value.startswith("UC") and len(value) == 22:
        value = "UC" + value
    return value


def _parse_entry(
    node: ET.Element, channel_id: str, channel_name: str
) -> Optional[FeedEntry]:
    """防御性解析单个 entry，失败返回 None。"""
    try:
        video_id = _text(node, f"{_YT}videoId")
        if not video_id:
            video_id = _text(node, f"{_ATOM}id", default="")
            if video_id.startswith("yt:video:"):
                video_id = video_id[len("yt:video:"):]

        title = _text(node, f"{_ATOM}title") or ""
        published = _text(node, f"{_ATOM}published") or ""
        updated = _text(node, f"{_ATOM}updated") or published
        entry_channel_id = _text(node, f"{_YT}channelId") or channel_id
        entry_channel_name = _text(node, f"{_YT}channelTitle") or channel_name

        url = ""
        for link in node.findall(f"{_ATOM}link"):
            rel = link.get("rel", "")
            href = link.get("href", "")
            if rel == "alternate" and href:
                url = href
                break
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"

        thumbnail = ""
        group = node.find(f"{_MEDIA}group")
        if group is not None:
            thumb = group.find(f"{_MEDIA}thumbnail")
            if thumb is not None:
                thumbnail = thumb.get("url", "")

        live_state = _detect_live_state(node, group)

        return FeedEntry(
            video_id=video_id,
            title=title,
            channel_id=entry_channel_id,
            channel_name=entry_channel_name,
            published_at=published,
            updated_at=updated,
            url=url,
            thumbnail_url=thumbnail,
            live_state=live_state,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[YT] 跳过无法解析的 feed entry: {exc!r}")
        return None


def _detect_live_state(node: ET.Element, group: Optional[ET.Element]) -> str:
    """探测 entry 的直播状态。优先 yt:liveBroadcastContent，其次 media:status。"""
    raw = _text(node, f"{_YT}liveBroadcastContent")
    if raw and raw in _LIVE_STATES:
        return raw

    if group is not None:
        status_node = group.find(f"{_MEDIA}status")
        if status_node is not None:
            value = (status_node.get("state") or status_node.text or "").strip().lower()
            if value in _LIVE_STATES:
                return value
            last = value.rstrip("/").split("/")[-1]
            if last in _LIVE_STATES:
                return last
    return ""


def _text(node: ET.Element, path: str, default: str = "") -> str:
    child = node.find(path)
    if child is None or child.text is None:
        return default
    return child.text.strip()
