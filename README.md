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
