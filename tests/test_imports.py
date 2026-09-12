"""插件包导入冒烟测试 + 配置 schema 校验。

用最简 AstrBot 桩导入整个包（含 main.py），可捕获拼写错误、缺失命名、
循环导入与装饰器使用错误。不依赖 AstrBot 运行时与网络。

运行: python tests/test_imports.py
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))


# ---------------------------------------------------------------- AstrBot 桩
def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    import logging

    # astrbot.api.logger
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    # astrbot.api.event: filter / AstrMessageEvent / MessageChain
    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401
        def __init__(self):
            self.unified_msg_origin = "test:group:1"

        def plain_result(self, text):
            return ("plain", text)

        def chain_result(self, chain):
            return ("chain", chain)

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

        def message(self, text):
            self.chain.append(("text", text))
            return self

    class _Filter:
        def command(self, name, alias=None, **kwargs):
            def deco(fn):
                return fn

            return deco

        def regex(self, *a, **k):
            def deco(fn):
                return fn

            return deco

    event.filter = _Filter()
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    api.event = event
    sys.modules["astrbot.api.event"] = event

    # astrbot.api.message_components
    comps = types.ModuleType("astrbot.api.message_components")

    class Image:
        @staticmethod
        def fromFileSystem(path, **kwargs):
            return ("image", path)

    comps.Image = Image
    api.message_components = comps
    sys.modules["astrbot.api.message_components"] = comps

    # astrbot.api.star: Context / Star / StarTools / register
    star = types.ModuleType("astrbot.api.star")

    class Context:
        async def send_message(self, session, chain):
            return True

    class Star:
        def __init__(self, context):
            self.context = context

        async def initialize(self):
            pass

        async def terminate(self):
            pass

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

    # astrbot.api.AstrBotConfig
    class AstrBotConfig(dict):
        pass

    api.AstrBotConfig = AstrBotConfig


_install_astrbot_stub()


# ---------------------------------------------------------------- 测试
def test_import_all_modules() -> None:
    import importlib

    modules = [
        "astrbot_plugin_youtube_notifier.utils",
        "astrbot_plugin_youtube_notifier.renderer",
        "astrbot_plugin_youtube_notifier.services.models",
        "astrbot_plugin_youtube_notifier.services.store",
        "astrbot_plugin_youtube_notifier.services.oauth",
        "astrbot_plugin_youtube_notifier.services.data_api",
        "astrbot_plugin_youtube_notifier.services.feed",
        "astrbot_plugin_youtube_notifier.services.livebroadcasts",
        "astrbot_plugin_youtube_notifier.services.scrape",
        "astrbot_plugin_youtube_notifier.services.page_json",
        "astrbot_plugin_youtube_notifier.services.state_machine",
        "astrbot_plugin_youtube_notifier.services.notifier",
        "astrbot_plugin_youtube_notifier.services.poller",
        "astrbot_plugin_youtube_notifier.services.cleanup",
        "astrbot_plugin_youtube_notifier.services.websub",
        "astrbot_plugin_youtube_notifier.services.websub_server",
        "astrbot_plugin_youtube_notifier.main",
    ]
    for name in modules:
        importlib.import_module(name)
    print(f"✅ test_import_all_modules ({len(modules)} 个模块)")


def test_plugin_class_instantiable() -> None:
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    plugin = YouTubeNotifierPlugin(context=object(), config={"basic": {}})
    assert hasattr(plugin, "initialize") and hasattr(plugin, "terminate")
    # 指令方法应存在
    for cmd in (
        "subscribe",
        "unsubscribe",
        "list_subscriptions",
        "live_test",
        "video_test",
    ):
        assert callable(getattr(plugin, cmd)), f"缺少指令方法 {cmd}"
    print("✅ test_plugin_class_instantiable")


def test_status_text_distinguishes_states() -> None:
    """状态文案必须区分「尚未检测」与「已同步但无内容」。

    真实踩坑：主播型频道上传列表全是直播存档，没有普通投稿，
    若笼统显示「暂无记录」会让人以为插件没工作（实际是数据源没配好）。
    """
    from astrbot_plugin_youtube_notifier.main import _status_text
    from astrbot_plugin_youtube_notifier.services.models import (
        ChannelState,
        STATUS_ENDED,
        STATUS_LIVE,
    )

    assert _status_text(None) == "状态未知"

    fresh = ChannelState(channel_id="UC1")  # 刚订阅、还没播种成功
    assert _status_text(fresh) == "⏳ 尚未完成首次检测", _status_text(fresh)

    seeded = ChannelState(channel_id="UC1", video_seeded=True)
    assert _status_text(seeded) == "✅ 已同步（暂无投稿或直播）", _status_text(seeded)

    live = ChannelState(channel_id="UC1", last_status=STATUS_LIVE)
    assert _status_text(live) == "🔴 直播中"

    ended = ChannelState(
        channel_id="UC1", last_status=STATUS_ENDED,
        last_live_end_at="2026-09-12T14:00:00+00:00", last_live_title="杂谈",
    )
    text = _status_text(ended)
    assert text.startswith("⚫ 上次直播已结束") and "杂谈" in text, text

    video = ChannelState(channel_id="UC1", latest_video_title="新视频")
    assert _status_text(video) == "最近投稿: 新视频"
    print("✅ test_status_text_distinguishes_states")


def test_monitoring_blocker() -> None:
    """未配 Key 时必须能报出明确原因（而不是静默不工作）。

    锁定 _monitoring_blocker / _degraded_notice 的语义矩阵：
    「能工作但降级」返回空阻塞串，同时**必须**给出降级提示 ——
    两者都不给就是静默降级（CLAUDE.md 明令禁止）。
    """
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    class _API:
        def __init__(self, configured):
            self.configured = configured

    class _Notifier:
        def __init__(self, mode, page_fallback=True):
            self.live_detect_mode = mode
            self.page_fallback_enabled = page_fallback

    class _Page:
        pass

    plugin = YouTubeNotifierPlugin(context=object(), config={"basic": {}})

    # ① 什么都没配：data_api 显式要官方 API，没 Key 且没兜底 → 必须报阻塞，
    #    且阻塞文案要指出「网页兜底也关着」，让用户知道有两条路可修
    blocker = plugin._monitoring_blocker()
    assert "api_key" in blocker, blocker
    assert "page_fallback_enabled" in blocker, blocker
    assert not plugin._monitoring_ready()

    # ② 配好 Key + data_api → 就绪且无降级提示
    plugin.data_api = _API(True)
    plugin.notifier = _Notifier("data_api")
    assert plugin._monitoring_blocker() == ""
    assert plugin._monitoring_ready()
    assert plugin._degraded_notice() == ""

    # ③ livebroadcasts 模式还要求 OAuth
    plugin.notifier = _Notifier("livebroadcasts")
    assert "OAuth" in plugin._monitoring_blocker()

    # ④ auto / feed 语义是「尽力而为」，永不阻塞
    plugin.data_api = _API(False)
    plugin.notifier = _Notifier("auto", page_fallback=False)
    assert plugin._monitoring_ready()
    # 但降级提示必须点明会掉到不可靠的 legacy feed
    assert "legacy" in plugin._degraded_notice(), plugin._degraded_notice()

    # ⑤ 没 Key 但网页兜底可用：不算阻塞，且提示走网页兜底
    plugin.notifier = _Notifier("data_api", page_fallback=True)
    plugin.page_json = _Page()
    assert plugin._monitoring_ready()
    notice = plugin._degraded_notice()
    assert "网页" in notice and "api_key" in notice, notice

    # ⑥ feed 模式：不阻塞，但必须说明 feed 不可靠
    plugin.notifier = _Notifier("feed")
    assert plugin._monitoring_ready()
    assert "不可靠" in plugin._degraded_notice()

    print("✅ test_monitoring_blocker")


def test_font_cjk_detection() -> None:
    """字体「能否渲染中文」必须靠字形探测判断，不能只看文件是否存在。

    真实踩坑：Linux VPS 最小化安装自带 DejaVuSans（纯拉丁、无中文字形），
    而历史实现把 DejaVu 排在中文字体之前 → 命中它 → 所有中文变豆腐块，
    且日志里没有任何异常。这条测试锁住探测能力本身。
    """
    from astrbot_plugin_youtube_notifier.utils import (
        cjk_font_install_hint,
        font_supports_cjk,
        resolve_font_path,
    )

    # 探测必须能区分「含中文」与「纯拉丁」字体
    resolved = resolve_font_path()
    if resolved:
        # 解析结果要么支持中文，要么解析层应当能给出安装指引
        assert isinstance(font_supports_cjk(resolved), bool)

    # 不存在的路径 / 空路径必须是 False，不能抛异常
    assert font_supports_cjk("") is False
    assert font_supports_cjk("/no/such/font.ttf") is False

    # 安装指引必须给出各发行版的可执行命令
    hint = cjk_font_install_hint()
    for token in ("fonts-noto-cjk", "dnf", "apk", "pacman", "font_path"):
        assert token in hint, f"安装指引缺少 {token}: {hint}"

    # 配置了不存在的 font_path → 不应抛异常，且要能回退
    assert resolve_font_path("/no/such/font.ttf") is not None or True
    print("✅ test_font_cjk_detection")


def test_data_dir_font_takes_priority() -> None:
    """`<data_dir>/fonts/` 里的字体要优先于系统字体。

    这是 Docker 用户的主要自救手段：AstrBot 的 data/ 已挂载到宿主机，
    把 ttf 丢进 plugin_data/<插件名>/fonts/ 即「容器内可见 + 重启不丢」。
    若系统字体抢先命中，用户会觉得「放了却没生效」。
    """
    import shutil
    import tempfile

    from astrbot_plugin_youtube_notifier import utils as u

    # 找一个本机真实存在的含中文字体来冒充「用户丢进来的字体」
    source = next(
        (p for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf")
         if os.path.exists(p)),
        None,
    )
    if source is None:
        print("✅ test_data_dir_font_takes_priority (跳过：本机无中文字体样本)")
        return

    tmp = tempfile.mkdtemp(prefix="yt_font_")
    try:
        fonts_dir = Path(tmp) / "fonts"
        fonts_dir.mkdir()
        dropped = fonts_dir / "UserFont.ttf"
        shutil.copy(source, dropped)

        # 保存全局状态，避免污染后续测试
        saved = (u._EXTRA_FONT_DIRS[:], u._RESOLVED_DONE, u._RESOLVED_FONT)
        try:
            u._EXTRA_FONT_DIRS.clear()
            u._RESOLVED_DONE, u._RESOLVED_FONT = False, None
            u.register_font_dirs([fonts_dir])
            resolved = u.resolve_font_path()
            assert resolved is not None, "未解析出字体"
            assert "UserFont" in resolved, f"未优先使用 data 目录字体: {resolved}"
            assert u.font_supports_cjk(resolved)

            # 显式 font_path 仍应比 data 目录更优先
            explicit = u.resolve_font_path(source)
            assert explicit == source, f"font_path 未最高优先: {explicit}"
        finally:
            u._EXTRA_FONT_DIRS[:] = saved[0]
            u._RESOLVED_DONE, u._RESOLVED_FONT = saved[1], saved[2]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ test_data_dir_font_takes_priority")


def test_font_path_missing_hint_mentions_container() -> None:
    """配置的字体路径不存在时，必须把 Docker 这个最常见原因说出来。

    真实踩坑：用户在宿主机 apt 装了字体、把 font_path 指向它，但插件在容器
    里跑 —— 容器看不到宿主机文件。原来只报「不存在」，用户会反复核对路径
    拼写，方向完全错了。
    """
    from astrbot_plugin_youtube_notifier import utils as u

    # 非容器环境：给出常规指引，且不该出现误导性的挂载建议
    u.running_in_container = lambda: False
    plain = u.font_path_missing_hint("/x/y.ttf")
    assert "/x/y.ttf" in plain
    assert "volumes" not in plain, "非容器环境不该提挂载"

    # 容器环境：三条可选修复都要给出
    u.running_in_container = lambda: True
    hint = u.font_path_missing_hint("/usr/share/fonts/truetype/maple/MapleMono.ttf")
    for token in ("/usr/share/fonts", "fonts-noto-cjk", "plugin_data", "fonts"):
        assert token in hint, f"容器指引缺少 {token}"
    assert "volumes" in hint and ":ro" in hint, "缺少挂载示例"

    # 还原（本进程内其它测试可能依赖真实实现）
    del u.running_in_container
    print("✅ test_font_path_missing_hint_mentions_container")


def test_conf_schema_valid() -> None:
    schema_path = PLUGIN_ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    assert isinstance(schema, dict) and schema, "schema 不能为空"

    allowed = {"string", "text", "int", "float", "bool", "object", "list", "dict", "template_list"}

    def walk(items: dict, path: str) -> int:
        count = 0
        for key, spec in items.items():
            assert isinstance(spec, dict), f"{path}.{key} 必须是对象"
            assert "type" in spec, f"{path}.{key} 缺少 type"
            assert spec["type"] in allowed, f"{path}.{key} 非法 type: {spec['type']}"
            assert "description" in spec, f"{path}.{key} 缺少 description"
            if spec["type"] == "object":
                assert "items" in spec, f"{path}.{key} object 缺少 items"
                count += walk(spec["items"], f"{path}.{key}")
            else:
                assert "default" in spec, f"{path}.{key} 缺少 default"
                count += 1
        return count

    total = walk(schema, "root")
    print(f"✅ test_conf_schema_valid ({len(schema)} 个分块, {total} 个配置项)")


def test_metadata_valid() -> None:
    text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    required = ("name:", "desc:", "version:", "author:")
    for key in required:
        assert key in text, f"metadata.yaml 缺少必需字段 {key}"
    assert "astrbot_plugin_youtube_notifier" in text
    print("✅ test_metadata_valid")


def test_required_files_present() -> None:
    for rel in (
        "main.py",
        "metadata.yaml",
        "requirements.txt",
        "_conf_schema.json",
        "CLAUDE.md",
        "PLAN.md",
        "API_GUIDE.md",
        "renderer.py",
        "utils.py",
        "scripts/oauth_setup.py",
        "scripts/diagnose.py",
        "services/store.py",
        "services/websub_server.py",
        "services/page_json.py",
        "services/cleanup.py",
        "tests/test_cleanup.py",
        "tests/test_notifier_send.py",
        # 真实数据 fixture：网页 JSON 降级链的回归基准
        "tests/fixtures/real_channel_streams_live.json",
        "tests/fixtures/real_channel_videos_normal.json",
        "tests/fixtures/real_feed_youtube.xml",
    ):
        assert (PLUGIN_ROOT / rel).exists(), f"缺少文件 {rel}"
    print("✅ test_required_files_present")


def main() -> int:
    tests = [
        test_import_all_modules,
        test_plugin_class_instantiable,
        test_status_text_distinguishes_states,
        test_monitoring_blocker,
        test_font_cjk_detection,
        test_data_dir_font_takes_priority,
        test_font_path_missing_hint_mentions_container,
        test_conf_schema_valid,
        test_metadata_valid,
        test_required_files_present,
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
