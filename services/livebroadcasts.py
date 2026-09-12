"""LiveBroadcasts API 客户端（OAuth 2.0，仅能监控认证账户自己的频道）。

⚠️ 限制：`liveBroadcasts.list?mine=true` 只返回**认证账户自己拥有**的直播，
无法监控第三方频道。监控任意频道请用 services/data_api.py（仅需 API Key）。

本模块保留给「监控自己频道」这一场景，且是唯一需要 OAuth 的路径。
"""

from __future__ import annotations

from astrbot.api import logger

from .models import LiveInfo
from .oauth import OAuthManager
from ..utils import retry_async

BROADCASTS_URL = "https://www.googleapis.com/youtube/v3/liveBroadcasts"


class ApiQuotaError(Exception):
    """YouTube Data API 配额不足。"""


class LiveBroadcastsClient:
    def __init__(self, session, oauth: OAuthManager, proxy: str = ""):
        self._session = session
        self.oauth = oauth
        self._proxy = proxy or None

    async def fetch_live_broadcasts(self) -> list[LiveInfo]:
        """返回当前 lifeCycleStatus == live 的直播（仅认证账户自己的）。"""
        if not self.oauth or not self.oauth.configured:
            raise RuntimeError("OAuth 未配置，无法调用 LiveBroadcasts API")
        token = await self.oauth.get_access_token()

        async def _do() -> list[LiveInfo]:
            params = {"part": "snippet,status,contentDetails", "mine": "true"}
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.get(
                BROADCASTS_URL,
                params=params,
                headers=headers,
                proxy=self._proxy,
                timeout=15,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    error = (body.get("error") or {}).get("message", body)
                    reasons = [
                        i.get("reason", "")
                        for i in (body.get("error") or {}).get("errors") or []
                    ]
                    if "quotaExceeded" in reasons or "quota" in str(error).lower():
                        raise ApiQuotaError(str(error))
                    raise RuntimeError(f"liveBroadcasts 返回 {resp.status}: {error}")
                items = body.get("items") or []

            lives: list[LiveInfo] = []
            for item in items:
                snippet = item.get("snippet") or {}
                status = item.get("status") or {}
                if status.get("lifeCycleStatus") != "live":
                    continue
                video_id = item.get("id", "")
                lives.append(
                    LiveInfo(
                        live_id=video_id,
                        title=snippet.get("title", ""),
                        channel_id=snippet.get("channelId", ""),
                        thumbnail_url=(
                            ((snippet.get("thumbnails") or {}).get("high") or {}).get(
                                "url", ""
                            )
                        ),
                        start_time=snippet.get("publishedAt", ""),
                        url=f"https://www.youtube.com/watch?v={video_id}",
                    )
                )
            return lives

        return await retry_async(_do, logger=logger, label="fetch liveBroadcasts")
