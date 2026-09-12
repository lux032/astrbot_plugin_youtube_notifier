"""OAuth 2.0 Device Flow 交互式配置助手。

用途：为「直播检测模式 = api」（LiveBroadcasts API）生成 refresh_token 并写入插件配置。

⚠️ 前置条件：已在 Google Cloud 创建 OAuth 客户端（类型：桌面应用），
   并启用了 YouTube Data API v3。详见 API_GUIDE.md。

用法：
    python scripts/oauth_setup.py
    python scripts/oauth_setup.py --config "D:/PMD/AstrBot/data/config/xxx_config.json"

本脚本独立运行，不依赖 AstrBot 运行时。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

import aiohttp

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
PLUGIN_NAME = "astrbot_plugin_youtube_notifier"


def default_config_path() -> Path:
    """默认插件配置路径：<AstrBot>/data/config/<plugin>_config.json"""
    plugin_root = Path(__file__).resolve().parents[1]
    data_dir = plugin_root.parents[1]  # data/plugins/<plugin> -> data/
    return data_dir / "config" / f"{PLUGIN_NAME}_config.json"


async def run_device_flow(client_id: str, client_secret: str) -> dict:
    async with aiohttp.ClientSession() as session:
        # ① 申请设备码
        async with session.post(
            DEVICE_CODE_URL, data={"client_id": client_id, "scope": SCOPE}
        ) as resp:
            device = await resp.json(content_type=None)
        if "device_code" not in device:
            raise RuntimeError(f"申请设备码失败: {device}")

        print()
        print("=" * 64)
        print("  请在浏览器打开下面的地址，并输入用户码完成授权：")
        print(f"    地址   : {device.get('verification_url', 'https://www.google.com/device')}")
        print(f"    用户码 : {device.get('user_code', '')}")
        print(f"    有效期 : {device.get('expires_in', 1800)} 秒")
        print("=" * 64)
        print("等待授权中...")

        # ② 轮询换 token
        interval = max(int(device.get("interval", 5)), 2)
        deadline = asyncio.get_event_loop().time() + int(device.get("expires_in", 1800))
        last: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(interval)
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
                return body
            last = body
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 2
                continue
            if err:
                raise RuntimeError(f"授权失败: {err} - {body.get('error_description', '')}")
        raise RuntimeError(f"授权超时: {last}")


def write_config(config_path: Path, client_id: str, client_secret: str, refresh_token: str) -> None:
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            print(f"⚠️  已有配置无法解析，将新建: {config_path}")
            data = {}
    oauth = data.setdefault("oauth", {})
    oauth["client_id"] = client_id
    oauth["client_secret"] = client_secret
    oauth["refresh_token"] = refresh_token
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube OAuth 2.0 配置助手")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="插件配置文件路径",
    )
    parser.add_argument("--client-id", default="", help="OAuth Client ID")
    parser.add_argument("--client-secret", default="", help="OAuth Client Secret")
    args = parser.parse_args()

    client_id = args.client_id or input("OAuth Client ID: ").strip()
    client_secret = args.client_secret or getpass.getpass("OAuth Client Secret: ").strip()
    if not client_id or not client_secret:
        print("❌ client_id / client_secret 不能为空")
        return 1

    try:
        token = asyncio.run(run_device_flow(client_id, client_secret))
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 授权失败: {exc}")
        return 1

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        print(
            "❌ 未拿到 refresh_token。\n"
            "   提示：若之前已授权过同一客户端，Google 可能不重复下发。\n"
            "   解决：到 https://myaccount.google.com/permissions 撤销后重试，\n"
            "   或在授权 URL 上加 prompt=consent。"
        )
        return 1

    write_config(args.config, client_id, client_secret, refresh_token)
    print()
    print(f"✅ 已写入配置: {args.config}")
    print("   现在可在 AstrBot 中将「直播检测模式」设为 api，并重载插件。")
    print("   注意：LiveBroadcasts API(mine=true) 只能监控认证账户自己的频道。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
