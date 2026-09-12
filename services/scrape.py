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

_CANONICAL_RE = re.compile(
    r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'
)
_EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")

# 电脑版 UA —— 移动版页面结构不同
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def uploads_playlist_id_for(channel_id: str) -> str:
    """由频道 ID 推导上传播放列表 ID：UCxxxx → UUxxxx。"""
    if channel_id.startswith("UC") and len(channel_id) == 24:
        return "UU" + channel_id[2:]
    return ""


async def resolve_channel_via_html(
    session, raw_input: str, proxy: Optional[str] = None
) -> Optional[ChannelMeta]:
    """抓频道页 HTML 解析频道 ID 与名称。失败返回 None。"""
    kind, value = parse_channel_input(raw_input)
    if not value:
        return None

    if kind == "id":
        url = f"https://www.youtube.com/channel/{value}"
    elif kind == "username":
        url = f"https://www.youtube.com/user/{value}"
    else:
        url = f"https://www.youtube.com/@{value}"

    try:
        async with session.get(
            url,
            proxy=proxy or None,
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[YT] 抓取频道页失败 {url}: HTTP {resp.status}")
                return None
            html = await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[YT] 抓取频道页异常 {url}: {exc!r}")
        return None

    channel_id = ""
    match = _CANONICAL_RE.search(html) or _EXTERNAL_ID_RE.search(html)
    if match:
        channel_id = match.group(1)
    if not channel_id:
        logger.warning(f"[YT] 无法从频道页提取 channel id: {url}")
        return None

    return ChannelMeta(
        channel_id=channel_id,
        title=_extract_title(html),
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
