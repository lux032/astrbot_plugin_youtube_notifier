"""无 API Key 时的兜底频道解析（抓频道页 HTML）。

⚠️ 脆弱方案：YouTube 改版即可能失效，且响应约 2MB。
仅用于「没配 API Key 但又想订阅 @handle」的降级场景。
配置了 Data API Key 时，请走 services/data_api.py 的 channels.list?forHandle。

已验证可用的提取方式：
    <link rel="canonical" href="https://www.youtube.com/channel/UC...">
    "externalId":"UC..."

上传播放列表 ID 可由频道 ID 推导：UCxxxxxx → UUxxxxxx
（YouTube 的约定；官方 channels.list 更可靠，这里作为兜底）
"""

from __future__ import annotations

import re
from typing import Optional

from astrbot.api import logger

from .data_api import parse_channel_input
from .models import ChannelMeta
from ..utils import BROWSER_HEADERS

_CANONICAL_RE = re.compile(
    r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'
)
_EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")


def uploads_playlist_id_for(channel_id: str) -> str:
    """由频道 ID 推导上传播放列表 ID：UCxxxx → UUxxxx。"""
    if channel_id.startswith("UC") and len(channel_id) == 24:
        return "UU" + channel_id[2:]
    return ""


def extract_channel_id_from_html(html: str) -> str:
    """从任意 YouTube 页面 HTML 提取频道 ID（canonical 优先，externalId 兜底）。

    两种信号都大量实测可用；canonical 更可靠（externalId 可能出现在
    无关的推荐位数据里）。取不到返回空串。
    """
    match = _CANONICAL_RE.search(html) or _EXTERNAL_ID_RE.search(html)
    return match.group(1) if match else ""


def extract_channel_name_from_html(html: str) -> str:
    """从页面 HTML 提取频道名（og:title 优先）。"""
    return _extract_title(html)


def channel_page_url(raw_input: str) -> str:
    """把频道标识转成频道页 URL（无 API Key 时抓页面用）。"""
    kind, value = parse_channel_input(raw_input)
    if not value:
        return ""
    if kind == "id":
        return f"https://www.youtube.com/channel/{value}"
    if kind == "username":
        return f"https://www.youtube.com/user/{value}"
    return f"https://www.youtube.com/@{value}"


async def resolve_channel_via_html(
    session, raw_input: str, proxy: Optional[str] = None
) -> Optional[ChannelMeta]:
    """抓频道页 HTML 解析频道 ID 与名称。失败返回 None。"""
    kind, value = parse_channel_input(raw_input)
    if not value:
        return None

    url = channel_page_url(raw_input)
    if not url:
        return None

    try:
        async with session.get(
            url,
            proxy=proxy or None,
            timeout=20,
            allow_redirects=True,
            headers=dict(BROWSER_HEADERS),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[YT] 抓取频道页失败 {url}: HTTP {resp.status}")
                return None
            html = await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[YT] 抓取频道页异常 {url}: {exc!r}")
        return None

    channel_id = extract_channel_id_from_html(html)
    if not channel_id:
        logger.warning(f"[YT] 无法从频道页提取 channel id: {url}")
        return None

    return ChannelMeta(
        channel_id=channel_id,
        title=extract_channel_name_from_html(html),
        handle=f"@{value}" if kind == "handle" else "",
        uploads_playlist_id=uploads_playlist_id_for(channel_id),
    )


def _extract_title(html: str) -> str:
    match = _OG_TITLE_RE.search(html)
    if match:
        return _unescape(match.group(1).strip())
    match = _TITLE_RE.search(html)
    if match:
        title = match.group(1).strip()
        for suffix in (" - YouTube", " – YouTube"):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
        return _unescape(title)
    return ""


def _unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
