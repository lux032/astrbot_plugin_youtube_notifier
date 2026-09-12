"""OAuth 2.0 管理。

- OAuthManager: 运行期用 refresh_token 自动换取短效 access_token（1 小时）。
- run_device_flow: 交互式 Device Flow，生成 refresh_token（供 scripts/oauth_setup.py 使用）。

所有请求走调用方传入的共享 aiohttp.ClientSession。
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from astrbot.api import logger

TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"

DEFAULT_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


class OAuthError(Exception):
    """OAuth 相关错误。"""


class OAuthManager:
    """运行期 token 管理：缓存 access_token，临期时用 refresh_token 续期。"""

    def __init__(
        self,
        session,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
    ):
        self._session = session
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._refresh_token = refresh_token.strip()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    async def get_access_token(self) -> str:
        """返回可用的 access_token，必要时自动续期。"""
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        if not self.configured:
            raise OAuthError("OAuth 未配置: 缺少 client_id/client_secret/refresh_token")
        form = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with self._session.post(TOKEN_URL, data=form) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise OAuthError(f"刷新 token 失败({resp.status}): {body}")
                self._access_token = body.get("access_token")
                if not self._access_token:
                    raise OAuthError(f"刷新 token 响应缺少 access_token: {body}")
                self._expires_at = time.monotonic() + int(body.get("expires_in", 3600))
                logger.info("[YT] OAuth access_token 已刷新")
                return self._access_token
        except OAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OAuthError(f"刷新 token 网络错误: {exc!r}") from exc


async def run_device_flow(
    session,
    client_id: str,
    client_secret: str,
    scope: str = DEFAULT_SCOPE,
) -> dict:
    """执行完整的 Device Flow，返回包含 access_token/refresh_token 的字典。

    流程：
    1. 请求 device_code；
    2. 提示用户访问 verification_url 输入 user_code 授权；
    3. 轮询 token 端点直至完成（interval 秒间隔）。
    """
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise OAuthError("需要提供 client_id 与 client_secret")

    # 1) 申请设备码
    try:
        async with session.post(
            DEVICE_CODE_URL, data={"client_id": client_id, "scope": scope}
        ) as resp:
            device = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"请求设备码失败: {exc!r}") from exc
    if "device_code" not in device:
        raise OAuthError(f"设备码响应异常: {device}")

    print("=" * 60)
    print("请打开以下地址并输入用户码完成授权:")
    print(f"  地址: {device.get('verification_url', 'https://www.google.com/device')}")
    print(f"  用户码: {device.get('user_code', '')}")
    print(f"  {device.get('expires_in', 1800)} 秒内有效")
    print("=" * 60)

    # 2) 轮询 token
    interval = max(int(device.get("interval", 5)), 2)
    deadline = time.monotonic() + int(device.get("expires_in", 1800))
    last_error = None
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            async with session.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("access_token"):
                    print("授权成功！")
                    return body
                if "refresh_token" in body or body.get("error") != "authorization_pending":
                    last_error = body
        except Exception as exc:  # noqa: BLE001
            last_error = {"_network_error": str(exc)}
    raise OAuthError(f"设备授权超时或失败: {last_error}")
