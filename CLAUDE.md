# CLAUDE.md — 项目约束文档

本文件为在 `astrbot_plugin_youtube_notifier` 上工作的任何编码会话提供强制约束。修改代码前必读。

## 项目概述

AstrBot v4.28.0 插件：订阅 YouTube 频道（**支持 `@handle`**），直播上播/下播与新投稿以 **PIL 渲染的图片通知**发送，按会话（`event.unified_msg_origin`）隔离订阅。

## 数据源决策（已实测验证，勿擅自改动）

| 数据源 | 鉴权 | 角色 | 可靠性 |
|---|---|---|---|
| **YouTube Data API v3** | **API Key** | **主数据源** | ✅ 官方 |
| 网页抓取（频道页 HTML） | 无 | 无 Key 时解析 `@handle` | ⚠️ 脆弱 |
| `liveBroadcasts` API | OAuth 2.0 | 可选，仅自己的频道 | ✅ 能力受限 |
| Atom feed | 无 | **legacy 兜底** | ❌ 见下 |

### ⚠️ 为什么不用 Atom feed（关键背景）

`https://www.youtube.com/feeds/videos.xml` 自 2025 年底起对自动化请求**间歇性 404/500**。
已实测确认（2026-09）：

- 同一 URL 多次请求结果不同（第 1/3/4 次 404，第 2 次 200）→ **不稳定，非永久下线**
- YouTube 官方频道、MrBeast、@ukaisaki 均出现过 404
- Google 未发布废弃公告，业界普遍认为是收紧非官方数据访问

因此 **不要把 feed 当作主数据源**。相关代码在 `services/feed.py`，已明确标注降级用途。

### ⚠️ liveBroadcasts 的能力边界

`liveBroadcasts.list?mine=true` **只返回认证账户自己拥有**的直播，无法监控第三方频道。
监控任意频道必须用 Data API + API Key（**不需要 OAuth**）。

### 数据源模式（`live_detect_mode`）

- `data_api`（默认）：Data API，任意频道，仅需 API Key
- `livebroadcasts`：OAuth 查直播 + Data API 查投稿，仅自己的频道
- `feed`：legacy Atom feed（⚠️ 不可靠）
- `auto`：优先 data_api，无 Key 时回退 feed

## 目录结构

```
main.py                    # Star 插件：指令 / 生命周期 / 装配
metadata.yaml / requirements.txt / _conf_schema.json
CLAUDE.md / PLAN.md / API_GUIDE.md / README.md
services/
  models.py                # ChannelState / FeedEntry / LiveInfo / Notification / ChannelMeta
  data_api.py              # ★ 主数据源：Data API v3（API Key）+ handle 解析 + 配额计数
  feed.py                  # legacy Atom feed（降级，勿依赖）
  livebroadcasts.py        # LiveBroadcasts API（OAuth，仅自己的频道）
  scrape.py                # 无 Key 时的频道页抓取兜底
  oauth.py                 # OAuth token 管理与 device flow
  store.py                 # 订阅存储 + 会话隔离 + JSON 持久化
  state_machine.py         # 直播状态机（纯逻辑，可单测）
  notifier.py              # 检测 → 渲染 → 推送所有订阅会话
  poller.py                # asyncio 后台轮询（默认 300s）
  websub.py / websub_server.py  # WebSub（默认关闭，见下）
renderer.py                # PIL 文生图通知（三模板）
utils.py                   # 重试退避 / 时间 / 字体 / emoji / 换行
tests/                     # 离线单测 + 真实 feed fixture
scripts/diagnose.py        # 数据源诊断
scripts/oauth_setup.py     # OAuth 交互式授权
```

## 数据模型要点

```python
@dataclass
class ChannelState:
    channel_id: str            # 统一为 UC... 形式，作为订阅 key
    channel_name: str
    channel_handle: str        # 用户输入的 @handle 形式，便于展示
    uploads_playlist_id: str   # channels.list 得到，缓存避免重复消耗配额
    last_video_id: str         # 投稿去重
    last_live_id: str          # 直播去重
    last_status: str           # none / live / ended
    last_live_start_at: str
    last_live_end_at: str
    last_live_title: str
    last_live_thumbnail_url: str
    recent_live_ids: list[str] # 已作为直播通知过的 id，防 VOD 重复推送
```

持久化 `data/state.json`：
```json
{
  "subscriptions": { "session_id": { "channel_id": {"channel_name": "..."} } },
  "channels": { "channel_id": { "...ChannelState 字段..." } }
}
```
- 会话 key = `event.unified_msg_origin`（`platform:message_type:session_id`）
- `subscriptions` 会话优先；`channels` 频道全局状态共享
- 轮询遍历 `channels`；事件触发后通知订阅该频道的**所有**会话

## 直播状态机（services/state_machine.py）

```
上播: 有 live 且 last_status!=live 且 id 变化 → live_start
      （首次接入 last_status==none 时若已在直播 → 只记状态不推送）
下播: last_status==live 且当前无 live → live_end（时长用 actualEndTime 优先）
防重复: 同一 live_id 只推一次；并写入 recent_live_ids
```

**防 VOD 重复推送**：直播结束后该视频会作为存档出现在上传播放列表里（`live_state` 变为
普通投稿）。若不加防护会再推一次「新投稿」。两道防线：
1. `find_new_videos` 过滤掉 `live_state` 非空的条目（completed 也算直播）；
2. `recent_live_ids` 再挡一次（有界 10 条）。

## 已实测的真实世界坑（勿重蹈）

1. **feed 根元素的 `<yt:channelId>` 不含 `UC` 前缀**
   实测根为 `BR8-60-B28hp2BmDPdntcQ`（22 字符），而 entry 内为 `UCBR8-60-...`（24 字符）。
   若直接用根值会得到错误 channel_id → 按 id 索引的状态查不到 → **WebSub 推送被静默丢弃**。
   修复：优先取 `<link rel="alternate" href=".../channel/UC...">`，否则给根值补 `UC`。
2. **feed 的 alternate 链接可能是 `/shorts/` 而非 `/watch?v=`**
   解析器保留 feed 原始链接，不强行改写。测试断言不要写死 `watch?v=`。
3. **feed 普通投稿 entry 不含 `liveBroadcastContent` / `media:status`**
   真实 entry 子元素为 `[author, channelId, group, id, link, published, title, updated, videoId]`；
   这两个直播信号字段只在直播时出现。**直播场景的 feed 解析未经真实样本验证**
   —— 这也是以 Data API 为主数据源的原因之一。

## AstrBot API 事实（已验证，勿改用不存在的 API）

- 入口 `main.py`，`@register(name, author, desc, version)`；`__init__(self, context, config: AstrBotConfig)`
- **`register` 必须显式导入**：`from astrbot.api.star import Context, Star, StarTools, register`
- metadata.yaml 必需字段：`name/desc/version/author`；依赖走 `requirements.txt`（自动安装）
- 配置：`_conf_schema.json` 嵌套块 `{"type":"object","items":{...}}`；`AstrBotConfig` 是 dict 子类。
  **不要用 `AstrBotConfig({}, {})` 做兜底**（第一个参数是文件路径，会误读文件）
- 后台任务：`asyncio.create_task` 在 `initialize()` 中启动；`terminate()` 中取消。无 `register_task` API
- 主动推送：`await self.context.send_message(umo, MessageChain().file_image(path))`
- 指令回复：`yield event.plain_result(text)` / `yield event.chain_result(chain)`
- 命令别名：`@filter.command("yt订阅", alias={"yt_subscribe"})`；typed 参数 `x: str = ""` 自动解析
- 插件以**包**形式按点分路径加载 → 相对导入可用（`from .services.x import y`）

## 工程约束（强制）

1. **所有外网请求**统一走共享 `aiohttp.ClientSession`（带超时、连接池复用）
2. **重试**：网络错误 / 5xx → 指数退避（1s/3s/9s + 抖动，最多 3 次）；429 → 尊重 `Retry-After`
3. **限流**：Data API 每次调用都记账（`_charge`），区分 `quotaExceeded` 并记录；
   legacy feed 每频道 ≥30s 节流 + 浏览器 UA
4. **异常**：单频道失败只记日志继续，不得中断整轮轮询；API 异常结构防御性解析 + `logger.warning`
5. **日志**：状态变化、推送记录（会话、video）、配额消耗、WebSub 校验失败均需打日志
6. **模块单职**：`services/` 不得 import `main.py`；渲染 / 网络 / 状态机解耦
7. **凭据安全**：API Key / token / client_secret 不写日志、不入 git（`data/`、`*_config.json` 已 gitignore）
8. 通知必须走**图片**；`/yt列表` 允许文本回复
9. 中文字体：`msyh.ttc` → `simhei.ttf` → `NotoSansCJK` → `DejaVuSans.ttf`；`font_path` 可覆盖
10. emoji：微软雅黑**不含彩色 emoji**（会渲染成豆腐块）。必须用 `utils.draw_text_with_emoji`
    （emoji 段用 `seguiemj.ttf` + `embedded_color`），并对齐字形墨迹避免裁切

## 测试

```bash
python tests/test_imports.py        # 包导入冒烟(15模块) + config schema + metadata
python tests/test_state_machine.py  # 状态机 + feed 解析（含真实 feed fixture）
python tests/test_store.py          # 会话隔离 + 持久化 + 损坏容错
python tests/test_data_api.py       # Data API 输入解析/响应映射/快照组装（mock HTTP）
```

测试用 `sys.modules` 注入最简 astrbot 桩，**不依赖 AstrBot 运行时与网络**。
`tests/fixtures/real_feed_youtube.xml` 是 2026-09 抓取的真实 feed（15 条），用于回归。
Windows GBK 控制台需 `sys.stdout.reconfigure(encoding="utf-8")` 才能打印 emoji。

## 设计原则：不允许「静默降级」

真实踩坑：`api_key` 未配置时，订阅流程仍会「成功」（handle 走抓页面兜底），
但**监控完全不会工作** —— 播种被跳过（`video_seeded=false`），轮询每轮抛
`ApiKeyMissingError`，而 `/yt列表` 只显示含糊的「暂无记录」。
用户看到订阅成功，完全不知道功能是废的。

据此确立约束：
1. **订阅时必须检查数据源就绪**（`_monitoring_blocker()`），不就绪就在回复里**明说**
   「订阅已保存但监控不会工作 + 具体原因 + 怎么修」，不能只回一句成功。
2. **状态文案要区分「尚未检测」与「已同步但无内容」**（`_status_text`）——
   主播型频道上传列表全是直播存档，没有普通投稿，笼统的「暂无记录」会误导。
3. **轮询失败不刷屏**：同类错误（如缺 Key）只报一次，之后降为 debug。
4. **降级路径要留痕**：抓页面兜底得到的频道名可能与官方不同语言
   （实测 `Rurudo Lion` vs `るるどらいおん`），用 `name_from_api` 标记，
   拿到 Key 后触发一次更正。

## 已实测验证的关键假设（2026-09-12，真实 API Key）

- ✅ **正在直播的流会作为上传播放列表的最新条目出现，且 `snippet.liveBroadcastContent == "live"`**
  —— 这是 live 检测的地基假设，已用正在直播的频道实测确认：
  某频道直播中时，`playlistItems` 首条即该直播，`videos.list` 返回
  `liveBroadcastContent=live` + `liveStreamingDetails.actualStartTime`，`find_live()` 正确命中。
  （若无此确认，live_start 会永不触发，就得改用 `search.list?eventType=live`，100 单位/次。）
- ✅ 已结束的直播：`liveBroadcastContent` 回落为 `none`，但有 `liveStreamingDetails`
  → 映射为 `completed`，并带 `actualStartTime/actualEndTime`（用于算真实时长）
- ✅ `channels.list?forHandle` 解析 `@handle`，与网页抓取兜底结果一致
- ✅ 配额：订阅时 1 次 `channels.list`，之后每频道每轮 2 单位

## 待验证（诚实记录）

- **AstrBot 内端到端**（`/yt订阅` → 收到图片通知）尚未在真实 bot 里跑过 —— 这是唯一剩下的主要验证项
- WebSub 与真实 hub 的握手未验证（且端点已不可靠，默认关闭）
- feed 场景的**直播**信号未取得真实样本（feed 已降级，影响有限）
