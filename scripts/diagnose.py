"""数据源诊断工具：验证 @handle 解析、直播检测与投稿拉取。

用插件的**生产代码路径**跑一遍，确认在你的网络环境下能否正常工作。

⚠️ 需要在能访问 YouTube 的机器上运行（通常是运行 AstrBot 的 VPS）。

用法：
    # 推荐：用 API Key 走官方 Data API（主路径）
    python scripts/diagnose.py @ukaisaki --api-key AIza...

    # 也可用环境变量，避免 key 进入 shell 历史
    set YT_API_KEY=AIza...          # Windows
    export YT_API_KEY=AIza...       # Linux
    python scripts/diagnose.py @ukaisaki

    # 走代理
    python scripts/diagnose.py @ukaisaki --api-key AIza... --proxy http://127.0.0.1:7890

    # 顺带体检 legacy feed（预期会失败，用于确认该端点确实不可用）
    python scripts/diagnose.py @ukaisaki --api-key AIza... --check-feed

    # 只体检网页 JSON 兜底（不需要 API Key —— 降级链的首选兜底）
    python scripts/diagnose.py @ukaisaki --check-page

    # 离线解析本地 XML
    python scripts/diagnose.py --file feed.xml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _install_stub() -> None:
    import logging

    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("astrbot")
        astrbot.api = api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api
    logging.basicConfig(level=logging.WARNING, format="%(message)s")


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services.data_api import (  # noqa: E402
    YouTubeDataAPI,
    parse_channel_input,
)
from astrbot_plugin_youtube_notifier.services.feed import (  # noqa: E402
    LegacyFeedClient,
    parse_feed,
)
from astrbot_plugin_youtube_notifier.services.page_json import (  # noqa: E402
    ChannelPageClient,
)
from PIL import ImageFont  # noqa: E402


def _line(char: str = "=") -> None:
    print(char * 68)


def check_fonts() -> int:
    """体检中文字体（通知图里中文变方框就是这个原因）。不需要网络。"""
    from astrbot_plugin_youtube_notifier.utils import (
        _EMOJI_FONT_CANDIDATES,
        _FONT_SCAN_DIRS,
        cjk_font_install_hint,
        find_cjk_font,
        font_supports_cjk,
        resolve_emoji_font_path,
        resolve_font_path,
    )

    _line()
    print("⓪ 字体体检（中文显示为方框时看这里）")
    _line()

    resolved = resolve_font_path()
    ok = font_supports_cjk(resolved) if resolved else False
    print(f"解析到的字体 : {resolved or '(无)'}")
    print(f"支持中文     : {'✅ 是' if ok else '❌ 否 —— 中文会渲染成方框'}")

    if not ok:
        print()
        print(cjk_font_install_hint())
        print()
        print("扫描过的目录:")
        for d in _FONT_SCAN_DIRS:
            import os as _os
            print(f"  {'存在' if _os.path.isdir(d) else '不存在'}  {d}")
        print()
        print("系统已安装的字体（前 20 个）:")
        try:
            import subprocess
            out = subprocess.run(
                ["fc-list", ":lang=zh", "family"], capture_output=True, text=True,
                timeout=10,
            )
            families = sorted({f.strip() for f in out.stdout.splitlines() if f.strip()})
            if families:
                for f in families[:20]:
                    print(f"  {f}")
            else:
                print("  (fc-list 没有列出任何中文字体 —— 确认未安装)")
            if not families:
                print("  提示: 安装字体后重跑本命令确认")
        except FileNotFoundError:
            print("  (未安装 fontconfig，无法用 fc-list 检查)")
        except Exception as exc:  # noqa: BLE001
            print(f"  (fc-list 调用失败: {exc!r})")

    emoji = resolve_emoji_font_path()
    print(f"emoji 字体   : {emoji or '(未找到，emoji 会被剥离而不是显示方框)'}")
    if resolved and ok:
        # 真渲染一次，肉眼确认没有方框
        try:
            from PIL import Image, ImageDraw
            from astrbot_plugin_youtube_notifier.utils import draw_text_with_emoji
            font = ImageFont.truetype(resolved, 40)
            img = Image.new("RGB", (520, 80), (18, 18, 18))
            draw_text_with_emoji(
                ImageDraw.Draw(img), (10, 20), "中文渲染测试 字幕组 直播",
                font, None, fill=(255, 255, 255),
            )
            out_path = PLUGIN_ROOT / "data" / "images" / "font_check.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            print(f"测试图已生成 : {out_path}")
            print("              （打开确认『中文渲染测试』不是方框）")
        except Exception as exc:  # noqa: BLE001
            print(f"(生成字体测试图失败: {exc!r})")

    return 0 if ok else 1


async def run_api(channel_input: str, api_key: str, proxy: str, max_results: int) -> int:
    import aiohttp

    kind, value = parse_channel_input(channel_input)
    print(f"输入解析: kind={kind} value={value!r}")

    timeout = aiohttp.ClientTimeout(total=40, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = YouTubeDataAPI(session, api_key, proxy=proxy)

        _line()
        print("① 频道解析 channels.list")
        _line()
        try:
            meta = await api.resolve_channel(channel_input)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 解析失败: {exc!r}")
            return 1
        print(f"channel_id           : {meta.channel_id}")
        print(f"title                : {meta.title!r}")
        print(f"handle               : {meta.handle!r}")
        print(f"uploads_playlist_id  : {meta.uploads_playlist_id}")

        _line()
        print(f"② 频道快照 playlistItems + videos (max={max_results})")
        _line()
        try:
            snap = await api.fetch_snapshot(
                meta.channel_id, meta.uploads_playlist_id,
                max_results=max_results, channel_name=meta.title,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 拉取失败: {exc!r}")
            return 1

        live = snap.find_live()
        print(f"共 {len(snap.entries)} 条：")
        for i, e in enumerate(snap.entries, 1):
            flag = {"live": "🔴 直播中", "upcoming": "⏳ 预告", "completed": "⚫ 已结束"}.get(
                e.live_state, "📺 投稿"
            )
            ts = e.actual_start_time or e.published_at
            print(f"  [{i}] {flag}  {e.video_id}  {ts}")
            print(f"       {e.title[:56]}")
            if e.actual_end_time:
                print(f"       结束: {e.actual_end_time}")
        print()
        if live:
            print(f"✅ 检测到直播: {live.video_id} / {live.title[:40]}")
        else:
            print("ℹ️ 当前无直播（若该频道确实在直播，说明检测有问题）")

        _line()
        print(f"配额消耗: {api.quota_used_today} 单位 "
              f"（每频道每轮约 2 单位，免费额度 10000/天）")
        _line()

    return 0


async def check_page(channel_input: str, proxy: str, max_results: int) -> int:
    """体检网页 JSON 兜底数据源（Data API 不可用时的首选降级路径）。

    不需要 API Key：这条路正是为「没 Key」或「配额耗尽」准备的。
    """
    import aiohttp

    print()
    _line()
    print("③ 网页 JSON 兜底体检（/videos + /streams）")
    _line()
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = ChannelPageClient(session, proxy=proxy, min_interval=0)
        meta = await client.resolve_channel(channel_input, respect_throttle=False)
        if meta is None:
            print("❌ 频道页解析失败（网络/代理不通，或页面结构变化）")
            return 1
        print(f"channel_id : {meta.channel_id}")
        print(f"title      : {meta.title!r}")
        print(f"uploads    : {meta.uploads_playlist_id}")

        snap = await client.fetch_snapshot(
            meta.channel_id, meta.handle or channel_input,
            max_results=max_results, channel_name=meta.title,
            respect_throttle=False,
        )
        if snap is None:
            print("❌ 快照抓取失败（/videos 与 /streams 都没拿到）")
            return 1

        live = snap.find_live()
        print(f"\n共 {len(snap.entries)} 条（直播免疫 max_results 截断）：")
        for i, e in enumerate(snap.entries, 1):
            flag = {"live": "🔴 直播中", "upcoming": "⏳ 预告"}.get(e.live_state, "📺 投稿")
            print(f"  [{i}] {flag}  {e.video_id}  {e.published_at or '(无时间信息)'}")
            print(f"       {e.title[:56]}")
        print()
        if live:
            print(f"✅ 检测到直播: {live.video_id} / {live.title[:40]}")
        else:
            print("ℹ️ 当前无直播（若该频道确实在直播，说明检测有问题）")
        print("ℹ️ 注意：网页数据无精确时间戳，时间均为近似值")
    return 0


async def check_feed(channel_id: str, proxy: str) -> None:
    import aiohttp

    print()
    _line()
    print("④ legacy Atom feed 体检（预期失败 —— 该端点已不可靠）")
    _line()
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = LegacyFeedClient(session, proxy=proxy)
        result = await client.fetch_feed(channel_id, respect_throttle=False)
    if result is None:
        print("✅ 确认不可用（符合预期，请使用 Data API 或网页兜底）")
    else:
        print(f"⚠️ 意外可用：解析到 {len(result.entries)} 条 entry")
        print("   （该端点时好时坏，不建议作为主数据源）")


def diagnose_file(path: str) -> int:
    xml_text = Path(path).read_text(encoding="utf-8")
    print(f"读取本地文件: {path}")
    result = parse_feed(xml_text)
    if result is None:
        print("❌ 解析失败")
        return 1
    print(f"channel: {result.channel_name!r} ({result.channel_id})")
    print(f"entries: {len(result.entries)}")
    for i, e in enumerate(result.entries, 1):
        print(f"  [{i}] live_state={e.live_state!r:12} {e.video_id}  {e.title[:44]}")
    live = result.find_live()
    print(f"find_live(): {live.video_id if live else None}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube 数据源诊断")
    ap.add_argument("channel", nargs="?", default="", help="@handle / 频道URL / UC频道ID")
    ap.add_argument("--api-key", default=os.environ.get("YT_API_KEY", ""),
                    help="YouTube Data API Key（或用环境变量 YT_API_KEY）")
    ap.add_argument("--proxy", default="", help="代理, 如 http://127.0.0.1:7890")
    ap.add_argument("--max-results", type=int, default=5, help="拉取最近视频条数")
    ap.add_argument("--check-feed", action="store_true", help="顺带体检 legacy feed")
    ap.add_argument("--check-page", action="store_true",
                    help="体检网页 JSON 兜底数据源（不需要 API Key）")
    ap.add_argument("--check-fonts", action="store_true",
                    help="体检中文字体（通知图中文变方框时用；不需要频道/Key/网络）")
    ap.add_argument("--file", default="", help="离线解析本地 feed XML")
    args = ap.parse_args()

    # 字体体检完全不依赖网络与频道，单独跑完即退出
    if args.check_fonts and not args.channel:
        return check_fonts()

    if args.file:
        return diagnose_file(args.file)

    if not args.channel:
        ap.error("需要提供频道标识，或用 --file / --check-fonts 单独体检")

    # 跟着频道一起跑时，字体体检放最前（它最能解释「图里全是方框」）
    if args.check_fonts:
        check_fonts()
        print()

    # 只体检网页兜底：不需要 Key，直接跑
    if args.check_page and not args.api_key:
        return asyncio.run(
            check_page(args.channel, args.proxy, args.max_results)
        )

    if not args.api_key:
        print("❌ 未提供 API Key。")
        print("   申请（免费、无需 OAuth）：Google Cloud Console → 启用 YouTube Data API v3")
        print("   → 凭据 → 创建凭据 → API 密钥")
        print("   然后：python scripts/diagnose.py @handle --api-key AIza...")
        print()
        print("   提示：也可以先用 --check-page 体检网页 JSON 兜底（无需 Key）：")
        print("         python scripts/diagnose.py @handle --check-page")
        return 1

    code = asyncio.run(run_api(args.channel, args.api_key, args.proxy, args.max_results))

    if args.check_page and not args.file:
        asyncio.run(check_page(args.channel, args.proxy, args.max_results))

    if args.check_feed and not args.file:
        # 体检需要频道 ID；从上面的解析结果拿不到就跳过
        try:
            kind, value = parse_channel_input(args.channel)
            if kind == "id":
                asyncio.run(check_feed(value, args.proxy))
            else:
                print()
                print("ℹ️ --check-feed 需要频道 ID 形式（UC...），已跳过")
        except Exception:  # noqa: BLE001
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
