"""文生图通知渲染（Pillow）。

render_notification_image(data: dict) -> str
    - live_start:  🔴 正在直播（红）
    - live_end:    ⚫ 直播结束（灰）
    - new_video:   📺 新视频发布（蓝）

深色背景 + 白字，圆角，封面自适应，中文自动换行，画布高度自适应。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .utils import (
    cjk_font_install_hint,
    draw_text_with_emoji,
    font_supports_cjk,
    format_duration,
    format_time_zh,
    measure_text_with_emoji,
    resolve_bold_font_path,
    resolve_emoji_font_path,
    resolve_font_path,
    wrap_text,
)

logger = logging.getLogger("astrbot")

BG_COLOR = (18, 18, 18)
TITLE_COLOR = (255, 255, 255)
META_COLOR = (189, 189, 189)
URL_COLOR = (144, 202, 249)
MUTED_COLOR = (97, 97, 97)

# 每种通知的头部样式
TYPE_STYLE = {
    "live_start": {
        "label": "🔴 正在直播",
        "accent": (229, 57, 53),      # 红
    },
    "live_end": {
        "label": "⚫ 直播结束",
        "accent": (97, 97, 97),       # 灰
    },
    "new_video": {
        "label": "📺 新视频发布",
        "accent": (30, 136, 229),     # 蓝
    },
}
DEFAULT_STYLE = TYPE_STYLE["new_video"]

PADDING = 40
MAX_TITLE_LINES = 3
THUMB_RATIO = 9 / 16
THUMB_RADIUS = 18


class NotificationRenderer:
    def __init__(
        self,
        image_width: int = 800,
        font_path: str = "",
        output_dir: Path = Path("data/images/notifications"),
    ):
        self.width = max(320, int(image_width))
        self.font_path = font_path or ""
        self.output_dir = Path(output_dir)
        self._font_cache: dict[str, ImageFont.FreeTypeFont] = {}
        self._emoji_font_path = resolve_emoji_font_path()
        if not self._emoji_font_path:
            logger.warning("[YT] 未找到 emoji 字体，通知头部将剥离 emoji 字符")

        # 中文渲染能力必须在启动时说清楚。
        # 踩坑：Linux VPS 常自带 DejaVu 但不带任何中文字体，历史实现会静默
        # 回退到 DejaVu → 所有中文变豆腐块，日志里却什么异常都没有，
        # 用户只能看到一堆方框而不知道是缺字体。
        self.cjk_ok = font_supports_cjk(self._resolved_font_path())
        if not self.cjk_ok:
            logger.error(f"[YT] {cjk_font_install_hint()}")

    def _resolved_font_path(self) -> str:
        """实际会用于渲染的字体路径。"""
        return resolve_font_path(self.font_path) or ""

    # ------------------------------------------------------------ 对外接口

    async def render(self, data: dict) -> str:
        """渲染通知图，返回图片本地路径（绝对路径字符串）。"""
        path = self._render(data)
        return str(path)

    def render_sync(self, data: dict) -> Path:
        return self._render(data)

    # ------------------------------------------------------------ 字体

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        key = f"{size}:{bold}"
        if key in self._font_cache:
            return self._font_cache[key]
        regular = resolve_font_path(self.font_path)
        if not regular:
            f = ImageFont.load_default(size=size)
        else:
            path = resolve_bold_font_path(regular, None) if bold else regular
            try:
                f = ImageFont.truetype(path or regular, size)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 字体加载失败 {path}: {exc!r}，使用默认字体")
                f = ImageFont.load_default(size=size)
        self._font_cache[key] = f  # type: ignore[assignment]
        return f

    def _emoji_font(self, size: int):
        """emoji 字体（彩色优先）；不可用时返回 None。"""
        if not self._emoji_font_path:
            return None
        key = f"emoji:{size}"
        if key in self._font_cache:
            return self._font_cache[key]
        try:
            f = ImageFont.truetype(self._emoji_font_path, size)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] emoji 字体加载失败: {exc!r}")
            return None
        self._font_cache[key] = f  # type: ignore[assignment]
        return f

    # ------------------------------------------------------------ 渲染

    def _render(self, data: dict) -> Path:
        ntype = data.get("type", "")
        style = TYPE_STYLE.get(ntype, DEFAULT_STYLE)
        accent = style["accent"]
        label = style["label"]
        # 测试通知：图上必须能一眼看出是测试，避免被当成真实推送
        if data.get("test"):
            label = f"🧪 测试 {label}"
        title = (data.get("title") or "").strip() or "（无标题）"
        channel = (data.get("channel_name") or "").strip()
        start_time = format_time_zh(data.get("start_time"))
        end_time = format_time_zh(data.get("end_time"))
        duration_seconds = int(data.get("duration_seconds") or 0)
        url = (data.get("url") or "").strip()
        thumbnail_path = data.get("thumbnail_path") or ""

        title_font = self._font(40, bold=True)
        body_font = self._font(26)
        small_font = self._font(22)
        label_font = self._font(28, bold=True)
        url_font = self._font(20)
        label_emoji_font = self._emoji_font(26)

        measure = ImageDraw.Draw(Image.new("RGB", (self.width, 1)))
        content_w = self.width - PADDING * 2

        # 标题折行（限 3 行，超出加省略号）
        title_lines = wrap_text(title, title_font, content_w, measure)
        if len(title_lines) > MAX_TITLE_LINES:
            title_lines = title_lines[:MAX_TITLE_LINES]
            title_lines[-1] = title_lines[-1][:-1] + "…"

        # ---- 布局计算
        pill_w = int(
            measure_text_with_emoji(measure, label, label_font, label_emoji_font)
        ) + 52
        pill_h = 54
        title_line_h = int(40 * 1.45)
        meta_line_h = 40

        has_thumb = bool(thumbnail_path) and Path(thumbnail_path).exists()
        thumb_w = content_w
        thumb_h = int(thumb_w * THUMB_RATIO) if has_thumb else 0

        meta_lines: list[str] = []
        if channel:
            meta_lines.append(f"频道: {channel}")
        if ntype == "live_end" and duration_seconds:
            meta_lines.append(f"时长: {format_duration(duration_seconds)}")
        if start_time:
            meta_lines.append(f"开始: {start_time}")
        if end_time:
            meta_lines.append(f"结束: {end_time}")

        height = (
            PADDING
            + pill_h
            + 26
            + title_line_h * len(title_lines)
            + 26
            + (thumb_h + 28 if has_thumb else 0)
            + meta_line_h * len(meta_lines)
            + (40 if meta_lines else 0)
            + (38 if url else 0)
            + PADDING
            + 30
        )

        img = Image.new("RGB", (self.width, height), BG_COLOR)
        d = ImageDraw.Draw(img)
        y = PADDING

        # 头部胶囊
        _draw_pill(
            d, PADDING, y, pill_w, pill_h, accent, label, label_font, label_emoji_font
        )
        y += pill_h + 26

        # 标题
        for line in title_lines:
            d.text((PADDING, y), line, font=title_font, fill=TITLE_COLOR)
            y += title_line_h
        y += 26

        # 封面
        if has_thumb:
            _paste_thumb(img, d, thumbnail_path, PADDING, y, thumb_w, thumb_h)
            y += thumb_h + 28

        # 元信息
        for line in meta_lines:
            d.text((PADDING, y), line, font=body_font, fill=META_COLOR)
            y += meta_line_h
        if meta_lines:
            y += 8

        # 链接
        if url:
            d.text((PADDING, y), url, font=url_font, fill=URL_COLOR)
            y += 38

        # 页脚
        y += 6
        d.text((PADDING, y), "YouTube 订阅提醒 · AstrBot", font=small_font, fill=MUTED_COLOR)

        # 圆角
        final = _rounded_corners(img)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        video_id = (data.get("video_id") or "x").replace("/", "_")[:24]
        out_path = self.output_dir / f"notif_{ntype}_{video_id}_{uuid.uuid4().hex[:8]}.png"
        final.save(out_path, "PNG")
        logger.info(f"[YT] 通知图已渲染: {out_path}")
        return out_path


# ---------------------------------------------------------------- 绘制工具


def _draw_pill(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    color,
    label: str,
    font,
    emoji_font=None,
) -> None:
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=color)
    # 用 CJK 字形的实际墨迹盒做垂直居中（字体 metrics 会偏上）
    ref_top, ref_bottom = _ink_box(font)
    text_y = y + (h - (ref_bottom - ref_top)) // 2 - ref_top
    draw_text_with_emoji(
        d,
        (x + 22, text_y),
        label,
        font,
        emoji_font=emoji_font,
        fill=(255, 255, 255),
        emoji_y_offset=_emoji_baseline_offset(font, emoji_font),
    )


def _ink_box(font) -> tuple[int, int]:
    """CJK 字体的代表字形墨迹上下界（相对基线）。"""
    try:
        box = font.getbbox("正")
        return int(box[1]), int(box[3])
    except Exception:  # noqa: BLE001
        ascent, descent = font.getmetrics()
        return -ascent, descent


def _emoji_baseline_offset(cjk_font, emoji_font) -> int:
    """让 emoji 墨迹中心与 CJK 文字墨迹中心对齐所需的基线偏移。"""
    if emoji_font is None:
        return 0
    cjk_top, cjk_bottom = _ink_box(cjk_font)
    cjk_center = (cjk_top + cjk_bottom) / 2
    try:
        ebox = emoji_font.getbbox("\U0001F534")  # 🔴 代表 emoji 墨迹
        emoji_center = (int(ebox[1]) + int(ebox[3])) / 2
    except Exception:  # noqa: BLE001
        return 0
    return int(round(cjk_center - emoji_center))


def _paste_thumb(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    path: str,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    try:
        thumb = Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[YT] 封面读取失败 {path}: {exc!r}")
        d.rectangle([x, y, x + w, y + h], fill=(40, 40, 40))
        return
    thumb = thumb.resize((w, h), Image.LANCZOS)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=THUMB_RADIUS, fill=255)
    img.paste(thumb, (x, y), mask)


def _rounded_corners(img: Image.Image, radius: int = 28) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    final = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    final.paste(img, (0, 0), mask)
    return final
