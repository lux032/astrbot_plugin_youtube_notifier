# API 接入指南

本插件有三种数据源，按推荐程度排列：

| 数据源 | 鉴权 | 用途 | 可靠性 | 默认 |
|---|---|---|---|---|
| **YouTube Data API v3** | **API Key** | 任意频道的直播 + 投稿 | ✅ 官方、稳定 | ✅ 主数据源 |
| 网页抓取（频道页 HTML） | 无 | 仅用于无 Key 时解析 @handle | ⚠️ 脆弱、约 2MB/次 | 兜底 |
| liveBroadcasts API | OAuth 2.0 | 仅**自己**频道的直播 | ✅ 但能力受限 | 可选 |
| ~~Atom feed~~ | 无 | ~~任意频道~~ | ❌ **已不可靠** | legacy |

---

## ⚠️ 重要：Atom feed 端点已不可用

`https://www.youtube.com/feeds/videos.xml?channel_id=...` 自 2025 年底起对自动化请求
**间歇性返回 404/500**。实测结果：

| 频道 | 结果 |
|---|---|
| YouTube 官方频道 | 有时 404，有时 200 |
| @ukaisaki | 404 |
| MrBeast | 404 |
| Google Developers | 500 |

多次重试同一 URL 偶尔能成功（实测第 2 次成功、第 1/3/4 次 404），说明不是永久下线，
而是 Google 收紧非官方数据访问导致的**不稳定**。Google 未发布废弃公告。

因此：**请务必配置 API Key**，不要依赖 feed。

---

## 一、申请 API Key（推荐，免费、无需 OAuth）

> 与 OAuth 不同：**API Key 不需要授权登录、不需要绑卡、不需要同意屏幕**，两分钟搞定。

1. 打开 [Google Cloud Console](https://console.cloud.google.com/)
2. 新建项目（或选择已有项目）
3. **API 和服务 → 库** → 搜索 **YouTube Data API v3** → **启用**
4. **API 和服务 → 凭据 → 创建凭据 → API 密钥**
5. 复制生成的 Key（形如 `AIza...`），填入插件配置的 **`api_key`** 项

> 建议在凭据页面点「限制密钥 → API 限制 → 仅 YouTube Data API v3」，降低泄露风险。

### 验证 Key 是否可用

```bash
# 应返回频道信息（不是 403）
curl -s "https://www.googleapis.com/youtube/v3/channels?part=snippet&forHandle=ukaisaki&key=YOUR_API_KEY"
```

或用插件自带诊断脚本（推荐，会跑完整流程）：

```bash
python scripts/diagnose.py @ukaisaki --api-key AIza...
```

输出示例：
```
channel_id           : UCNydvA0D7GSuT0c9Zs-zWdw
title                : '鵜飼沙樹 / 銀海渡ニシェ'
uploads_playlist_id  : UUNydvA0D7GSuT0c9Zs-zWdw
[1] 📺 投稿   DfECjUL9ZvU  2026-09-10T20:00:18+00:00
...
配额消耗: 2 单位（每频道每轮约 2 单位，免费额度 10000/天）
```

---

## 二、配额说明

免费额度 **10,000 单位/天**（太平洋时间午夜重置）。本插件每频道每轮消耗约 **2 单位**：

| 调用 | 单位 | 说明 |
|---|---|---|
| `channels.list` | 1 | handle/ID → 频道元数据（**仅订阅时一次**，之后缓存） |
| `playlistItems.list` | 1 | 上传播放列表 → 最新视频 |
| `videos.list` | 1 | 直播状态 + 实际起止时间 |

**轮询间隔 vs 可支撑频道数**：

| 间隔 | 单频道/天 | 可支撑 |
|---|---|---|
| 60 秒 | 2,880 | ≈ 3 个频道 |
| 3 分钟 | 960 | ≈ 10 个频道 |
| **5 分钟（默认）** | **576** | **≈ 17 个频道** |
| 10 分钟 | 288 | ≈ 34 个频道 |

超出配额会返回 `quotaExceeded`，插件会打错误日志并在当天停止调用。
如需更多，可在 Google Cloud 申请配额提升（免费）。

---

## 三、频道标识支持的形式

`/yt订阅` 接受以下任意形式，**直接给 @handle 即可**：

| 输入 | 解析方式 |
|---|---|
| `@ukaisaki` | `channels.list?forHandle`（官方） |
| `ukaisaki` | 同上（无 @ 也识别为 handle） |
| `https://www.youtube.com/@ukaisaki` | 同上 |
| `https://www.youtube.com/@ukaisaki/videos` | 同上 |
| `UCxxxxxxxxxxxxxxxxxxxxxx` | `channels.list?id`（直接查 ID） |
| `https://www.youtube.com/channel/UC...` | 同上 |
| `https://www.youtube.com/c/CustomName` | 按 handle 解析 |
| `https://www.youtube.com/user/LegacyName` | `channels.list?forUsername` |

内部统一解析为 `UC...` 频道 ID 并存为订阅 key，所以同一频道无论用哪种形式输入都视为同一个。

> **无 API Key 时**：handle 会退化为抓取频道页 HTML（`<link rel="canonical">`）解析，
> 功能可用但脆弱（YouTube 改版即失效，且每次约 2MB）。仍建议配置 Key。

---

## 四、OAuth 2.0（可选，仅用于监控自己的频道）

仅当 `live_detect_mode=livebroadcasts` 时需要。

> ⚠️ `liveBroadcasts.list?mine=true` **只返回认证账户自己拥有**的直播，无法监控第三方频道。
> 监控任意频道用 Data API + API Key 即可，**不需要 OAuth**。

### 申请凭据

1. Google Cloud Console → 启用 **YouTube Data API v3**
2. **OAuth 同意屏幕** → 外部 → 把自己加入 **测试用户**
3. **凭据 → 创建凭据 → OAuth 客户端 ID → 桌面应用**
4. 记录 `client_id` 与 `client_secret`

### 交互式授权（推荐）

```bash
python scripts/oauth_setup.py
```

脚本会走完 Device Flow 并把 `refresh_token` 写入插件配置。

### 手动 Device Flow（可全 curl）

```bash
# ① 申请设备码
curl -s -X POST https://oauth2.googleapis.com/device/code \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "scope=https://www.googleapis.com/auth/youtube.readonly"

# ② 浏览器打开 verification_url，输入 user_code 授权

# ③ 轮询换 token
curl -s -X POST https://oauth2.googleapis.com/token \
  -d "client_id=..." -d "client_secret=..." \
  -d "device_code=..." \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code"
# → access_token + refresh_token
```

### 运行期续期（插件自动处理）

```bash
curl -s -X POST https://oauth2.googleapis.com/token \
  -d "client_id=..." -d "client_secret=..." \
  -d "refresh_token=..." -d "grant_type=refresh_token"
```

### 实际请求一次

```bash
curl -s "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status,contentDetails&mine=true" \
  -H "Authorization: Bearer ACCESS_TOKEN"
```

用 `status.lifeCycleStatus`（created/ready/testing/live/complete/revoked）驱动状态机。

---

## 五、WebSub（默认关闭，不建议启用）

WebSub 推送的内容就是上面那个**已不可靠的 Atom feed**，hub 无法向失效端点完成校验，
因此本插件默认关闭 WebSub。若你仍想尝试：

```bash
curl -s -X POST https://pubsubhubbub.appspot.com/subscribe \
  -d "hub.callback=https://你的公网地址/yt/callback" \
  -d "hub.mode=subscribe" \
  -d "hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=CHANNEL_ID" \
  -d "hub.verify_token=自定义校验串" \
  -d "hub.lease_seconds=864000"
```

回调服务监听 `websub.callback_port`（默认 8477），
`GET` 回显 `hub.challenge`，`POST` 解析 Atom 并推送新视频通知。

---

## 六、排查工具

```bash
# 完整诊断（推荐先跑这个）
python scripts/diagnose.py @handle --api-key AIza...

# 走代理
python scripts/diagnose.py @handle --api-key AIza... --proxy http://127.0.0.1:7890

# 顺带体检 legacy feed（确认其确实不可用）
python scripts/diagnose.py @handle --api-key AIza... --check-feed

# 离线解析本地 XML
python scripts/diagnose.py --file feed.xml
```

> ⚠️ 诊断脚本需在**能访问 YouTube** 的机器上运行（通常是 VPS）。
