# CLAUDE.md — 项目约束文档

本文件为在 `astrbot_plugin_youtube_notifier` 上工作的任何编码会话提供强制约束。修改代码前必读。

## 项目概述

AstrBot v4.28.0 插件：订阅 YouTube 频道（**支持 `@handle`**），直播上播/下播与新投稿以 **PIL 渲染的图片通知**发送，按会话（`event.unified_msg_origin`）隔离订阅。

## 数据源决策（已实测验证，勿擅自改动）

| 数据源 | 鉴权 | 角色 | 可靠性 |
|---|---|---|---|
| **YouTube Data API v3** | **API Key** | **主数据源** | ✅ 官方 |
| **网页 JSON**（频道页 `ytInitialData`） | 无 | **降级链首选** + 无 Key 时监控 | ⚠️ 可用但脆弱，见下 |
| 网页抓取（频道页 HTML 正则） | 无 | 只用来解析 `@handle` | ⚠️ 脆弱 |
| `liveBroadcasts` API | OAuth 2.0 | 可选，仅自己的频道 | ✅ 能力受限 |
| Atom feed | 无 | **最后兜底** | ❌ 见下 |

### 降级链（`page_fallback_enabled=true` 时）

```
Data API 配额耗尽 / 请求失败 / API Key 无效 / 未配置 Key
    → 网页 JSON（services/page_json.py）⭐ 实测可用
    → legacy Atom feed（仅当网页也失败；端点已大面积 404）
```

降级**必须留痕**：日志 + 通知服务的 `degraded_reason()` → `/yt列表` 展示
⚠️ 数据源已降级 + `/yt订阅` 回复提示。`live_detect_mode=feed` 是用户显式选择，
不参与降级链。

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
- `feed`：legacy Atom feed（⚠️ 不可靠；显式选择，不参与降级）
- `auto`：优先 data_api，无 Key 时回退网页 JSON

## 目录结构

```
main.py                    # Star 插件：指令 / 生命周期 / 装配
metadata.yaml / requirements.txt / _conf_schema.json
CLAUDE.md / PLAN.md / API_GUIDE.md / README.md
services/
  models.py                # ChannelState / FeedEntry / LiveInfo / Notification / ChannelMeta
  data_api.py              # ★ 主数据源：Data API v3（API Key）+ handle 解析 + 配额计数
  page_json.py             # ★ 网页 JSON 兜底：ytInitialData 抓取与解析
  feed.py                  # legacy Atom feed（最后兜底，勿依赖）
  livebroadcasts.py        # LiveBroadcasts API（OAuth，仅自己的频道）
  scrape.py                # 频道页 HTML 正则（handle 解析兜底）
  oauth.py                 # OAuth token 管理与 device flow
  store.py                 # 订阅存储 + 会话隔离 + JSON 持久化
  state_machine.py         # 直播状态机（纯逻辑，可单测）
  notifier.py              # 检测 → 渲染 → 推送所有订阅会话 + 降级链
  poller.py                # asyncio 后台轮询（默认 300s）
  cleanup.py               # 通知图定时清理（按年龄 + 按总量，默认每天 4:00）
  websub.py / websub_server.py  # WebSub（默认关闭，见下）
renderer.py                # PIL 文生图通知（三模板 + 测试标记）
utils.py                   # 重试退避 / 时间 / 字体 / emoji / 换行 / BROWSER_HEADERS
tests/                     # 离线单测 + 真实 feed / 网页 JSON fixture
scripts/diagnose.py        # 数据源诊断（含 --check-page 网页兜底体检）
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

## 网页 JSON 数据源（services/page_json.py，2026-09-12 实测）

**必须知道的限制**（全部实测确认）：

1. **直播只能从 `/streams` 标签页拿到**：`/videos` 页直播数为 **0**
   （@NASA、@SkyNews 均如此）→ 每次快照要抓 **2 个页面**，各约 1.2MB。
2. **`/streams` 页只可用于判断直播**：实测 MrBeast 的 `/streams` 里列出的
   全是他的**普通投稿**（首播视频被 YouTube 归入 streams），24 条中有 12 条
   与 `/videos` 完全重合。所以该页非直播条目**既不是「往期直播存档」、
   也不能当普通投稿** → 一律丢弃，只取 live/upcoming。
   ⚠️ 曾把非直播条目标成 `completed`，会让 `find_new_videos` 把这些真实投稿
   **永久跳过**（`was_live` → 过滤掉）→ 静默漏推。**别改回去。**
3. **没有 ISO 时间戳**：只有相对时间（"6 days ago"）或日期（"Sep 5, 2026"），
   换算成**近似** ISO 仅用于排序/展示。月份用自带映射表解析，
   **不要用 `strptime("%b")`** —— 中文 Windows 上会因 locale 解析失败。
4. **直播没有 actualStartTime**：`/streams` 只给 "N watching" /
   "Started streaming 2 hours ago" → 直播时长不准确（状态机用 now 兜底）。
5. **条目结构已迁移**：列表项从旧的 `videoRenderer` 变成了
   `lockupViewModel`（实测）。直播信号是缩略图角标
   `badgeStyle == "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"`。
6. **观看页的 `ytInitialPlayerResponse` 不可用**：自动请求返回
   `playabilityStatus: LOGIN_REQUIRED`（"Sign in to confirm you're not a bot"），
   **不含 videoDetails**。观看页信息一律从 `ytInitialData`
   （`videoPrimaryInfoRenderer` / `playerOverlayVideoDetailsRenderer`）取。
7. **JSON 提取要用花括号配对**，不要用 `\{(.*?)\};</script>` 正则 ——
   实测 @spacex 页面的结尾写法不匹配该正则，会整页解析失败。
8. **频道 ID 从页面取**：`<link rel="canonical">` 优先，`"externalId"` 兜底。
   `channelMetadataRenderer` **不可靠**（实测部分页面根本没有该节点）。
9. **节流按用途区分**：轮询走 60s 节流，冷却期内**复用缓存**（不是返回 None，
   否则会被当成抓取失败而触发多余的 feed 回退）；交互式命令（订阅解析、
   `/yt*测试`）用 `respect_throttle=False` 绕过 —— 否则连跑两次同一频道
   会因冷却解析不到，被误报成「未找到频道」。

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
- 依赖：`requirements.txt` 必须列出**所有**运行时第三方依赖。`renderer.py` 顶层
  `import PIL`，所以 pillow 是硬依赖（曾漏列）
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
11. **通知图与封面必须定期清理**（`services/cleanup.py`）。每张约 300–700KB，
    文件名带 uuid、每次推送新建、永不复用 → 不清理就单调增长写满磁盘
    （实测 19 张 = 9.7MB）。两条策略同时生效：按年龄（默认 7 天）+ 按总量
    （默认 500MB 上限，从最旧开始删）。**不可省略的安全约束**：
    ① 只删指定目录里的图片扩展名，绝不碰 `state.json` 等其它文件；
    ② 绝不删 `min_keep_seconds`（默认 1 小时）内的文件 —— 刚渲染的图可能还在
       发送队列里，删了用户收到的就是坏图；③ 单文件删除失败只记日志跳过，
       cleanup 绝不向上抛异常（否则会打死后台任务）。
    另外 `retention_days=0` 的语义是「不按天数删」而非「全删」—— 删除不可逆，
    0 必须取保守解释。两条策略都用 0 = 关闭，保持一致。
12. **中文字体必须先验证「含中文字形」，不能只看文件存在**（`font_supports_cjk`）。
    真实踩坑：Linux VPS 最小化安装自带 `DejaVuSans.ttf`（纯拉丁、无中文字形），
    而历史候选列表把 DejaVu 排在中文字体**之前** → 静默命中它 → 通知图里所有
    中文变豆腐块，日志里却毫无异常。禁止把纯拉丁字体混进 CJK 候选链：
    `_FONT_CANDIDATES` 只放含中文的字体，纯拉丁字体单独放 `_FALLBACK_LATIN_FONTS`
    且只在确实找不到中文字体时使用（同时必须打 ERROR + 安装指引）。
    探测方法见 `utils.font_supports_cjk`（私用区字符位图比对）。

## 测试

```bash
python tests/test_imports.py        # 包导入冒烟(16模块) + config schema + metadata + 就绪语义矩阵
python tests/test_state_machine.py  # 状态机 + feed 解析（含真实 feed fixture）
python tests/test_store.py          # 会话隔离 + 持久化 + 损坏容错
python tests/test_data_api.py       # Data API 输入解析/响应映射/快照组装（mock HTTP）
python tests/test_page_json.py      # 网页 JSON 解析 + 降级链（含真实页面 fixture）
python tests/test_cleanup.py        # 图片清理：年龄/总量策略 + 误删防护 + 任务装配
```

测试用 `sys.modules` 注入最简 astrbot 桩，**不依赖 AstrBot 运行时与网络**。
真实数据 fixture（回归用）：

| fixture | 内容 |
|---|---|
| `tests/fixtures/real_feed_youtube.xml` | 2026-09 抓取的真实 Atom feed（15 条） |
| `tests/fixtures/real_channel_streams_live.json` | 2026-09-12 @NASA `/streams` 的 ytInitialData（含 2 条直播 + 1 条预告） |
| `tests/fixtures/real_channel_videos_normal.json` | 2026-09-12 @MrBeast `/videos` 的 ytInitialData（6 条普通投稿） |

⚠️ 两个网页 fixture 是**白名单裁剪版**：只保留解析器实际读取的字段
（条目 id/标题/元信息行/缩略图/角标 + 频道 canonical）。真实页面的
`ytInitialData` 里还有大量与解析无关的东西（`googlevideo` 预览流 URL、
`clickTrackingParams` 等跟踪参数），那些**不要**加回来 —— 既是噪音，
也会让仓库体积涨约 28 倍。重建裁剪版见 CLAUDE.md「网页 JSON 数据源」一节。

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
5. **「能工作但降级」也必须明说**：`_monitoring_blocker()` 返回空串 ≠ 一切正常。
   只要实际走的是降级链路，`_degraded_notice()` 必须有话说，
   且要出现在 `/yt订阅` 回复与 `/yt列表` 里。运行期降级按频道记在
   `notifier.degraded_reason(channel_id)`。
6. **就绪语义矩阵**（`test_monitoring_blocker` 锁定，改动前先看测试）：
   `livebroadcasts` 缺 Key/OAuth → 阻塞；`data_api` 没 Key 且网页兜底关闭 →
   阻塞；`auto` / `feed` → 永不阻塞（语义就是尽力而为）；其余情形 → 不阻塞
   但给降级提示。
7. **宁可少推也不能多推是错的**：判不准的内容宁可交给 `recent_live_ids`
   那道防线，也不要「一律当存档」—— 后者会永久静默漏推真实投稿
   （见网页 JSON 坑 #2）。

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

- **AstrBot 内端到端**（`/yt订阅` → 收到图片通知）尚未在真实 bot 里跑过 ——
  唯一剩下的主要验证项。`/yt直播测试` `/yt视频测试` 正是为缩小这个缺口加的：
  它们走完整「抓取 → 渲染 → 推送」链路，可在真实 bot 里直接验证。
- WebSub 与真实 hub 的握手未验证（且端点已不可靠，默认关闭）
- feed 场景的**直播**信号未取得真实样本（feed 已降级，影响有限）
- 网页 JSON 的**预告/首播角标**（`_BADGE_UPCOMING`）未取得真实样本，
  仅按实测的同族命名防御性保留；实际预告识别目前靠
  `/streams` 页条目 + "Scheduled"/"Premieres" 文案兜底（已实测命中 @NASA 的预告）
- 网页 JSON 的**中文页面**分支（`parse_relative_time_to_iso` 里的 `6天前`）
  未取得真实样本：请求固定发 `Accept-Language: en-US`，该分支属防御性代码
