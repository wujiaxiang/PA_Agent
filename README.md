# PA Agent — AI K线分析辅助工具（桌面端）

**交流 QQ 群：975328619**

---

面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线，将结构化 K 线数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），**不是**截图识图，**不连接券商、不执行下单**。

---

## 主要功能

- 📈 **多数据源**：MT5（Windows）、TradingView（全平台）、yfinance（期货/加密货币）、AkShare（A 股）
- 🧠 **两阶段 AI 分析**：市场诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 🔄 **增量分析与持续跟踪**：新增 K 线时复用上次结论；开启 `keep_analysis` 后新 K 线收盘自动触发新一轮分析
- 🌳 **决策树可视化**：赛博科幻风格可交互流程图，自动播放闸门→策略路径动画
- 🔮 **未来走势预期**：AI 预测下一根 K 线方向和下一个市场周期位置
- 💬 **分析后自由追问**：完整对话会话管理器，实时推理流 + Token 进度条，对话历史持久化
- 📚 **经验库**：按周期位置检索历史案例供分析参考
- 📝 **完整落盘**：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🛡️ **可配置校验体系**：JSON 校验、一致性检查、语义校验、截断修复、失败自动重试
- 🔒 **API Key** 本地加密存储

---

## 环境要求

| 项目     | 要求                                                                    |
| -------- | ----------------------------------------------------------------------- |
| 操作系统 | Windows 10 / 11（主支持）、macOS 12+（TradingView 数据源）              |
| Python   | 3.11+                                                                    |
| 数据源   | MT5 / TradingView / yfinance / AkShare **至少配置一种**                  |
| 网络     | 可访问所配置的 AI API（如 DeepSeek、PackyAPI 等）                        |

---

## 快速开始

直接在系统中安装（推荐部署在本机）：

```cmd
pip install -e .
python -m pa_agent.main
```

首次启动后在**设置**中填写 **Base URL**、**模型名** 与 **API Key**。

> 如需隔离环境也可创建虚拟环境：`python -m venv .venv` 后激活再 `pip install -e .`。

**安装内容**：PyQt6（GUI 框架）+ pyqtgraph（K 线图表绘图）+ numpy/pandas（数据处理）+ openai（AI API 客户端）+ **akshare/baostock（A 股数据源）** + json 校验、模型定义等全套依赖。

> 若需运行测试（pytest）或代码格式化（ruff/black），额外安装：`pip install -e ".[dev]"`。

---

## 详细说明

完整操作界面说明见 [`PA_Agent使用文档.md`](PA_Agent使用文档.md)，配置字段说明见 [`config/README.md`](config/README.md)。

---

## 开发者指南

> 本节面向继续开发与调试的 agent / 贡献者。终端用户可忽略。

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
- **Web 后端**（`web/server.py`）：FastAPI + SSE，**云端调试主入口**。前端 100% 走后端代理（`/api/bars`、`/api/analyze/stream`），不直连 TradingView。

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

### 启动 Web 后端（云端调试）

```bash
# 安装依赖（含 web 额外依赖）
pip install -e .
pip install openai  # 若报 RuntimeError: openai package is not installed

# 启动
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000

# 验证
curl http://localhost:8000/api/health/check
curl http://localhost:8000/api/settings   # 确认 analysis_bar_count / data_source
```

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

### 测试

```bash
pytest -q                    # 全量
pytest tests/unit -q         # 仅单元测试（快）
pytest -m "not live" -q      # 跳过需要真实 API key 的 live 测试
pip install -e ".[dev]"      # 安装 pytest/ruff/black/hypothesis
```

测试分层：`tests/unit` / `tests/property`（hypothesis）/ `tests/integration` / `tests/e2e`。`live` 标记的测试必须通过环境变量配置 API key，**绝不读取** `config/settings.json`。

### 代码风格

- `ruff check . && black --check .`（line-length 100, py311）
- `make lint` / `make test` / `make run`

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

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。

---

## 群友反馈榜单

感谢群友的使用反馈与鼓励，以下为群友评价截图（按时间从早到晚排列）：

<p align="center">
  <img src="qunyou/BD58CB2D6E4F45CC17CF832C506A982C.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/653EC872A0D6883A34B7B37B692C8B1D.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260619-205140.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260619-235505.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-150714.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-150833.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260620-220824.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260623-125929.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/91003065F07407E92B50964AE7F8A944.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260624-191001.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260628-014043.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260628-213700.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260629-163821.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ20260701-212522.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/BB4AE8110A7011426BD29D5CE8B5F73B.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/F383D366F2254692418DB18AAA617ACE.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/AD48DF6289CB6A9D51FE0B8EE2EC38C2.jpg" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/F61C8DCDB67924B64B33403D20047E0B.png" alt="群友反馈" width="480" />
</p>
<p align="center">
  <img src="qunyou/QQ_1783089951396.png" alt="群友反馈" width="480" />
</p>

---

## 打赏与支持

如果你觉得这个程序对你有帮助的话，可以打赏激励作者继续优化程序，感谢你的支持和鼓励！

（作者会优先解决打赏人的问题，因为人太多了！回复不过来！）

<p align="center">
  <img src="赞助码.jpeg" alt="打赏二维码" width="420" />
</p>
