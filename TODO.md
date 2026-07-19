# PA Agent（二次开发版）后续优化待办

> 本机部署测试 + 后续优化路线图。已脱敏：不含任何 API Key、账号密码、个人凭证。
>
> Spec 阶段一至四的 15 个 Task 与本文件历史上的 P0/P1/P2 共 8 项已全部交付，仅保留"已完成项的验收要点"供回归参考。

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

# 配置文件（本机用真实数据源）
cp config/settings.example.json config/settings.json
# 编辑 config/settings.json 填入实际值（见下方 1.2）
```

### 1.2 `config/settings.json` 关键配置项

默认示例（TradingView + Gate.io 加密货币）：

```json
{
  "provider": {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": "<your-api-key>",
    "thinking": true,
    "reasoning_effort": "high",
    "context_window": 128000,
    "max_output_tokens": 0
  },
  "general": {
    "last_data_source": "tradingview",
    "last_tradingview_exchange": "GATEIO",
    "last_symbol": "BTCUSDT",
    "last_timeframe": "15m",
    "analysis_bar_count": 50
  }
}
```

**关键坑**：
- `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`
- TradingView 匿名访问可用（`TradingViewSource()` 空参）；填账号密码需通过 `env_loader.py`（见 P1.2）
- 加密货币：`last_tradingview_exchange=GATEIO` + `last_symbol=BTCUSDT` 即可，匿名模式无需 API key
- `max_output_tokens`：`0` 或留空 = 按 provider 默认值；填正整数则覆盖（free 模型建议 32768）

### 1.3 TradingView 数据源适配

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
curl http://localhost:8000/api/health         # 存活探针（返回缓存状态）
curl http://localhost:8000/api/health/check    # 完整健康检查（模型 API + 数据源）
# 浏览器打开 http://localhost:8000 测试前端
```

---

## 二、已完成优化项（按优先级）

### P0 — 影响可用性

#### P0.2 前端 bar_count 同步 ✅

- `/api/settings` GET 响应已加 `Cache-Control: no-store`，避免中间层缓存旧值。
- 代码：[web/api/routes_settings.py](web/api/routes_settings.py)

---

### P1 — 工程质量

#### P1.2 `.env` 环境变量系统 ✅

- 新建 [pa_agent/config/env_loader.py](pa_agent/config/env_loader.py)：`apply_env_overrides()` / `get_env_str/int/bool` / `get_tv_credentials()`，手动 dotenv 解析（不依赖 python-dotenv）。
- 新建 [.env.example](.env.example) 模板，含 `PA_AGENT_PROVIDER_*` / `PA_AGENT_GENERAL_*` / `PA_AGENT_TRADINGVIEW_USERNAME/PASSWORD` / `PA_AGENT_PUSHPLUS_TOKEN`。
- 应用入口：[pa_agent/app_context.py](pa_agent/app_context.py) 的 `bootstrap()` 在 `load_settings()` 之后调用 `apply_env_overrides(settings)`。
- 三层覆盖优先级：shell env > `.env` > `config/settings.json`。

#### P1.3 启动健康检查 + 运行时心跳 ✅

- 新建 [pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py)：`check_model_api()`（`client.chat("ping")`）/ `check_data_source()`（`latest_snapshot(2)`）/ `run_full_check()` 返回 `HealthReport`。
- 新增 `GET /api/health`：返回缓存状态（`starting`/`ok`/`degraded`/`error`），不触发真实检查。
- 新增 `GET /api/health/check`：同步执行完整检查，返回各项详情与延迟。
- 后台心跳任务：[web/server.py](web/server.py) 的 `_health_heartbeat()` 每 300s 跑一次，结果缓存到 `app.state.last_health_report`。
- **坑**：`check_model_api` 必须用 `client.chat()`，不能用 `stream_chat()`（返回 `AIReply` 不可迭代）。

---

### P2 — 长期演进

#### P2.1 Docker 化清理 ✅

- [web/Dockerfile](web/Dockerfile) 删除所有 Qt/X11 库安装行 + `QT_QPA_PLATFORM=offscreen`。
- [web/docker-compose.yml](web/docker-compose.yml) 删除 `QT_QPA_PLATFORM=offscreen` 环境变量。
- 新建 [.dockerignore](.dockerignore)：排除 `.git`、`logs/`、`__pycache__`、`tests/`。

#### P2.2 DataSource 契约校验 ✅

- [pa_agent/data/base.py](pa_agent/data/base.py) 新增 `_validate_snapshot(n, bars)` 方法：校验恰好 n+1 根、bars[0] 为 forming（closed=False, seq=0）、bars[1:] 为 closed（seq=1..n）。
- `latest_snapshot(n)` 文档化契约：必须返回 n+1 根（1 forming + n closed）。
- [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) 的 `latest_snapshot` 已修复为返回 n+1 根并在末尾调用 `_validate_snapshot()`。

#### P2.3 max_tokens 可配置化 ✅

- [pa_agent/config/settings.py](pa_agent/config/settings.py) 新增 `provider.max_output_tokens: int | None` 字段。
- [pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py) 的 `_provider_max_output_tokens()` 优先读 `settings.max_output_tokens`（>0 时覆盖 per-provider 默认值）。
- `.env` 支持 `PA_AGENT_PROVIDER_MAX_OUTPUT_TOKENS`。

#### P2.4 日志结构化 ✅

- [pa_agent/util/logging.py](pa_agent/util/logging.py) 新增 `JsonlFormatter`（JSON-lines 格式）。
- 新增 `set_trace_id()` / `get_trace_id()` contextvar，每条日志带 `trace_id` 字段。
- 通过 `PA_AGENT_LOG_JSON=1` 环境变量启用 JSON 输出（默认仍为文本，向后兼容）。
- [web/server.py](web/server.py) 的 `trace_id_middleware`：从 `X-Trace-Id` 请求头读或生成 12 位 hex，响应头回传。

#### P2.5 上游同步自动化 ✅

- 新建 [.github/workflows/sync-upstream.yml](.github/workflows/sync-upstream.yml)：每周一 09:00 UTC 检查上游 `rosemarycox5334-debug/PA_Agent` 是否有新提交。
- 有新提交时自动开 PR，标题 `chore: sync upstream YYYYMMDD`，PR 描述列出冲突文件清单 + 建议处理方式。

---

## 三、后续可推进的优化（非阻塞）

以下为非紧急、可按需推进的优化点：

1. **前端数据源切换 UI 完善**：当前前端 `#ds-kind` / `#ds-symbol` / `#ds-timeframe` 仅加载列表，未回填 `last_data_source`/`last_symbol`/`last_tradingview_exchange`；需要添加"应用"按钮调用 `/api/subscribe`，并新增"交易所"输入框（TradingView 模式可见）。
2. **前端顶部健康状态指示**：轮询 `/api/health`，`degraded`/`error` 时顶部红条提示。
3. **`_PRACTICAL_UNLIMITED_MAX_TOKENS` 按 provider 动态读模型上限**：当前用静态默认值，DeepSeek 原生 393216、OpenRouter free 32768、其他 128000。
4. **`_validate_snapshot` 性能优化**：大 n（>1000）时 list 遍历可优化。
5. **`max_output_tokens` 前端可配**：当前只能在 `settings.json` / `.env` 配置，前端设置面板未暴露。

---

## 四、建议执行顺序

```
阶段 1：本机跑通真实 TV 数据 ✅
  └─ 按 1.1-1.4 部署，确认 TV 登录和拉数据正常
  └─ 健康检查 /api/health/check 返回 ok

阶段 2：P0/P1 根治 ✅
  ├─ P0.2 Cache-Control: no-store
  ├─ P1.2 .env 机制（env_loader + .env.example）
  └─ P1.3 运行时健康监控

阶段 3：P2 工程质量 ✅
  ├─ P2.1 Dockerfile 清理
  ├─ P2.2 n+1 约束校验
  ├─ P2.3 max_tokens 可配置化
  ├─ P2.4 日志结构化
  └─ P2.5 上游同步自动化

阶段 4：前端体验完善（待推进）
  └─ 见第三节"后续可推进的优化"
```

---

## 五、关键文件索引

| 模块 | 文件 |
| --- | --- |
| Web 入口 | [web/server.py](web/server.py) |
| SSE 分析路由 | [web/api/routes_analyze.py](web/api/routes_analyze.py) |
| 数据源路由 | [web/api/routes_data.py](web/api/routes_data.py) |
| 配置加载 | [pa_agent/config/settings.py](pa_agent/config/settings.py) |
| .env 解析 | [pa_agent/config/env_loader.py](pa_agent/config/env_loader.py) |
| 健康检查 | [pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py) |
| 结构化日志 | [pa_agent/util/logging.py](pa_agent/util/logging.py) |
| AI 客户端 | [pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py) |
| 两阶段编排 | [pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py) |
| 数据源工厂 | [pa_agent/data/factory.py](pa_agent/data/factory.py) |
| 市场默认值 | [pa_agent/data/market_defaults.py](pa_agent/data/market_defaults.py) |
| K 线快照 | [pa_agent/data/snapshot.py](pa_agent/data/snapshot.py) |
| TV 数据源 | [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) |
| Docker | [web/Dockerfile](web/Dockerfile) / [web/docker-compose.yml](web/docker-compose.yml) |
| 上游同步 | [.github/workflows/sync-upstream.yml](.github/workflows/sync-upstream.yml) |

---

## 六、参考文档

- [README.md](README.md) — 项目总览 + 开发者指南
- [PA_Agent使用文档.md](PA_Agent使用文档.md) — 原项目完整功能说明
