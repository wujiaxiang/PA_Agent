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

> **当前状态：无进行中条目。** 下方最近一条已完工。

---

## ✅ 已提交（本条改动待 commit；条目已不再占用写入范围）

### 2026-10-05 · 多 Session 存储层会话（存储层 + 会话身份 + 跨品种历史）

**状态**：已完工（**未提交**，改动仍在工作区）

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

**状态**：进行中

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