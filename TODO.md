# PA Agent（二次开发版）后续优化待办

> 本机部署测试 + 后续优化路线图。已脱敏：不含任何 API Key、账号密码、个人凭证。
>
> 历史已完成项（SSE 流式改造、tvDatafeed bug 修复、Web 后端测试、session_ledger no-Qt stub、TradingView 数据源切换）已从本文件移除。

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

# 配置文件（本机用真实数据源，不用 mock）
cp config/settings.example.json config/settings.json
# 编辑 config/settings.json 填入实际值（见下方 1.2）
```

### 1.2 `config/settings.json` 关键配置项

```json
{
  "provider": {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": "<your-api-key>",
    "thinking": true,
    "reasoning_effort": "high",
    "context_window": 128000
  },
  "general": {
    "last_data_source": "tradingview",
    "last_symbol": "XAUUSD",
    "last_timeframe": "15m",
    "analysis_bar_count": 50
  }
}
```

**关键坑**：
- `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`
- TradingView 匿名访问可用（`TradingViewSource()` 空参）；填账号密码需通过 `env_loader.py`（见 P1.2）

### 1.3 TradingView 数据源适配（若用 TV）

```bash
# 装 tvDatafeed
pip install git+https://github.com/rongardF/tvdatafeed.git

# tvDatafeed 2.1.0 的 WebSocket headers bug 已在 pa_agent/data/tradingview.py
# 启动时 monkey-patch 自动修复（_patch_tvdatafeed_ws_headers），无需手动改源码。
```

### 1.4 启动与验证

```bash
# 启动
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000

# 验证
curl http://localhost:8000/api/health         # 存活探针
curl http://localhost:8000/api/health/check    # 启动健康检查（模型+TV） — 待实现，见 P1.3
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

#### P0.2 前端 bar_count 同步问题

**现状**：前端启动时已强制读 `/api/settings` 渲染 `#ds-bar-count`，但后端 `/api/settings` 响应未设 `Cache-Control: no-store`，浏览器/中间层可能缓存旧值。

**剩余方案**：
- 后端 `/api/settings` GET 响应加 `Cache-Control: no-store`
- 或加版本号 cache-busting

---

### P1 — 工程质量，建议补齐

#### P1.2 配置双轨冲突

**现状**：目前只有 `config/settings.json` 单轨配置。`.env` 机制（`env_loader.py`、`.env.example`）尚未实现，TODO 早期描述的"双轨冲突"暂不存在。

**方案**：
- 建 `pa_agent/config/env_loader.py`，提供 `get_env_str` / `get_env_int` / `get_env_bool` 工具
- 建 `.env.example` 模板（含 `PA_AGENT_PROVIDER_*`、`PA_AGENT_GENERAL_*`、`PA_AGENT_TRADINGVIEW_USERNAME/PASSWORD`）
- Web 模式启动时优先读 .env 覆盖 settings.json（明确分层：Web 模式 .env 优先，GUI 模式只信 settings.json）
- 检测 .env 与 settings.json 的 provider/general 字段冲突时 warn 提示

#### P1.3 健康检查运行时无监控

**现状**：`/api/health` 只返回 `{"status":"ok"}`，不检查模型 API。模型挂了无感知。`startup_health_check.py` 不存在，`/api/health/check` 不存在。

**方案**：
- 新建 `pa_agent/util/startup_health_check.py`，提供 `check_model_api()` / `check_data_source()` 函数
- 加 `/api/health/check` 端点：启动时跑一次完整检查（模型 ping + TV 连接），返回各项 ok/warning/error
- 加**定时心跳**：后台 task 每 5 分钟 ping 一次模型 API（最小 chat completion）
- 失败时 `/api/health` 返回 `degraded` 状态
- 前端轮询 `/api/health`，degraded 时顶部提示"模型 API 异常"

---

### P2 — 长期演进，按需推进

#### P2.1 Docker 化（待清理）

**现状**：`web/Dockerfile` 和 `web/docker-compose.yml` 已存在，但 Dockerfile 仍装了一堆 Qt/X11 库（`libxcb-*` / `libegl1` / `libgl1`）并设 `QT_QPA_PLATFORM=offscreen`。在 `session_ledger.py` 改为 no-Qt stub 后这些已不再需要。

**剩余方案**：
- 删除 Dockerfile 中 Qt/X11 库安装行
- 删除 `ENV QT_QPA_PLATFORM=offscreen`
- 删除 docker-compose.yml 中 `QT_QPA_PLATFORM=offscreen` 环境变量
- 新建 `.dockerignore`：排除 `.git`、`logs/`、`__pycache__`、`tests/`

#### P2.2 MockSource 的 n+1 约束显式化

**现状**：`build_analysis_frame` 丢 forming 后要 n 根 closed，这个约束只在 `pa_agent/data/base.py` 的 `latest_snapshot` 文档注释里说明，无自动校验。新数据源容易踩。

**方案**：
- 在 `DataSource` 基类加 `_validate_snapshot(n, bars)` 方法
- `latest_snapshot(n)` 的契约文档化：必须返回 n+1 根（含 1 根 forming）
- 不满足抛 `ValueError` 带清晰提示
- `mock_source.py`（若实现，见 P1.2 依赖）需遵守此契约

#### P2.3 AI max_tokens 硬编码可配置化

**现状**：`pa_agent/ai/deepseek_client.py` 的 `_PRACTICAL_UNLIMITED_MAX_TOKENS = 524288`（已从旧值 32768 提升），但仍硬编码，未走配置。

**方案**：
- 改为 settings.json 可配置：`provider.max_output_tokens`
- 或按 provider 动态读模型上限：DeepSeek 原生用 393216，OpenRouter free 用 32768，其他用 128000
- 依赖 P1.2 的 `env_loader.py` 支持 `get_env_int`

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
阶段 1：本机跑通真实 TV 数据 ✅
  └─ 按 1.1-1.5 部署，确认 TV 登录和拉数据正常
  └─ 健康检查 TV 项应为 ok（待 P1.3 实现后）

阶段 2：P0/P1 根治
  ├─ P0.2 Cache-Control: no-store
  ├─ P1.2 .env 机制（env_loader + .env.example）
  └─ P1.3 运行时健康监控

阶段 3：P2 工程质量
  ├─ P2.1 Dockerfile 清理
  ├─ P2.2 n+1 约束校验
  ├─ P2.3 max_tokens 可配置化
  ├─ P2.4 日志结构化
  └─ P2.5 上游同步自动化
```

---

## 四、关键文件索引

| 模块 | 文件 |
| --- | --- |
| Web 入口 | [web/server.py](web/server.py) |
| SSE 分析路由 | [web/api/routes_analyze.py](web/api/routes_analyze.py) |
| 配置加载 | [pa_agent/config/settings.py](pa_agent/config/settings.py) |
| AI 客户端 | [pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py) |
| 两阶段编排 | [pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py) |
| 数据源工厂 | [pa_agent/data/factory.py](pa_agent/data/factory.py) |
| K 线快照 | [pa_agent/data/snapshot.py](pa_agent/data/snapshot.py) |
| TV 数据源 | [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) |
| Docker | [web/Dockerfile](web/Dockerfile) / [web/docker-compose.yml](web/docker-compose.yml) |

---

## 五、参考文档

- [README.md](README.md) — 项目总览 + 开发者指南
- [PA_Agent使用文档.md](PA_Agent使用文档.md) — 原项目完整功能说明
