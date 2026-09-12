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
    non_retryable: tuple[type[BaseException], ...] = (),
):
    """指数退避重试：网络错误 / 5xx / 429 时重试。

    Args:
        coro_factory: 返回协程的可调用对象（每次重试重新创建，避免复用已消费的协程）。
        retries: 最大重试次数（不含首次）。
        base_delay / factor: 退避基数与倍率，延迟 = base * factor**i + 抖动。
        jitter: 抖动比例。
        logger: 打日志用 logger。
        label: 日志前缀。
        non_retryable: **不该重试**的异常类型，命中即刻抛出。

    Note:
        若异常带 Retry-After（含 aiohttp 响应头），优先采用该值作为退避时长。

    Note:
        `non_retryable` 很重要：把「永久性失败」丢进重试循环既浪费时间又刷屏。
        实测踩坑：API Key 无效这类调用方语义错误被重试 3 次，
        每次白等 1.1s + 3.3s + 11.6s ≈ 16 秒，才走到本该立即执行的降级逻辑。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except non_retryable:
            raise  # 语义性失败，重试不会变好
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


# ---------------------------------------------------------------- 网络常量

# 浏览器 UA —— YouTube 对缺少浏览器特征的请求会返回精简页/机器人校验。
# 手机版页面结构不同，务必用桌面版 UA。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 抓取 YouTube 网页时统一的请求头（Accept-Language 固定英文，
# 相对时间/日期文案才能用固定词表解析，见 services/page_json.py）
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------- 字体解析

_FONT_CANDIDATES = [
    # Windows 中文字体
    "C:/Windows/Fonts/msyh.ttc",        # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",      # 微软雅黑 Bold
    "C:/Windows/Fonts/simhei.ttf",      # 黑体
    "C:/Windows/Fonts/simsun.ttc",      # 宋体
    "C:/Windows/Fonts/Deng.ttf",        # 等线
    # Debian / Ubuntu（apt-get install fonts-noto-cjk）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    # 某些发行版把 noto-cjk 放在别处
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # 文泉驿（apt-get install fonts-wqy-microhei / fonts-wqy-zenhei）
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
    # 文鼎（apt-get install fonts-arphic-uming / -ukai）
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    # Droid Sans Fallback（fonts-droid-fallback，最小化系统的常见兜底）
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # 思源黑体（手动安装）
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSans-Regular.otf",
    "/usr/share/fonts/truetype/source-han-sans/SourceHanSans-Regular.otf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

# 纯拉丁字体：**不含任何中文字形**，只能作为最后手段。
# ⚠️ 绝不能把它当成「找到中文字体」：在 Debian/Ubuntu 最小化安装上
# DejaVu 一定存在而中文字体可能没装，若把它排在中文字体之前（历史 bug），
# 就会静默命中 DejaVu → 所有中文渲染成豆腐块，且没有任何报错。
_FALLBACK_LATIN_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# 找不到显式路径时，在这些目录里扫一遍（按需、带缓存）
_FONT_SCAN_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/truetype",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
]

# 扫描时优先测试的文件名特征（命中这些先试，多数情况能立刻找到）
_CJK_NAME_HINTS = (
    "notosanscjk", "notoserifcjk", "sourcehansans", "sourcehanserif",
    "wqy", "droid", "arphic", "uming", "ukai",
    "msyh", "simhei", "simsun", "deng", "pingfang", "heiti",
    "unifont", "hanazono", "notosanssc", "notosanstc", "notosansjp",
)

_FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc")

# 中文探测字符与「必定缺字」的私用区字符
_CJK_PROBE = "中"
_MISSING_PROBE = "\ue000"

_CJK_SUPPORT_CACHE: dict[str, bool] = {}
_RESOLVED_FONT: Optional[str] = None
_RESOLVED_DONE = False


def font_supports_cjk(path: str, size: int = 40) -> bool:
    """判断字体是否真的含中文字形。

    做法：把中文字的位图与一个**私用区字符**（U+E000，几乎必定缺字）的
    位图比对 —— 字体没有该字形时两者都会退化成同一个 .notdef 豆腐块。
    这是唯一不依赖 fontTools / FreeType 内部接口的可靠办法：
    `os.path.exists` 只能说明「文件在」，说明不了「能画中文」。

    Note:
        实测对照：msyh.ttc → False（中文位图与缺字位图不同，支持中文）；
        arial.ttf → True（两者一致，即中文也是豆腐块）。
    """
    if not path:
        return False
    if path in _CJK_SUPPORT_CACHE:
        return _CJK_SUPPORT_CACHE[path]
    result = False
    try:
        # 延迟导入：utils 被多个不需要绘图的模块引用，不该因为 Pillow
        # 缺失而整个包导入失败（renderer 会先一步报出真正的原因）
        from PIL import ImageFont
    except ImportError:  # pragma: no cover - 正常安装都会有 Pillow
        logger.error("[YT] 未安装 Pillow，无法检测字体中文支持（pip install pillow）")
        return True
    try:
        font = ImageFont.truetype(path, size)
        cjk_mask = font.getmask(_CJK_PROBE)
        missing_mask = font.getmask(_MISSING_PROBE)
        # 中文与「必定缺字」位图一致 → 说明中文也走的 .notdef
        result = bytes(cjk_mask) != bytes(missing_mask)
    except Exception as exc:  # noqa: BLE001 - 字体损坏/不支持时视为不可用
        logger.warning(f"[YT] 字体可用性检测失败 {path}: {exc!r}")
        result = False
    _CJK_SUPPORT_CACHE[path] = result
    return result


def _iter_scan_fonts() -> list[str]:
    """扫描常见字体目录，名称像 CJK 字体的排在前面。"""
    hinted: list[str] = []
    others: list[str] = []
    for root_dir in _FONT_SCAN_DIRS:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for name in filenames:
                if not name.lower().endswith(_FONT_EXTS):
                    continue
                full = os.path.join(dirpath, name)
                # os.walk 返回的是本地分隔符；统一成 POSIX 便于跨平台缓存与展示
                if os.sep != "/":
                    full = full.replace(os.sep, "/")
                if any(hint in name.lower() for hint in _CJK_NAME_HINTS):
                    hinted.append(full)
                else:
                    others.append(full)
    return hinted + others


def find_cjk_font() -> Optional[str]:
    """找出一个真正能渲染中文的字体；找不到返回 None。

    顺序：① 已知路径（含推荐安装的 Noto CJK）→ ② 扫描字体目录。
    结果缓存，避免每轮渲染重复扫描文件系统。
    """
    for path in _FONT_CANDIDATES:
        if os.path.exists(path) and font_supports_cjk(path):
            return path

    for path in _iter_scan_fonts():
        if font_supports_cjk(path):
            logger.info(f"[YT] 扫描到可用中文字体: {path}")
            return path
    return None


def resolve_font_path(override: Optional[str] = None) -> Optional[str]:
    """解析可用的**中文字体**路径。

    Args:
        override: 配置项 render.font_path。存在且可用时优先；
            配置了但不可用会明确告警（而不是静默忽略，那会让人以为生效了）。

    Returns:
        可渲染中文的字体路径；实在找不到中文字体时返回纯拉丁字体兜底
        （此时中文会变豆腐块，调用方应据 font_supports_cjk 告警），
        连拉丁字体都没有则返回 None。
    """
    global _RESOLVED_FONT, _RESOLVED_DONE

    if override:
        if not os.path.exists(override):
            logger.error(
                f"[YT] 配置的 render.font_path 不存在，已忽略并自动选择: {override}"
            )
        elif not font_supports_cjk(override):
            logger.error(
                f"[YT] 配置的 render.font_path 不含中文字形（中文会显示为方框）: "
                f"{override}"
            )
            return override  # 用户显式指定了，尊重其选择但已告警
        else:
            return override

    if _RESOLVED_DONE and _RESOLVED_FONT:
        return _RESOLVED_FONT

    found = find_cjk_font()
    if found:
        _RESOLVED_FONT, _RESOLVED_DONE = found, True
        return found

    for path in _FALLBACK_LATIN_FONTS:
        if os.path.exists(path):
            _RESOLVED_DONE = True
            return path
    _RESOLVED_DONE = True
    return None


def cjk_font_install_hint() -> str:
    """没找到中文字体时给用户的可执行修复指引。"""
    return (
        "未找到任何中文字体，通知图里的中文会渲染成方框。请任选一种修复：\n"
        "  ① Debian/Ubuntu:  apt-get install -y fonts-noto-cjk\n"
        "  ② CentOS/RHEL/Fedora:  dnf install -y google-noto-sans-cjk-fonts\n"
        "  ③ Alpine:  apk add font-noto-cjk\n"
        "  ④ Arch:  pacman -S noto-fonts-cjk\n"
        "  ⑤ 或把任意中文字体文件放到服务器上，并把插件配置项 "
        "render.font_path 设为该文件的绝对路径\n"
        "装好后可用 `fc-list :lang=zh | head` 确认，然后重载插件。"
    )


def resolve_bold_font_path(regular_path: Optional[str], override: Optional[str] = None) -> Optional[str]:
    """尝试为常规字体找到粗体变体；找不到返回 None（调用方回退到常规字体）。

    候选粗体也要求含中文字形：否则 msyh.ttc（含中文）可能匹配到某个
    纯拉丁的粗体文件，标题里的中文就变方框了。
    """
    if override and os.path.exists(override):
        return override
    if not regular_path:
        return None
    name, ext = os.path.splitext(regular_path)
    for candidate in (
        # Noto CJK 的粗体是 -Bold 后缀
        regular_path.replace("Regular", "Bold"),
        name + "bd" + ext,
        name + "bd.ttc",
    ):
        if candidate == regular_path:
            continue
        if os.path.exists(candidate) and font_supports_cjk(candidate):
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
