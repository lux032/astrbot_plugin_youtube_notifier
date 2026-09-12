# astrbot_plugin_youtube_notifier

AstrBot 的 YouTube 订阅提醒插件：订阅频道后，**直播上播 / 下播**与**新投稿**都会以
**图片通知**（Pillow 渲染的深色卡片）推送到会话，订阅按会话隔离。

直接给 `@handle` 就能订阅，例如 `/yt订阅 @ukaisaki`。

> 文档：[实现计划](PLAN.md)｜[项目约束](CLAUDE.md)｜[API 接入指南](API_GUIDE.md)

## 功能

- 🔴 **上播提醒**：检测到频道开播即推送（标题 + 封面 + 开始时间）
- ⚫ **下播提醒**：直播结束后推送（含**直播时长**，取 YouTube 的实际起止时间）
- 📺 **新投稿提醒**：新视频发布推送（标题 + 封面）
- 🔒 **会话隔离**：每个会话（群/私聊）的订阅互不影响
- 🖼 **全部图片化**：通知渲染成图，非纯文本

## 安装

1. 把本目录放入 AstrBot 的 `data/plugins/`，重启或在 WebUI 重载插件
2. **配置 `api_key`**（见下，必填）
3. `/yt订阅 @handle` 开始使用

依赖 `aiohttp`（`requirements.txt` 声明，AstrBot 自动安装；Pillow 已随 AstrBot 提供）。

## 必须先配 API Key

① 到 [Google Cloud Console](https://console.cloud.google.com/) 免费申请：

- 新建项目 → **API 和服务 → 库** → 启用 **YouTube Data API v3**
- **凭据 → 创建凭据 → API 密钥** → 复制 `AIza...`
- 无需 OAuth、无需绑卡

② 填进插件配置的 **`api_key`**，或先验证：

```bash
python scripts/diagnose.py @ukaisaki --api-key AIza...
```

> **为什么必须配？** YouTube 的 Atom feed（`feeds/videos.xml`）自 2025 年底起对自动化
> 请求间歇性返回 404/500，实测 YouTube 官方频道也会 404，已无法作为主数据源。
> 官方 Data API 稳定且只需一个免费 API Key。详见 [API_GUIDE.md](API_GUIDE.md)。

## 指令

| 指令 | 说明 |
|---|---|
| `/yt订阅 @handle` | 订阅频道，也接受频道ID / 频道URL |
| `/yt取消订阅 @handle` | 取消本会话的订阅（同样接受 ID / URL）|
| `/yt列表` | 查看本会话订阅与频道状态 |
| `/yt直播测试 <目标>` | 抓目标当前直播，渲染推送一张**测试**图 |
| `/yt视频测试 <目标>` | 抓目标最新视频，渲染推送一张**测试**图 |

支持的输入形式：`@ukaisaki`、`ukaisaki`、`https://www.youtube.com/@ukaisaki`、
`https://www.youtube.com/channel/UC...`、`UCxxxxxxxxxxxxxxxxxxxxxx`。

### 测试指令

`/yt直播测试` 与 `/yt视频测试` 走的是与真实推送**完全相同**的抓取 → 渲染 →
推送链路，只是图上多一个「🧪 测试」标记，且不触碰任何去重状态。

```bash
/yt直播测试 @NASA                                      # 该频道当前直播
/yt视频测试 @MrBeast                                   # 该频道最新视频
/yt视频测试 https://www.youtube.com/watch?v=gTKS8SAwUzE  # 指定视频
```

用途：确认渲染与推送到当前会话是否正常（字体、封面下载、适配器发图）。
目标当前没有直播时，回复会**明确说明**这张图借用的是哪条内容，不会假装是直播。

## 配额与轮询间隔

免费额度 **10,000 单位/天**，每频道每轮约消耗 **2 单位**（`channels.list` 只在订阅时消耗一次并缓存）。

| 轮询间隔 | 可支撑频道数 |
|---|---|
| 60 秒 | ≈ 3 |
| 3 分钟 | ≈ 10 |
| **5 分钟（默认）** | **≈ 17** |
| 10 分钟 | ≈ 34 |

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `api_key` | — | **强烈建议填**，YouTube Data API Key（不填会走网页兜底） |
| `live_detect_mode` | `data_api` | `data_api` / `livebroadcasts` / `feed` / `auto` |
| `page_fallback_enabled` | `true` | **配额耗尽/请求失败时自动改用网页 JSON** |
| `page_fallback_min_interval_seconds` | `60` | 网页兜底同频道最小抓取间隔 |
| `poll_interval_seconds` | `300` | 轮询间隔，见上表 |
| `max_results` | `5` | 每轮拉取最近多少条视频（1-50） |
| `proxy` | — | 代理，如 `http://127.0.0.1:7890`（大陆网络通常需要） |
| `cover_download` | `true` | 是否下载封面到通知图 |
| `notify.*` | 全开 | 分别开关上播/下播/新投稿通知 |
| `oauth.*` | — | 仅 `livebroadcasts` 模式需要 |
| `websub.*` | 关闭 | ⚠️ 依赖已不可靠的 feed，不建议启用 |

### 自动降级（配额用完也不会断）

```
Data API 配额耗尽 / 请求失败 / API Key 无效 / 未配置 Key
    → 网页 JSON（抓频道页 ytInitialData）⭐ 实测可用
    → legacy Atom feed（仅当网页也失败）
```

降级**不是静默的**：`/yt列表` 会显示 `⚠️ 数据源已降级: …`，订阅回复也会提示。

网页兜底的代价（已实测，心里有数即可）：

- 直播只在 `/streams` 标签页有 → 每次检查抓 **2 个页面**，约 **2.4MB**
- **没有精确时间戳**，时间由 "6 days ago" 这类相对文案换算，仅够排序展示
- 直播的**时长不准确**（网页不给实际开始/结束时间）
- 未配置 Key 时自动使用，所以**不配 Key 也能用**（只是不如官方 API 稳）

## 开发

```bash
python tests/test_imports.py        # 包导入冒烟 + 配置 schema 校验 + 就绪语义矩阵
python tests/test_state_machine.py  # 状态机 + feed 解析（含真实 feed 回归）
python tests/test_store.py          # 会话隔离 + 持久化
python tests/test_data_api.py       # Data API 解析与快照组装（mock HTTP）
python tests/test_page_json.py      # 网页 JSON 解析 + 降级链（含真实页面回归）
```

测试全部离线，不依赖 AstrBot 运行时与网络。

### 排查

```bash
python scripts/diagnose.py @handle --api-key AIza...   # 完整数据源诊断
python scripts/diagnose.py --check-fonts                # 只体检中文字体（无需网络）
python scripts/diagnose.py @handle --check-page         # 只体检网页兜底（无需 Key）
python scripts/diagnose.py @handle --check-feed         # 顺带体检 legacy feed
python scripts/diagnose.py --file feed.xml              # 离线解析本地 XML
```

> 诊断脚本需在**能访问 YouTube** 的机器上运行（通常是 VPS）。

## 架构

```
main.py                 # Star 插件：指令 / 生命周期 / 装配
renderer.py             # Pillow 文生图通知（三模板）
utils.py                # 重试退避 / 时间 / 字体 / emoji / 换行
services/
  data_api.py           # ★ 主数据源：Data API v3（API Key）+ @handle 解析 + 配额计数
  page_json.py          # ★ 网页 JSON 兜底：频道页 ytInitialData 抓取与解析
  feed.py               # legacy Atom feed（⚠️ 端点已不可靠，仅最后兜底）
  livebroadcasts.py     # LiveBroadcasts API（OAuth，仅自己的频道）
  scrape.py             # 频道页 HTML 正则（handle 解析兜底）
  oauth.py              # OAuth token 管理与 device flow
  models.py             # 数据模型
  store.py              # 订阅存储（会话隔离 + JSON 持久化）
  state_machine.py      # 直播状态机（纯逻辑）
  poller.py             # asyncio 后台轮询
  notifier.py           # 检测 → 渲染 → 推送 + 降级链
  websub*.py            # WebSub 推送（默认关闭）
```

数据保存在插件数据目录 `data/`（已 gitignore）：`state.json` 与 `images/`。

## 常见问题

### VPS 上图里中文全是方框（豆腐块）

**原因**：服务器没装中文字体。Linux 最小化安装通常自带 DejaVuSans（纯拉丁、
不含任何中文字形），插件会拿它来渲染，于是所有中文变成 `□`。

**修复**（任选其一，装完重载插件）：

```bash
# Debian / Ubuntu
apt-get install -y fonts-noto-cjk

# CentOS / RHEL / Fedora
dnf install -y google-noto-sans-cjk-fonts

# Alpine
apk add font-noto-cjk

# Arch
pacman -S noto-fonts-cjk
```

或者把任意中文字体文件传到服务器，将配置项 `render.font_path` 设为它的
绝对路径（例如 `/opt/fonts/msyh.ttc`）。

**确认是否修好**：

```bash
fc-list :lang=zh | head          # 应列出中文字体
python scripts/diagnose.py --check-fonts   # 会打印判定结果并生成一张测试图
```

> 插件启动时会自己检测中文字体：缺失时日志里会打出带安装命令的 ERROR，
> `/yt列表` 也会在聊天里提示 —— 不会让你对着方框猜原因。

## License

见 [LICENSE](LICENSE)。
