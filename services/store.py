"""订阅存储：会话隔离 + 频道全局状态 + JSON 持久化。

结构：
    subscriptions: { session_id: { channel_id: {"channel_name": str} } }
    channels:      { channel_id: ChannelState }
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from astrbot.api import logger

from .models import ChannelState

SUBSCRIPTIONS_KEY = "subscriptions"
CHANNELS_KEY = "channels"


class SubscriptionStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_file = data_dir / "state.json"
        self._lock = asyncio.Lock()
        self.subscriptions: dict[str, dict[str, dict]] = {}
        self.channels: dict[str, ChannelState] = {}

    # ------------------------------------------------------------ 持久化

    def load(self) -> None:
        """从磁盘载入状态（启动时调用）。文件不存在则保持空。"""
        try:
            if not self.state_file.exists():
                return
            with self.state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[YT] 状态文件解析失败，已忽略: {exc}")
            return

        subs = data.get(SUBSCRIPTIONS_KEY) or {}
        if isinstance(subs, dict):
            for sid, session_channels in subs.items():
                if not isinstance(session_channels, dict):
                    continue
                self.subscriptions[str(sid)] = {
                    str(cid): (meta if isinstance(meta, dict) else {"channel_name": ""})
                    for cid, meta in session_channels.items()
                }
        chans = data.get(CHANNELS_KEY) or {}
        if isinstance(chans, dict):
            for cid, raw in chans.items():
                self.channels[str(cid)] = ChannelState.from_dict(
                    raw if isinstance(raw, dict) else {}
                )
        logger.info(
            f"[YT] 已载入状态: {len(self.subscriptions)} 个会话, "
            f"{len(self.channels)} 个频道"
        )

    async def save(self) -> None:
        """异步原子写盘。"""
        async with self._lock:
            payload = {
                SUBSCRIPTIONS_KEY: self.subscriptions,
                CHANNELS_KEY: {cid: st.to_dict() for cid, st in self.channels.items()},
            }
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.data_dir), prefix="state.", suffix=".json"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self.state_file)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise
            except OSError as exc:
                logger.warning(f"[YT] 状态写盘失败: {exc}")

    # ------------------------------------------------------------ 订阅操作

    def add_subscription(self, session_id: str, channel_id: str, channel_name: str = "") -> bool:
        """为会话添加频道订阅。返回 True 表示该会话之前未订阅此频道。"""
        sid = str(session_id)
        cid = str(channel_id)
        channels = self.subscriptions.setdefault(sid, {})
        if cid in channels:
            return False
        channels[cid] = {"channel_name": channel_name}
        state = self._ensure_channel(cid)
        if channel_name:
            state.channel_name = channel_name
        return True

    def remove_subscription(self, session_id: str, channel_id: str) -> bool:
        """移除会话对频道的订阅。返回 True 表示确实移除了。"""
        sid = str(session_id)
        cid = str(channel_id)
        channels = self.subscriptions.get(sid)
        if not channels or cid not in channels:
            return False
        del channels[cid]
        if not channels:
            del self.subscriptions[sid]
        return True

    def has_subscription(self, session_id: str, channel_id: str) -> bool:
        return str(channel_id) in self.subscriptions.get(str(session_id), {})

    def get_session_channels(self, session_id: str) -> dict[str, dict]:
        return self.subscriptions.get(str(session_id), {})

    def sessions_for_channel(self, channel_id: str) -> list[str]:
        cid = str(channel_id)
        return [sid for sid, chans in self.subscriptions.items() if cid in chans]

    def all_channel_ids(self) -> list[str]:
        """所有至少被一个会话订阅的频道 id。"""
        ids = set()
        for chans in self.subscriptions.values():
            ids.update(chans.keys())
        return sorted(ids)

    # ------------------------------------------------------------ 频道状态

    def _ensure_channel(self, channel_id: str) -> ChannelState:
        cid = str(channel_id)
        if cid not in self.channels:
            self.channels[cid] = ChannelState(channel_id=cid)
        return self.channels[cid]

    def get_channel_state(self, channel_id: str) -> Optional[ChannelState]:
        return self.channels.get(str(channel_id))

    def set_channel_name(self, channel_id: str, channel_name: str) -> None:
        self._ensure_channel(channel_id).channel_name = channel_name
        cid = str(channel_id)
        for chans in self.subscriptions.values():
            if cid in chans:
                chans[cid]["channel_name"] = channel_name
