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


def _line(char: str = "=") -> None:
    print(char * 68)


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


async def check_feed(channel_id: str, proxy: str) -> None:
    import aiohttp

    print()
    _line()
    print("③ legacy Atom feed 体检（预期失败 —— 该端点已不可靠）")
    _line()
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = LegacyFeedClient(session, proxy=proxy)
        result = await client.fetch_feed(channel_id, respect_throttle=False)
    if result is None:
        print("✅ 确认不可用（符合预期，请使用 Data API）")
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
    ap.add_argument("--file", default="", help="离线解析本地 feed XML")
    args = ap.parse_args()

    if args.file:
        return diagnose_file(args.file)

    if not args.channel:
        ap.error("需要提供频道标识，或用 --file 离线解析")

    if not args.api_key:
        print("❌ 未提供 API Key。")
        print("   申请（免费、无需 OAuth）：Google Cloud Console → 启用 YouTube Data API v3")
        print("   → 凭据 → 创建凭据 → API 密钥")
        print("   然后：python scripts/diagnose.py @handle --api-key AIza...")
        return 1

    code = asyncio.run(run_api(args.channel, args.api_key, args.proxy, args.max_results))

    if args.check_feed and not args.file:
        # 体检需要频道 ID；从上面的解析结果拿不到就跳过
        try:
            kind, value = parse_channel_input(args.channel)
            if kind == "id":
                asyncio.run(check_feed(value, args.proxy))
        except Exception:  # noqa: BLE001
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
