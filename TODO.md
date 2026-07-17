# PA Agent（二次开发版）后续优化待办

> 本机部署测试 + 后续优化路线图。已脱敏：不含任何 API Key、账号密码、个人凭证。

---

## 一、本机部署测试步骤

### 1.1 基础部署

```bash
# 克隆仓库
git clone https://github.com/wujiaxiang/PA_Agent.git
cd PA_Agent

# 安装依赖（Web 后端为主）
pip install -e .
pip install openai  # 若启动报 "openai package is not installed" 时单独装

# 配置环境变量（本机用真实数据源，不用 mock）
cp .env.example .env
# 编辑 .env 填入实际值（见下方 1.2）
```

### 1.2 .env 关键配置项

```dotenv
# 模型 API（必填）
PA_AGENT_PROVIDER_MODEL=tencent/hy3:free
PA_AGENT_PROVIDER_BASE_URL=https://openrouter.ai/api/v1
PA_AGENT_PROVIDER_API_KEY=<your-openrouter-key>

# 数据源（本机直连真实数据）
PA_AGENT_GENERAL_LAST_DATA_SOURCE=tradingview
PA_AGENT_GENERAL_LAST_SYMBOL=XAUUSD
PA_AGENT_GENERAL_LAST_TIMEFRAME=1h
PA_AGENT_GENERAL_ANALYSIS_BAR_COUNT=20

# TradingView 账号（仅数据源为 tradingview 时需要）
PA_AGENT_TRADINGVIEW_USERNAME=<your-tv-username>
PA_AGENT_TRADINGVIEW_PASSWORD=<your-tv-password>
```

**关键坑**：
- `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`
- `analysis_bar_count` 默认 20，超过 50 会触发 TRAE 网关 SSE 超时

### 1.3 TradingView 数据源适配（若用 TV）

```bash
# 装 tvDatafeed
pip install git+https://github.com/rongardF/tvdatafeed.git

# 修 tvDatafeed 2.1.0 已知 bug：WebSocket headers 传 JSON 字符串导致握手失败
# 定位文件
python -c "import tvDatafeed; import os; print(os.path.dirname(tvDatafeed.__file__))"
# 编辑该目录下的 main.py，第 35 行附近：
#   __ws_headers = json.dumps({"Origin": "https://data.tradingview.com"})
# 改为：
#   __ws_headers = {"Origin": "https://data.tradingview.com"}
```

### 1.4 启动与验证

```bash
# 启动
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000

# 验证
curl http://localhost:8000/api/health         # 存活探针
curl http://localhost:8000/api/health/check    # 启动健康检查（模型+TV）
# 浏览器打开 http://localhost:8000 测试前端
```

### 1.5 本机 vs 云端关键差异

| 项 | 云端沙箱 | 本机 |
| --- | --- | --- |
| 外网访问 | 受限（TV/yfinance/akshare 被拦） | 正常 |
| 数据源 | 用 `mock` | 用 `tradingview` 真实数据 |
| 健康检查 TV 项 | warning（SSL 失败降级） | ok |
| SSE 超时 | TRAE 网关有超时限制 | 本机无网关，可放宽 bar_count |

---

## 二、优化点（按优先级）

### P0 — 影响可用性，必须根治

#### P0.1 SSE 超时根治：Stage1/Stage2 改为流式输出

**现状**：当前用 `bar_count` clamp 到 20 是治标。AI 分析一次性返回，前端单次等待过长。

**方案**：
- `pa_agent/ai/deepseek_client.py` 改用 `stream=True` 调 OpenAI SDK
- `pa_agent/orchestrator/two_stage.py` 把 Stage1/Stage2 改为生成器，逐 token yield
- `web/api/routes_analyze.py` 的 SSE 流改为 `reasoning_token` / `content_token` 实时推送
- 前端边收边渲染，避免单次长等待

**预期收益**：bar_count 可放宽到 50-100 不超时，用户体验显著提升。

#### P0.2 前端 bar_count 同步问题

**现状**：前端 `#ds-bar-count` 显示旧值 100（浏览器缓存 settings），导致请求 URL 一直带 `bar_count=100`，后端靠 clamp 兜底。

**方案**：
- 前端启动时**强制读 `/api/settings` 后再渲染**，不读 localStorage
- 或加版本号 cache-busting
- 后端 `/api/settings` 响应加 `Cache-Control: no-store`

#### P0.3 tvDatafeed bug 自动修复

**现状**：tvDatafeed 2.1.0 的 WebSocket headers bug 需手动改源码，新机器部署易漏。

**方案**（任选一）：
- A. fork tvDatafeed 修 bug 后用 `git+https://github.com/<your-fork>/tvdatafeed.git` 引自己 fork
- B. 在 `pa_agent/data/tradingview.py` 启动时 monkey-patch 修复 `__ws_headers`
- C. 写一个 `pa_agent/data/tvdatafeed_patch.py`，在 `factory.py` 创建 TV 数据源前自动 import 应用补丁

**推荐**：方案 B，无需改外部依赖，最干净。

---

### P1 — 工程质量，建议补齐

#### P1.1 Web 后端无测试

**现状**：`tests/` 里只有原项目的桌面 GUI 测试，Web 后端零覆盖。

**需补**：
- `tests/unit/web/test_routes_analyze.py`：mock 数据源 + mock AI client，测 SSE 事件序列（reasoning_token / content_token / done / error）
- `tests/unit/web/test_routes_settings.py`：测 PUT /api/settings 的合并逻辑 + API Key 脱敏
- `tests/unit/web/test_health_check.py`：测 `/api/health/check` 的 recheck 流程、降级逻辑
- `tests/integration/web/test_sse_e2e.py`：端到端跑一次 20 根 K 线分析，断言事件顺序

**预期收益**：后续重构有保障，不怕改坏 SSE 流。

#### P1.2 配置双轨冲突

**现状**：`.env` 和 `config/settings.json` 可能冲突。settings.json 是 GUI 写的，云端没用 GUI 但文件还在（含旧数据）。

**方案**：
- Web 后端模式下**忽略 settings.json，只读 .env**（启动时检测 `--web-only` flag 或环境变量）
- 或启动时检测 `.env` 与 settings.json 的 provider/general 字段冲突，warn 提示
- 推荐：明确分层 — Web 模式只信 .env，GUI 模式只信 settings.json

#### P1.3 健康检查运行时无监控

**现状**：健康检查只在启动时跑一次。模型 API 运行中挂了无感知。

**方案**：
- 加**定时心跳**：后台 task 每 5 分钟 ping 一次模型 API（最小 chat completion）
- 失败时 `/api/health` 返回 `degraded` 状态
- 前端轮询 `/api/health`，degraded 时顶部提示"模型 API 异常"
- 复用 `pa_agent/util/startup_health_check.py` 的 `check_model_api` 函数

---

### P2 — 长期演进，按需推进

#### P2.1 Docker 化

**目标**：一键启动，避免本机环境差异。

**产出**：
- `Dockerfile`：基于 `python:3.12-slim`，装依赖 + 复制代码
- `docker-compose.yml`：挂载 `.env` 为 volume，暴露 8000 端口
- `.dockerignore`：排除 `.git`、`logs/`、`__pycache__`
- README 加 Docker 启动章节

#### P2.2 MockSource 的 n+1 约束显式化

**现状**：`build_analysis_frame` 丢 forming 后要 n 根 closed，这个约束只靠注释说明，新数据源容易踩（已踩过一次）。

**方案**：
- 把约束**提到 `pa_agent/data/base.py` 的 `DataSource` 基类**
- `latest_snapshot(n)` 的契约文档化：必须返回 n+1 根（含 1 根 forming）
- 在基类加 `_validate_snapshot(n, bars)` 自动校验，不满足抛 `ValueError` 带清晰提示

#### P2.3 AI max_tokens 硬编码可配置化

**现状**：`pa_agent/ai/deepseek_client.py` 的 `_PRACTICAL_UNLIMITED_MAX_TOKENS = 32768` 是为 OpenRouter free 模型降的。换 deepseek-chat 原生 API 时 32768 偏小。

**方案**：
- 改为 .env 可配置：`PA_AGENT_PROVIDER_MAX_OUTPUT_TOKENS=32768`
- 或按 provider 动态读模型上限：DeepSeek 原生用 393216，OpenRouter free 用 32768，其他用 128000
- `env_loader.py` 加 `get_env_int` 支持（已实现）

#### P2.4 日志结构化

**现状**：`logs/pa_agent.log` 是纯文本，难以查询。

**方案**：
- 改 structured logging（jsonl 格式）
- 每条日志带 `trace_id`、`stage`、`event_type` 字段
- 方便后续接 ELK / Loki 做分析链路追踪
- 复用 `pa_agent/util/logging.py` 的 handler 配置

#### P2.5 上游同步自动化

**现状**：README 写了手动 `git merge upstream/main` 流程，依赖人工。

**方案**：
- 配 GitHub Actions 定期（每周）检查上游 `rosemarycox5334-debug/PA_Agent` 是否有新提交
- 有新提交时自动开 PR 到本仓库，标题 `chore: sync upstream YYYYMMDD`
- PR 描述列出冲突文件清单 + 建议处理方式
- 降低长期维护成本

---

## 三、建议执行顺序

```
阶段 1：本机跑通真实 TV 数据
  └─ 按 1.1-1.5 部署，确认 TV 登录和拉数据正常
  └─ 健康检查 TV 项应为 ok

阶段 2：P0 根治
  ├─ P0.1 SSE 流式改造（核心，工作量大）
  ├─ P0.2 前端 bar_count 同步
  └─ P0.3 tvDatafeed bug 自动修复

阶段 3：P1 工程质量
  ├─ P1.1 补 Web 测试
  ├─ P1.2 配置双轨冲突
  └─ P1.3 运行时健康监控

阶段 4：P2 长期演进（按需）
  └─ Docker / 日志结构化 / 上游同步自动化 ...
```

---

## 四、关键文件索引

| 模块 | 文件 |
| --- | --- |
| Web 入口 | [web/server.py](web/server.py) |
| SSE 分析路由 | [web/api/routes_analyze.py](web/api/routes_analyze.py) |
| .env 解析器 | [pa_agent/config/env_loader.py](pa_agent/config/env_loader.py) |
| 配置覆盖 | [pa_agent/config/settings.py](pa_agent/config/settings.py) |
| 健康检查 | [pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py) |
| Mock 数据源 | [pa_agent/data/mock_source.py](pa_agent/data/mock_source.py) |
| AI 客户端 | [pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py) |
| 两阶段编排 | [pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py) |
| 数据源工厂 | [pa_agent/data/factory.py](pa_agent/data/factory.py) |
| K 线快照 | [pa_agent/data/snapshot.py](pa_agent/data/snapshot.py) |
| TV 数据源 | [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) |

---

## 五、参考文档

- [README.md](README.md) — 项目总览 + 开发者指南
- [智能体部署方法.txt](智能体部署方法.txt) — 详细部署步骤 + 故障排查
- [.env.example](.env.example) — 环境变量模板与说明
- [PA_Agent使用文档.md](PA_Agent使用文档.md) — 原项目完整功能说明
