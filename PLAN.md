# 实现计划 — AstrBot YouTube 订阅提醒插件

> 本文档是插件的实现蓝图。约束与编码规范见 [CLAUDE.md](CLAUDE.md)，API 接入见 [API_GUIDE.md](API_GUIDE.md)。

## 需求

1. 直播上播 / 下播提醒
2. 投稿视频提醒
3. 所有通知以「文生图」渲染成图片发送
4. 订阅按会话（session_id）隔离
5. **支持直接给 `@handle` 订阅**

## 关键架构决策

| 决策 | 结论 |
|---|---|
| 主数据源 | **YouTube Data API v3 + API Key**（官方、稳定、支持 `@handle`） |
| 直播检测 | `videos.list?part=snippet,liveStreamingDetails` 的 `liveBroadcastContent` |
| 投稿检测 | `playlistItems.list`（上传播放列表）+ `videos.list` |
| handle 解析 | `channels.list?forHandle`（无 Key 时抓频道页兜底） |
| 直播时长 | `liveStreamingDetails.actualStartTime/actualEndTime`（真实起止，非轮询时刻） |
| 定时任务 | `asyncio.create_task` 后台循环（默认 300s） |
| 通知渲染 | Pillow 深色卡片，三模板，CJK + 彩色 emoji |
| 持久化 | 内存 + `data/state.json` 原子落盘 |

### 架构演进记录（重要）

**v1 设计（已废弃）** 以 Atom feed（`feeds/videos.xml`）为主数据源，理由是「无需鉴权、无配额」。
实测后发现该端点自 2025 年底起对自动化请求**间歇性 404/500**：

- 同一 URL 多次请求结果不一致（第 1/3/4 次 404，第 2 次 200）
- YouTube 官方频道、MrBeast、@ukaisaki 均出现过 404
- Google 无废弃公告，业界认为是收紧非官方数据访问

**v2 设计（当前）** 改用官方 Data API v3。代价是需要一个 API Key（免费、无需 OAuth）
且受配额约束，收益是稳定、官方、原生支持 `@handle`。
feed 降级为 legacy 兜底，WebSub 默认关闭（其推送内容正是该 feed）。

## 目录结构

```
main.py                  # 指令 / 生命周期 / 装配
metadata.yaml / requirements.txt / _conf_schema.json
CLAUDE.md / PLAN.md / API_GUIDE.md / README.md
services/
  models.py              # ChannelState / FeedEntry / LiveInfo / Notification / ChannelMeta
  data_api.py            # ★ 主数据源（API Key）+ handle 解析 + 配额计数
  feed.py                # legacy Atom feed（降级）
  page_json.py           # 网页 JSON 兜底（降级链首选）
  livebroadcasts.py      # LiveBroadcasts API（OAuth，仅自己的频道）
  scrape.py              # 无 Key 时抓频道页兜底
  oauth.py               # OAuth token 管理 + device flow
  store.py               # 订阅存储（会话隔离 + JSON 持久化）
  state_machine.py       # 直播状态机（纯逻辑）
  notifier.py            # 检测 → 渲染 → 推送
  poller.py              # asyncio 后台轮询
  websub.py / websub_server.py   # WebSub（默认关闭）
renderer.py / utils.py
tests/                   # 5 个离线测试文件 + 真实 feed / 网页 JSON fixture
scripts/diagnose.py / scripts/oauth_setup.py
```

## 数据模型

- `ChannelState`：`channel_id`(统一 UC 形式) / `channel_name` / `channel_handle` /
  `uploads_playlist_id`(缓存) / `last_video_id` / `last_live_id` / `last_status` /
  `last_live_start_at` / `last_live_end_at` / `last_live_title` / `last_live_thumbnail_url` /
  `recent_live_ids`(防 VOD 重复推送)
- 持久化：`{subscriptions: {session_id: {channel_id: meta}}, channels: {channel_id: ChannelState}}`
- 会话 key = `event.unified_msg_origin`；事件通知该频道所有订阅会话

## 直播状态机

```
上播: 有 live 且 last_status!=live 且 id 变化 → live_start
      （首次接入若已在直播 → 只记状态不推送）
下播: last_status==live 且当前无 live → live_end（时长优先用 actualEndTime）
防重复: 同一 live_id 只推一次，并写入 recent_live_ids
防 VOD 重复: find_new_videos 过滤 live_state 非空 + recent_live_ids 二次拦截
```

## 配额模型

| 调用 | 单位 | 频次 |
|---|---|---|
| `channels.list` | 1 | 仅订阅时一次（结果缓存到 ChannelState） |
| `playlistItems.list` | 1 | 每频道每轮 |
| `videos.list` | 1 | 每频道每轮 |

免费 10,000 单位/天 → 5 分钟轮询约支撑 17 个频道。配额耗尽时记错误日志并当日停止调用。

## 实现顺序与状态

1. ✅ 骨架（metadata / requirements / _conf_schema / gitignore）
2. ✅ 文档（CLAUDE.md / API_GUIDE.md）
3. ✅ models + store + utils
4. ✅ **架构重构**：新增 `data_api.py` 为主数据源，feed 降级，OAuth 转为可选
5. ✅ state_machine（含 VOD 防重复、真实起止时间）
6. ✅ renderer（PIL 三模板 + 彩色 emoji）+ notifier + poller
7. ✅ main.py 指令装配（支持 `@handle` / URL / ID）
8. ✅ scrape（无 Key 兜底）+ livebroadcasts（OAuth 可选）
9. ✅ 诊断脚本 + 测试 + 文档
10. ✅ **网页 JSON 兜底**（`page_json.py`）：配额耗尽/请求失败/无 Key 时自动降级
11. ✅ **测试指令** `/yt直播测试` `/yt视频测试`（走完整抓取→渲染→推送链路）
12. ✅ 修复：语义性错误（Key 无效/配额耗尽）不再进重试循环（原先白等约 16s/次）

## 验证状态

| 项 | 状态 |
|---|---|
| 包导入冒烟（16 模块，含 main.py） | ✅ 7/7 |
| 配置 schema（5 分块 20 项）+ metadata | ✅ |
| 状态机（静默接入/去重/换流/时长/VOD 防重/主播型频道） | ✅ 20/20 |
| 会话隔离 + 持久化 + 损坏容错 | ✅ 6/6 |
| Data API 输入解析/响应映射/快照组装（mock） | ✅ 8/8 |
| 网页 JSON 解析 + 降级链（真实页面 fixture） | ✅ 18/18 |
| **Data API 真实 Key：@handle 解析** | ✅ **实测** `@ukaisaki` → `UCNydvA0D7GSuT0c9Zs-zWdw` |
| **Data API 真实 Key：快照 + 直播状态映射** | ✅ **实测** completed 直播起止时间正确 |
| **直播流出现在上传播放列表（最关键的地基假设）** | ✅ **实测确认** 正在直播的频道首条即 `liveBroadcastContent=live`，`find_live()` 命中 |
| 真实 feed 回归（15 条真实数据 + 根元素无 UC 前缀的坑） | ✅ 实测抓取 |
| `@handle` 网页抓取兜底 | ✅ 对真实 YouTube 实测通过 |
| feed 端点不可用性 | ✅ 实测确认为间歇性失败 |
| **网页 JSON：直播检测（LIVE 角标）** | ✅ **实测** @NASA/@SkyNews/@AlJazeera 等命中，2 条直播 |
| **网页 JSON：/videos + /streams 合并快照** | ✅ **实测** 8 条（含直播），时间倒序正确 |
| **网页 JSON：观看页解析（测试命令用）** | ✅ **实测** 直播/普通视频均正确识别 |
| **降级链：无效 Key 自动改走网页兜底** | ✅ **实测** 订阅回复与 `/yt列表` 均明示降级 |
| **`/yt直播测试` `/yt视频测试` 六个用例** | ✅ **实测** 均渲染并推送真实图片（含「🧪 测试」标记） |
| 三种通知图渲染（emoji + 封面） | ✅ 人工目检（含测试标记版） |
| AstrBot 内端到端 | ⏳ **唯一剩下的主要项**（测试指令可在真实 bot 里直接验它） |
| WebSub 真实握手 | ⏳ 端点不可靠，默认关闭 |

## 测试

```bash
python tests/test_imports.py        # 导入冒烟 + schema + metadata + 就绪语义
python tests/test_state_machine.py  # 状态机 + feed 解析（真实 fixture）
python tests/test_store.py          # 会话隔离 + 持久化
python tests/test_data_api.py       # Data API（mock HTTP）
python tests/test_page_json.py      # 网页 JSON 解析 + 降级链（真实页面 fixture）
python scripts/diagnose.py @handle --api-key AIza...   # 真实环境诊断
python scripts/diagnose.py @handle --check-page         # 只体检网页兜底（无需 Key）
```

全部离线，不依赖 AstrBot 运行时与网络。
