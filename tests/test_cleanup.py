"""图片清理单测：年龄策略、总量兜底、以及「绝不能误删」的安全约束。

删除是不可逆的，所以这里重点覆盖「不该删的不能删」：
刚渲染好可能还在发送队列里的图、非图片文件、目录外的文件。

运行: python tests/test_cleanup.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _install_stub() -> None:
    """注入完整 astrbot 桩（装配测试要 import main.py，需要 star/filter 等）。

    自建而不复用 test_imports 的：后者见 sys.modules 里已有 "astrbot" 会直接
    返回，导致 main.py 需要的 AstrBotConfig / astrbot.api.star 缺失。
    """
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

from astrbot_plugin_youtube_notifier.services.cleanup import (  # noqa: E402
    ImageCleaner,
)

NOW = time.time()
DAY = 86400.0


class TempImages:
    """临时图片目录，可精确控制每个文件的「年龄」。"""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="yt_cleanup_")
        self.root = Path(self._tmp.name)

    def make(
        self, name: str, age_days: float, size_kb: int = 1, sub: str = "notifications"
    ) -> Path:
        d = self.root / sub
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_bytes(b"x" * (size_kb * 1024))
        mtime = NOW - age_days * DAY
        os.utime(path, (mtime, mtime))
        return path

    def path(self, name: str, sub: str = "notifications") -> Path:
        return self.root / sub / name

    def cleanup(self):
        self._tmp.cleanup()


def _cleaner(root: Path, **kwargs) -> ImageCleaner:
    kwargs.setdefault("min_keep_seconds", 0.0)  # 测试里关掉「太新不删」
    return ImageCleaner([root / "notifications", root / "covers"], **kwargs)


# ---------------------------------------------------------------- 年龄策略


def test_deletes_older_than_retention() -> None:
    t = TempImages()
    try:
        old = t.make("old.png", age_days=10)
        edge = t.make("edge.png", age_days=8)
        fresh = t.make("fresh.png", age_days=1)
        stats = _cleaner(t.root, retention_days=7).cleanup_once(now=NOW)
        assert not old.exists(), "超过 7 天的应被删除"
        assert not edge.exists(), "8 天前应被删除"
        assert fresh.exists(), "1 天前的不该被删除"
        assert stats.deleted == 2 and stats.kept == 1, stats
        assert stats.deleted_bytes == 2048
    finally:
        t.cleanup()
    print("✅ test_deletes_older_than_retention")


def test_retention_zero_means_disabled_not_delete_all() -> None:
    """retention_days=0 必须解释为「不按年龄删」，而不是「全删」。

    删除不可逆，0 必须取保守解释 —— 否则一个手滑的配置就清空了所有图片。
    """
    t = TempImages()
    try:
        ancient = t.make("ancient.png", age_days=999)
        stats = _cleaner(t.root, retention_days=0).cleanup_once(now=NOW)
        assert ancient.exists(), "retention_days=0 不该删除任何文件"
        assert stats.deleted == 0 and stats.kept == 1
    finally:
        t.cleanup()
    print("✅ test_retention_zero_means_disabled_not_delete_all")


# ---------------------------------------------------------------- 安全约束


def test_never_deletes_recent_files() -> None:
    """刚渲染的图可能还在发送队列里，绝不能被删（否则发出的是坏图）。"""
    t = TempImages()
    try:
        # 很旧的文件 + 关闭年龄策略，强制让总量兜底去删
        just_now = t.make("just_now.png", age_days=0.0, size_kb=50)
        # 手工把 mtime 设为「30 分钟前」—— 小于默认 1 小时窗口
        mtime = NOW - 1800
        os.utime(just_now, (mtime, mtime))

        stats = _cleaner(
            t.root, retention_days=0, max_total_mb=0,
            min_keep_seconds=3600.0,
        ).cleanup_once(now=NOW)
        assert just_now.exists(), "1 小时内的文件绝不能被删"
        assert stats.skipped_recent == 1, stats
        assert stats.deleted == 0

        # 总量兜底也必须尊重这个保护：给一个必然超限的上限
        c2 = _cleaner(
            t.root, retention_days=0, max_total_mb=0,
            min_keep_seconds=3600.0,
        )
        c2.max_total_bytes = 1  # 1 字节上限 → 理论上要把所有东西删光
        stats2 = c2.cleanup_once(now=NOW)
        assert just_now.exists(), "总量兜底越限也不许删太新的文件"
        assert stats2.deleted == 0
    finally:
        t.cleanup()
    print("✅ test_never_deletes_recent_files")


def test_ignores_non_image_files() -> None:
    """只删图片；同目录下的其它文件一律不碰（state.json 等）。"""
    t = TempImages()
    try:
        img = t.make("old.png", age_days=30)
        keep_json = t.make("state.json", age_days=30)
        keep_txt = t.make("notes.txt", age_days=30)
        no_ext = t.make("README", age_days=30)
        stats = _cleaner(t.root, retention_days=7).cleanup_once(now=NOW)
        assert not img.exists()
        for p in (keep_json, keep_txt, no_ext):
            assert p.exists(), f"非图片文件被误删: {p.name}"
        assert stats.deleted == 1
    finally:
        t.cleanup()
    print("✅ test_ignores_non_image_files")


def test_covers_dir_also_cleaned() -> None:
    """封面目录（scratch）同样要清，否则它也是单调增长。"""
    t = TempImages()
    try:
        cover = t.make("vid.jpg", age_days=30, sub="covers")
        stats = _cleaner(t.root, retention_days=7).cleanup_once(now=NOW)
        assert not cover.exists(), "covers 里的旧封面应被删除"
        assert stats.deleted == 1
    finally:
        t.cleanup()
    print("✅ test_covers_dir_also_cleaned")


def test_missing_dirs_are_tolerated() -> None:
    """目录不存在（还没渲染过）不能报错。"""
    t = TempImages()
    try:
        cleaner = ImageCleaner([t.root / "nope", t.root / "also_nope"])
        stats = cleaner.cleanup_once(now=NOW)
        assert stats.deleted == 0 and stats.kept == 0
        assert len(stats.missing_dirs) == 2
    finally:
        t.cleanup()
    print("✅ test_missing_dirs_are_tolerated")


# ---------------------------------------------------------------- 总量兜底


def test_size_cap_deletes_oldest_first() -> None:
    """按总量兜底时从最旧的开始删，且删到限额以内就停。"""
    t = TempImages()
    try:
        # 5 个文件各 100KB = 500KB；上限设为 250KB → 只需删掉最旧的 3 个
        paths = [t.make(f"f{i}.png", age_days=10 - i, size_kb=100) for i in range(5)]
        cleaner = _cleaner(t.root, retention_days=0, max_total_mb=0)
        cleaner.max_total_bytes = 250 * 1024
        stats = cleaner.cleanup_once(now=NOW)

        assert not paths[0].exists() and not paths[1].exists(), "最旧的应先被删"
        assert paths[4].exists(), "最新的必须保留"
        assert stats.kept_bytes <= 250 * 1024, f"仍超限: {stats.kept_bytes}"
        assert stats.deleted == 3, f"应删 3 个，实际 {stats.deleted}"
    finally:
        t.cleanup()
    print("✅ test_size_cap_deletes_oldest_first")


def test_size_cap_disabled_when_zero() -> None:
    t = TempImages()
    try:
        keeper = t.make("old_but_kept.png", age_days=999, size_kb=100)
        stats = _cleaner(t.root, retention_days=0, max_total_mb=0).cleanup_once(now=NOW)
        assert keeper.exists()
        assert stats.deleted == 0
    finally:
        t.cleanup()
    print("✅ test_size_cap_disabled_when_zero")


# ---------------------------------------------------------------- 调度


def test_seconds_until_next() -> None:
    """定时点计算：当天未到 → 今天；已过 → 明天。跨月也要对。"""
    cleaner = ImageCleaner([], hour=4)

    now = datetime(2026, 9, 12, 1, 30, 0)
    delta = cleaner.seconds_until_next(now)
    assert abs(delta - 2.5 * 3600) < 1, f"1:30 → 4:00 应为 2.5h，实际 {delta}"

    # 正好在定时点（含秒）→ 顺延到明天，避免同一天重复跑
    now = datetime(2026, 9, 12, 4, 0, 0)
    assert abs(cleaner.seconds_until_next(now) - 24 * 3600) < 1

    now = datetime(2026, 9, 12, 4, 0, 1)
    assert abs(cleaner.seconds_until_next(now) - (24 * 3600 - 1)) < 1

    # 已过 → 明天
    now = datetime(2026, 9, 12, 23, 59, 0)
    assert abs(cleaner.seconds_until_next(now) - 4 * 3600 - 60) < 1

    # 跨月
    now = datetime(2026, 9, 30, 23, 0, 0)
    nxt = now + timedelta(seconds=cleaner.seconds_until_next(now))
    assert (nxt.day, nxt.hour, nxt.minute) == (1, 4, 0), nxt

    # 越界的 hour 会被夹到 0-23
    assert ImageCleaner([], hour=99).hour == 23
    assert ImageCleaner([], hour=-5).hour == 0
    print("✅ test_seconds_until_next")


def test_dirs_deduplicated() -> None:
    """同一目录传两次不能导致重复统计（否则 kept_bytes 会翻倍）。"""
    t = TempImages()
    try:
        t.make("a.png", age_days=1)
        dup = t.root / "notifications"
        cleaner = ImageCleaner([dup, dup, Path(str(dup))], min_keep_seconds=0.0)
        assert len(cleaner.dirs) == 1, cleaner.dirs
        stats = cleaner.cleanup_once(now=NOW)
        assert stats.kept == 1, f"重复目录导致重复计数: {stats}"
    finally:
        t.cleanup()
    print("✅ test_dirs_deduplicated")


def test_start_stop_task_lifecycle() -> None:
    """后台任务要能幂等启动、干净停止（不能泄漏 task）。"""

    async def _run() -> None:
        t = TempImages()
        try:
            cleaner = _cleaner(t.root, retention_days=7, run_on_startup=False)
            cleaner.start()
            cleaner.start()  # 幂等
            await asyncio.sleep(0.05)
            assert cleaner._task is not None and not cleaner._task.done()
            await cleaner.stop()
            assert cleaner._task is None
            # 停止后不应再跑
            assert not cleaner._running
        finally:
            t.cleanup()

    asyncio.run(_run())
    print("✅ test_start_stop_task_lifecycle")


def test_plugin_wires_cleaner() -> None:
    """插件必须真的把清理任务装配起来并启动 —— 否则功能等于不存在。"""
    import astrbot.api.star as star
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    class Ctx:
        async def send_message(self, session, chain):
            return True

    star.Context = Ctx

    async def _run() -> None:
        plugin = YouTubeNotifierPlugin(context=Ctx(), config={
            "basic": {"proxy": "", "api_key": ""},
            "cleanup": {"enabled": True, "retention_days": 3,
                        "max_total_mb": 123, "hour": 5,
                        "run_on_startup": False},
            "websub": {"enabled": False},
        })
        await plugin.initialize()
        try:
            assert plugin.cleaner is not None, "未创建 cleaner"
            assert plugin.cleaner.retention_days == 3
            assert plugin.cleaner.max_total_bytes == 123 * 1024 * 1024
            assert plugin.cleaner.hour == 5
            # 清理目录必须指向插件自己的 data 目录，不能是别处
            names = [str(p) for p in plugin.cleaner.dirs]
            assert any("notifications" in n for n in names), names
            assert any("covers" in n for n in names), names
            # 已启动
            assert plugin.cleaner._task is not None
            assert not plugin.cleaner._task.done()
        finally:
            await plugin.terminate()
        # 停止后任务必须干净退出
        assert plugin.cleaner._task is None

    asyncio.run(_run())

    # 关闭开关时不许启动任务
    async def _run_disabled() -> None:
        plugin = YouTubeNotifierPlugin(context=Ctx(), config={
            "basic": {"proxy": "", "api_key": ""},
            "cleanup": {"enabled": False},
            "websub": {"enabled": False},
        })
        await plugin.initialize()
        try:
            assert plugin.cleaner is not None
            assert plugin.cleaner._task is None, "enabled=false 却启动了任务"
        finally:
            await plugin.terminate()

    asyncio.run(_run_disabled())
    print("✅ test_plugin_wires_cleaner")


def test_zero_config_values_are_respected() -> None:
    """配置里填 0 必须原样生效，不能被 `x or default` 吞掉。

    实测踩坑：`int(cfg.get("retention_days", 7) or 7)` 让 0 变成 7 ——
    文档写着「填 0 表示不按天数删除」，实际却是按 7 天删，用户无法察觉。
    hour=0（午夜）同理会被替换成 4 点。
    """
    import astrbot.api.star as star
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    class Ctx:
        async def send_message(self, session, chain):
            return True

    star.Context = Ctx

    async def _run() -> None:
        plugin = YouTubeNotifierPlugin(context=Ctx(), config={
            "basic": {"proxy": "", "api_key": ""},
            "cleanup": {"enabled": True, "retention_days": 0,
                        "max_total_mb": 0, "hour": 0, "run_on_startup": False},
            "websub": {"enabled": False},
        })
        await plugin.initialize()
        try:
            c = plugin.cleaner
            assert c.retention_days == 0, f"retention_days 被吞成 {c.retention_days}"
            assert c.max_total_bytes == 0, f"max_total_mb 被吞成 {c.max_total_bytes}"
            assert c.hour == 0, f"hour 被吞成 {c.hour}"
        finally:
            await plugin.terminate()

    asyncio.run(_run())

    # 缺省 / 非法值仍要正常回退
    from astrbot_plugin_youtube_notifier.main import _cfg_int

    assert _cfg_int({}, "retention_days", 7) == 7
    assert _cfg_int({"retention_days": None}, "retention_days", 7) == 7
    assert _cfg_int({"retention_days": ""}, "retention_days", 7) == 7
    assert _cfg_int({"retention_days": "3"}, "retention_days", 7) == 3
    assert _cfg_int({"retention_days": 0}, "retention_days", 7) == 0
    assert _cfg_int({"retention_days": "abc"}, "retention_days", 7) == 7
    print("✅ test_zero_config_values_are_respected")


def main() -> int:
    tests = [
        test_deletes_older_than_retention,
        test_retention_zero_means_disabled_not_delete_all,
        test_never_deletes_recent_files,
        test_ignores_non_image_files,
        test_covers_dir_also_cleaned,
        test_missing_dirs_are_tolerated,
        test_size_cap_deletes_oldest_first,
        test_size_cap_disabled_when_zero,
        test_seconds_until_next,
        test_dirs_deduplicated,
        test_start_stop_task_lifecycle,
        test_plugin_wires_cleaner,
        test_zero_config_values_are_respected,
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
