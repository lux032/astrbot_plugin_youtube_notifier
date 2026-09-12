"""批量订阅 / 批量取消订阅单测。

重点覆盖三件容易出错的事：
  1. 参数切分：AstrBot 的指令参数是逐 token 绑定的，`x: str = ""` 只拿得到
     第一个 token，批量命令必须自己从原始消息里取全部参数；
  2. 数量上限：超出 BATCH_MAX_TARGETS 的部分必须**明说**未处理，不能静默丢弃；
  3. 批量取消必须走本地匹配 —— 逐个查 API 就是逐个烧配额。

全部离线：不碰网络、不读插件真实 data/（store 指向临时目录）。

运行: python tests/test_batch_commands.py
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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _install_stub() -> None:
    """注入最简 astrbot 桩（导入 main.py 需要 star / event.filter 等）。"""
    import logging

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot_test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    logging.basicConfig(level=logging.CRITICAL)

    class AstrBotConfig(dict):
        pass

    event_mod = types.ModuleType("astrbot.api.event")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "test:group:1"

        def plain_result(self, text):
            return ("plain", text)

    class _Filter:
        def command(self, name, alias=None, **kwargs):
            def deco(fn):
                return fn

            return deco

    event_mod.MessageChain = MessageChain
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    api.event = event_mod
    sys.modules["astrbot.api.event"] = event_mod

    star = types.ModuleType("astrbot.api.star")

    class Context:
        async def send_message(self, session, chain):
            return True

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            return PLUGIN_ROOT / "data"

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register
    api.star = star
    sys.modules["astrbot.api.star"] = star
    api.AstrBotConfig = AstrBotConfig


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier import main as m  # noqa: E402
from astrbot_plugin_youtube_notifier.services.data_api import (  # noqa: E402
    ChannelNotFoundError,
)
from astrbot_plugin_youtube_notifier.services.models import ChannelMeta  # noqa: E402
from astrbot_plugin_youtube_notifier.services.store import SubscriptionStore  # noqa: E402


# ---------------------------------------------------------------- 测试替身


class FakeEvent:
    """最小事件替身：handler 只会用到 message_str、umo 与 plain_result。"""

    def __init__(self, text: str, umo: str = "test:group:1"):
        self._text = text
        self.unified_msg_origin = umo

    def get_message_str(self) -> str:
        return self._text

    def plain_result(self, text):
        return ("plain", text)


class FakeDataAPI:
    """只用来让 `configured` 为真 —— 让 _monitoring_ready/_degraded_notice
    走到「一切正常」分支，从而能断言**没有**多余的降级提示。"""

    def __init__(self, configured: bool = True):
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured


def _make_plugin(tmp: Path, *, ready: bool = True, channels: dict | None = None):
    """构造插件实例：store 指向临时目录，频道解析走假实现（不发网络）。"""
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    class Ctx:
        async def send_message(self, session, chain):
            return True

    plugin = YouTubeNotifierPlugin(context=Ctx(), config={})
    plugin.data_dir = tmp
    plugin.store = SubscriptionStore(tmp)
    plugin.store.load()
    # ready=True 时假装 Data API 可用 → 监控就绪、无降级提示；
    # ready=False 时什么都没配 → 应走到「监控尚未生效」的提示。
    plugin.data_api = FakeDataAPI(ready) if ready else None
    plugin.page_json = None
    plugin.notifier = None
    plugin.websub = None

    table = channels or {}
    plugin.resolve_calls: list[str] = []

    async def fake_resolve(raw: str):
        plugin.resolve_calls.append(raw)
        meta = table.get(raw.strip().lstrip("@").lower())
        if meta is None:
            raise ChannelNotFoundError(raw)
        return meta, True

    plugin._resolve_channel = fake_resolve  # 实例属性，绕过真实网络解析
    return plugin


def _meta(cid: str, title: str, handle: str = "") -> ChannelMeta:
    return ChannelMeta(
        channel_id=cid,
        title=title,
        handle=handle or f"@{title.lower().replace(' ', '')}",
        uploads_playlist_id="",  # 留空 → 不触发订阅时的快照播种
    )


async def _collect(agen) -> list:
    return [item async for item in agen]


def _text_of(reply) -> str:
    assert reply[0] == "plain", reply
    return reply[1]


# ---------------------------------------------------------------- 参数切分


def test_split_after_command_takes_all_tokens() -> None:
    """批量参数必须拿到**全部** token（`x: str = ""` 只能拿到第一个）。"""
    ev = FakeEvent("yt批量订阅 @a @b @c")
    got = m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES)
    assert got == ["@a", "@b", "@c"], got

    # 别名同样要能被剥掉
    ev = FakeEvent("yt_batch_subscribe @a @b")
    assert m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES) == ["@a", "@b"]

    # 无参数 → 空列表（调用方回用法说明），不能抛异常
    assert m._split_after_command(FakeEvent("yt批量订阅"), m._BATCH_SUBSCRIBE_NAMES) == []

    # 多余空白不该产生空 token
    ev = FakeEvent("yt批量订阅   @a   @b ")
    assert m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES) == ["@a", "@b"]

    # 命令名之后可能是制表符 / 全角空格 —— 框架是先用 `\s+` 归一化才判定
    # 命令匹配的，所以这些也能唤醒命令，但 message_str 里仍是原字符
    for sep in ("\t", "　", "\n"):
        ev = FakeEvent(f"yt批量订阅{sep}@a{sep}@b")
        assert m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES) == ["@a", "@b"], repr(sep)
    print("✅ test_split_after_command_takes_all_tokens")


def test_split_after_command_never_guesses() -> None:
    """首 token 不是本命令名时不许瞎猜 —— 猜错会变成「静默订阅错的频道」。"""
    # 消息结构不符预期（例如框架将来把命令名也剥掉了）：必须返回空，
    # 绝不能把 "@b" 当成第一个参数、把 "@a" 丢掉。
    ev = FakeEvent("@a @b")
    assert m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES) == []

    # 拿不到结构时退回类型注解给的那个 token，至少不当成「没有参数」
    assert m._split_after_command(ev, m._BATCH_SUBSCRIBE_NAMES, "fallback") == ["fallback"]
    print("✅ test_split_after_command_never_guesses")


def test_dedup_keeps_case_sensitive_ids() -> None:
    """只做字面去重：频道 ID 大小写敏感，不能统一小写合并。"""
    unique, dropped = m._dedup_targets(["@a", "@a", "UCAbc", "UCABC", "@a"])
    assert unique == ["@a", "UCAbc", "UCABC"], unique
    assert dropped == 2, dropped
    print("✅ test_dedup_keeps_case_sensitive_ids")


# ---------------------------------------------------------------- 回复渲染


def test_channel_label_avoids_repetition() -> None:
    assert m._channel_label("NASA", "@NASA") == "NASA"
    assert m._channel_label("NASA", "@nasa") == "NASA"
    assert m._channel_label("NASA", "@nasatv") == "NASA (@nasatv)"
    assert m._channel_label("NASA", "") == "NASA"
    assert m._channel_label("", "@nasa") == "@nasa"
    print("✅ test_channel_label_avoids_repetition")


def test_batch_lines_report_every_outcome() -> None:
    results = [
        ("@a", m.SubscribeOutcome("added", "UC1", "A", "@a")),
        ("@b", m.SubscribeOutcome("exists", "UC2", "B", "@b")),
        ("@c", m.SubscribeOutcome("failed", reason="not_found")),
        ("@d", m.SubscribeOutcome("failed", reason="quota")),
    ]
    lines = m._batch_subscribe_lines(results, deduped=1, notice="数据源已降级")
    text = "\n".join(lines)

    assert lines[0] == "批量订阅完成：新增 1 个 / 已订阅 1 个 / 失败 2 个", lines[0]
    assert "✅ A — UC1" in text
    assert "⏭️ 已订阅: B" in text
    assert "❌ @c — 未找到频道" in text
    assert "❌ @d — API 配额已耗尽" in text
    assert "去重" in text
    assert "⚠️ 数据源已降级" in text
    print("✅ test_batch_lines_report_every_outcome")


def test_batch_lines_disclose_skipped_targets() -> None:
    """超出上限的目标必须明说，不能静默丢弃。"""
    results = [("@a", m.SubscribeOutcome("added", "UC1", "A", "@a"))]
    skipped = [f"@skip{i}" for i in range(12)]
    lines = m._batch_subscribe_lines(results, skipped=skipped)
    text = "\n".join(lines)
    assert "12 个本次未处理" in text, text
    assert "@skip0" in text
    assert "其余 2 个未列出" in text, text
    print("✅ test_batch_lines_disclose_skipped_targets")


def test_batch_unsubscribe_lines() -> None:
    results = [
        ("@a", m.UnsubscribeOutcome("removed", "UC1", "A")),
        ("@b", m.UnsubscribeOutcome("missing")),
    ]
    lines = m._batch_unsubscribe_lines(results)
    assert lines[0] == "批量取消订阅完成：已取消 1 个 / 未订阅 1 个", lines[0]
    assert "✅ 已取消订阅: A — UC1" in "\n".join(lines)
    assert "❌ 未订阅: @b" in "\n".join(lines)
    print("✅ test_batch_unsubscribe_lines")


# ---------------------------------------------------------------- 流程


def test_batch_subscribe_flow() -> None:
    """端到端（无网络）：解析 → 落库 → 汇总回复。"""
    channels = {
        "a": _meta("UC1", "A"),
        "b": _meta("UC2", "B"),
        "c": _meta("UC3", "C"),
    }

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            tmp = Path(tmpdir)
            plugin = _make_plugin(tmp, channels=channels)
            ev = FakeEvent("yt批量订阅 @a @b @c @nope @a")

            replies = await _collect(plugin.batch_subscribe(ev))
            text = _text_of(replies[0])

            # @a 重复一次 → 去重；@nope 不存在 → 失败
            assert text.splitlines()[0] == (
                "批量订阅完成：新增 3 个 / 已订阅 0 个 / 失败 1 个"
            ), text
            assert "❌ @nope — 未找到频道" in text, text
            assert "去重" in text, text
            # 解析失败的输入不该再浪费一次 API 调用（去重生效的旁证）
            assert plugin.resolve_calls == ["@a", "@b", "@c", "@nope"], plugin.resolve_calls

            subs = plugin.store.get_session_channels(ev.unified_msg_origin)
            assert set(subs) == {"UC1", "UC2", "UC3"}, subs
            # 落库必须真的写盘
            assert (tmp / "state.json").exists()
            # 一切就绪时不该出现降级/未生效提示
            assert "⚠️" not in text, text
    asyncio.run(_run())
    print("✅ test_batch_subscribe_flow")


def test_resubscribe_by_id_skips_api() -> None:
    """输入本身就是频道 ID 且已订阅时，不该再查一次 API。

    批量重发同一份 ID 列表是很常见的（「再加几个」时的顺手粘贴），
    每个都查一次就是白烧配额。
    """
    cid = "UC" + "a" * 22  # 真实频道 ID 形状：UC + 22 位

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            plugin = _make_plugin(Path(tmpdir), channels={cid.lower(): _meta(cid, "A")})
            first = _text_of((await _collect(plugin.batch_subscribe(FakeEvent(f"yt批量订阅 {cid}"))))[0])
            assert "新增 1 个" in first, first
            assert plugin.resolve_calls == [cid], plugin.resolve_calls

            plugin.resolve_calls.clear()
            again = _text_of((await _collect(plugin.batch_subscribe(FakeEvent(f"yt批量订阅 {cid}"))))[0])
            assert "已订阅 1 个" in again, again
            assert plugin.resolve_calls == [], (
                "已订阅的频道 ID 不该再查一次 API",
                plugin.resolve_calls,
            )
    asyncio.run(_run())
    print("✅ test_resubscribe_by_id_skips_api")


def test_batch_subscribe_warns_when_monitoring_dead() -> None:
    """订阅成功但监控废掉时必须明说（禁止静默降级），且只说一次。"""
    channels = {"a": _meta("UC1", "A"), "b": _meta("UC2", "B")}

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            plugin = _make_plugin(Path(tmpdir), ready=False, channels=channels)
            ev = FakeEvent("yt批量订阅 @a @b")
            text = _text_of((await _collect(plugin.batch_subscribe(ev)))[0])

            assert "监控尚未生效" in text, text
            assert text.count("监控尚未生效") == 1, text  # 两个频道只提示一次
    asyncio.run(_run())
    print("✅ test_batch_subscribe_warns_when_monitoring_dead")


def test_batch_subscribe_caps_targets() -> None:
    """超过上限的部分：处理前 N 个，并明确告知其余未处理。"""
    channels = {f"c{i}": _meta(f"UC{i}", f"C{i}") for i in range(m.BATCH_MAX_TARGETS + 5)}

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            plugin = _make_plugin(Path(tmpdir), channels=channels)
            targets = " ".join(f"@c{i}" for i in range(m.BATCH_MAX_TARGETS + 5))
            ev = FakeEvent(f"yt批量订阅 {targets}")
            text = _text_of((await _collect(plugin.batch_subscribe(ev)))[0])

            assert len(plugin.resolve_calls) == m.BATCH_MAX_TARGETS, len(plugin.resolve_calls)
            subs = plugin.store.get_session_channels(ev.unified_msg_origin)
            assert len(subs) == m.BATCH_MAX_TARGETS, len(subs)
            assert "5 个本次未处理" in text, text
            assert f"@c{m.BATCH_MAX_TARGETS}" in text, text
    asyncio.run(_run())
    print("✅ test_batch_subscribe_caps_targets")


def test_batch_unsubscribe_matches_locally_without_api() -> None:
    """批量取消必须靠本地匹配完成 —— 因此一次 API 都不该调用。

    三种写法都要能命中：@handle、频道 ID、频道名。
    """
    channels = {
        "a": _meta("UC1", "A"),
        "b": _meta("UC2", "B"),
        "c": _meta("UC3", "C"),
    }

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            plugin = _make_plugin(Path(tmpdir), channels=channels)
            ev = FakeEvent("yt批量订阅 @a @b @c")
            await _collect(plugin.batch_subscribe(ev))
            plugin.resolve_calls.clear()

            umo = ev.unified_msg_origin
            text = _text_of(
                (await _collect(
                    plugin.batch_unsubscribe(FakeEvent("yt批量取消订阅 @a UC2 C", umo))
                ))[0]
            )
            assert text.splitlines()[0] == "批量取消订阅完成：已取消 3 个 / 未订阅 0 个", text
            assert plugin.resolve_calls == [], (
                "批量取消订阅不该逐个查 API（会烧配额）",
                plugin.resolve_calls,
            )
            assert plugin.store.get_session_channels(umo) == {}, "订阅没删干净"
    asyncio.run(_run())
    print("✅ test_batch_unsubscribe_matches_locally_without_api")


def test_batch_unsubscribe_reports_missing() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="yt_batch_") as tmpdir:
            plugin = _make_plugin(Path(tmpdir), channels={})
            # store 里没有该频道；resolve 也会抛 ChannelNotFoundError
            text = _text_of(
                (await _collect(plugin.batch_unsubscribe(FakeEvent("yt批量取消订阅 @ghost"))))[0]
            )
            assert "批量取消订阅完成：已取消 0 个 / 未订阅 1 个" in text, text
            assert "❌ 未订阅: @ghost" in text, text
            # 本会话没有任何订阅 → 连解析都不该做（解析不了 ≠ 没订阅，
            # 但「没有订阅」是确定的：不值得为它花每个目标 1 单位配额）
            assert plugin.resolve_calls == [], plugin.resolve_calls
    asyncio.run(_run())
    print("✅ test_batch_unsubscribe_reports_missing")


def test_usage_texts_document_space_separator() -> None:
    """用法文案必须写清「空格分隔」与数量上限，否则用户不知道能一次发多个。"""
    for usage in (m._BATCH_SUBSCRIBE_USAGE, m._BATCH_UNSUBSCRIBE_USAGE):
        assert "空格分隔" in usage, usage
        assert str(m.BATCH_MAX_TARGETS) in usage, usage
    # 命令名/别名与参数解析用的名字集合必须一致，否则剥不掉命令名
    assert "yt批量订阅" in m._BATCH_SUBSCRIBE_NAMES
    assert "yt批量取消订阅" in m._BATCH_UNSUBSCRIBE_NAMES
    print("✅ test_usage_texts_document_space_separator")


def main() -> int:
    tests = [
        test_split_after_command_takes_all_tokens,
        test_split_after_command_never_guesses,
        test_dedup_keeps_case_sensitive_ids,
        test_channel_label_avoids_repetition,
        test_batch_lines_report_every_outcome,
        test_batch_lines_disclose_skipped_targets,
        test_batch_unsubscribe_lines,
        test_batch_subscribe_flow,
        test_resubscribe_by_id_skips_api,
        test_batch_subscribe_warns_when_monitoring_dead,
        test_batch_subscribe_caps_targets,
        test_batch_unsubscribe_matches_locally_without_api,
        test_batch_unsubscribe_reports_missing,
        test_usage_texts_document_space_separator,
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
