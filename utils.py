"""通用工具：网络重试退避、时间处理、中文字体解析、文本换行。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Sequence

logger = logging.getLogger("astrbot")


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串（无微妙）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO8601 字符串，失败返回 None。"""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def seconds_between(start_iso: str, end_iso: str) -> int:
    """两个 ISO 时间相差秒数；任一解析失败返回 0。"""
    s, e = parse_iso(start_iso), parse_iso(end_iso)
    if s is None or e is None:
        return 0
    return max(0, int((e - s).total_seconds()))


def format_duration(seconds: int) -> str:
    """秒数格式化为 hh:mm:ss 或 mm:ss。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_time_zh(iso_str: Optional[str]) -> str:
    """ISO 时间 → 中文展示（本地时区 HH:MM），解析失败返回空串。"""
    dt = parse_iso(iso_str)
    if dt is None:
        return ""
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """从异常中提取 Retry-After（秒）。支持 aiohttp 响应头与自定义属性。"""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    headers = getattr(exc, "headers", None)
    if headers:
        try:
            raw = headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            try:
                return float(str(raw).strip())
            except ValueError:
                return None  # HTTP-date 形式，忽略，走默认退避
    return None


async def retry_async(
    coro_factory: Callable[[], Awaitable],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    factor: float = 3.0,
    jitter: float = 0.3,
    logger: Optional[logging.Logger] = None,
    label: str = "request",
):
    """指数退避重试：网络错误 / 5xx / 429 时重试。

    Args:
        coro_factory: 返回协程的可调用对象（每次重试重新创建，避免复用已消费的协程）。
        retries: 最大重试次数（不含首次）。
        base_delay / factor: 退避基数与倍率，延迟 = base * factor**i + 抖动。
        jitter: 抖动比例。
        logger: 打日志用 logger。
        label: 日志前缀。

    Note:
        若异常带 Retry-After（含 aiohttp 响应头），优先采用该值作为退避时长。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - 网络层统一重试
            last_exc = exc
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = base_delay * (factor**attempt)
            delay += delay * jitter * random.random()
            if attempt < retries:
                if logger:
                    logger.warning(
                        f"[YT] {label} 失败({attempt + 1}/{retries + 1}): {exc!r}, "
                        f"{delay:.1f}s 后重试"
                    )
                await asyncio.sleep(delay)
            else:
                if logger:
                    logger.warning(f"[YT] {label} 重试耗尽: {exc!r}")
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------- 字体解析

_FONT_CANDIDATES = [
    # Windows 中文字体
    "C:/Windows/Fonts/msyh.ttc",        # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",      # 微软雅黑 Bold
    "C:/Windows/Fonts/simhei.ttf",      # 黑体
    "C:/Windows/Fonts/simsun.ttc",      # 宋体
    "C:/Windows/Fonts/Deng.ttf",        # 等线
    # Linux / macOS
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]


def resolve_font_path(override: Optional[str] = None) -> Optional[str]:
    """按序解析可用中文字体路径；override 存在且有效时优先。"""
    if override and os.path.exists(override):
        return override
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def resolve_bold_font_path(regular_path: Optional[str], override: Optional[str] = None) -> Optional[str]:
    """尝试为常规字体找到粗体变体。"""
    if override and os.path.exists(override):
        return override
    if not regular_path:
        return None
    name, ext = os.path.splitext(regular_path)
    for candidate in (name + "bd" + ext, name + "bd.ttc", name.replace("Regular", "Bold")):
        if os.path.exists(candidate):
            return candidate
    return None


# ---------------------------------------------------------------- emoji

_EMOJI_FONT_CANDIDATES = [
    "C:/Windows/Fonts/seguiemj.ttf",       # Segoe UI Emoji（彩色）
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguisym.ttf",       # 单色兜底
]


def resolve_emoji_font_path() -> Optional[str]:
    """解析可用的 emoji 字体（优先彩色）。无则返回 None。"""
    for path in _EMOJI_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def is_emoji_char(ch: str) -> bool:
    """粗略判断字符是否属于 emoji 区段。"""
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or cp in (0xFE0F, 0x20E3, 0x2139, 0x2194, 0x2195)
    )


def split_emoji_runs(text: str) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否emoji) 的序列，供混合字体绘制。"""
    runs: list[tuple[str, bool]] = []
    cur = ""
    cur_is_emoji: Optional[bool] = None
    for ch in text:
        is_e = is_emoji_char(ch)
        if cur_is_emoji is None or is_e == cur_is_emoji:
            cur += ch
            cur_is_emoji = is_e
        else:
            runs.append((cur, cur_is_emoji))
            cur, cur_is_emoji = ch, is_e
    if cur:
        runs.append((cur, cur_is_emoji))
    return runs


def draw_text_with_emoji(
    draw,
    xy: tuple[int, int],
    text: str,
    cjk_font,
    emoji_font=None,
    fill=(255, 255, 255),
    emoji_y_offset: int = 0,
) -> int:
    """绘制可能含 emoji 的文本：emoji 段用 emoji 字体（彩色），其余用 CJK 字体。

    emoji 字体缺失时自动剥离 emoji 字符，避免渲染成豆腐块。

    Returns:
        绘制结束后的 x 坐标。
    """
    x, y = xy
    if emoji_font is None:
        text = "".join(ch for ch in text if not is_emoji_char(ch))
        if text:
            draw.text((x, y), text, font=cjk_font, fill=fill)
            x += draw.textlength(text, font=cjk_font)
        return int(x)

    for run, is_emoji in split_emoji_runs(text):
        if not run:
            continue
        font = emoji_font if is_emoji else cjk_font
        yy = y + emoji_y_offset if is_emoji else y
        if is_emoji:
            try:
                draw.text((x, yy), run, font=font, fill=fill, embedded_color=True)
            except Exception:  # noqa: BLE001 - 非彩色字体不接受 embedded_color
                draw.text((x, yy), run, font=font, fill=fill)
        else:
            draw.text((x, yy), run, font=font, fill=fill)
        x += draw.textlength(run, font=font)
    return int(x)


def measure_text_with_emoji(draw, text: str, cjk_font, emoji_font=None) -> float:
    """测量含 emoji 文本的像素宽度（与 draw_text_with_emoji 一致）。"""
    if emoji_font is None:
        text = "".join(ch for ch in text if not is_emoji_char(ch))
        return draw.textlength(text, font=cjk_font)
    total = 0.0
    for run, is_emoji in split_emoji_runs(text):
        if run:
            total += draw.textlength(run, font=emoji_font if is_emoji else cjk_font)
    return total


def wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    """按像素宽度将文本折成多行（逐字符，中英文混排友好）。

    Args:
        text: 原始文本（可含换行）。
        font: PIL ImageFont。
        max_width: 最大像素宽度。
        draw: PIL ImageDraw，用于 measure。
    """
    lines: list[str] = []
    for raw_line in text.split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for ch in raw_line:
            if draw.textlength(current + ch, font=font) <= max_width:
                current += ch
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines
