# PA Agent（二次开发版）— AI K线分析辅助工具

> 本仓库是在 [原项目 `rosemarycox5334-debug/PA_Agent`](https://github.com/rosemarycox5334-debug/PA_Agent)（作者 qq564020069）基础上的 **二次开发版本**，由 [wujiaxiang](https://github.com/wujiaxiang) 维护。
>
> 本仓库**不提供原项目的用户支持、交流群与打赏入口**；如需了解原项目完整功能或支持原作者，请访问上游仓库。

---

## 本仓库相对原项目的二次开发改动

- 🌐 **新增 Web 后端**（[web/server.py](web/server.py)）：FastAPI + SSE，支持云端 / 无 GUI 环境运行，前端 100% 走后端代理
- 🔧 **`.env` 环境变量系统**：三层覆盖优先级（shell env > `.env` > `config/settings.json`），启动即可用，无需前端录入配置
- 🩺 **启动健康检查**：服务启动自动验证模型 API 与 TradingView 账号连通性，不通即明确报错
- 🎭 **Mock 数据源**：云端沙箱无可用金融数据源时（TV/yfinance/akshare 被防火墙拦截），用模拟 K 线让前后端流程跑通
- 🛡️ **SSE 稳定性修复**：分析路由强制 clamp `bar_count`，异常必推 error 事件，避免 `ERR_INCOMPLETE_CHUNKED_ENCODING`

详细改动见 [CHANGELOG](#与上游的同步策略)。

---

## 功能概览

面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线，将结构化 K 线数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），**不连接券商、不执行下单**。

- 📈 多数据源：MT5 / TradingView / yfinance / AkShare / Mock
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
- 覆盖应用：[pa_agent/config/settings.py](pa_agent/config/settings.py) 的 `Settings._apply_env_overrides`（`model_validator(mode="after")`）

### 启动健康检查

服务启动时会自动验证模型 API 与 TradingView 账号连通性，结果写入 `app.state.health_report` 并打印日志：

- `GET /api/health/check`：返回结构化诊断（支持 `?recheck=1` 重新检查）
- 智能降级：当前数据源不是 `tradingview` 时，TV 失败降级为 `warning`，不阻塞启动
- 代码：[pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py)
- **坑**：健康检查拼接 URL 时会智能识别 `base_url` 是否已带 `/v1`，避免 `/v1/v1/` 重复

### 关键 API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活探针 |
| GET | `/api/health/check` | 启动健康检查（模型+TV），支持 `?recheck=1` |
| GET | `/api/settings` / PUT `/api/settings` | 读取/合并保存配置（API Key 自动脱敏） |
| GET | `/api/datasources` | 列出可选数据源 |
| GET | `/api/timeframes` | 当前数据源支持的周期 |
| POST | `/api/subscribe` | 切换品种/周期/数据源 |
| GET | `/api/bars?count=N` | 拉取最新 N 根 K 线（前端图表用） |
| GET | `/api/analyze/stream?bar_count=N` | **SSE 两阶段分析**（见下方注意） |

路由定义在 [web/api/](web/api/)：`routes_settings.py` / `routes_data.py` / `routes_analyze.py` / `routes_chat.py`。

### 数据源系统

抽象基类 [pa_agent/data/base.py](pa_agent/data/base.py)（`DataSource` + `KlineBar` dataclass），工厂在 [pa_agent/data/factory.py](pa_agent/data/factory.py)。支持：

- `mt5`（Windows）、`tradingview`、`yfinance`、`akshare`、`eastmoney`、`tushare`、**`mock`**
- **`mock`**（[pa_agent/data/mock_source.py](pa_agent/data/mock_source.py)）：生成带趋势+噪声的模拟 K 线，**无网络依赖**。云端沙箱无法访问 TV/yfinance/akshare 时用它让前后端流程跑通调试。
- 关键约束：`latest_snapshot(n)` 必须返回 **n+1 根**（含 1 根 forming bar），否则 `build_analysis_frame` 丢掉 forming 后不足 n 根 closed 会返回 `None`。

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
   - 根因 B：`bar_count=100` 分析耗时过长，超过 TRAE 预览网关 SSE 超时 → 路由已强制 clamp 前端传值到 `settings.general.analysis_bar_count` 上限（默认 20）。

2. **`build_display_frame()` TypeError**：`now_ms` 是 keyword-only 参数，必须 `now_ms=now_ms` 传，不能当位置参数。

3. **OpenRouter 返回 404 HTML**：`.env` 的 `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`。

4. **`400 output tokens too large`**：`_PRACTICAL_UNLIMITED_MAX_TOKENS` 原值 524288 会让 free 模型直接 400，已降到 32768。

5. **云端网络限制**：TRAE 远程沙箱出站防火墙拦截 `tradingview.com:443`，yfinance/akshare/binance 也被限流。调试数据源问题请用 `mock`。

6. **MockSource 返回 None**：见上方"数据源系统"的 n+1 约束。

### 目录结构速查

```
pa_agent/
├── main.py / app_context.py     入口与依赖装配
├── config/    settings.py / env_loader.py / paths.py
├── data/      数据源（mt5/tradingview/yfinance/akshare/mock/factory/snapshot）
├── ai/        deepseek_client / prompt_assembler / router / json_validator / 校验
├── orchestrator/  two_stage.py 两阶段编排
├── gui/       PyQt6 桌面端（云端不使用）
├── util/      startup_health_check / logging / threading / mask_secret
web/
├── server.py  FastAPI 入口
├── api/       routes_{settings,data,analyze,chat}.py
├── static/    前端（index.html / js / css）
└── bridge/    AsyncEventBus（替代 Qt EventBus）
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
  - `pa_agent/data/mock_source.py`（Mock 数据源）
- **核心层以上游为准**：`pa_agent/ai/`、`pa_agent/orchestrator/`、`prompt_engineering/`、`pa_agent/data/base.py` 等跟随上游更新，本仓库只做最小必要补丁。
- **冲突高发点**：`pa_agent/data/factory.py`（新增了 mock 注册）、`pa_agent/config/settings.py`（新增了 `_apply_env_overrides`）、`web/api/routes_analyze.py`（bar_count clamp）。
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
