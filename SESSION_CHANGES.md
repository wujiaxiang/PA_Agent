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

### 2026-10-05 · 多 Session 存储层会话（另一会话）

**状态**：进行中（工作区未提交）

### 需求
为每个浏览器 tab 维护独立的会话上下文（订阅、分析状态互不干扰）。

### 方案
三层存储：会话快照 + 内存热层 + 游标。切品种时不再只改全局订阅，而是写本 tab 的游标。

### 改动文件（写入范围）

| 文件 | 说明 |
|---|---|
| `pa_agent/storage/` | **新增目录**（未跟踪） |
| `web/api/session_ctx.py` | **新增文件**（未跟踪） |
| `docs/SESSION_STORAGE_DESIGN.md` | **新增文件**（未跟踪） |
| `tests/unit/test_storage_layer.py` | **新增文件**（未跟踪） |
| `web/api/routes_analyze.py` | 已修改 |
| `web/api/routes_data.py` | 已修改 |
| `web/api/routes_records.py` | 已修改 |
| `web/server.py` | 已修改 |
| `tests/unit/test_routes_records.py` | 已修改 |

### 冲突风险 ⚠️
- **`web/api/routes_records.py` 是热点文件**：本会话在「历史回看锚点」一轮已改过
  （新增 `_derive_anchor_bar_ts_ms()` 与 `anchor_bar_ts_ms` 字段，已提交）。
  该文件的改动**尚未提交**，直接改会覆盖对方未提交的工作。
- `web/api/routes_analyze.py` / `routes_data.py` / `web/server.py` 同理。
- `test_routes_records.py` 本就有 2 项**既有不稳定失败**（失败项在多次运行间漂移，
  干净工作树的 HEAD 上同样失败）。**不要把这个文件的失败当成自己改坏的。**

---

## ✅ 已提交

### 2026-10-05 · 经验库 / 前端 / 性能会话

**状态**：已提交 `1531dbe`

### 需求
- 两阶段经验库闭环：定时结算、定时/手工模式、每条记录独立 LLM 复盘
- 历史回看时 K 线联动（用户报告方向箭头指向错误的 K 线）
- K 线联动全面审计
- 数据源模式状态机（实时/历史/Demo）+ 分析按钮合并 + 非实时只读
- 分析前主动预热 prompt cache

### 方案
详见 CHANGELOG 第 20–24 条。要点：两阶段状态机（目录即状态）、后端按 `closed`
标志找锚点而非硬编码下标、模式状态机统一入口 `setDataMode()`、prompt cache
预热接 `chat()` + `stream_chat()` 两个入口。

### 改动文件（写入范围）

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

### 接口变更 ⚠️
- `GET /api/records` 与 `GET /api/records/{id}` **新增** `anchor_bar_ts_ms`
- `GET /api/experience` **新增** `symbol`/`timeframe` 入参与
  `status_counts`、`anchor` 等展示字段
- **新增** `POST /api/experience/verify`、`POST /api/experience/verify/once`、
  `GET /api/experience/review/stream`、`GET /api/tv/search`
- `provider` 配置**新增** `prompt_cache_prime`、`experience_*` 系列

### 冲突风险
- `web/static/js/app.js` 与 `web/api/routes_records.py` 是多人/多会话热点，
  改动前务必确认「进行中」区没有别人占着
- HTML 控件 id 变更：`btn-back-to-live` → **`btn-live`**（语义改为模式切换）

---

## 条目模板

复制以下内容新增到「🔴 进行中」区：

```markdown
### YYYY-MM-DD · <会话/主题简称>

**状态**：进行中

### 需求
一句话写清用户要什么 / 解决什么问题。

### 方案
怎么做的。**关键取舍与被否决的方案也要写**（后来人不知道你为什么没走那条路）。

### 改动文件（写入范围）
开工时就列全，完工后核对增减。

| 文件 | 改动 |
|---|---|
| `path/to/file.py` | 新增/修改/删除，一句话说明 |

### 接口变更
改了 API 路径、请求/响应字段、配置项、事件名、DOM id 的，在这里写。
**没有就写「无」。**

### 冲突风险
- 正在占用哪些文件
- 与其他会话的潜在交叉点
- 已知会失败/不稳定的既有测试，避免误判
```