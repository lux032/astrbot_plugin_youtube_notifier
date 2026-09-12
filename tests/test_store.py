"""订阅存储会话隔离 + 持久化单测。

运行: python tests/test_store.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services.models import (  # noqa: E402
    ChannelState,
    STATUS_LIVE,
)
from astrbot_plugin_youtube_notifier.services.store import SubscriptionStore  # noqa: E402

S1 = "qq:group:111"
S2 = "telegram:private:222"
C1 = "UC_AAA"
C2 = "UC_BBB"


def test_session_isolation() -> None:
    """不同会话的订阅互不影响。"""
    store = SubscriptionStore(Path(tempfile.mkdtemp()))
    store.add_subscription(S1, C1, "频道A")
    store.add_subscription(S2, C2, "频道B")

    assert store.get_session_channels(S1) == {C1: {"channel_name": "频道A"}}
    assert store.get_session_channels(S2) == {C2: {"channel_name": "频道B"}}
    assert store.has_subscription(S1, C1) and not store.has_subscription(S1, C2)
    print("✅ test_session_isolation")


def test_shared_channel_sessions() -> None:
    """多个会话订阅同一频道 → sessions_for_channel 返回全部。"""
    store = SubscriptionStore(Path(tempfile.mkdtemp()))
    store.add_subscription(S1, C1, "频道A")
    store.add_subscription(S2, C1, "频道A")
    assert sorted(store.sessions_for_channel(C1)) == sorted([S1, S2])
    assert store.all_channel_ids() == [C1]

    # S1 退订后 S2 仍在，频道仍在轮询范围
    assert store.remove_subscription(S1, C1)
    assert store.sessions_for_channel(C1) == [S2]
    assert C1 in store.all_channel_ids()

    # S2 也退订 → 频道不再被轮询
    store.remove_subscription(S2, C1)
    assert store.all_channel_ids() == []
    print("✅ test_shared_channel_sessions")


def test_add_remove_idempotent() -> None:
    store = SubscriptionStore(Path(tempfile.mkdtemp()))
    assert store.add_subscription(S1, C1) is True
    assert store.add_subscription(S1, C1) is False, "重复订阅应返回 False"
    assert store.remove_subscription(S1, C1) is True
    assert store.remove_subscription(S1, C1) is False, "重复退订应返回 False"
    assert store.get_session_channels(S1) == {}
    print("✅ test_add_remove_idempotent")


def test_persistence_roundtrip() -> None:
    """写入 → 重新载入，订阅与频道状态都应恢复。"""
    data_dir = Path(tempfile.mkdtemp())
    store = SubscriptionStore(data_dir)
    store.add_subscription(S1, C1, "频道A")
    state = store.get_channel_state(C1)
    state.channel_name = "频道A"
    state.last_video_id = "VID123"
    state.last_live_id = "LIVE9"
    state.last_status = STATUS_LIVE
    state.last_live_start_at = "2026-09-12T10:00:00+00:00"
    state.latest_video_title = "某视频"
    asyncio.run(store.save())

    assert store.state_file.exists(), "state.json 应已写出"

    reloaded = SubscriptionStore(data_dir)
    reloaded.load()
    assert reloaded.get_session_channels(S1) == {C1: {"channel_name": "频道A"}}
    st = reloaded.get_channel_state(C1)
    assert st is not None
    assert st.last_video_id == "VID123"
    assert st.last_live_id == "LIVE9"
    assert st.last_status == STATUS_LIVE
    assert st.latest_video_title == "某视频"
    print("✅ test_persistence_roundtrip")


def test_load_missing_and_corrupt() -> None:
    """状态文件缺失或损坏时不应抛异常。"""
    data_dir = Path(tempfile.mkdtemp())
    s = SubscriptionStore(data_dir)
    s.load()  # 文件不存在
    assert s.subscriptions == {} and s.channels == {}

    (data_dir / "state.json").write_text("{ not valid json", encoding="utf-8")
    s2 = SubscriptionStore(data_dir)
    s2.load()  # 损坏文件
    assert s2.subscriptions == {}
    print("✅ test_load_missing_and_corrupt")


def test_channel_state_from_dict_tolerates_extra() -> None:
    """from_dict 容忍未知字段与缺失字段。"""
    st = ChannelState.from_dict({"channel_id": "X", "unknown_field": 1, "last_status": "live"})
    assert st.channel_id == "X" and st.last_status == "live"
    assert st.last_video_id == ""
    print("✅ test_channel_state_from_dict_tolerates_extra")


def main() -> int:
    tests = [
        test_session_isolation,
        test_shared_channel_sessions,
        test_add_remove_idempotent,
        test_persistence_roundtrip,
        test_load_missing_and_corrupt,
        test_channel_state_from_dict_tolerates_extra,
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
