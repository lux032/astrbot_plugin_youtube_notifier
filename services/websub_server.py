"""WebSub 回调 HTTP 服务（aiohttp）。

- GET  /yt/callback : hub 握手校验，返回 hub.challenge 原文
- POST /yt/callback : 接收推送的 Atom XML
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from aiohttp import web
from astrbot.api import logger

CALLBACK_PATH = "/yt/callback"

# (query: dict[str,str]) -> 需要回显的 challenge，校验失败返回 None
VerifyHandler = Callable[[dict], Optional[str]]
# (body: bytes, headers: dict) -> None
PushHandler = Callable[[bytes, dict], Awaitable[None]]


class WebSubCallbackServer:
    def __init__(
        self,
        port: int,
        on_verify: VerifyHandler,
        on_push: PushHandler,
        host: str = "0.0.0.0",
    ):
        self.port = int(port)
        self.host = host
        self._on_verify = on_verify
        self._on_push = on_push
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(CALLBACK_PATH, self._handle_get)
        app.router.add_post(CALLBACK_PATH, self._handle_post)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(
            f"[YT] WebSub 回调服务已启动: http://{self.host}:{self.port}{CALLBACK_PATH}"
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("[YT] WebSub 回调服务已停止")

    # ------------------------------------------------------------ handlers

    async def _handle_get(self, request: web.Request) -> web.Response:
        params = {k: v for k, v in request.query.items()}
        challenge = self._on_verify(params)
        if challenge is None:
            logger.warning(
                f"[YT] WebSub 校验失败 mode={params.get('hub.mode')} "
                f"topic={params.get('hub.topic')}"
            )
            return web.Response(status=404, text="verification failed")
        logger.info(
            f"[YT] WebSub 握手成功 mode={params.get('hub.mode')} "
            f"topic={params.get('hub.topic')}"
        )
        # 必须返回 200 + challenge 原文
        return web.Response(status=200, text=challenge, content_type="text/plain")

    async def _handle_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] WebSub 推送读取失败: {exc!r}")
            return web.Response(status=400, text="bad request")
        try:
            await self._on_push(body, dict(request.headers))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] WebSub 推送处理失败: {exc!r}")
        # 始终 204，避免 hub 重推
        return web.Response(status=204)
