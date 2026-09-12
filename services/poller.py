"""后台轮询调度：asyncio 循环遍历所有订阅频道，调用 notifier 检查。

- 默认 60s 一轮；多频道之间交错错峰（短 sleep），避免瞬时并发。
- 单频道异常不影响整轮轮询。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from astrbot.api import logger


class PollScheduler:
    def __init__(
        self,
        store,
        notifier,
        interval_seconds: int = 60,
        stagger_max: float = 1.0,
    ):
        self.store = store
        self.notifier = notifier
        self.interval = max(10, int(interval_seconds))
        self.stagger_max = stagger_max
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动后台轮询（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[YT] 轮询已启动，间隔 {self.interval}s")

    async def stop(self) -> None:
        """停止轮询。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[YT] 轮询已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[YT] 轮询异常: {exc!r}")
            await asyncio.sleep(self.interval)

    async def _poll_once(self) -> None:
        channel_ids = self.store.all_channel_ids()
        if not channel_ids:
            return
        logger.debug(f"[YT] 开始轮询 {len(channel_ids)} 个频道")
        for idx, channel_id in enumerate(channel_ids):
            if not self._running:
                return
            try:
                await self.notifier.check_channel(channel_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[YT] 频道 {channel_id} 检查失败，跳过: {exc!r}"
                )
            # 频道间错峰
            if idx < len(channel_ids) - 1:
                await asyncio.sleep(min(self.stagger_max, self.interval / max(len(channel_ids), 1)))
