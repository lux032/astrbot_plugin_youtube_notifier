"""Data API v3 客户端单测：输入解析、响应映射、快照组装（mock HTTP）。

运行: python tests/test_data_api.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

if "astrbot" not in sys.modules:
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot_test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    logging.basicConfig(level=logging.WARNING)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services.data_api import (  # noqa: E402
    ChannelNotFoundError,
    InvalidApiKeyError,
    QuotaExceededError,
    YouTubeDataAPI,
    parse_channel_input,
)
from astrbot_plugin_youtube_notifier.services.scrape import (  # noqa: E402
    uploads_playlist_id_for,
)


# ---------------------------------------------------------------- mock HTTP


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """按 URL 片段路由到预设响应，并记录调用次数。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        for fragment, payload in self.routes.items():
            if fragment in url:
                resp = payload
                if callable(payload):
                    resp = payload(params or {})
                if isinstance(resp, tuple):
                    return FakeResponse(resp[0], resp[1])
                return FakeResponse(resp)
        return FakeResponse({"error": {"message": "no route"}}, 404)


# ---------------------------------------------------------------- 输入解析


def test_parse_channel_input() -> None:
    cases = [
        ("@ukaisaki", ("handle", "ukaisaki")),
        ("ukaisaki", ("handle", "ukaisaki")),
        ("https://www.youtube.com/@ukaisaki", ("handle", "ukaisaki")),
        ("https://www.youtube.com/@ukaisaki/videos", ("handle", "ukaisaki")),
        ("https://www.youtube.com/channel/UCNydvA0D7GSuT0c9Zs-zWdw",
         ("id", "UCNydvA0D7GSuT0c9Zs-zWdw")),
        ("UCNydvA0D7GSuT0c9Zs-zWdw", ("id", "UCNydvA0D7GSuT0c9Zs-zWdw")),
        ("https://www.youtube.com/c/CustomName", ("handle", "CustomName")),
        ("https://www.youtube.com/user/LegacyName", ("username", "LegacyName")),
        ("https://www.youtube.com/@handle?si=xyz", ("handle", "handle")),
    ]
    for raw, expected in cases:
        got = parse_channel_input(raw)
        assert got == expected, f"{raw!r} → {got}，期望 {expected}"
    assert parse_channel_input("") == ("handle", "")
    print(f"✅ test_parse_channel_input ({len(cases)} 个用例)")


def test_uploads_playlist_derivation() -> None:
    assert uploads_playlist_id_for("UCNydvA0D7GSuT0c9Zs-zWdw") == "UUNydvA0D7GSuT0c9Zs-zWdw"
    assert uploads_playlist_id_for("short") == ""
    print("✅ test_uploads_playlist_derivation")


# ---------------------------------------------------------------- 响应映射


def test_apply_video_detail_states() -> None:
    from astrbot_plugin_youtube_notifier.services.data_api import YouTubeDataAPI as A
    from astrbot_plugin_youtube_notifier.services.models import FeedEntry

    # 正在直播
    e = FeedEntry(video_id="V1")
    A._apply_video_detail(e, {
        "id": "V1",
        "snippet": {"title": "直播中", "liveBroadcastContent": "live",
                    "thumbnails": {"medium": {"url": "t.jpg"}}},
        "liveStreamingDetails": {"actualStartTime": "2026-09-12T12:00:00Z"},
    })
    assert e.live_state == "live" and e.is_live
    assert e.actual_start_time == "2026-09-12T12:00:00Z"
    assert e.thumbnail_url == "t.jpg"

    # 已结束的直播（liveBroadcastContent 回落为 none，但有 liveStreamingDetails）
    e2 = FeedEntry(video_id="V2")
    A._apply_video_detail(e2, {
        "id": "V2",
        "snippet": {"title": "已结束", "liveBroadcastContent": "none"},
        "liveStreamingDetails": {"actualStartTime": "2026-09-12T10:00:00Z",
                                 "actualEndTime": "2026-09-12T11:30:00Z"},
    })
    assert e2.live_state == "completed" and not e2.is_live and e2.was_live
    assert e2.actual_end_time == "2026-09-12T11:30:00Z"

    # 预告
    e3 = FeedEntry(video_id="V3")
    A._apply_video_detail(e3, {
        "id": "V3",
        "snippet": {"liveBroadcastContent": "upcoming"},
        "liveStreamingDetails": {"scheduledStartTime": "2026-09-13T10:00:00Z"},
    })
    assert e3.live_state == "upcoming"

    # 普通投稿（无 liveStreamingDetails）
    e4 = FeedEntry(video_id="V4")
    A._apply_video_detail(e4, {
        "id": "V4", "snippet": {"title": "普通视频", "liveBroadcastContent": "none"},
    })
    assert e4.live_state == "" and not e4.was_live
    print("✅ test_apply_video_detail_states")


def test_map_error() -> None:
    from astrbot_plugin_youtube_notifier.services.data_api import YouTubeDataAPI as A

    quota = A._map_error(403, {"error": {"message": "quota", "errors": [
        {"reason": "quotaExceeded"}]}})
    assert isinstance(quota, QuotaExceededError), quota

    bad_key = A._map_error(400, {"error": {"message": "API key not valid. Please pass a valid API key.",
                                           "errors": [{"reason": "keyInvalid"}]}})
    assert isinstance(bad_key, InvalidApiKeyError), bad_key

    not_found = A._map_error(404, {"error": {"message": "not found", "errors": [
        {"reason": "channelNotFound"}]}})
    assert isinstance(not_found, ChannelNotFoundError), not_found
    print("✅ test_map_error")


# ---------------------------------------------------------------- 快照组装


def test_resolve_channel_handle() -> None:
    session = FakeSession({
        "/channels": {"items": [{
            "id": "UCNydvA0D7GSuT0c9Zs-zWdw",
            "snippet": {"title": "うかいはる", "customUrl": "@ukaisaki"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUNydvA0D7GSuT0c9Zs-zWdw"}},
        }]},
    })
    api = YouTubeDataAPI(session, "FAKE_KEY")
    meta = asyncio.run(api.resolve_channel("@ukaisaki"))
    assert meta.channel_id == "UCNydvA0D7GSuT0c9Zs-zWdw"
    assert meta.title == "うかいはる"
    assert meta.uploads_playlist_id == "UUNydvA0D7GSuT0c9Zs-zWdw"
    # 应带上 forHandle 参数（handle 走官方解析，而非抓页面）
    assert session.calls[0][1].get("forHandle") == "ukaisaki"
    print("✅ test_resolve_channel_handle")


def test_resolve_channel_not_found() -> None:
    session = FakeSession({"/channels": {"items": []}})
    api = YouTubeDataAPI(session, "FAKE_KEY")
    try:
        asyncio.run(api.resolve_channel("@nobody"))
    except ChannelNotFoundError:
        print("✅ test_resolve_channel_not_found")
        return
    raise AssertionError("应抛 ChannelNotFoundError")


def test_fetch_snapshot_merges_live_and_videos() -> None:
    session = FakeSession({
        "/playlistItems": {"items": [
            # 一条正在直播的
            {"snippet": {"title": "直播标题", "channelId": "UC1", "channelTitle": "频道",
                         "publishedAt": "2026-09-12T12:00:00Z",
                         "resourceId": {"videoId": "LIVEID00001"},
                         "thumbnails": {"medium": {"url": "a.jpg"}}},
             "contentDetails": {"videoId": "LIVEID00001",
                                "videoPublishedAt": "2026-09-12T12:00:00Z"}},
            # 一条普通投稿
            {"snippet": {"title": "普通视频", "channelId": "UC1", "channelTitle": "频道",
                         "publishedAt": "2026-09-11T08:00:00Z",
                         "resourceId": {"videoId": "VID0000001"},
                         "thumbnails": {"medium": {"url": "b.jpg"}}},
             "contentDetails": {"videoId": "VID0000001",
                                "videoPublishedAt": "2026-09-11T08:00:00Z"}},
        ]},
        "/videos": {"items": [
            {"id": "LIVEID00001",
             "snippet": {"title": "直播标题", "liveBroadcastContent": "live",
                         "thumbnails": {"medium": {"url": "a.jpg"}}},
             "liveStreamingDetails": {"actualStartTime": "2026-09-12T12:05:00Z"}},
            {"id": "VID0000001",
             "snippet": {"title": "普通视频", "liveBroadcastContent": "none",
                         "thumbnails": {"medium": {"url": "b.jpg"}}}},
        ]},
    })
    api = YouTubeDataAPI(session, "FAKE_KEY")
    snap = asyncio.run(api.fetch_snapshot("UC1", "UU1", max_results=5, channel_name="频道"))

    assert snap.channel_id == "UC1"
    assert len(snap.entries) == 2, snap.entries
    live = snap.find_live()
    assert live is not None and live.video_id == "LIVEID00001"
    assert live.actual_start_time == "2026-09-12T12:05:00Z"
    assert live.url == "https://www.youtube.com/watch?v=LIVEID00001"

    regular = [e for e in snap.entries if not e.live_state]
    assert len(regular) == 1 and regular[0].video_id == "VID0000001"

    # 消耗 2 单位（playlistItems + videos）
    assert api.quota_used_today == 2, api.quota_used_today
    print("✅ test_fetch_snapshot_merges_live_and_videos")


def test_missing_api_key_raises() -> None:
    from astrbot_plugin_youtube_notifier.services.data_api import ApiKeyMissingError

    api = YouTubeDataAPI(FakeSession({}), "")
    assert not api.configured
    try:
        asyncio.run(api.resolve_channel("@x"))
    except ApiKeyMissingError:
        print("✅ test_missing_api_key_raises")
        return
    raise AssertionError("应抛 ApiKeyMissingError")


def main() -> int:
    tests = [
        test_parse_channel_input,
        test_uploads_playlist_derivation,
        test_apply_video_detail_states,
        test_map_error,
        test_resolve_channel_handle,
        test_resolve_channel_not_found,
        test_fetch_snapshot_merges_live_and_videos,
        test_missing_api_key_raises,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"❌ {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"❌ {fn.__name__} 异常: {exc!r}")
    print()
    print(f"{'❌' if failed else '🎉'} {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
