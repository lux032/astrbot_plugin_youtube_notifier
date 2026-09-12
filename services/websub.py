"""WebSub 订阅管理：hub 订阅/退订/续期 + 推送解析。

hub 协议（PubSubHubbub）：
  订阅: POST {hub}  form: hub.mode=subscribe / hub.callback / hub.topic /
                          hub.verify_token / hub.lease_seconds
  校验: hub 向 callback 发 GET（带 hub.challenge）→ 原样回显 challenge
  推送: hub 向 callback POST Atom XML
"""

from __future__ import annotations

import time
from typing import Optional

from astrbot.api import logger

from .feed import parse_feed
from .notifier import NotificationService
from ..utils import retry_async

HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
TOPIC_TEMPLATE = "https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"
DEFAULT_LEASE_SECONDS = 864000  # 10 天
# 距租约到期不足该时长时提前续期
RENEW_BEFORE_SECONDS = 86400  # 1 天


class WebSubManager:
    def __init__(
        self,
        session,
        notifier: NotificationService,
        callback_base_url: str,
        verify_token: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        self._session = session
        self.notifier = notifier
        self.callback_base_url = callback_base_url.rstrip("/")
        self.verify_token = verify_token
        self.lease_seconds = max(3600, int(lease_seconds))
        # channel_id -> 到期 monotonic 时间
        self._leases: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.callback_base_url and self.verify_token)

    @property
    def callback_url(self) -> str:
        return f"{self.callback_base_url}/yt/callback"

    @staticmethod
    def topic_for(channel_id: str) -> str:
        return TOPIC_TEMPLATE.format(channel_id=channel_id)

    # ------------------------------------------------------------ hub 操作

    async def subscribe(self, channel_id: str) -> bool:
        """向 hub 订阅频道 feed。返回是否请求成功（真正生效需 hub 回调校验）。"""
        return await self._request_hub("subscribe", channel_id)

    async def unsubscribe(self, channel_id: str) -> bool:
        """向 hub 退订频道 feed。"""
        ok = await self._request_hub("unsubscribe", channel_id)
        self._leases.pop(channel_id, None)
        return ok

    async def _request_hub(self, mode: str, channel_id: str) -> bool:
        if not self.configured:
            logger.warning("[YT] WebSub 未配置 callback_url/verify_token，跳过")
            return False

        form = {
            "hub.mode": mode,
            "hub.callback": self.callback_url,
            "hub.topic": self.topic_for(channel_id),
            "hub.verify_token": self.verify_token,
        }
        if mode == "subscribe":
            form["hub.lease_seconds"] = str(self.lease_seconds)

        async def _do() -> int:
            async with self._session.post(HUB_URL, data=form, timeout=20) as resp:
                if resp.status not in (200, 202, 204):
                    text = await resp.text()
                    raise RuntimeError(f"hub 返回 {resp.status}: {text[:200]}")
                return resp.status

        try:
            status = await retry_async(
                _do, logger=logger, label=f"websub {mode} {channel_id}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] WebSub {mode} 失败 channel={channel_id}: {exc!r}")
            return False

        if mode == "subscribe":
            self._leases[channel_id] = time.monotonic() + self.lease_seconds
            logger.info(
                f"[YT] WebSub 已订阅 channel={channel_id} (hub {status}), "
                f"等待 hub 握手校验"
            )
        else:
            logger.info(f"[YT] WebSub 已退订 channel={channel_id}")
        return True

    async def renew_due(self, channel_ids: list[str]) -> None:
        """对临近过期的订阅续期（poller 周期性调用）。"""
        if not self.configured:
            return
        now = time.monotonic()
        for channel_id in channel_ids:
            expiry = self._leases.get(channel_id)
            if expiry is None:
                # 从未成功订阅（如插件重启）→ 补订阅
                await self.subscribe(channel_id)
                continue
            if expiry - now < RENEW_BEFORE_SECONDS:
                logger.info(f"[YT] WebSub 租约将到期，续期 channel={channel_id}")
                await self.subscribe(channel_id)

    # ------------------------------------------------------------ 回调处理

    def handle_verification(self, params: dict) -> Optional[str]:
        """校验 hub 握手请求，返回需要回显的 challenge；失败返回 None。"""
        if not self.configured:
            return None
        mode = params.get("hub.mode", "")
        if mode not in ("subscribe", "unsubscribe"):
            return None
        if self.verify_token and params.get("hub.verify_token") != self.verify_token:
            logger.warning("[YT] WebSub 校验 token 不匹配，拒绝")
            return None
        topic = params.get("hub.topic", "")
        if "youtube.com/xml/feeds/videos.xml" not in topic:
            logger.warning(f"[YT] WebSub topic 非预期: {topic}")
            return None
        challenge = params.get("hub.challenge")
        if not challenge:
            return None
        return challenge

    async def handle_push(self, body: bytes, headers: Optional[dict] = None) -> None:
        """处理 hub 推送的 Atom XML：解析 → 新投稿/直播检测 → 推送通知。"""
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] WebSub 推送解码失败: {exc!r}")
            return

        result = parse_feed(text)
        if result is None or not result.entries:
            logger.warning("[YT] WebSub 推送内容无法解析为空 feed")
            return

        channel_id = result.channel_id
        if not channel_id:
            channel_id = result.entries[0].channel_id
        if not channel_id:
            logger.warning("[YT] WebSub 推送缺少 channel_id，无法路由")
            return

        logger.info(
            f"[YT] 收到 WebSub 推送 channel={channel_id} entries={len(result.entries)}"
        )
        await self.notifier.process_feed_result(channel_id, result)
