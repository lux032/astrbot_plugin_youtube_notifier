"""通知图片的定时清理。

为什么需要：每张通知图约 300–700KB（含封面），渲染后即完成使命，但文件名带
uuid 后缀、每次推送都新建一个，永远不会被复用或覆盖。按 5 分钟轮询 + 几个
频道估算，不清理的话磁盘只会单调增长直到写满。

清理策略（两条，同时生效）：
  1. **按年龄**：删除 mtime 早于 `retention_days` 的文件（默认 7 天）；
  2. **按总量**：若清理后仍超过 `max_total_mb`，从最旧的开始继续删到限额
     以内（默认 500MB，0 表示不限制）。年龄策略在「每天几条」时够用，
     但频道一多就会出现「一天就把盘写满」的情况，所以必须有硬上限兜底。

安全约束（都不可省略）：
  - 只处理**指定目录**里的**图片扩展名**，绝不递归删除未知文件；
  - **绝不删除太新的文件**（`min_keep_seconds`，默认 1 小时）——
    刚渲染好、可能还在排队发送的图片必须留着，否则发出去的是坏图；
  - 单个文件删除失败（Windows 上文件被占用很常见）只记日志跳过，
    不中断整轮清理，也绝不向上抛异常（否则会打死后台任务）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from astrbot.api import logger

# 只清理这些扩展名，别的文件一律不碰
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# 无论什么策略，都不删这么新的文件（秒）。刚渲染的图可能还在发送队列里。
MIN_KEEP_SECONDS = 3600.0


@dataclass
class CleanupStats:
    """一轮清理的结果。"""

    deleted: int = 0
    deleted_bytes: int = 0
    kept: int = 0
    kept_bytes: int = 0
    skipped_recent: int = 0
    failed: int = 0
    missing_dirs: list[str] = field(default_factory=list)

    @property
    def freed_mb(self) -> float:
        return self.deleted_bytes / (1024 * 1024)

    @property
    def kept_mb(self) -> float:
        return self.kept_bytes / (1024 * 1024)

    def summary(self) -> str:
        parts = [
            f"删除 {self.deleted} 个（{self.freed_mb:.1f}MB）",
            f"保留 {self.kept} 个（{self.kept_mb:.1f}MB）",
        ]
        if self.skipped_recent:
            parts.append(f"因过新跳过 {self.skipped_recent} 个")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        if self.missing_dirs:
            parts.append(f"目录不存在 {len(self.missing_dirs)} 个")
        return "，".join(parts)


class ImageCleaner:
    """按年龄 + 总量清理通知图片，并提供每日定时任务。"""

    def __init__(
        self,
        dirs: Iterable[Path],
        *,
        retention_days: int = 7,
        max_total_mb: int = 500,
        hour: int = 4,
        min_keep_seconds: float = MIN_KEEP_SECONDS,
        run_on_startup: bool = True,
    ):
        # 去重并规范化：同一目录传两次会导致统计与删除重复计算
        self.dirs = []
        seen: set[str] = set()
        for d in dirs:
            p = Path(d)
            key = str(p.resolve()) if p.exists() else str(p)
            if key not in seen:
                seen.add(key)
                self.dirs.append(p)

        self.retention_days = max(0, int(retention_days))
        # 0 或负数表示不限制总量
        self.max_total_bytes = max(0, int(max_total_mb)) * 1024 * 1024
        self.hour = min(23, max(0, int(hour)))
        self.min_keep_seconds = max(0.0, float(min_keep_seconds))
        self.run_on_startup = bool(run_on_startup)

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ 单轮清理

    def _collect(self) -> list[tuple[Path, float, int]]:
        """收集候选文件 → [(路径, mtime, 大小)]，按 mtime 升序（最旧在前）。"""
        found: list[tuple[Path, float, int]] = []
        for d in self.dirs:
            if not d.is_dir():
                continue
            try:
                entries = list(d.iterdir())
            except OSError as exc:
                logger.warning(f"[YT] 清理时无法读取目录 {d}: {exc!r}")
                continue
            for path in entries:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    stat = path.stat()
                except OSError as exc:
                    logger.debug(f"[YT] 跳过无法 stat 的文件 {path}: {exc!r}")
                    continue
                found.append((path, stat.st_mtime, stat.st_size))
        found.sort(key=lambda item: item[1])
        return found

    @staticmethod
    def _unlink(path: Path) -> bool:
        """删除单个文件；失败只记日志（Windows 上被占用很常见）。"""
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(f"[YT] 删除图片失败（已跳过）{path}: {exc!r}")
            return False

    @staticmethod
    def _is_disposable(mtime: float, now: float, min_keep_seconds: float) -> bool:
        """是否允许删除：太新的文件一律不删（可能还在发送队列里）。"""
        return (now - mtime) >= min_keep_seconds

    def cleanup_once(self, now: Optional[float] = None) -> CleanupStats:
        """执行一轮清理，返回统计。**不抛异常**。"""
        import time as _time

        stats = CleanupStats()
        now_ts = _time.time() if now is None else now

        for d in self.dirs:
            if not d.is_dir():
                stats.missing_dirs.append(str(d))

        candidates = self._collect()
        if not candidates:
            return stats

        # 两条策略都用 0 表示「关闭该策略」。注意 retention_days=0 的语义是
        # 「不按年龄删」，不是「全删」—— 删除是不可逆的，0 必须取保守解释。
        cutoff = (
            now_ts - self.retention_days * 86400
            if self.retention_days > 0
            else 0.0
        )

        survivors: list[tuple[Path, float, int]] = []
        for path, mtime, size in candidates:
            if not self._is_disposable(mtime, now_ts, self.min_keep_seconds):
                stats.skipped_recent += 1
                survivors.append((path, mtime, size))
                continue
            keep_by_age = self.retention_days <= 0 or mtime >= cutoff
            if keep_by_age:
                survivors.append((path, mtime, size))
                continue
            if self._unlink(path):
                stats.deleted += 1
                stats.deleted_bytes += size
            else:
                stats.failed += 1
                survivors.append((path, mtime, size))

        # 总量兜底：从最旧的开始继续删（survivors 保持 mtime 升序）
        if self.max_total_bytes:
            total = sum(size for _p, _m, size in survivors)
            still_kept: list[tuple[Path, float, int]] = []
            for path, mtime, size in survivors:
                too_recent = not self._is_disposable(
                    mtime, now_ts, self.min_keep_seconds
                )
                if total > self.max_total_bytes and not too_recent:
                    if self._unlink(path):
                        total -= size
                        stats.deleted += 1
                        stats.deleted_bytes += size
                        continue
                    stats.failed += 1
                still_kept.append((path, mtime, size))
            survivors = still_kept

        stats.kept = len(survivors)
        stats.kept_bytes = sum(size for _p, _m, size in survivors)
        return stats

    # ------------------------------------------------------------ 定时任务

    def seconds_until_next(self, now: Optional[datetime] = None) -> float:
        """距离下一个 hour:00 的秒数（本地时间）。

        每次循环都重新计算，因此机器休眠/时区变化后不会算错，
        也不会像「固定 sleep 24h」那样逐渐漂移。
        """
        current = now or datetime.now()
        target = current.replace(
            hour=self.hour, minute=0, second=0, microsecond=0
        )
        if target <= current:
            target += timedelta(days=1)
        return (target - current).total_seconds()

    def start(self) -> None:
        """启动每日清理任务（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[YT] 图片清理已启动：每天 {self.hour:02d}:00 执行，"
            f"保留 {self.retention_days} 天"
            + (
                f"，总量上限 {self.max_total_bytes // (1024 * 1024)}MB"
                if self.max_total_bytes
                else ""
            )
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            # 先跑一次：bot 若每天重启，永远不会正好赶上定时点，
            # 只靠「每天 hour:00」会让旧图无限堆积
            if self.run_on_startup:
                await asyncio.sleep(30)  # 避开启动高峰
                self._run_and_log("启动")

            while self._running:
                delay = self.seconds_until_next()
                logger.debug(f"[YT] 下次图片清理在 {delay / 3600:.1f} 小时后")
                await asyncio.sleep(delay)
                if not self._running:
                    return
                self._run_and_log("每日")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能因异常退出
            logger.error(f"[YT] 图片清理任务异常退出: {exc!r}")

    def _run_and_log(self, reason: str) -> None:
        try:
            stats = self.cleanup_once()
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响插件
            logger.error(f"[YT] {reason}图片清理失败: {exc!r}")
            return
        if stats.deleted or stats.failed or stats.missing_dirs:
            logger.info(f"[YT] {reason}图片清理完成: {stats.summary()}")
        else:
            logger.info(f"[YT] {reason}图片清理完成: 无需删除（{stats.summary()}）")
