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

### 2026-10-05 · 收尾：user_id 统一为 admin + 会话收口补漏

**状态**：已完工

#### 需求
把剩余收尾项做掉：DDL 里不存在的 `default` 用户、`_chat_sessions` 无上限、
过时 TTL 注释、握手 ack 窗口。

#### 改动文件
`pa_agent/storage/schema.py`、`pa_agent/storage/db.py`、`pa_agent/storage/schema.sql`、
`web/api/routes_chat.py`、`web/api/storage_gc.py`、`web/server.py`、
`web/static/js/api.js`、`web/static/index.html`、`docs/SESSION_STORAGE_DESIGN.md`、
`tests/unit/test_user_id_ddl.py`（新）

#### 关键决策
- **DDL 的 `DEFAULT 'default'` 必须删掉**：`users` 表里只有 `admin`，
  `default` 是个**不存在的用户**。写进去不报错，按用户过滤时永远查不到。
  SQLite 无「删列默认值」语句，只能重建表 ⇒ 索引 DDL 从 `sqlite_master`
  **动态抓取**后补回（硬编码会在将来新增索引时静默丢掉它，退化成全表扫）
- **存量 `'default'` 行不改写成 `'admin'`**：把「身份未知」谎报成管理员比查不到
  更糟 —— 用户会看到一批不属于��己的历史。改写反而制造「查得到但归属错误」
- **`_chat_sessions` 补 LRU 128**：TTL 管的是「空闲多久」，而键是
  `sid|record|k/n` ⇒ 单 tab 不停换 record_id 就能在 TTL 内堆出任意多条
- **握手 ack 窗口外保留 + bornAt 仲裁**：⚠️ 这条**未能复现原报告的 bug**
  （修复前后桩测四档行为完全一致且都正确），按防御性改动记账，不按 bug fix 记账

#### 迁移已在生产库执行
`.bak/pa_agent.db.<时间戳>` 先备份（独立目录，**不用同名前缀**，否则会被
`records/pa_agent.db*` 命中）。7 张表迁移后：行数零变化、索引零丢失、
默认值全部为 None、schema_version 不变。

#### 冲突风险
- `pa_agent/storage/schema.py` 本轮含**另一会话的未提交改动**（+32 行），
  我只改 7 处 DEFAULT 与新增 `create_table_ddl`，未触碰其余
- 存量 `chat_turns` 的旧键（前端此前拼的 `{symbol}_{tf}_{iso}`）仍是孤儿，
  新键格式为 `{symbol}|{tf}|{ms}`，`chat_repo.clear_thread` 可清


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

### 2026-10-05 · 修 CI 基线误报 + 基线来源错误（当前）

**状态**：已提交 `（本提交）`

#### 需求
用户要求修 72 项单测失败。排查后发现主体不是普通 bug，而是**别人未提交的重构**，
遂改为修自己上一轮引入的两个问题。

#### 问题 1：`ci_diff_baseline.py` 会把应用日志误当成测试失败
2026-10-05 实测：原正则 `^(FAILED|ERROR)\s+(\S+)` 全文扫描，会命中
**Captured log 段**里的应用日志行：

    ERROR    web.api.routes_data:routes_data.py:451 experience browse: store unreadable

被当成名为 `web.api.routes_data:routes_data.py:451` 的测试 → 报成「新增回归」。
应用日志里出现 ERROR 是**正常运行的一部分**，与测试成败无关。

修复：**只解析 `short test summary info` 段**，并要求条目形如 `路径.py::用例`
（双重约束）。反向验证：注入伪造日志行后解析结果不变（0 新增）。

#### 问题 2（更严重）：基线是从**脏工作区**生成的
原基线记录 102 项。用 `git archive HEAD` 导出纯净树复测 —— **HEAD 上只有 32 项失败**。
多出的 **40 项全部来自别人未提交的经验库重构**，被我当成「已知失败」记了进去。

后果：**CI 对这 40 项真实回归保持绿灯**，基线反而替未完成的代码背了书。
这与 AGENTS.md 警告的「假绿灯」是同一类错误，只是方向相反 —— 我上一轮
修的正是它，却用同样的方式重新造了一遍。

修复：基线按**纯净 HEAD 重新生成**（32 项），并在文件头写明生成方式必须是
`git archive HEAD` 导出干净树，禁止用脏工作区。

双向验证：
| 场景 | 期望 | 实测 |
|---|---|---|
| 纯净 HEAD | exit 0 | ✅ exit 0 |
| 脏工作区（含别人 40 项） | exit 1 并点名 | ✅ exit 1，精确报出 40 项 |

#### 未能处理的部分（已查明，非本会话范围）
72 项中的 **40 项属于别人未提交的经验库重构**，本会话**未修改**其代码：
- `pa_agent/storage/experience_repo.py` 把 `upsert_entry(file_path=)` 改成必填
  `entry_id=`，**该重构完全在未提交工作区**（`git log -S` 无对应 commit）
- `pa_agent/storage/importer.py:148` 仍传 `file_path=` —— **真实生产 bug**，
  经 `web/server.py:119 → import_all()` 每次启动触发，被宽 `except` 吞成一行
  WARNING，导致 `import_trade_records()` 永不执行
- `ExperienceWriter.save()` 返回类型 `Path → str`、`_read_top5_from_files` 已删、
  `EXPERIENCE_DIR` 已删、`schema.sql` 未重新生成
- 另一会话诊断期间仍在实时改动这些文件（mtime 14:46–14:49，失败数 69→53），
  `test_storage_dualwrite.py` 近 15 分钟内有写入

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `tools/ci_diff_baseline.py` | 只解析 summary 段 + 校验条目形态 |
| `tests/ci/baseline_failures.txt` | 按纯净 HEAD 重建（102 → 32 项） |
| `tests/unit/test_mt5_clock_skew.py` | MT5 缺依赖时逐用例 skip |
| `tests/unit/test_cursor_sdk_client.py` | cursor_sdk 缺依赖时 skip |
| `tools/stage2_raw_sample.txt` + `.gitignore` | 恢复被 ignore 掉的测试夹具 |
| `SESSION_CHANGES.md` / `CHANGELOG.md` / `AGENTS.md` | 记录 |

#### 接口变更
无。`upsert_entry` 的 `file_path → entry_id` 变更属**别人未提交的重构**，
本会话未采纳、未代为提交。

#### 冲突风险 ⚠️
- **`tests/unit/test_storage_dualwrite.py` 正被另一会话写入**，本会话一度改过
  9 处 `file_path → entry_id`，已确认不再触碰，避免互相覆盖
- `tools/stage2_raw_sample.txt` 原被 `.gitignore` 排除，导致
  `test_json_validator.py` 在 CI 上必然失败；已解除忽略并加注释放说明它是夹具
- 剩余 32 项基线中 `test_mt5_clock_skew` 的 2 项在 CI（Linux）上会 skip，
  基线留着无害（skip 不计入 FAILED）

### 2026-10-05 · CI 只跑单元测试（当前）

**状态**：进行中

#### 需求
用户要求：CI **只跑单元测试**，暂不接集成测试。

#### 背景（为什么不能直接加一句 pytest）
现有 `test` job 名为 test，实际只做 `pip install -e ".[dev]"` + `import pa_agent`，
**一条测试都没跑**，是「假绿灯」。本轮要让它真跑起来。

但直接跑会红。本地现状：
- `FAILED=78 ERROR=30`
- **30 个 ERROR 全是缺 `pytest-qt`**（5 个文件）→ 纯环境问题，装上就好
- **78 个 FAILED 主体是经验库 / 存储层**
  （`test_experience_library_loop` 13、`test_chart_fit_view` 11、
  `test_storage_dualwrite` 10、`test_experience_two_stage` 9 …），
  这些属**其他会话正在改的代码**，非本会话范围
- 另有 `MetaTrader5` 仅 `win32` 可用，Linux CI 上必然缺

#### 方案
1. `pyproject.toml` 补 **`[dev]` extra**（当前**根本不存在**，`pip install -e ".[dev]"`
   只会 warning 后继续 —— 这正是「test job 不跑测试却显示绿」的根因之一）
2. `test` job 改跑 `pytest tests/unit`，runner 换 ubuntu（贴近实际 Docker 部署）
3. **基线文件 + 只对新增失败红**：提交一份已知失败清单，CI 比对出「新失败」才失败。
   否则这些存量失败会让 CI 永久红，等于把 CI 关掉
4. 补 GitHub 惯例四件套：`concurrency`（取消过期 run）、`timeout-minutes`、
   `cache: pip`、`permissions: contents: read`

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | 新增 `[dev]` extra（pytest / pytest-qt / playwright 等） |
| `.github/workflows/ci.yml` | `test` job 真跑单元测试；补惯例配置 |
| `tests/ci/baseline_failures.txt` | **新增**：已知失败基线 |
| `tools/ci_diff_baseline.py` | **新增**：比对基线、只对新增失败红 |
| `SESSION_CHANGES.md` / `CHANGELOG.md` / `AGENTS.md` | 记录 |

#### 接口变更
无 API 变更。新增环境变量（CI 用）：`CI=1`、`PA_AGENT_CI_BASELINE`。

#### 冲突风险
- **78 个存量失败主要来自经验库/存储层，属其他会话写入范围**，本会话**不修改其代码**，
  只通过基线文件记录为「已知失败」。对方修好后需更新基线（否则漏报回归）
- `test_multisession_contract.py` 曾因对方文件自身 `IndentationError` 报 9 项，
  属对方在写状态
- 基线文件会随存量失败变化而需更新 —— 这是**有意的成本**：
  比「CI 永久红」或「CI 名存实亡」都划算


### 2026-10-05 · 三路并行收尾：LLM 开关 UI / 配置按请求解析 / 三个缺口

**状态**：已完工（待本次统一提交）

#### 需求
补齐三块：LLM 配置开关的前端、配置按请求解析（多用户前置）、
DELETE 删库行 / 追问落库 / 路径统一。

#### 改动文件（三路写集两两不相交）
| 范围 | 文件 |
|---|---|
| 前端开关 | `web/static/js/app.js`、`web/static/index.html`、`web/static/css/style.css` |
| 按请求解析 | `pa_agent/storage/settings_store.py`、`pa_agent/config/settings.py`、`pa_agent/app_context.py`、`web/api/auth_ctx.py`、`web/api/routes_settings.py`、`web/server.py` |
| 三个缺口 | `web/api/routes_records.py`、`web/api/routes_chat.py`、`pa_agent/storage/chat_repo.py`(新)、`pa_agent/storage/sessions.py`、`pa_agent/ai/decision_continuity.py` |
| 新测试 | `tests/unit/test_chat_repo.py`(新)、`tests/unit/test_record_delete_db_row.py`(新)、`tests/unit/test_trade_repo.py` |

#### 关键决策
- **`ctx.settings` 由 dataclass 字段改成 property**，读请求内按用户解析、请求外回落启动默认；
  靠 **FastAPI 中间件**绑定而非逐个路由改 —— 全仓 135 处读取一行未改即自动按用户取对值
- **缓存 dict 而非 Settings 实例**：PUT 是就地 setattr，共享实例必然「A 改完 B 跟着变」。
  失效用**代数计数器**而非 TTL，写配置后下一次请求立即生效
- **DELETE 先删文件后删库行**：要让「索引指向真实存在的文件」在任何失败点成立。
  **键必须是 `target.stem`**（upsert 用 Path.stem 作主键），用 URL 里的 record_id 会静默删不掉
- **chat_turns 内存热态 / DB 持久态，DB 优先**；DB 不得成为写盘前置条件

#### 接口变更 ⚠️
- `provider.use_custom`（默认 False）：为假时用户对 provider 的覆盖**整段作废**
- `POST /api/settings/promote-default`：显式提升为出厂默认，需 `{"confirm": true}`
- `DELETE /api/records/{id}` 响应新增 `db_deleted: bool`（向后兼容）
- `AppContext.__init__` 的 `settings=` 关键字改为 `_default_settings=`（property 占用该名）
- 前端 `app.js?v=66`、`style.css?v=41`

#### 冲突风险
- **本轮发生过生产事故**：子代理的验证脚本调了 `promote-default`，把 `config/settings.json`
  写成了代码默认值（api_key 空、base_url 冲成 api.deepseek.com）。已用 DB baseline
  合并恢复并校验一致。教训：`PA_AGENT_DB_PATH` **只隔离 DB，不隔离 settings.json**
- 既有失败基线 **31 failed / 30 errors**（30 个 error 全是缺 pytest-qt 的 qtbot fixture）
- `test_experience_two_stage` / `test_experience_library_loop` 有跨文件 flaky，单独跑通过
- **另有两个并发会话**在改经验库读端（`experience_reader/writer/repo`、`two_stage.py`、
  `docs/EXPERIENCE_READ_CUTOVER.md`）与 E2E 接入 —— 提交时务必用显式文件列表，别捎走


### 2026-10-05 · 配置按请求解析（多用户前置）

**状态**：已完工（未 commit，按要求不提交）

#### 需求
让配置**按请求**解析。级联本身早已支持多用户（`resolve(userA)=250` /
`resolve(userB)=999`），但 `app_context.py` 只在启动时解析一次存进全局
`ctx.settings`，所有请求共用一份 —— 两个用户登录后看到同一份配置。

#### 方案
**把「请求内 / 请求外」的差别收敛到一个 ContextVar**，而不是让每个路由各自解析。

- `pa_agent/app_context.py`：`settings` 由 dataclass 字段改为 **property**，
  读 → 请求内返回本请求用户的配置，请求外返回启动默认解析；
  写 → 请求内只改本请求副本（否则 A 读一次设置页就把全局默认换成 A 的）。
- `web/server.py`：中间件每个请求解析一次并绑定，请求结束还原。
- 读取侧**一行未改**：135 处 `ctx.settings`（分析主流程 / 结算 / 取数）自动按用户取对值。

**为什么不逐个路由改**：那是一次横跨半个仓库的重构，且**漏一处就静默串用户**
（漏改的代码继续用全局配置，不报错）。中间件是唯一需要知道「这次是谁」的地方。

**性能**：首版实测比改造前**更慢**（0.107ms vs 0.051ms）—— `apply_env_overrides`
41µs/次（每次都重读 `.env`）。改为把 .env 覆盖**烘进缓存**，热路径回到 0.041ms、
**0 次 DB 读 + 0 次文件读**。缓存的是 dict 不是 `Settings`：后者会被
`PUT /api/settings` 的就地 `setattr` 改到，共享实例必然「A 改完 B 跟着变」。

**失效**：代数计数器（`save_baseline` / `save_overrides` / `clear_overrides` 自增）
→ 写配置后**下一次请求立即生效**，不等 TTL；30s TTL 只兜「绕过本模块直接改
SQLite / 另一个进程改同一文件」。写配置是人工点击级低频，全量作废可忽略，
且**结构性杜绝**跨用户陈旧读。

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `pa_agent/storage/settings_store.py` | 代数计数器 + 按用户解析缓存 + `resolve_cached`；三个写入口自增代数；`promote_to_baseline` 增 `user_id` |
| `pa_agent/config/settings.py` | `load_settings(user_id=)`；播种源改**惰性**读（命中缓存时零文件 IO）；新增 `resolve_effective_settings`（含 .env 覆盖的生效配置缓存） |
| `pa_agent/app_context.py` | `settings` 改 property + ContextVar 绑定/还原 |
| `web/api/auth_ctx.py` | 新增 `resolve_request_settings(request)` |
| `web/server.py` | 新增 `bind_user_settings_middleware` |
| `web/api/routes_settings.py` | GET 按 `current_user_id` 解析；PUT 写**发起者**的覆盖区；promote 传发起者 |
| `web/api/session_ctx.py` | **未改动**（列入写集但无需改：`resolve_view` 读 `ctx.settings` 已自动按用户） |

#### 接口变更
- `AppContext.__init__` 的 `settings=` 关键字改为 `_default_settings=`。
  **读写真接口 `ctx.settings` 不变**；全仓仅 `bootstrap()` 用过该关键字。
- `promote_to_baseline(current, user_id=None)`：新增可选第二参（默认行为不变）。
  传发起者后清的是**他的**覆盖区 —— 单机下 admin 与发起者恒等，多用户下否则
  发起者的覆盖会继续遮蔽新出厂默认。
- 无新增 API / 请求头 / 配置项。

#### 验证
- 必跑集 137 项全绿
- `tests/unit tests/property`：以**同一工作树、仅回退本会话 7 个文件**为基线
  （38 failed）对比 → 见下「冲突风险」
- 真实库未被污染：`stat -c "%Y:%s"` 前后一致（`1791203390:16035840`）

#### 冲突风险 ⚠️
- **`test_chat_repo.py` 3 项「新增失败」不属本会话**：`pa_agent/storage/chat_repo.py`
  与 `tests/unit/test_chat_repo.py` 都是**未跟踪新文件**，mtime 在本会话执行期间
  仍在变（12:30 / 12:34）；单跑 13 passed（顺序依赖 flaky）。二者与本会话 7 个文件
  **零耦合**（不 import settings / config / app_context / auth_ctx）。
- `web/api/routes_records.py`、`web/api/routes_data.py` 在本会话期间被其它会话改动，
  一度处于**无法 import**（缺 `import os` / IndentationError）状态，
  `web.server` 因此暂时无法导入。基线与对比均加 `--ignore=tests/unit/test_routes_records.py`
- **本会话的验证脚本曾误写生产 `config/settings.json`**：`promote-default` 走
  `promote_to_baseline`，它写的是 `SETTINGS_JSON_PATH`，而临时 `PA_AGENT_DB_PATH`
  **只隔离 DB、不隔离该文件**。教训见 CHANGELOG。
  当前 `settings.json` 的 `provider.api_key` 为空、`base_url` 与 `analysis_bar_count`
  已与 DB baseline 不符（并发会话亦在写该文件）。**运行期不受影响**（真源是
  `global_config.settings.baseline`，实测完好：api_key 有效、base_url 正确）；
  仅播种源/灾备副本失真。修复 = 用 DB baseline 覆盖写回该文件，**未擅自执行**。

---

### 2026-10-05 · E2E 接入 CI（当前）

**状态**：已提交 `（本提交）`

#### 方案（施工中发现的硬约束）

自播种原本写成「测试进程直接写文件 + `upsert_record()` 写 DB」，本地能跑通，
但**在容器化部署下必然错位**，连续踩了三个坑：

1. `experience_loaded` 写成 `False`，schema 要求 `list` → 记录被列表接口静默过滤
2. 只写文件不写 DB → API 查不到。`routes_records._list_records()` 明确
   「**数据库是唯一真源**，磁盘文件只是补种来源」
3. 补上 `upsert_record()` 仍查不到 → DB 校验 `f.resolve().relative_to(RECORDS_DIR)`
   失败。**根因是宿主机与容器是两套文件系统视图**：
   宿主 `/root/shared-workspace/PA_Agent/records/pending`
   vs 容器 `/app/records/pending`（同一目录、不同挂载点）
   同一 inode，但 `relative_to` 必然失败

**结论**：任何「测试进程自己写库」的方案在容器下都不成立。必须让**服务端自己
播种** —— 由服务端进程写盘、用它自己的 `RECORDS_DIR`、走它自己的 `upsert_record`。
因此新增一个**默认关闭、仅显式开启时可用**的 E2E 播种端点：
`POST /api/records/__e2e_seed__`，仅在 `PA_AGENT_E2E=1` 时**注册路由**
（生产环境该路由根本不存在，404）。播种走服务端自己的 `RECORDS_DIR` +
自己的 `upsert_record`，并用服务端同一套 `AnalysisRecord` schema 先自检 ——
不过就 500，免得「播种成功但被列表接口静默过滤」，在 CI 上表现为
「什么都没测到」。

#### 验证
- 播种端点返回 `{"seeded": true, "record_id": ...}`，
  `GET /api/records` 随即能查到（`order_type=限价单`），详情端点可取
- 清空全部 e2e 记录后重跑 E2E：**5 passed / 63s**，核心用例未 skip
- `pytest tests/e2e` 与 `pytest tests/unit` 分开跑；CI 新增独立 `e2e` job，
  与 `test` 并行、失败必须红，另配「失败时打印 server.log」便于定位

#### 踩坑记录（4 个，全是「本地能跑、容器里必错」类）
1. `experience_loaded` 写成 `False`，schema 要求 `list` → 被列表接口静默过滤
2. 只写文件不写 DB → API 查不到（`_list_records` 明确「数据库是唯一真源」）
3. 补上 `upsert_record()` 仍查不到 → `f.resolve().relative_to(RECORDS_DIR)`
   失败。**根因：宿主机与容器是两套文件系统视图**（`/root/.../records/pending`
   vs `/app/records/pending`，同一 inode、不同挂载点）。任何「测试进程自己写库」
   的方案在容器下都不成立
4. `_safe_path_segment()` 返回 `str` 不是 `Path`，`a / b` 报
   `TypeError: unsupported operand type(s) for /: 'str' and 'str'`

#### 冲突风险 ⚠️
- `.github/workflows/ci.yml` 是共享基础设施，开工前已确认「进行中」区无占用
- **本会话部署时两次把另一会话的半成品代码打进镜像**：一次 `routes_data.py`
  的 `IndentationError`（编辑中间态被我 tar 快照捕获），一次容器 `pa_agent/`
  未同步导致 `chat_repo` 缺失。教训：**部署前必须对 `web/` 与 `pa_agent/` 全量
  语法自检**，且两个目录要么都覆盖、要么都不覆盖
- `/api/health` 目前为 `degraded`：`model_api` 报 401（网关 API key 无效），
  属配置层问题，非本会话引入

#### 需求
用户批准把 `tests/e2e/` 接入 CI。此前 E2E 只在本地跑，`ci.yml` 中 0 处引用 ——
测试写了但不接流水线，等于没写。

#### 方案
（施工中）

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `tests/e2e/test_modes_e2e.py` | BASE_URL 走环境变量；空库时请求服务端播种 |
| `web/api/routes_records.py` | **新增** E2E 播种端点（默认关闭，需环境变量开启） |
| `tests/e2e/conftest.py` | 新增：服务就绪等待 |
| `.github/workflows/ci.yml` | 新增 `e2e` job |
| `SESSION_CHANGES.md` / `CHANGELOG.md` / `AGENTS.md` | 记录 |

#### 接口变更
新增端点 `POST /api/records/__e2e_seed__`，**仅在 `PA_AGENT_E2E=1` 时注册**
（默认不注册，生产环境不存在该路由）。
新增环境变量：`PA_AGENT_E2E_BASE_URL`（E2E 目标地址）、`PA_AGENT_E2E=1`（服务端开关）。

#### 冲突风险
- `.github/workflows/ci.yml` 是共享基础设施，开工前已确认「进行中」区无占用
- 关键风险：CI 是**空库**，`_records()` 返回空会让核心测试**静默 skip**，
  CI 全绿但什么都没测。必须让 E2E 在无历史记录时自播种


### 2026-10-05 · 经验库读端切库（P-1 → P4）

**状态**：P-1/P-2/P0/P1/P2/P3/P4 **全部已完工**（待 commit）。经验库已废弃文件布局，库为唯一真源

#### 需求
身份/鉴权层已由 `650fc8f` 落地（`storage/auth.py` + `web/api/auth_ctx.py`），但经验库
**读端一行未动**：三条读路径（提示词注入 / 浏览 API / 复盘取档）全在文件系统。
`experience_repo.list_entries`/`get_entry`/`count_by_status` 除测试外零生产调用者，
`current_user_id` 零调用者 —— `experience_entries` 表自建起是死索引，`user_id` 列从未生效。
用户要求：做计划 → 评审 → 再并线开发。

#### 方案
照搬 `docs/SESSION_STORAGE_DESIGN.md` §7 的 **C 阶段切读**（SQLite 读，miss 回退文件），
不做 D 阶段（SQLite only，风险高收益零）。分 P-1…P4，见方案文档。

**v1 经两轮独立评审后被推翻**，5 处错误（详见方案 §7），关键三条：
- **`hub.read_failed` 是进程级共享标志**（`_read_error` 非 thread-local，且成功读会清它），
  v1 的降级契约在并发下是死代码 → 已实测复现，提为 P-1
- **漏了 reader 异常会连同已付费的 Stage 1 一起毁掉整次分析**
  （`two_stage.py:678` 无 try/except，下一次落盘在 703）→ P-1
- **P1 伪代码绕过 `read_top5()`**，会**静默废掉 12 个 Mock 测试点**（不红，只是没测）

**被否决的方案**：
- **不做「顺手补写」** —— 读路径不写。会顶住 `max_workers=2` 的分析池
  （`busy_timeout=5000`），且破坏 AGENTS「写入方唯一入口」；回填交给 importer
- **DB 优先逻辑放进 `read_top5()` 内部**，不改 `read_for_stage2` —— 保住 14 个测试点
- **`entry_id` 改取值而非改主键** —— SQLite 不能 ALTER 主键，改取值零迁移成本
- **复盘回写进条目文件，不新增只存 DB 的表** —— 保住「文件是权威副本」与可追溯性，
  且免 schema 迁移（`db.migrate()` 每次启动无条件跑 `all_statements()`）
- **不上 PostgreSQL** —— `db.py:20` 是 `sqlite3`，依赖 `ON CONFLICT`/`AUTOINCREMENT`/
  `threading.local`
- **打分逻辑不搬 SQL** —— `_score` 只有方向 +2、形态交集两项，数据量百级，留在 Python

#### 改动文件（写入范围）

| 文件 | 改动 |
|---|---|
| `docs/EXPERIENCE_READ_CUTOVER.md` | 新增（方案文档，含评审推翻记录） |
| `pa_agent/storage/db.py` | **P-1** `_read_error` 移入 `threading.local` |
| `pa_agent/storage/experience_repo.py` | **P-1** 查询显式回传失败标志；**P-2** 增 `statuses`、`entry_id` 按 user 分区 |
| `pa_agent/orchestrator/two_stage.py` | **P-1** reader 包 try/except + `save_partial` 前移 |
| `web/api/session_ctx.py` | **P0** `bind_session` 改走 `current_user_id(request)` |
| `pa_agent/records/schema.py` | **P0** `RecordMeta` 增 `user_id` |
| `pa_agent/records/experience_writer.py` | **P0** 全链透传 `user_id`（含 `save()` 补镜像）；**P3** `attach_review()` |
| `pa_agent/records/experience_reader.py` | **P1** DB 优先（塞进 `read_top5`）+ 文件回落 + direction 枚举修复 + 死参数清理 |
| `web/api/routes_data.py` | **P2** 浏览 API 切读 repo |
| `web/api/routes_experience_review.py` | **P3** `_find_entry` 切 `get_entry`；复盘结果回写 |
| `web/api/order_followup.py` | **P0** 派发线程时透传 `user_id` |
| `web/api/experience_verifier.py` | **P0** 结算用记录自身的 `user_id` |
| `pa_agent/ai/prompt_assembler.py` | **P4** `_render_experience` 字段感知渲染 |
| `tests/unit/test_storage_*.py`、`test_experience_*.py`、`tests/integration/`、`tests/e2e/` | 新增/调整测试 |
| `SESSION_CHANGES.md`、`CHANGELOG.md`、`AGENTS.md` | 记录 |

#### 接口变更
- **P0**：`/api/records` 响应中 legacy 记录的 `meta.user_id` 为 `""`（多一个 key）
- **P-2**：`experience_repo.list_entries` 增 `statuses: list[str] | None`（`status` 保留兼容）
- **P-1**：`hub.read_failed` 语义改为线程局部（调用方行为不变，但并发下不再互相清标志）
- 其余待实施后补

#### 冲突风险
- `web/api/routes_data.py` 近期被其它会话改过（mtime 10-05 10:13），但其改动
  不在经验库段内；提交时按 AGENTS「`git commit -- <显式文件列表>`」避免捎走他人改动
- 既有失败/不稳定测试：**无**。开工前基线
  `test_experience_library_loop / two_stage / watch_integrity / storage_dualwrite /
  session_ctx / auth_placeholder` = **107 passed, 1 skipped**
- **已知既有缺陷（本次只登记不修）**：`spawn_post_order_followup` 每次分析新建线程，
  经双写链每次 +1 条永不关闭的 SQLite 连接（实测 20 线程 → 21 连接）。P1 放大泄漏面
- 本机 8000 端口是 **docker-proxy**（非本仓库进程），404；最终验收需另行起 uvicorn

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