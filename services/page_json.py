"""网页 JSON 数据源（抓频道页/观看页里的 ytInitialData）。

用途（调用方见 services/notifier.py 与 main.py）：
  1. **Data API 配额耗尽或请求失败时自动降级**；
  2. **未配置 API Key 时取代已不可靠的 legacy Atom feed**（feed 端点自 2025
     年底起大面积 404，见 services/feed.py）；
  3. `/yt直播测试` `/yt视频测试` 拉取指定目标的数据。

⚠️ 与 Data API 的差距（2026-09-12 实测，务必知悉）：

  * **直播只能从 `/streams` 标签页拿到**：实测 `/videos` 页直播数为 0
    （@NASA、@SkyNews 均如此），所以一次快照要抓 **2 个页面**，各约 1.2MB
    → 降级期间流量与耗时显著高于 Data API。
  * **`/streams` 页只可用于判断直播**：实测 MrBeast 的 `/streams` 里列出的
    全是他的**普通投稿**（首播视频被 YouTube 归入 streams 标签），24 条中
    有 12 条与 `/videos` 完全重合。所以该页的非直播条目既不是「往期直播
    存档」、也不能当普通投稿处理 —— 这里直接丢弃，只取 live/upcoming。
  * **没有 ISO 时间戳**：页面只给相对时间（"6 days ago"）或日期
    （"Sep 5, 2026"），这里换算成**近似** ISO 供排序与展示，不精确。
  * **直播没有 actualStartTime**：`/streams` 页只给 "N watching" 或
    "Started streaming 2 hours ago"，所以起止时间只能是近似值，
    直播时长因此不准确（状态机在没有准确 end 时用 now 兜底）。
  * **条目结构已迁移**：视频列表项从旧的 `videoRenderer` 变成了
    `lockupViewModel`（实测），按旧结构写的解析器会一条都取不到。
  * **直播存档的识别靠 recent_live_ids**：页面无法区分「直播存档」与
    「普通投稿」（角标相同），所以挡 VOD 重复推送只能靠状态机里
    「已作为直播通知过的 video id」那道防线（见 state_machine.py）。

⚠️ 观看页（watch）的特殊性：实测自动请求的 `ytInitialPlayerResponse`
   返回 `playabilityStatus: LOGIN_REQUIRED`（"Sign in to confirm you're
   not a bot"），**不含 videoDetails**。因此观看页信息一律从
   `ytInitialData` 取（title / dateText / subtitle），不要依赖 playerResponse。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from astrbot.api import logger

from .data_api import parse_channel_input
from .models import (
    ChannelMeta,
    FeedEntry,
    FeedResult,
    LIVE_STATE_COMPLETED,
    LIVE_STATE_LIVE,
    LIVE_STATE_UPCOMING,
)
from .scrape import (
    channel_page_url,
    extract_channel_id_from_html,
    extract_channel_name_from_html,
    uploads_playlist_id_for,
)
from ..utils import BROWSER_HEADERS, retry_async

YOUTUBE_BASE = "https://www.youtube.com"

# 频道页标签：直播只在 streams 里，普通投稿只在 videos 里，两者缺一不可
TAB_VIDEOS = "videos"
TAB_STREAMS = "streams"

# 同一 (频道, 标签) 的最小抓取间隔（秒）。网页约 1.2MB，比 API 贵得多，
# 配额耗尽后若轮询间隔很短，没有节流会把带宽打满。
PAGE_MIN_INTERVAL = 60.0

# 实测：直播项的缩略图角标 badgeStyle
_BADGE_LIVE = "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"
# 未实测到 upcoming 角标，防御性保留（预约直播/首播）
_BADGE_UPCOMING = "THUMBNAIL_OVERLAY_BADGE_STYLE_UPCOMING"

_VIDEO_URL_RES = (
    re.compile(r"(?:youtube\.com/watch\?(?:[^#]*&)?v=)([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
)
_RAW_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]*)"'
)

# 相对时间（en，Accept-Language 固定 en-US）
_REL_UNITS = {
    "second": 1, "minute": 60, "hour": 3600,
    "day": 86400, "week": 604800, "month": 2592000, "year": 31536000,
}
_REL_EN_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
# 相对时间（zh，防御性：万一代理/UA 导致返回中文页面）
_REL_ZH_UNITS = {
    "秒": 1, "分钟": 60, "小时": 3600, "天": 86400,
    "周": 604800, "个月": 2592000, "月": 2592000, "年": 31536000,
}
_REL_ZH_RE = re.compile(r"(\d+)\s*(秒|分钟|小时|天|周|个月|月|年)前")

# 月份缩写自己映射，不用 strptime 的 %b —— 后者依赖系统 locale，
# 中文 Windows 上会解析不了英文月份（实测环境即中文系统）。
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})")

# 观看页 dateText 的前缀（直播/首播/预约）
_DATE_PREFIXES = (
    "started streaming on ", "streamed live on ", "streamed on ",
    "premiered ", "scheduled for ", "premieres ",
)


class VideoPageData:
    """观看页抓取结果（供 /yt直播测试 /yt视频测试 使用）。"""

    __slots__ = ("entry", "is_live_now", "date_text")

    def __init__(self, entry: FeedEntry, is_live_now: bool, date_text: str):
        self.entry = entry
        self.is_live_now = is_live_now
        self.date_text = date_text


# ---------------------------------------------------------------- 输入解析


def parse_video_input(raw: str) -> str:
    """从用户输入里取出 video id。

    支持观看/直播/短视频/嵌入链接与裸 id（11 字符）。不是视频标识返回空串。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    for pattern in _VIDEO_URL_RES:
        match = pattern.search(value)
        if match:
            return match.group(1)
    if _RAW_VIDEO_ID_RE.match(value):
        return value
    return ""


def is_video_input(raw: str) -> bool:
    """输入是否为视频标识（而非频道标识）。

    注意裸 11 字符 id 会被判为视频；频道 @handle 通常不含这种短串。
    """
    return bool(parse_video_input(raw))


def parse_target(raw: str) -> tuple[str, str]:
    """判定测试命令的目标是视频还是频道。

    Returns:
        ("video", video_id) / ("channel", 原样输入) / ("", "")。

    判定顺序很重要：先看是不是视频链接，再排除频道链接，最后才敢把
    裸的 11 字符串当视频 id —— 因为裸 handle 也可能是 11 字符。
    """
    value = (raw or "").strip()
    if not value:
        return "", ""

    lowered = value.lower()
    # ① 明确的视频链接 → 视频
    for pattern in _VIDEO_URL_RES:
        match = pattern.search(value)
        if match:
            return "video", match.group(1)

    # ② 明确的频道形式 → 频道（UC 开头的 24 字符 id 也是频道）
    kind, channel_value = parse_channel_input(value)
    if channel_value and (
        value.startswith("@")
        or "youtube.com/channel/" in lowered
        or "youtube.com/c/" in lowered
        or "youtube.com/user/" in lowered
        or (kind == "id" and channel_value == value)
    ):
        return "channel", value

    # ③ 裸的 11 字符 id → 视频
    if _RAW_VIDEO_ID_RE.match(value):
        return "video", value

    # ④ 其余交给频道解析（可能是自定义 handle 或 /c/ 名称）
    return "channel", value


# ---------------------------------------------------------------- JSON 提取


def extract_json_object(html: str, marker: str) -> Optional[dict]:
    """提取赋给 `marker` 的 JSON 对象（花括号配对，不用正则）。

    为什么不用正则：`var ytInitialData = {...};</script>` 这种写法在部分页面
    不成立（实测 @spacex 页面的 `;</script>` 结尾匹配不到），而惰性正则
    `\\{.*?\\};</script>` 还可能截断 JSON。花括号配对对两种写法都成立。

    Args:
        html: 页面 HTML。
        marker: 变量名，如 "ytInitialData" / "ytInitialPlayerResponse"。

    Returns:
        解析出的 dict；找不到或解析失败返回 None。
    """
    import json

    idx = html.find(marker)
    while idx != -1:
        start = html.find("{", idx)
        # marker 与 `{` 之间只应隔 ` = ` 之类；太远说明命中的是别处的字符串
        if start != -1 and start - idx <= 60:
            raw = _slice_balanced_object(html, start)
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
                except ValueError:
                    pass  # 命中 JS 里的同名串，继续找下一个
        idx = html.find(marker, idx + 1)
    return None


def _slice_balanced_object(html: str, start: int) -> str:
    """从 html[start]（必须是 '{'）起按花括号配对切出完整对象文本。"""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start:i + 1]
    return ""


def extract_yt_initial_data(html: str) -> Optional[dict]:
    return extract_json_object(html, "ytInitialData")


# ---------------------------------------------------------------- 通用遍历


def _walk(obj, key: str) -> Iterator[dict]:
    """深度优先找出所有名为 key 的值（防御性遍历，不假设层级）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, dict):
                yield v
            yield from _walk(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, key)


def _first(obj, key: str) -> Optional[dict]:
    for found in _walk(obj, key):
        return found
    return None


def _text_of(node) -> str:
    """取 YouTube 富文本节点的纯文本。

    同一份数据可能是 `{"simpleText": x}`、`{"content": x}`
    或 `{"runs": [{"text": x}, ...]}` 三种形态之一，这里统一处理。
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    if isinstance(node.get("content"), str):
        return node["content"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(
            r.get("text", "") for r in runs if isinstance(r, dict)
        )
    return ""


# ---------------------------------------------------------------- 时间解析


def parse_relative_time_to_iso(text: str, now: Optional[datetime] = None) -> str:
    """把相对时间文案换算成近似 ISO 时间戳（UTC）。

    页面只给 "6 days ago" / "2 weeks ago" 这类相对时间，没有精确时间戳。
    换算结果仅用于排序与展示，**不是精确发布时间**。无法解析返回空串。
    """
    if not text:
        return ""
    base = now or datetime.now(timezone.utc)
    match = _REL_EN_RE.search(text)
    if match:
        seconds = int(match.group(1)) * _REL_UNITS[match.group(2).lower()]
        return _iso(base - timedelta(seconds=seconds))
    match = _REL_ZH_RE.search(text)
    if match:
        seconds = int(match.group(1)) * _REL_ZH_UNITS[match.group(2)]
        return _iso(base - timedelta(seconds=seconds))
    return ""


def parse_date_text_to_iso(text: str) -> str:
    """把绝对日期文案换算成 ISO 时间戳（UTC，时间部分为 00:00:00）。

    支持 "Sep 5, 2026"、"Started streaming on Jul 30, 2026"。
    只有日期没有时刻，所以是当天零点，**不是精确时刻**。
    """
    if not text:
        return ""
    lowered = text.strip().lower()
    for prefix in _DATE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    match = _DATE_RE.search(text)
    if not match:
        return ""
    month = _MONTHS.get(match.group(1).lower()[:3])
    if not month:
        return ""
    try:
        dt = datetime(
            int(match.group(3)), month, int(match.group(2)), tzinfo=timezone.utc
        )
    except ValueError:
        return ""
    return _iso(dt)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 频道页解析


def iter_video_items(data: dict) -> list[dict]:
    """取出频道页里的视频条目（lockupViewModel）。

    实测页面已从 `videoRenderer` 迁移到 `lockupViewModel`，
    故按 contentType 过滤，避免混入播放列表/合集等其它 lockup。
    """
    items = []
    for lockup in _walk(data, "lockupViewModel"):
        if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            items.append(lockup)
    return items


def _lockup_badges(item: dict) -> list[dict]:
    """取缩略图角标（直播/时长/预约都在这）。"""
    badges: list[dict] = []
    image = item.get("contentImage") or {}
    thumb = image.get("thumbnailViewModel") or {}
    for overlay in thumb.get("overlays") or []:
        if not isinstance(overlay, dict):
            continue
        bottom = overlay.get("thumbnailBottomOverlayViewModel") or {}
        for badge in bottom.get("badges") or []:
            if isinstance(badge, dict) and isinstance(
                badge.get("thumbnailBadgeViewModel"), dict
            ):
                badges.append(badge["thumbnailBadgeViewModel"])
    return badges


def _lockup_thumbnail(item: dict) -> str:
    """挑分辨率最高的缩略图（实测 sources 含 360/720 两档）。"""
    image = item.get("contentImage") or {}
    thumb = image.get("thumbnailViewModel") or {}
    sources = ((thumb.get("image") or {}).get("sources")) or []
    best_url, best_w = "", -1
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = source.get("url") or ""
        width = source.get("width") or 0
        if url and width >= best_w:
            best_url, best_w = url, width
    return best_url


def _lockup_rows(item: dict) -> list[str]:
    """取条目的元信息行（"81M views"、"6 days ago"、"77 watching"）。"""
    md = (item.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    meta = md.get("metadata") or {}
    view = meta.get("contentMetadataViewModel") or {}
    rows: list[str] = []
    for row in view.get("metadataRows") or []:
        if not isinstance(row, dict):
            continue
        for part in row.get("metadataParts") or []:
            if isinstance(part, dict):
                text = _text_of(part.get("text") or {})
                if text:
                    rows.append(text)
    return rows


def lockup_to_entry(item: dict) -> Optional[FeedEntry]:
    """把一条 lockupViewModel 转成 FeedEntry。信息不足返回 None。

    直播状态**只认角标**（LIVE / UPCOMING）：实测频道页无法区分
    「直播存档」与「普通投稿」—— 两者的角标完全一样（都只有时长）。
    ⚠️ 因此绝不能靠所在标签页推断状态，见 parse_channel_videos 的说明。
    """
    video_id = (item.get("contentId") or "").strip()
    if not video_id:
        return None

    md = (item.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = _text_of(md.get("title") or {})
    badges = _lockup_badges(item)
    rows = _lockup_rows(item)

    styles = {(b.get("badgeStyle") or "") for b in badges}
    texts = " ".join((b.get("text") or "") for b in badges)
    row_text = " ".join(rows)

    if _BADGE_LIVE in styles:
        live_state = LIVE_STATE_LIVE
    elif _BADGE_UPCOMING in styles or "upcoming" in texts.lower():
        live_state = LIVE_STATE_UPCOMING
    elif "scheduled" in row_text.lower() or "premieres" in row_text.lower():
        live_state = LIVE_STATE_UPCOMING
    else:
        live_state = ""

    # 时间：直播常给 "Started streaming 2 hours ago" 或 "77 watching"；
    # 普通投稿给 "6 days ago"。解析不出就留空（排序时视为最旧）。
    published_at = ""
    for row in rows:
        published_at = parse_relative_time_to_iso(row)
        if published_at:
            break

    entry = FeedEntry(
        video_id=video_id,
        title=title,
        channel_name="",
        published_at=published_at,
        url=f"{YOUTUBE_BASE}/watch?v={video_id}",
        thumbnail_url=_lockup_thumbnail(item),
        live_state=live_state,
    )
    # 直播的 "Started streaming X ago" 是唯一的开始时间线索
    if live_state in (LIVE_STATE_LIVE, LIVE_STATE_UPCOMING) and published_at:
        entry.actual_start_time = published_at
    return entry


def parse_channel_videos(data: dict, *, lives_only: bool = False) -> list[FeedEntry]:
    """解析一个频道页的 ytInitialData → FeedEntry 列表。

    Args:
        lives_only: 只保留 live/upcoming 条目。用于 `/streams` 标签页 ——
            那一页只**可信**于直播状态，其非直播条目**不能**当作
            「往期直播存档」：实测 MrBeast 的 /streams 里全是他的普通投稿
            （首播视频会被 YouTube 归到 streams 标签），24 条里有 12 条与
            /videos 完全重合。若把它们标成 completed，find_new_videos 会
            把这些真实投稿当成「已结束的直播」**永久跳过**，用户再也收不到
            新投稿通知 —— 静默漏推，正是本项目明令禁止的失败模式。
    """
    entries: list[FeedEntry] = []
    for item in iter_video_items(data):
        entry = lockup_to_entry(item)
        if entry is None:
            continue
        if lives_only and entry.live_state not in (
            LIVE_STATE_LIVE,
            LIVE_STATE_UPCOMING,
        ):
            continue
        entries.append(entry)
    return entries


# ---------------------------------------------------------------- 客户端


class ChannelPageClient:
    """抓取 YouTube 页面 JSON 的数据源客户端（带每频道节流与重试）。"""

    def __init__(
        self,
        session,
        proxy: str = "",
        *,
        min_interval: float = PAGE_MIN_INTERVAL,
    ):
        self._session = session
        self._proxy = proxy or None
        self._min_interval = max(0.0, float(min_interval))
        self._last_fetch: dict[str, float] = {}
        # 上次成功解析的条目，冷却期内复用（见 _fetch_tab）
        self._cache: dict[str, list[FeedEntry]] = {}
        self._throttle = asyncio.Lock()

    # ------------------------------------------------------------ 抓取

    async def _enter(self, key: str, *, respect_throttle: bool) -> bool:
        """登记一次抓取。返回 True 表示在冷却期内、应改用缓存。

        respect_throttle=False 用于**交互式命令**（订阅解析、测试命令）：
        这类调用由用户动作触发，一次一次来，节流只会让命令莫名失败
        （真实踩坑：连跑两次 /yt视频测试 @MrBeast，第二次因 60s 冷却
        解析不到频道，被误报成「未找到频道」）。
        """
        if not respect_throttle:
            async with self._throttle:
                self._last_fetch[key] = time.monotonic()
            return False
        async with self._throttle:
            last = self._last_fetch.get(key, 0.0)
            if time.monotonic() - last < self._min_interval:
                return True
            self._last_fetch[key] = time.monotonic()
            return False

    async def _fetch_html(
        self, url: str, label: str, *, timeout: int = 25
    ) -> Optional[str]:
        """抓页面 HTML。失败（含非 200）返回 None，不抛异常。"""

        async def _do() -> str:
            async with self._session.get(
                url,
                proxy=self._proxy,
                timeout=timeout,
                allow_redirects=True,
                headers=dict(BROWSER_HEADERS),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.text()

        try:
            return await retry_async(_do, logger=logger, label=label)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 网页抓取失败 {url}: {exc!r}")
            return None

    async def fetch_tab(
        self,
        channel_ref: str,
        tab: str,
        label: str = "page",
        *,
        respect_throttle: bool = True,
    ) -> Optional[list[FeedEntry]]:
        """抓某个标签页并解析出条目；失败返回 None。

        冷却期内**返回上次成功的缓存**而不是 None —— 返回 None 会被上游
        当成「抓取失败」并触发多余的 legacy feed 回退与误导性告警。
        缓存对状态机是安全的：同一份快照重复喂进去不会产生重复通知。
        """
        key = f"{channel_ref}/{tab}"
        if await self._enter(key, respect_throttle=respect_throttle):
            cached = self._cache.get(key)
            logger.debug(
                f"[YT] 网页抓取冷却中 {key}，"
                f"{'复用缓存' if cached is not None else '且无缓存'}"
            )
            return cached

        html = await self._fetch_html(
            f"{channel_ref}/{tab}", f"{label} {channel_ref}/{tab}"
        )
        if not html:
            return self._cache.get(key)
        data = extract_yt_initial_data(html)
        if data is None:
            logger.warning(f"[YT] 页面里找不到 ytInitialData: {channel_ref}/{tab}")
            return self._cache.get(key)
        entries = parse_channel_videos(data, lives_only=(tab == TAB_STREAMS))
        self._cache[key] = entries
        return entries

    @staticmethod
    def _channel_ref(channel_id: str, handle: str) -> str:
        """频道页 URL 前缀：优先稳定的 /channel/UC...，否则用 /@handle。"""
        if channel_id and channel_id.startswith("UC") and len(channel_id) == 24:
            return f"{YOUTUBE_BASE}/channel/{channel_id}"
        clean = (handle or "").lstrip("@").strip()
        return f"{YOUTUBE_BASE}/@{clean}" if clean else ""

    async def fetch_snapshot(
        self,
        channel_id: str,
        handle: str = "",
        *,
        max_results: int = 5,
        channel_name: str = "",
        respect_throttle: bool = True,
    ) -> Optional[FeedResult]:
        """抓 /videos + /streams 合并成一份频道快照。两个页面都失败返回 None。

        两个页面各司其职：
          /videos   —— 上传播放列表（普通投稿 + 直播存档，全都无直播角标）
          /streams  —— **只取 live/upcoming 条目**，这是直播状态的唯一来源

        返回列表按时间倒序（直播常无时间信息，排在最后）。
        """
        ref = self._channel_ref(channel_id, handle)
        if not ref:
            logger.warning(f"[YT] 无法构造频道页地址 channel={channel_id} handle={handle}")
            return None

        label = f"page channel={channel_id or handle}"
        results = await asyncio.gather(
            self.fetch_tab(ref, TAB_VIDEOS, label, respect_throttle=respect_throttle),
            self.fetch_tab(ref, TAB_STREAMS, label, respect_throttle=respect_throttle),
        )
        videos, streams = results
        if videos is None and streams is None:
            return None

        merged: dict[str, FeedEntry] = {}
        for entry in videos or []:
            merged[entry.video_id] = entry
        for entry in streams or []:
            # /streams 只产出 live/upcoming（见 fetch_tab），其状态可信度更高
            prev = merged.get(entry.video_id)
            if prev is None:
                merged[entry.video_id] = entry
            else:
                prev.live_state = entry.live_state
                if entry.actual_start_time and not prev.actual_start_time:
                    prev.actual_start_time = entry.actual_start_time

        # 频道名与 id 以页面为准补全
        name = channel_name
        for entry in merged.values():
            entry.channel_id = entry.channel_id or channel_id
            entry.channel_name = entry.channel_name or name

        ordered = _sort_entries(list(merged.values()))
        return FeedResult(channel_id, name, _cap_entries(ordered, max_results))

    async def fetch_video(
        self, video_id: str, *, respect_throttle: bool = True
    ) -> Optional[VideoPageData]:
        """抓观看页取单个视频信息（供测试命令）。

        ⚠️ 只用 ytInitialData：实测自动请求的 ytInitialPlayerResponse 返回
        LOGIN_REQUIRED，不含 videoDetails。
        """
        if not video_id:
            return None
        url = f"{YOUTUBE_BASE}/watch?v={video_id}"
        if await self._enter(f"watch/{video_id}", respect_throttle=respect_throttle):
            logger.debug(f"[YT] 网页抓取冷却中，跳过 watch/{video_id}")
            return None
        html = await self._fetch_html(url, f"page watch {video_id}")
        if not html:
            return None

        data = extract_yt_initial_data(html)
        if data is None:
            logger.warning(f"[YT] 观看页里找不到 ytInitialData: {video_id}")
            return None

        primary = _first(data, "videoPrimaryInfoRenderer") or {}
        overlay = _first(data, "playerOverlayVideoDetailsRenderer") or {}
        owner = _first(data, "videoOwnerRenderer") or {}

        title = _text_of(primary.get("title") or {}) or _text_of(
            overlay.get("title") or {}
        )
        # overlay 的 subtitle.runs = [频道名, "  ", "86", " watching now"]
        subtitle_runs = [
            r.get("text", "")
            for r in ((overlay.get("subtitle") or {}).get("runs") or [])
            if isinstance(r, dict)
        ]
        subtitle = " ".join(subtitle_runs).strip()
        is_live_now = "watching" in subtitle.lower()

        channel_name = _text_of(owner.get("title") or {}) or (
            subtitle_runs[0].strip() if subtitle_runs else ""
        )
        channel_id = _owner_channel_id(owner) or extract_channel_id_from_html(html)

        date_text = _text_of(_first(primary, "dateText") or {})
        published_at = parse_date_text_to_iso(date_text)

        entry = FeedEntry(
            video_id=video_id,
            title=title,
            channel_id=channel_id,
            channel_name=channel_name,
            published_at=published_at,
            url=url,
            thumbnail_url=_watch_thumbnail(html, video_id),
            live_state=LIVE_STATE_LIVE if is_live_now else "",
            actual_start_time=published_at if is_live_now else "",
        )
        return VideoPageData(entry=entry, is_live_now=is_live_now, date_text=date_text)

    async def resolve_channel(
        self, raw_input: str, *, respect_throttle: bool = True
    ) -> Optional[ChannelMeta]:
        """抓频道页解析频道元数据（canonical/externalId + og:title）。"""
        url = channel_page_url(raw_input)
        if not url:
            return None
        if await self._enter(f"resolve/{url}", respect_throttle=respect_throttle):
            logger.debug(f"[YT] 网页抓取冷却中，跳过 resolve {url}")
            return None
        html = await self._fetch_html(url, f"page resolve {raw_input}")
        if not html:
            return None

        channel_id = extract_channel_id_from_html(html)
        if not channel_id:
            logger.warning(f"[YT] 无法从频道页提取 channel id: {url}")
            return None
        return ChannelMeta(
            channel_id=channel_id,
            title=extract_channel_name_from_html(html),
            uploads_playlist_id=uploads_playlist_id_for(channel_id),
        )


# ---------------------------------------------------------------- 组装工具


def _sort_entries(entries: list[FeedEntry]) -> list[FeedEntry]:
    """按发布时间倒序；时间未知的排最后（避免把无时间的条目误判成最新）。"""
    return sorted(entries, key=lambda e: (e.published_at or ""), reverse=True)


def _cap_entries(entries: list[FeedEntry], max_results: int) -> list[FeedEntry]:
    """按 max_results 截断，但**直播/预约条目永不截断**。

    直播可能已经持续很多天（实测 NASA 的 ISS 直播），按时间排序会掉到
    窗口之外；一旦被截掉，live_start 就永远不会触发。Data API 路径靠
    「直播必是上传列表第一条」的假设规避，网页路径没有这个保证。
    """
    limit = max(1, int(max_results))
    keep: list[FeedEntry] = []
    seen_live = 0
    others = 0
    for entry in entries:
        if entry.live_state in (LIVE_STATE_LIVE, LIVE_STATE_UPCOMING):
            keep.append(entry)
            seen_live += 1
        elif others < limit:
            keep.append(entry)
            others += 1
    if len(keep) < len(entries):
        logger.debug(
            f"[YT] 网页快照截断 {len(entries)} → {len(keep)} "
            f"(保留 {seen_live} 条直播/预约)"
        )
    return keep


def _owner_channel_id(owner: dict) -> str:
    """从 videoOwnerRenderer 取频道 ID（导航端点里带 browseId）。"""
    title = owner.get("title") or {}
    for run in title.get("runs") or []:
        if not isinstance(run, dict):
            continue
        endpoint = run.get("navigationEndpoint") or {}
        browse = endpoint.get("browseEndpoint") or {}
        browse_id = browse.get("browseId") or ""
        if browse_id.startswith("UC"):
            return browse_id
    return ""


def _watch_thumbnail(html: str, video_id: str) -> str:
    """观看页缩略图：og:image 优先，兜底用固定的 hqdefault（必然存在）。"""
    match = _OG_IMAGE_RE.search(html)
    if match and match.group(1):
        return match.group(1)
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
