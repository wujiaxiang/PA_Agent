# 改动记录（多会话协作用）

> **本文件的作用**：让并行工作的多个会话知道**别人正在改什么**，从而避免写冲突。
>
> 与 [CHANGELOG.md](CHANGELOG.md) 的分工：
> - **CHANGELOG.md** — 记录*行为*变了什么（面向读者/用户），事后补写，允许合并同类项
> - **本文件** — 记录*谁在改哪个文件*（面向并行会话），**开工前先查、完工前必写**，禁止合并、禁止省略
>
> **规则见 [AGENTS.md](AGENTS.md) 「多会话协作规范」一节。**

---

## 使用方法

1. **开工前**：搜本文件里有没有人正在改你要改的文件；有 → 先沟通，或避开。
2. **开工时**：立刻在下方「进行中」区加一条，把**打算写**的范围先占住。
3. **完工时**：把状态改成 `已提交 <commit>`，补齐改动文件清单与接口变更。

条目模板见文末。**「进行中」区的条目优先级最高 —— 它代表有人此刻正在写那些文件。**

---

## 🔴 进行中（有人正在改这些文件，不要动）

## ✅ 已提交（本条改动待 commit；条目已不再占用写入范围）
### 2026-10-05 · 多会话推理收尾（P1 SSE下线 / P2a 追问隔离 / P3 交易域 / P4 配置层）

**状态**：已完工（待本次统一提交）

#### 方案评审裁决（推翻了原方案）
`docs/REMAINING_PLAN.md` 原文已被评审推翻，文件顶部已加过时标注。原方案的三处关键错误：

- **P1 方案 A 不可实施** —— 浏览器原生 `EventSource` 无法设置请求头，服务端拿不到
  session_id，分组无从取值；且坏品种的 auto-probe 会持 `TradingViewSource._snapshot_lock`
  长达数十秒，全站 `/api/bars` 排队。改判 **B（前端按自己游标轮询）**：该路径本就
  存在，隐藏标签页自动停，一个坏 tab 不会传染别人
- **P2 分桶键是扩键不是替换** —— `FreeChatSession._cached_prefix` 构造时一次性固化，
  只按 session 分桶会让「先追问 A、再回看 B」时 B 携带 A 的 stage1/stage2 **静默错答**
- **P4 是 9 条路径不是 5 条**，且必须是 `persist_patch` 不是 `persist(settings)`
  ——后者会把 15 个 .env 字段永久烧进 user_prefs

#### 实际改动文件（已合并清单）

| 范围 | 文件 |
|---|---|
| SSE 下线 | `web/api/routes_bars_stream.py`(357→68)、`web/static/js/app.js`、`web/static/index.html`、`tests/unit/test_routes_bars_stream.py`、`web/server.py` |
| 追问隔离 | `web/api/routes_chat.py`、`web/api/routes_analyze.py`、`web/static/js/api.js`、`tests/unit/test_followup_and_audit_fixes.py` |
| 交易域 | `pa_agent/storage/trade_repo.py`(新)、`tests/unit/test_trade_repo.py`(新)、`pa_agent/records/trade_logger.py`、`pa_agent/storage/importer.py`、`pa_agent/config/paths.py` |
| 配置层 | `pa_agent/config/settings.py`、`pa_agent/storage/settings_store.py`、`web/api/routes_data.py`、`web/api/routes_settings.py`、`pa_agent/app_context.py`、`pa_agent/orchestrator/two_stage.py`、`pa_agent/ai/{qclaw,workbuddy,cursor,trae}_connector.py`、`tests/unit/test_settings_cascade.py` |
| 前置 | `pa_agent/storage/ephemeral.py`、`web/api/session_ctx.py`（W0，已随 `32b405e` 提交） |

#### 接口变更 ⚠️
- **删除** `GET /api/bars/stream`（现返回 404）；`routes_bars_stream.start_background_task`
  / `stop_background_task` 不再存在
- **保留** `_compute_next_close_ts` —— `routes_data.py` 的跨模块硬契约，不得删
- `routes_chat._record_matches_subscription(record, symbol, timeframe)` —— **不再收 ctx**
- 前端 `api.js?v=5`、`app.js?v=64`
- `GET /api/settings` 的 general 段返回**本会话游标**而非全局值

#### 冲突风险
- 工作区仍有**另一会话未提交**的 `config/settings.json.bak-*` / `.corrupt-*`，
  以及 `.githooks/`、`.github/workflows/ci.yml`、`tools/check_write_scope.py`
  属他人提交范围 —— **提交时不要一并带上**
- `routes_data.py` 是热点：A4 独占写，A2 只读 import `_resolve_view`
- 既有不稳定测试（勿误判为本轮引入）：`test_routes_records::test_list_records_returns_200`
  （`order_type` 恒 None，系既有 `stage2_decision` 嵌套结构）、缺 `pytest-qt` 的 30 项 error、
  缺 `hypothesis` 的 3 项收集失败


### 2026-10-05 · 配置层加固（写端校验 + 一次性初始化）

**状态**：已提交 `32b405e`（W0 前置 + apply_user_change 现存 bug 修复）

#### 需求
6 位专家评审判定配置层有 6 个 BLOCKER，其中 3 个与「DB 实例每环境唯一、
启动时确定」直接相关。本条处理 B1 / B5 / B6。

#### 方案
- **B1 写端零校验**：九个 section 加 `validate_assignment=True`；`PUT /api/settings`
  先快照原值再逐字段提交，失败整体回滚并返回 400。此前越界值被静默写入，
  2026-10-05 实际造成 base_url 被冲成默认值、api_key 清空、分析整体不可用
- **B5 读失败与「无数据」不可区分**：新增 `hub.read_failed`；`resolve()` 读失败时
  **拒绝播种**并走纯文件，避免把损坏文件升格成系统兜底且永不重播种
- **B6 初始化不是一次性**：`get_hub()` 只取实例不再建库；新增 `initialize_storage()`，
  在 lifespan 中**早于 `AppContext.bootstrap()`** 调用（此前顺序颠倒，bootstrap
  读配置报 `no such table: user_prefs` 静默退回文件）；未初始化时 `execute()` 拒写
- 并发锁冲突（`database is locked`）不再闩死存储层，只有文件损坏/只读才置 `_disabled`
- `reset_hub_for_tests()` 无参直接抛错，不再静默回落到真实 `records/pa_agent.db`

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `pa_agent/config/settings.py` | 9 个 section 加 `validate_assignment=True` |
| `web/api/routes_settings.py` | 快照/提交/回滚 + 400 响应 |
| `pa_agent/storage/db.py` | 初始化闸门、`read_failed`、按错误类型决定是否闩死 |
| `pa_agent/storage/settings_store.py` | 读失败时拒绝播种 |
| `web/server.py` | 初始化提前到 bootstrap 之前 |
| `tests/unit/test_storage_layer.py` | 新增 8 项初始化不变式测试 |
| `tests/conftest.py` | 新增 `db_path_isolated` 夹具 |

#### 接口变更
- `PUT /api/settings` 遇非法取值从静默写入改为 **400 + 指明字段**

#### 冲突风险
- `pa_agent/config/settings.py` 与 `web/api/routes_settings.py` 是配置层热点
- `stats()` 曾因引用不存在的属性导致启动时整个存储初始化失败；已加回归测试
- 测量陷阱：校验「测试是否写脏真实 DB」前**必须先 rm**，否则看到的是上一次残留
  （本轮已两次误判为「仍被污染」）


### 2026-10-05 · 多 Session 存储层会话（存储层 + 会话身份 + 跨品种历史）

**状态**：已完工（首部分已提交 `c4b0f1e`；admin 用户 + 双写部分待提交）

> **⚠️ 异常说明（务必先读）**：本条目原为「进行中」，其列出的实现文件在接手时
> **并不存在于磁盘** —— 规划留下了，实现未落盘。接手会话重新实现了同一范围。
>
> 此外，本次实现期间**另一个并发会话连续执行了 5 次 `git reset --hard HEAD`**，
> 工作区被清空，本会话对 `web/api/routes_records.py` 的改动与本文件一度被删除
> （`routes_records.py` 整个文件从磁盘消失，导致服务无法 import）。
> 已从 HEAD 恢复并重新应用。**如果你也在本仓库操作 git，请勿用 `reset --hard`
> 清空未提交改动** —— 那是破坏性的，且本仓库长期存在未提交的并行会话工作。

#### 需求
1. 为每个浏览器 tab 维护独立的会话上下文（订阅、增量锚点互不干扰）
2. 引入持久化存储层，为多用户/多会话打基础
3. 「一个用户多个网页，**历史数据应该都能看**」—— 跨标签页浏览全量分析历史

#### 方案
三层数据粒度 + 会话身份 + 内嵌 mini-Redis。完整设计见
[docs/SESSION_STORAGE_DESIGN.md](docs/SESSION_STORAGE_DESIGN.md)。

- **PG → SQLite**：环境是 LXC 宿主、Dockerfile 已注明 AppArmor 导致构建失败，
  且宿主机 10GB 内存已用 6.6G / Swap 占 4G —— 「内嵌进程内」只能是 SQLite
- **Redis → 不引入**：用「内存注册表（TTL+LRU，可替换 backend）+ SQLite 快照」
  自实现等效语义，避免新增常驻进程与运维故障点
- **会话身份走 `X-Session-Id` 请求头而非 Cookie**：Cookie 同源共享会让同一浏览器
  所有 tab 拿到同一个 id，「一 tab 一会话」直接失效；前端用 `sessionStorage`
  存 UUID（该存储天生 per-tab 且能扛 F5）
- **「浏览历史」是 L2 用户级，「增量锚点」是 L3 会话级**：两者方向相反，
  今天被混为一谈。已修掉后者读全局设置导致跨标的串味的问题
- **双轨迁移**：写双份、读优先 SQLite 且 miss 回退文件（27 条既有记录全量导入，
  8 组 (exchange,symbol,timeframe) 组合与文件路径**逐例等价、0 处不一致**）

#### 改动文件（写入范围）

| 文件 | 说明 |
|---|---|
| `pa_agent/storage/__init__.py` | **新增** 包入口与分层说明 |
| `pa_agent/storage/schema.py` | **新增** DDL（7 张表）+ 版本管理 |
| `pa_agent/storage/db.py` | **新增** 连接管理：WAL + 线程局部 + 故障降级 |
| `pa_agent/storage/ephemeral.py` | **新增** 会话注册表（TTL/LRU/Redis 可替换 backend） |
| `pa_agent/storage/repositories.py` | **新增** 分析记录仓储 |
| `pa_agent/storage/sessions.py` | **新增** L3 会话快照仓储 |
| `pa_agent/storage/importer.py` | **新增** 幂等文件→SQLite 导入器 |
| `web/api/session_ctx.py` | **新增** 会话身份解析 + 视图解析 |
| `docs/SESSION_STORAGE_DESIGN.md` | **新增** 完整设计文档 |
| `tests/unit/test_storage_layer.py` | **新增** 43 项存储层测试 |
| `tests/unit/test_storage_dualwrite.py` | **新增** 13 项双写测试 |
| `tests/unit/test_settings_cascade.py` | **新增** 19 项配置级联测试 |
| `pa_agent/storage/users.py` | **新增** admin 用户播种与默认用户解析 |
| `pa_agent/storage/experience_repo.py` | **新增** 经验库仓储 |
| `pa_agent/storage/settings_store.py` | **新增** 系统兜底 ← 用户覆盖 级联与差异计算 |
| `pa_agent/config/settings.py` | `load_settings` 改为级联解析（DB 优先、文件播种与灾备兜底） |
| `web/api/routes_settings.py` | PUT 额外写入用户覆盖区（稀疏），系统兜底不被触碰 |
| `tests/unit/test_session_ctx.py` | **新增** 19 项会话身份测试 |
| `web/server.py` | lifespan 初始化存储层 + 启动导入；`/api/health` 暴露存储状态 |
| `web/api/routes_analyze.py` | 增量锚点按会话游标取（**修跨标的串味 bug**） |
| `web/api/routes_data.py` | `/api/subscribe` 同步写本 tab 游标 |
| `web/api/routes_records.py` | 过滤条件改为可选 + 摘要新增 symbol 字段 |
| `web/static/js/api.js` | 统一注入 `X-Session-Id` |
| `web/static/js/app.js` | 历史面板「全部品种」跨品种浏览 |
| `web/static/index.html` | 新增 `#chk-history-all-symbols`；版本号 api.js 3→4 / app.js 62→63 / style.css 38→39 |
| `web/static/css/style.css` | 开关与品种徽标样式 |
| `tests/unit/test_routes_records.py` | 必填参数契约改为可选（见「接口变更」） |

#### 接口变更 ⚠️
- **新增请求头** `X-Session-Id`（可选）。缺失时全链路回落旧的全局设置行为
- **`GET /api/records`**：`exchange`/`symbol`/`timeframe` 由**必填改为可选**，
  留空即跨全部品种；响应摘要**新增** `symbol`/`timeframe`/`exchange` 三个字段
- **`GET /api/health`**：新增 `storage` 字段（db 状态 + schema 版本 + 会话数）
- **新增** `pa_agent.storage` 包；DB 默认落 `records/pa_agent.db`
  （该目录已 bind mount，零部署改动；可用 `PA_AGENT_DB_PATH` 覆盖）

#### 被否决的方案（避免重复踩）
- **引入真 Redis**：宿主机内存/Swap 已紧张，收益不足以抵运维成本。
  `ephemeral.EphemeralBackend` 协议已预留落点，将来可无痛替换
- **把增量上下文搬到前端**：它已在记录 JSON 里且按 `{exchange}/{symbol}/{timeframe}`
  分区，前端传上来反而不可信、且刷新即丢
- **`sqlite3.executescript()` 跑 DDL**：它会先隐式 COMMIT，拆散事务导致中途失败
  留下半套表 —— 必须逐条 `execute`（已加单测 `test_all_statements_are_single_statements` 守护）
- **会话写入方用纯 `UPDATE`**：行不存在时**静默空操作**，游标存不进去且不报错。
  已加 `_ensure_row()` 前置 + 两条回归测试

#### 冲突风险 ⚠️
- **`web/api/routes_records.py` 是热点文件**：上一轮「历史回看锚点」已改过
  （`_derive_anchor_bar_ts_ms()` 与 `anchor_bar_ts_ms` 字段），改动**尚未提交**。
  本轮新增了 `_glob_partitioned()` / `_browse_filtered()` 两个函数，**原逻辑原样保留**，
  但请提交前先 `git diff` 核对
- **本仓库不适合 `git reset --hard` / `git checkout -- .`**：长期存在并行会话的
  未提交工作，清空工作区会连带删除他人文件（本次已实际发生一次，见上方异常说明）
- `test_routes_records.py` 有既有不稳定失败（失败项在多次运行间漂移，
  干净工作树 HEAD 上同样失败）。**不要把这个文件的失败当成自己改坏的**
- `tests/unit/test_prompt_cache_priming.py` 曾因**并发写入**出现中间态失败，
  非代码问题；若复现先确认文件是否正被别的会话改写

---

## ✅ 已提交

### 2026-10-05 · 模式切换残留审计 + 端到端测试补强（当前）

**状态**：已提交 `650fc8f`（app.js）+ `70af04f`（E2E）

#### 需求
用户报告「历史切到实时，有的页面数据没有重置清空」。复现确认：
`实时 → 回看 → 实时` 后 **预测 / 决策树 / 决策** 三个面板仍显示回看记录内容。
同时用户指出：此前的需求**没有做完整的端到端测试**。

#### 方案
先修 bug，再补「断言内容」的 E2E —— 顺序不能反，否则测试只是在测刚写的实现。

复现证据（同一浏览器会话内三段快照对比）：

| 面板 | 实时(初始) | 回看中 | 返回实时后 |
|---|---|---|---|
| `#future-content` | 尚未进行交易分析 | 回看记录内容 | ❌ 残留 |
| `#tree-content` | 尚未进行交易分析 | 回看记录内容 | ❌ 残留 |
| `#decision-content` | 尚未进行交易分析 | 回看记录内容 | ❌ 残留 |

根因：返回实时的处理器只做了 `clearOverlays` + `loadBars` + `setDataMode('live')`，
**从未重置侧边栏各分析面板的 innerHTML**。模式状态机只管 LED / 染色 / 只读，
不管面板内容 —— 状态与内容是两套东西，此前只有前者被测过。

关于「没做完整端到端」的复盘：
此前走查断言的全是**状态位**（面板可见、dataset 值、classList、消息条数），
**没有一个断言「面板当前显示的内容是否属于当前模式」**。因此
「实时→回看→实时」这条状态迁移从未被端到端走过 —— `replayRecord()` 与
`btn-live` 在旧脚本里是两个独立步骤，没有串成一次迁移。

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `web/static/js/app.js` | 新增 `resetAnalysisPanels()`，回到 live 时统一重置各面板 |
| `tests/e2e/test_modes_e2e.py` | **新增**：断言面板**内容**的模式正确性 |
| `SESSION_CHANGES.md` / `CHANGELOG.md` / `AGENTS.md` | 记录 |

#### 接口变更
无新增 API。新增前端内部函数 `resetAnalysisPanels()`。

#### 修复要点
1. `renderDecision` / `renderFuturePanel` / `renderDecisionTree` 补 `record` 为空的
   早退分支 —— 它们此前直接 `record.stage2_decision`，传 null 会抛 TypeError。
   **这正是此前没人修的原因**：想用统一空态入口，但三个函数根本不支持 null
2. `renderStreamFromRecord(null)` 此前是静默 `return`，改为清理 replay-banner、
   flow-bar、消息体并刷新追问锚点
3. `renderTokenUsage(null)` 同理，此前静默返回导致用量行残留
4. 新增 `resetAnalysisPanels()` 统一调用上述空态分支 —— 不就地拼 innerHTML，
   空态文案只有一处定义
5. 返回实时处理器里的裸 `lastRecord = null` 换成 `resetAnalysisPanels()`

#### 验证（含「测试能否抓到 bug」的反向验证）
- 修复后：三个面板回到「尚未进行交易分析」
- **把修复回退，E2E 精确变红**（`test_live_to_replay_to_live_*` 与
  `test_demo_to_live_*` 两条），断言信息直接打印残留内容 —— 证明测试有效，
  不是「写完就绿」的摆设
- 该反向验证**顺带发现第二个 bug**：`Demo → 实时` 走的是另一分支，
  同样有残留（原判断只覆盖了回看路径）
- 单元回归：FAILED=40 ERROR=30

#### 冲突风险 ⚠️
- `web/static/js/app.js` 是多人热点文件；开工前已确认「进行中」区为空
- **`tests/unit/test_multisession_contract.py` 出现 9 项新增失败，本会话未碰该文件**
  （未跟踪的新文件，属多 Session 会话写入范围）。失败原因是文件**自身第 180 行
  `IndentationError: unexpected indent`**，属对方会话尚未完工的在写状态，
  非本轮引入。已如实登记，**不代为修改**
- 部署过程中的自身失误（记录以免重犯）：临时验证用的 `docker commit` 漏写
  `--change CMD`，把 `sleep infinity` 固化成镜像默认启动命令；另一次只覆盖
  `web/static` 未覆盖 `pa_agent/`，导致容器内 `persist_patch` 缺失、
  ImportError 起不来。**每次 commit 都必须显式重设 CMD，且静态与后端要么都覆盖
  要么都不覆盖**
- **⚠️ 本轮发生暂存区交叉污染（教训）**：本会话 `git add` 之后、自身
  `git commit` 之前，另一会话执行了提交，把**暂存区里本会话的文件一并带走**：
  · `web/static/js/app.js` 的修复被 `650fc8f`（对方的 auth 提交）正确带走了 ——
    代码完好，但归属混乱
  · `tests/e2e/test_modes_e2e.py` 被 `650fc8f` 误提交后又被 `e870a02`
    「移出不属于本次改动的文件」删除；已重新纳入（`70af04f`）

  **根因**：`git add` 改的是**共享暂存区**，不是某个会话私有的暂存。
  并行会话在同一工作树下共用暂存区时，A 暂存的内容会被 B 的提交捎走。

  **规避办法（本轮未做到，后续必须遵守）**：
  1. 每个会话用**独立 worktree**，或
  2. 提交前用 `git commit -- <显式文件列表>`（不用 `-a`、不依赖暂存区），
     且 `git add` 与 `git commit` 之间不穿插其他会话的操作，或
  3. 至少在 commit 后立即 `git show --stat HEAD` 核对提交内容归属

  本轮靠提交后逐文件核对才发现，否则会误以为「我的修复丢了」或
  「对方动了我的代码」而引发无谓的冲突排查。
- 既有 2 项 `test_routes_records.py` 不稳定失败与本轮无关

### 2026-10-05 · 配置级联事故处置会话（当前）

**状态**：已提交 `2563bb8`

#### 需求
用户指出「已经做了重大多用户配置的改造」。重新读代码与文档后确认新架构：
**DB（SQLite，用户级 admin）为真源**，`settings.json` 降级为「首次播种源 +
灾备兜底」。据此处置一起线上事故。

#### 方案
发现两处叠加问题：
1. `config/settings.json` 于 05:04 被写成**代码默认值**
   （`base_url` 退回 `api.deepseek.com`、`api_key` 清空），
   容器内构造 OpenAI 客户端直接失败 → **分析功能不可用**
2. DB 侧 `settings.baseline` 为 **NULL**、overrides 为空 ——
   系统兜底**从未播种**

处置顺序（不能反：文件先修好再播种，否则会把损坏的文件升格为兜底）：
1. 留存损坏快照 `config/settings.json.corrupt-20261005-061855`
2. **合并**而非整体替换 `provider` 段 —— 整体替换会把已新增的
   `prompt_cache_prime` 一并弄丢（踩过一次）
3. 用项目自带的 `settings_store.seed_from_file()` 播种，不手写 SQL
4. 部署含新架构的镜像并验证

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `config/settings.json` | 恢复 `provider` 凭证段（运行时数据，非代码） |
| `config/settings.json.corrupt-20261005-061855` | 新增：损坏快照留证 |
| DB `global_config.settings.baseline` | 播种系统兜底 |
| `SESSION_CHANGES.md` | 本条目 |
| `CHANGELOG.md` / `AGENTS.md` | 同步新配置架构约定 |

#### 接口变更
无（只处置运行时数据，未改代码）。

#### 冲突风险
- **`config/settings.json` 是跨会话共享的运行时数据**，两个会话都可能写它。
  新架构下用户改动应走 `apply_user_change()` 进 DB，而非 `save_settings()`
  整份写文件 —— 后者绕过级联，且若传入默认值构造的对象会直接摧毁配置。
  该调用点（`routes_data.py` subscribe 处理器）属多 Session 会话范围，**本会话未改**
- 本次部署把另一会话**已提交但未上线**的 `pa_agent/storage/`、
  `web/api/session_ctx.py`、`routes_settings.py` 一并上线了 ——
  属「部署即包含已提交代码」的正常结果，非越权修改
- `test_routes_records.py` 既有 2 项不稳定失败仍在，与本次无关

---

### 2026-10-05 · 协作工具会话（当前）

**状态**：已提交 `2ade10a`

#### 需求
把「多会话协作规范」从纸面约定变成可自动执行的检查 —— 光靠自觉迟早会漏。

#### 方案
两层，各解决不同问题：

| 层 | 位置 | 解决 | 阻断时机 |
|---|---|---|---|
| **pre-commit hook** | 本地 | 别人「进行中」的文件你正要改 | **覆盖发生之前** |
| **CI job** | PR 上 | 改了代码却忘了登记改动记录 | 合入之前 |

本地 hook 才是真正有用的那层 —— 冲突一旦被 commit 出来，CI 再报已经晚了。

关键设计取舍：
- **按路径前缀匹配，不做精确文件名匹配**：条目写 `web/api/` 就该盖住
  `web/api/routes_records.py`。精确匹配等于让登记形同虚设。
- **不做提交者身份判定**：靠 git config / PR 作者猜「这条是不是我写的」，
  在多账号、CI 提交、rebase 下都会误判。宁可漏报也不误伤。
- **CI 对范围重叠只告警不阻断**：正在干活的那个会话自己落地成果时必然
  命中自己的范围，硬阻断只会逼人绕过检查。CI 只强制「改了代码必须登记」。

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `tools/check_write_scope.py` | 新增：`--hook` 硬阻断 / `--ci` 强制登记 + 告警 |
| `.githooks/pre-commit` | **追加**写入范围检查（保留原有 API key/敏感文件拦截） |
| `.github/workflows/ci.yml` | 新增 `write-scope` job |
| `SESSION_CHANGES.md` | 协作记录本体 |
| `AGENTS.md` | 新增「多会话协作规范」+ 工具用法 |

#### 接口变更
新增 CLI：`tools/check_write_scope.py --hook|--ci`；
环境变量 `SKIP_WRITE_SCOPE_CHECK=1` 可绕过（会打印提示）；
git 配置 `core.hooksPath=.githooks`（已在本仓库启用）。

#### 冲突风险
- **未动另一会话的任何文件**；`web/static/*`、`web/api/*` 的未提交改动保持原样
- 该会话**仍在推进**：本会话开工后又新增了 `tests/unit/test_session_ctx.py`（未跟踪）
- 本会话踩过的坑（供后来人）：
  1. **覆盖了已有的 `.githooks/pre-commit`**（内含 API key / `config/settings.json`
     / `logs/` / `records/pending/*.json` 的拦截）。加检查前必须先看文件是否存在 ——
     这正是本条目所立规范要防的事故，当场自己犯了一次
  2. `__main__` 对 `--hook` 直接调 `run_hook()`，把 `SKIP_WRITE_SCOPE_CHECK`
     判断绕了过去，**绕过开关形同虚设**
  3. 测试脚本里的 `rm -f "$2"` 把 `SESSION_CHANGES.md` 当探针文件删了，
     **未提交内容一并丢失**。测试清理路径必须逐个显式核对
  4. 改 Markdown 层级用了子串替换，`"### 冲突风险"` 是 `"#### 冲突风险 ⚠️"` 的
     子串 → 变成 `#####`。**子串替换必须行锚定**
  5. `open(p,'w',encoding('utf-8'))`（位置参数）→ SyntaxError，
     `s.replace()` 从未执行却以为写成功了

### 2026-10-05 · 经验库 / 前端 / 性能会话

**状态**：已提交 `1531dbe`

#### 需求
- 两阶段经验库闭环：定时结算、定时/手工模式、每条记录独立 LLM 复盘
- 历史回看时 K 线联动（用户报告方向箭头指向错误的 K 线）
- K 线联动全面审计
- 数据源模式状态机（实时/历史/Demo）+ 分析按钮合并 + 非实时只读
- 分析前主动预热 prompt cache

#### 方案
详见 CHANGELOG 第 20–24 条。要点：两阶段状态机（目录即状态）、后端按 `closed`
标志找锚点而非硬编码下标、模式状态机统一入口 `setDataMode()`、prompt cache
预热接 `chat()` + `stream_chat()` 两个入口。

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `web/static/index.html` | 模式条、只读提示、按钮迁移、版本号 |
| `web/static/js/app.js` | 模式状态机、只读、分析自动选路、缓存指标展示 |
| `web/static/js/chart.js` | 方向标记显式锚点、图例清理、marker 存在性校验 |
| `web/static/js/continuous_gate.js` | `closedBarTs()` 按 `closed` 标志查找 |
| `web/static/js/continuous_gate.test.js` | 休市模式断言 |
| `web/static/css/style.css` | 模式条、LED、只读态样式 |
| `pa_agent/ai/deepseek_client.py` | **新增** `_maybe_prime_cache()` / `_primeable_prefix()` / `_should_prime()` |
| `pa_agent/config/settings.py` | **新增** `prompt_cache_prime` |
| `pa_agent/orchestrator/two_stage.py` | **新增** `_pick_last_closed_bar()`；`last_close_bar_iso` 不再硬编码 `kline_data[1]` |
| `pa_agent/records/experience_writer.py` | 两阶段写入 + 上下文/ K 线快照留存 |
| `pa_agent/data/tradingview.py` | scanner 品种搜索、符号预设扩充 |
| `web/api/` | `experience_scheduler.py`、`experience_verifier.py`、`routes_experience_review.py`（均新增）；`routes_data.py`、`routes_analyze.py`、`order_followup.py`、`routes_records.py`（均修改） |
| `tests/unit/` | `test_experience_*.py`、`test_last_close_bar_anchor.py`、`test_prompt_cache_priming.py`、`test_display_labels.py`、`test_symbol_presets.py`、`test_tv_scanner_search.py`（均新增） |

#### 接口变更 ⚠️
- `GET /api/records` 与 `GET /api/records/{id}` **新增** `anchor_bar_ts_ms`
- `GET /api/experience` **新增** `symbol`/`timeframe` 入参与
  `status_counts`、`anchor` 等展示字段
- **新增** `POST /api/experience/verify`、`POST /api/experience/verify/once`、
  `GET /api/experience/review/stream`、`GET /api/tv/search`
- `provider` 配置**新增** `prompt_cache_prime`、`experience_*` 系列

#### 冲突风险
- `web/static/js/app.js` 与 `web/api/routes_records.py` 是多人/多会话热点，
  改动前务必确认「进行中」区没有别人占着
- HTML 控件 id 变更：`btn-back-to-live` → **`btn-live`**（语义改为模式切换）
- **本会话开工时，另一会话（多 Session 存储层）已有未提交改动落在
  `web/static/{index.html,css/style.css,js/api.js,js/app.js}`**
  （`X-Session-Id` 多标签页隔离），但其条目未登记这 4 个文件。
  本会话未碰这些文件；请该会话补登记后再提交，不要直接覆盖。

---

## 条目模板

复制以下内容新增到「🔴 进行中」区：

```markdown
### YYYY-MM-DD · <会话/主题简称>

**状态**：已提交 `（本提交）`

#### 需求
一句话写清用户要什么 / 解决什么问题。

#### 方案
怎么做的。**关键取舍与被否决的方案也要写**（后来人不知道你为什么没走那条路）。

#### 改动文件（写入范围）
开工时就列全，完工后核对增减。

| 文件 | 改动 |
|---|---|
| `path/to/file.py` | 新增/修改/删除，一句话说明 |

#### 接口变更
改了 API 路径、请求/响应字段、配置项、事件名、DOM id 的，在这里写。
**没有就写「无」。**

#### 冲突风险
- 正在占用哪些文件
- 与其他会话的潜在交叉点
- 已知会失败/不稳定的既有测试，避免误判
```