# PA Agent（二次开发版）— AI K线分析辅助工具

> 本仓库是在 [原项目 `rosemarycox5334-debug/PA_Agent`](https://github.com/rosemarycox5334-debug/PA_Agent)（作者 qq564020069）基础上的 **二次开发版本**，由 [wujiaxiang](https://github.com/wujiaxiang) 维护。
>
> 本仓库**不提供原项目的用户支持、交流群与打赏入口**；如需了解原项目完整功能或支持原作者，请访问上游仓库。

---

## 本仓库相对原项目的二次开发改动

- 🌐 **新增 Web 后端**（[web/server.py](web/server.py)）：FastAPI + SSE，支持云端 / 无 GUI 环境运行，前端 100% 走后端代理
- 🎨 **决策 tab 三段式重设计**（frontend-design 方法论）：VERDICT（决策结论/蓝）+ VITALS（市场状态/青）+ EVIDENCE（详细依据/紫），补齐置信度/支撑位/阻力位等缺失字段，EVIDENCE 含 7 个独立折叠子区 + 主控按钮
- 🔧 **`.env` 环境变量系统**：三层覆盖优先级（shell env > `.env` > `config/settings.json`），启动即可用，无需前端录入配置
- 🩺 **启动健康检查 + 运行时心跳**：服务启动自动验证模型 API 与 TradingView 账号连通性；后台每 5 分钟 ping 模型 API，`/api/health` 返回 `ok`/`degraded`/`error` 状态
- 🪙 **加密货币数据源**：TradingView 直连 Gate.io 交易所，匿名模式即可拉取 BTCUSDT/ETHUSDT 等现货 K 线（无需 API key）
- 🛡️ **SSE 稳定性修复**：分析路由强制 clamp `bar_count`，异常必推 error 事件，避免 `ERR_INCOMPLETE_CHUNKED_ENCODING`
- 🔍 **结构化日志 + trace_id**：JSONL 格式日志，每条带 `trace_id`，响应头回传 `X-Trace-Id` 便于链路追踪
- 📋 **DataSource 契约校验**：`latest_snapshot(n)` 必须返回 n+1 根（1 forming + n closed），基类 `_validate_snapshot()` 强制校验
- 📡 **K 线 SSE 实时流**（[web/api/routes_bars_stream.py](web/api/routes_bars_stream.py)）：`/api/bars/stream` 推送实时 K 线更新，前端无需轮询
- 🗂️ **历史记录 API**（[web/api/routes_records.py](web/api/routes_records.py)）：`/api/records` 列表 / `/api/records/{id}` 详情 / `DELETE` 删除，支持分区布局和旧平铺布局
- 🐛 **Stage2 嵌套 schema 修复**：`_serialize_record` 将内层 `decision` 字典扁平化到顶层，解决前端"缺失字段"投诉的根因
- 📊 **自建指标管理器**（[web/static/js/indicators.js](web/static/js/indicators.js)）：TradingView 完整版需商业授权，自建 EMA 等指标管理

详细改动见 [CHANGELOG](#与上游的同步策略)。

---

## 功能概览

面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线（TradingView 含 Gate.io 等加密交易所），将结构化 K 线数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），**不连接券商、不执行下单**。

- 📈 多数据源：MT5 / TradingView（含 Gate.io 加密货币）/ yfinance / AkShare
- 🧠 两阶段 AI 分析：市场诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 🔄 增量分析与持续跟踪；决策树可视化；未来走势预期；分析后自由追问
- 📝 完整落盘：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🛡️ 可配置校验体系：JSON 校验、一致性检查、语义校验、截断修复、失败自动重试

完整功能说明见原项目文档 [`PA_Agent使用文档.md`](PA_Agent使用文档.md)，配置字段说明见 [`config/README.md`](config/README.md)。

### 文档索引

本仓库文档分四类，按使用场景查阅：

#### 用户文档（使用工具）

| 文档 | 内容 | 适用模式 |
| --- | --- | --- |
| [PA_Agent使用文档.md](PA_Agent使用文档.md) | 原项目完整功能说明（桌面 GUI 为主） | 桌面 GUI |
| [docs/获取数据功能说明.md](docs/获取数据功能说明.md) | 数据源连接、刷新机制、切换逻辑、错误处理 | 双模式 |
| [docs/图表K线与分析快照说明.md](docs/图表K线与分析快照说明.md) | K 线序号 K1…KN、forming bar、休市模式行为 | 双模式 |
| [config/README.md](config/README.md) | `settings.json` 全字段说明、安全提醒 | 双模式 |

#### 开发者文档（贡献代码）

| 文档 | 内容 |
| --- | --- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、启动方式、提交前检查、PR 建议、开发者参考约束 |
| [README.md §开发者指南](#开发者指南) | 架构概览、关键 API、数据源系统、测试分层、已知坑库 |
| [README.md §已知坑与修复记录](#已知坑与修复记录重要) | 13 条已踩过的坑，调试前必读 |

#### 项目管理文档（迭代规划与日志）

| 文档 | 内容 |
| --- | --- |
| [TODO.md](TODO.md) | 本机部署步骤、P0/P1/P2 优化项完成记录、阶段 1-9 执行顺序、后续可推进的优化 |
| [AGENTS.md](AGENTS.md) | 项目改动日志（按日期），大迭代 / 重构 / 关键 bug 修复必须追加 |

#### 二次开发改动概览

| 改动 | 文档位置 |
| --- | --- |
| Web 后端 vs 桌面 GUI 差异表 | [§Web 后端 vs 桌面 GUI 的差异](#web-后端-vs-桌面-gui-的差异重要) |
| 13 条已知坑与修复 | [§已知坑与修复记录](#已知坑与修复记录重要) |
| 上游同步策略 | [§与上游的同步策略](#与上游的同步策略) |

> **维护规则**：四类文档联动更新规则见 [AGENTS.md §维护提醒](AGENTS.md#维护提醒)。

---

## 快速开始

### 桌面 GUI（原项目主入口）

```bash
pip install -e .
python -m pa_agent.main
```

首次启动后在**设置**中填写 **Base URL**、**模型名** 与 **API Key**。

### Web 后端（本仓库二次开发新增，云端调试主入口）

```bash
# 1. 配置环境变量（复制模板并填值）
cp .env.example .env
# 编辑 .env：填入 PA_AGENT_PROVIDER_API_KEY 等

# 2. 安装依赖
pip install -e .
pip install openai  # 若报 RuntimeError: openai package is not installed

# 3. 启动
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000

# 4. 验证（启动健康检查 + 配置）
curl http://localhost:8000/api/health/check
curl http://localhost:8000/api/settings
```

环境要求：Python 3.11+；Windows/macOS/Linux。`.env` 模板与三层覆盖优先级说明见 [`.env.example`](.env.example)。

#### Windows 一键启动

Windows 用户可双击 [`start_pa_agent.bat`](start_pa_agent.bat)，脚本会自动切换到项目根目录并启动 Web 后端（默认端口 8000）。

#### Web 后端 vs 桌面 GUI 的差异（重要）

本仓库**默认推荐 Web 后端**模式，但桌面 GUI 仍可用。两者差异如下：

| 维度 | Web 后端（推荐） | 桌面 GUI |
| --- | --- | --- |
| 启动入口 | `python -m uvicorn web.server:app` 或 `start_pa_agent.bat` | `python -m pa_agent.main` |
| UI 技术栈 | FastAPI + 前端（HTML/JS/CSS，浏览器渲染） | PyQt6 |
| 配置入口 | `.env` + `config/settings.json`（三层覆盖） | 启动后 GUI 设置面板写入 `config/settings.json` |
| 实时 K 线 | SSE 推送（`/api/bars/stream`） | Qt EventBus + 定时器轮询 |
| 历史记录 | REST API `/api/records` | 本地文件浏览 |
| 适用场景 | 云端 / 无显示器 / 远程访问 | 本地桌面、需要直连 MT5 |
| Demo 模式 | ❌ 暂未实现 | ✅ 支持 |

> ⚠️ **启动方式不一致提醒**：原项目主入口是 `python -m pa_agent.main`（桌面 GUI），本仓库二次开发后**默认改为 Web 后端**启动。若你按原项目文档执行 `python -m pa_agent.main`，会启动 PyQt6 GUI（需显示器），与 Web 后端是完全独立的进程。两种模式共享 `pa_agent/` 核心层与 `config/settings.json` 配置，但运行时互不通信，**不要同时启动两个**。

---

## 开发者指南

> 面向继续开发与调试的 agent / 贡献者。

### 架构概览

项目有**双入口**，共享同一套核心层（`pa_agent/`）：

```
┌─────────────┐   ┌──────────────┐
│ 桌面 GUI    │   │ Web 后端      │
│ pa_agent/   │   │ web/server.py │
│ main.py     │   │ (FastAPI+SSE)│
└──────┬──────┘   └──────┬───────┘
       │  共享 AppContext │
       ▼                  ▼
┌─────────────────────────────────────────┐
│  pa_agent/                              │
│  ├── app_context.py  依赖装配           │
│  ├── data/           数据源抽象层        │
│  ├── ai/             两阶段分析 + 校验    │
│  ├── orchestrator/   TwoStageOrchestrator│
│  ├── config/         settings + .env     │
│  └── util/            健康检查/日志/线程  │
└─────────────────────────────────────────┘
```

- **桌面 GUI**（`pa_agent/main.py` → `pa_agent.gui.main_window`）：PyQt6，本地使用，可直连 MT5。
- **Web 后端**（[web/server.py](web/server.py)）：FastAPI + SSE，**云端调试主入口**。前端 100% 走后端代理（`/api/bars`、`/api/analyze/stream`），不直连 TradingView。

### 环境与配置

配置采用**三层覆盖优先级**（高 → 低）：

1. **shell 环境变量** `PA_AGENT_*`（CI / 容器场景）
2. **`.env` 文件**（项目根，本地开发首选，已在 `.gitignore`）
3. **`config/settings.json`**（GUI 持久化写入，前端设置面板的存储）

- 模板见 [`.env.example`](.env.example)，复制为 `.env` 后填值即可。
- 解析逻辑：[pa_agent/config/env_loader.py](pa_agent/config/env_loader.py)（轻量实现，不依赖 python-dotenv）
- 覆盖应用：[pa_agent/app_context.py](pa_agent/app_context.py) 的 `bootstrap()` 在 `load_settings()` 之后调用 `apply_env_overrides(settings)`，把 env 值合并进已加载的 Settings 对象

### 启动健康检查 + 运行时心跳

服务启动时启动后台心跳任务（每 5 分钟跑一次完整检查），结果缓存到 `app.state.last_health_report`：

- `GET /api/health`：返回缓存状态（`starting`/`ok`/`degraded`/`error`），轻量探针，不触发真实检查
- `GET /api/health/check`：同步执行完整检查（模型 API ping + 数据源 `latest_snapshot(2)`），返回各项详情与延迟
- 智能降级：当前数据源不是 `tradingview` 时，TV 失败降级为 `warning`，不阻塞启动
- 代码：[pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py)
- **坑**：健康检查拼接 URL 时会智能识别 `base_url` 是否已带 `/v1`，避免 `/v1/v1/` 重复

### 关键 API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活探针（返回缓存状态） |
| GET | `/api/health/check` | 完整健康检查（模型 API + 数据源），同步执行 |
| GET | `/api/settings` / PUT `/api/settings` | 读取/合并保存配置（API Key 自动脱敏） |
| GET | `/api/datasources` | 列出可选数据源 |
| GET | `/api/timeframes` | 当前数据源支持的周期 |
| POST | `/api/subscribe` | 切换品种/周期/数据源（TradingView 可带 `exchange` 字段，如 `GATEIO`） |
| GET | `/api/bars?count=N` | 拉取最新 N 根 K 线（前端图表用） |
| GET | `/api/bars/stream` | **SSE 实时 K 线流**（K 线收盘/新 K 线形成时推送，前端无需轮询） |
| GET | `/api/analyze/stream?bar_count=N` | **SSE 两阶段分析**（见下方注意） |
| GET | `/api/records?exchange=&symbol=&timeframe=&limit=` | 列出指定 (exchange, symbol, timeframe) 下的历史分析记录摘要 |
| GET | `/api/records/{record_id}` | 获取单条记录详情（与 SSE done 事件字段一致） |
| DELETE | `/api/records/{record_id}` | 删除单条记录 |
| POST | `/api/chat/stream` | SSE 自由对话（基于最近一次分析上下文继续提问） |

路由定义在 [web/api/](web/api/)：`routes_settings.py` / `routes_data.py` / `routes_analyze.py` / `routes_chat.py` / `routes_bars_stream.py` / `routes_records.py`。

### 数据源系统

抽象基类 [pa_agent/data/base.py](pa_agent/data/base.py)（`DataSource` + `KlineBar` dataclass），工厂在 [pa_agent/data/factory.py](pa_agent/data/factory.py)。UI 可选（`DATA_SOURCE_CHOICES`）：`mt5` / `tradingview`；仅代码支持：`yfinance` / `akshare` / `eastmoney` / `tushare`。

- **TradingView 加密货币**：`last_data_source=tradingview` + `last_tradingview_exchange=GATEIO` + `last_symbol=BTCUSDT` 即可，匿名模式无需 API key。支持的加密交易所在 [pa_agent/data/market_defaults.py](pa_agent/data/market_defaults.py) 的 `TV_CRYPTO_EXCHANGES` 定义。
- **关键约束**：`latest_snapshot(n)` 必须返回 **n+1 根**（1 forming + n closed），由基类 `_validate_snapshot()` 强制校验，不满足抛 `ValueError`。

### AI 两阶段分析流程

入口路由 [web/api/routes_analyze.py](web/api/routes_analyze.py) 的 `/api/analyze/stream`：

1. 在 `ThreadPoolExecutor` 中跑同步的 `TwoStageOrchestrator.submit()`
2. 通过 `loop.call_soon_threadsafe` 把回调事件推到 `asyncio.Queue`
3. SSE 流式推给前端：`reasoning_token` / `content_token` / `stage_prompt` / `orchestrator_event` / `strategy_files` / `done` / `error`
4. 空闲时每 0.1s 推 `heartbeat` 保活

- 编排器：[pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py)
- AI 客户端：[pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py)（OpenAI 兼容）+ [pa_agent/ai/client_factory.py](pa_agent/ai/client_factory.py)
- 提示词工程：[prompt_engineering/](prompt_engineering/)（Brooks 价格行为决策树）

### 测试与代码风格

```bash
pytest -q                    # 全量
pytest tests/unit -q         # 仅单元测试（快）
pytest -m "not live" -q      # 跳过需要真实 API key 的 live 测试
pip install -e ".[dev]"      # 安装 pytest/ruff/black/hypothesis

ruff check . && black --check .   # line-length 100, py311
make lint / make test / make run
```

测试分层：`tests/unit` / `tests/property`（hypothesis）/ `tests/integration` / `tests/e2e`。`live` 标记的测试必须通过环境变量配置 API key，**绝不读取** `config/settings.json`。

### 已知坑与修复记录（重要）

后续 agent 调试时请先了解这些已踩过的坑：

1. **`ERR_INCOMPLETE_CHUNKED_ENCODING`（SSE 被截断）**
   - 根因 A：`_run_analysis` 的 `build_display_frame` 调用在 try 块外，异常逃出线程池，event_queue 永远收不到 done/error → 已把整个函数体包进 try/except，保证推 error 事件。
   - 根因 B：`bar_count=100` 分析耗时过长，超过 TRAE 预览网关 SSE 超时 → 路由已强制 clamp 前端传值到 `settings.general.analysis_bar_count` 上限。

2. **`build_display_frame()` TypeError**：`now_ms` 是 keyword-only 参数，必须 `now_ms=now_ms` 传，不能当位置参数。

3. **OpenRouter 返回 404 HTML**：`.env` 的 `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`。

4. **`400 output tokens too large`**：`_PRACTICAL_UNLIMITED_MAX_TOKENS` 原值 524288 会让 free 模型直接 400，已降到 32768；可通过 `provider.max_output_tokens` 覆盖。

5. **`check_model_api` TypeError**：`stream_chat()` 返回 `AIReply` 不可迭代，健康检查必须用 `client.chat()` 直接调用。

6. **TradingView 加密品种**：`normalize_gold_symbol_for_kind` 旧逻辑会把 `BTCUSDT` 这种 crypto symbol 强制改回 `XAUUSD`，已修复为识别 crypto 时保持原样。

7. **休市后 K1 序号不显示**（2026-07-20 修复）：
   - 现象：NVDA 美股收盘后，最后一根 K 线是 close bar，但前端不显示 `#1` 序号。
   - 根因：[bar_close_wait.py](pa_agent/data/bar_close_wait.py) 的 `seconds_until_bar_closes` 用 `elapsed_ms % duration_ms` 取模算法，对已收盘 bar 仍返回正值（"到下一次开盘的剩余时间"），导致 bar 被错误标记为 `seq=0, closed=False`（forming bar），前端 `setSeqMarkers` 跳过 `seq <= 0` 的 bar。
   - 修复：`seconds_until_bar_closes` 在 `now_ms >= ts_open_ms + duration_ms` 时直接返回 0（绝对时间判断）；[tradingview.py](pa_agent/data/tradingview.py) 的 `_latest_snapshot_inner` 在休市模式下 `bars[0]` 用 `seq=1, closed=True`；[base.py](pa_agent/data/base.py) 的 `_validate_snapshot` 支持两种模式（正常 n+1 根含 forming / 休市 n 根全 closed）。

8. **tvDatafeed 未安装 / NumPy CPU 不兼容**：
   - 现象：切换港交所（HKEX）等非加密交易所时 `No module named 'tvDatafeed'`；或 `numpy` 报 `X86_V2` CPU 指令集错误。
   - 修复：`pip install git+https://github.com/rongardF/tvdatafeed.git`；NumPy 不兼容时降级到 `numpy==1.26.4`。A股数据源（AkShare）无需 tvDatafeed，可作 fallback。

9. **K 线时间轴 8h 偏移**（[tradingview.py](pa_agent/data/tradingview.py) `_row_ts_ms`）：
   - 现象：TradingView 返回的 naive DatetimeIndex 被当作 UTC，导致 K 线显示在未来 8 小时。
   - 根因：`datetime_to_ts_ms` 把 naive Timestamp 当 UTC 处理，但 tvDatafeed 实际返回的是交易所本地时间（服务器时区，如 UTC+8）。
   - 修复：`_row_ts_ms` 先把 naive Timestamp 本地化到服务器时区再转 UTC。

10. **主副图时间轴不对齐**（[indicators.js](web/static/js/indicators.js)）：
    - 现象：添加 MACD/RSI 副图后，滚动/缩放主图时副图时间轴不同步。
    - 根因：原同步用逻辑索引（数据点位置），但主副图数据点数量不同导致索引错位。
    - 修复：改用时间戳范围同步（`getVisibleRange()` / `setVisibleRange()` / `subscribeVisibleTimeRangeChange`）。

11. **SSE 倒计时不显示 / 显示异常**：
    - 现象 1：选 1h 周期时持续追踪倒计时显示 10000+ 秒。根因：`onopen` 立即设置 `sseLastBarUpdateTs`，但 `sseNextCloseTs` 尚未被第一个 `bar_update` 事件更新。修复：`onopen` 仅启动定时器，不设置 `sseLastBarUpdateTs`；新增 sanity check（`remaining > tfSecs` 则丢弃）。
    - 现象 2：切换周期后倒计时消失。根因：`applySubscribe` 切换周期后未刷新 `currentSettings`。修复：`tfSecs` 优先从 `#ds-timeframe` 读取，`applySubscribe` 成功后调用 `loadSettings()`。
    - 现象 3：`_compute_next_close_ts()` 用 `ts_open + duration` 产生时区偏移。修复：改用 `elapsed % duration` 取模算法（与 `seconds_until_bar_closes` 对齐）。
    - 现象 4：SSE 连接失败降级到轮询后无倒计时。修复：新增 `fetchAndUpdateNextCloseTs()` 低频拉取 `/api/bars/next-close`。

12. **分析失败后进度条跳到完成**：
    - 现象：阶段一分析失败后，进度条仍推进到 100%。
    - 根因：后端在分析失败时仍推送 `done` 事件，前端无条件调用 `setFlowBarStep(6)`。
    - 修复：新增 `.flow-step.failed` CSS 类与 `setFlowBarFailed(failedStep)`；`done` 事件检查 `evt.record.exception`，有异常时保留失败状态 10 秒。

13. **`_validate_snapshot` 休市模式契约**（2026-07-20 修复，配合坑 7）：
    - 正常模式：返回 `n+1` 根，`bars[0].closed == False, seq == 0`（forming bar）。
    - 休市模式：返回 `n` 根，全部 `closed == True`，`bars[0].seq == 1`（无 forming bar）。
    - [snapshot.py](pa_agent/data/snapshot.py) 的 `has_forming_bar_at_head` 已正确处理两种模式，消费方应使用该函数判断，**不要**硬编码 `bars[0].closed == False`。

### 目录结构速查

```
pa_agent/
├── main.py / app_context.py     入口与依赖装配
├── config/    settings.py / env_loader.py / paths.py
├── data/      数据源（mt5/tradingview/yfinance/akshare/eastmoney/tushare/factory/snapshot）
├── ai/        deepseek_client / prompt_assembler / router / json_validator / 校验
├── orchestrator/  two_stage.py 两阶段编排
├── gui/       PyQt6 桌面端（云端不使用）
├── util/      startup_health_check / logging / threading / mask_secret
web/
├── server.py  FastAPI 入口
├── api/       routes_{settings,data,analyze,chat,bars_stream,records}.py
├── static/    前端（index.html / js / css）
│   └── js/    app.js / api.js / chart.js / indicators.js（自建指标管理器）
├── bridge/    AsyncEventBus（替代 Qt EventBus）
├── Dockerfile / docker-compose.yml  Web 后端容器化
prompt_engineering/  Brooks 价格行为提示词库（市场诊断/决策树/各形态策略）
tests/         unit / property / integration / e2e
```

---

## 与上游的同步策略

本仓库 fork 自 [rosemarycox5334-debug/PA_Agent](https://github.com/rosemarycox5334-debug/PA_Agent)。为持续获得上游 bug 修复与新功能，建议定期同步：

### 配置 upstream 远程

```bash
# 一次性配置：添加上游远程
git remote add upstream https://github.com/rosemarycox5334-debug/PA_Agent.git
git remote -v   # 确认 origin + upstream 都在
```

### 同步上游改动的工作流

```bash
# 1. 拉取上游最新提交（不影响本地工作区）
git fetch upstream

# 2. 切到 main 并确保干净
git checkout main
git status

# 3. 合并上游 main（保留本仓库的二次开发提交）
git merge upstream/main
# 若冲突：优先保留本仓库 web/、.env.example、pa_agent/config/env_loader.py、
#         pa_agent/util/startup_health_check.py 等二次开发新增文件；
#         核心层 pa_agent/ai、pa_agent/data、prompt_engineering 等以上游为准。

# 4. 推送到自己的 origin
git push origin main
```

### 同步注意事项

- **二次开发独有文件**（避免被上游覆盖）：
  - `web/` 整个目录（Web 后端 + 前端）
  - `.env` / `.env.example`（环境变量系统）
  - `pa_agent/config/env_loader.py`（轻量 .env 解析器）
  - `pa_agent/util/startup_health_check.py`（启动健康检查）
  - `.github/workflows/sync-upstream.yml`（上游同步自动化）
- **核心层以上游为准**：`pa_agent/ai/`、`pa_agent/orchestrator/`、`prompt_engineering/`、`pa_agent/data/base.py` 等跟随上游更新，本仓库只做最小必要补丁。
- **冲突高发点**：`pa_agent/data/factory.py`（新增了 env_loader 集成）、`pa_agent/data/market_defaults.py`（Gate.io 加密交易所列表）、`pa_agent/config/settings.py`（新增了 `max_output_tokens` 字段）、`pa_agent/data/tradingview.py`（WebSocket headers monkey-patch + n+1 契约修复）。
- **推荐用 PR 而非直接 merge 到 main**：重大上游更新可先建分支 `sync/upstream-YYYYMMDD`，提 PR review 后再合并，避免直接污染 main。

### 也可用 GitHub 官方同步

如果本仓库与上游无复杂冲突，可直接在 GitHub 网页点 "Sync fork" 按钮，或：

```bash
gh repo sync wujiaxiang/PA_Agent --source rosemarycox5334-debug/PA_Agent
```

> ⚠️ `gh repo sync` 会快进合并，若本仓库有与上游冲突的提交会失败，此时需走上面的手动 merge 流程。

---

## 致谢

本项目基于 [rosemarycox5334-debug/PA_Agent](https://github.com/rosemarycox5334-debug/PA_Agent)（原作者 qq564020069）二次开发，遵循其 [AGPL-3.0](LICENSE) 协议。感谢原作者的开源贡献。

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。
