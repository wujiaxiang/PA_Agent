# PA Agent（二次开发版）后续优化待办

> 本机部署测试 + 后续优化路线图。已脱敏：不含任何 API Key、账号密码、个人凭证。
>
> Spec 阶段一至四的 15 个 Task 与本文件历史上的 P0/P1/P2 共 8 项已全部交付。
> 2026-07-19/20 又交付了 Web GUI 对齐 + 切换性能重构 + 休市 K1 序号修复等 30+ 项改动。

## 相关文档

- [README.md](README.md) — 项目总览、快速开始、已知坑库
- [AGENTS.md](AGENTS.md) — 项目改动日志（与 TODO 互补：TODO 是「待办」，AGENTS 是「已办」）
- [CONTRIBUTING.md](CONTRIBUTING.md) — 开发环境与提交规范
- [PA_Agent使用文档.md](docs/PA_Agent使用文档.md) — 完整功能说明
- [docs/](docs/) — 技术细节文档

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
cp .env.example .env
# 编辑 .env 填入 PA_AGENT_PROVIDER_API_KEY 等（三层覆盖优先级见 1.2）
```

### 1.2 配置三层覆盖优先级

`shell env > .env > config/settings.json`，详见 [`.env.example`](.env.example)。

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

**关键坑**（完整列表见 [README.md 已知坑与修复记录](README.md#已知坑与修复记录重要)）：
- `base_url` 必须带 `/v1`（如 `https://openrouter.ai/api/v1`），SDK 内部自动拼 `/chat/completions`
- TradingView 匿名访问可用（`TradingViewSource()` 空参）；填账号密码通过 `env_loader.py`
- 加密货币：`last_tradingview_exchange=GATEIO` + `last_symbol=BTCUSDT` 即可，匿名模式无需 API key
- `max_output_tokens`：`0` 或留空 = 按 provider 默认值；填正整数则覆盖（free 模型建议 32768）

### 1.3 TradingView 数据源适配

```bash
# 装 tvDatafeed（HKEX/NYSE/NASDAQ 等非加密交易所必需）
pip install git+https://github.com/rongardF/tvdatafeed.git

# 若遇 NumPy CPU 不兼容（X86_V2 错误），降级
pip install numpy==1.26.4

# tvDatafeed 2.1.0 的 WebSocket headers bug 已在 pa_agent/data/tradingview.py
# 启动时 monkey-patch 自动修复（_patch_tvdatafeed_ws_headers），无需手动改源码。
```

### 1.4 启动与验证

```bash
# 启动（默认 Web 后端模式）
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000
# Windows 也可双击 start_pa_agent.bat

# 验证
curl http://localhost:8000/api/health         # 存活探针（返回缓存状态）
curl http://localhost:8000/api/health/check    # 完整健康检查（模型 API + 数据源）
# 浏览器打开 http://localhost:8000 测试前端
```

> ⚠️ 不要同时启动 Web 后端与桌面 GUI（`python -m pa_agent.main`），两者共享 `config/settings.json` 但运行时互不通信。详细差异见 [README.md](README.md#web-后端-vs-桌面-gui-的差异重要)。

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

#### P2.2 DataSource 契约校验 ✅（2026-07-20 修订：支持休市模式）

- [pa_agent/data/base.py](pa_agent/data/base.py) 的 `_validate_snapshot(n, bars)` 支持两种模式：
  - 正常模式：`n+1` 根，`bars[0].closed=False, seq=0`（forming bar）
  - 休市模式：`n` 根，全部 `closed=True`，`bars[0].seq=1`（无 forming bar）
- 消费方判断 forming bar 必须用 [pa_agent/data/bar_close_wait.py](pa_agent/data/bar_close_wait.py) 的 `has_forming_bar_at_head()`，**不要**硬编码 `bars[0].closed == False`。
- [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) 的 `_latest_snapshot_inner` 已适配两种模式（`still_forming=False` 时 `bars[0]` 用 `seq=1, closed=True`）。

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

## 三、Web GUI 阶段完成记录

### 阶段 4：Web GUI 对齐原 GUI ✅（2026-07-19）

详见第四节"Web GUI 阶段 5 完成记录"。

### 阶段 5：决策 tab 三段式重设计 ✅（2026-07-19）

采用 frontend-design skill 方法论的完整重设计，详见第四节。

### 阶段 6：前端体验完善（部分完成）✅（2026-07-19/20）

| 子项 | 状态 | 说明 |
|---|---|---|
| 侧边栏可拖拽调宽 + 可折叠 | ✅ | 拖拽时实时 `resizeChart()`，ResizeObserver 监听 |
| 所有 tab Tips/Help 重设计 | ✅ | 统一四段式：是什么 → 结构 → 视觉/操作 → 使用建议 |
| 决策 tab 长文字截断修复 | ✅ | 移除 `max-width/overflow/text-overflow/white-space`，允许换行 |
| FlowBar 失败状态 | ✅ | 新增 `.flow-step.failed` + `setFlowBarFailed(failedStep)`，done 事件检查 `exception` |
| SSE 倒计时 4 项异常修复 | ✅ | onopen 不设 `sseLastBarUpdateTs`、sanity check、`tfSecs` 优先读 `#ds-timeframe`、轮询模式 fallback |
| 降级轮询模式倒计时 | ✅ | 新增 `fetchAndUpdateNextCloseTs()` 低频拉取 `/api/bars/next-close` |
| 指标移除/切换品种同步 | ✅ | 改用正确 API 删除序列，切换前清空指标 |
| 主副图时间轴同步 | ✅ | [indicators.js](web/static/js/indicators.js) 改用时间戳范围同步（`getVisibleRange` / `setVisibleRange`） |
| 分析预测侧边栏 | ✅ | 分析按钮移入侧边栏头部，分析/取消合并为单按钮状态切换 |
| 决策 tab 字段补齐 | ✅ | 补充置信度/支撑位/阻力位，合理分组+折叠 |
| 决策树 tab 拆分 | ✅ | 拆分为独立 tab，便于查看 |
| 追问功能嵌入实时 tab | ✅ | 实时 tab 中支持追问 |
| 未来 tab 程序补全前缀 | ✅ | 为未来 tab 字段添加程序补全前缀 |
| 调试 tab JSON 说明提示 | ✅ | 添加 JSON 结构说明提示 |
| 原始 tab 功能补齐 | ✅ | 补齐缺失功能 |

### 阶段 7：切换性能重构 ✅（2026-07-20）

针对交易所/品种切换的报错与响应问题做完整重构：

**前端**（[web/static/js/app.js](web/static/js/app.js) 的 `applySubscribe`）：
- 使用 `Promise.allSettled` 并行执行请求
- 新增 `_inflightSwitch` 防重入机制
- 品种列表缓存：`Map<exchange, symbolList>` + 10 分钟 TTL
- 新增 `showSwitchError` 错误提示组件
- API 请求加 `AbortController` 15 秒超时

**后端**（[web/api/routes_data.py](web/api/routes_data.py)）：
- 同数据源切换时跳过 `connect()` 重连，复用连接
- 错误响应包含 `error_type: connection|symbol|timeout` 分类
- 响应头 `Cache-Control: max-age=600`

**测试**：使用 webapp-testing 工具完成 19 项 checklist 全部通过，报告保存在 `dogfood-output/switch-refactor`（临时目录，已清理）。

### 阶段 8：休市 K 线序号修复 ✅（2026-07-20）

修复 NVDA 美股收盘后最后一根 K 线不显示 `#1` 序号的问题：

- 根因：[bar_close_wait.py](pa_agent/data/bar_close_wait.py) 的 `seconds_until_bar_closes` 用取模算法，对已收盘 bar 仍返回正值
- 修复：加入绝对时间判断（`now_ms >= ts_open_ms + duration_ms` 时返回 0），配合 P2.2 的休市模式契约
- 前端 `setSeqMarkers` 保持 `seq <= 0 跳过` 逻辑，无需改动

详见 [README.md 坑 7、13](README.md#已知坑与修复记录重要)。

---

## 四、Web GUI 阶段 5 完成记录（决策 tab 重设计）

> 本轮（2026-07-19）针对用户反馈"决策 tab 不好理解、字段缺失、需要折叠"做的完整重设计，采用 frontend-design skill 方法论。

### 4.1 决策 tab 三段式视觉架构 ✅

- **VERDICT（决策结论 / 蓝 / 常显）**：结论横幅（订单类型 + 方向 + 置信度）+ 派生字段（趋势/周期/阶段）+ 价格栅格（入场/SL/TP1·TP2）+ 盈亏比 + 三条置信度进度条（诊断/交易/胜率）
- **VITALS（市场状态 / 青 / 常显）**：4 子区
  - 2.1 趋势结构：direction / cycle_position / alternative_cycle_position
  - 2.2 市场阶段：market_phase / transition_risk / volatility_regime / spike_stage / climax_risk
  - 2.3 关键价位：support_levels（绿）vs resistance_levels（红）并排
  - 2.4 形态与信号：detected_patterns / key_signals
- **EVIDENCE（详细依据 / 紫 / 折叠）**：7 个独立 `<details>` 子区 + 主控按钮
  - 3.1 决策理由（默认展开）/ 3.2 入场规则 / 3.3 K线分析 / 3.4 趋势上下文 / 3.5 风险评估 / 3.6 关键因素与关注点 / 3.7 置信度说明

### 4.2 Stage2 嵌套 schema bug 修复 ✅（缺失字段根因）

- **根因**：Stage2 实际存储为 `{decision: {order_type, diagnosis_confidence, ...}, diagnosis_summary, ...}`，但前端期望扁平字段在顶层 → 直接读 `stage2_decision.order_type` 得到 undefined
- **修复**：[web/api/routes_analyze.py](web/api/routes_analyze.py) 的 `_serialize_record` 将内层 `decision` 字典扁平化到顶层；[web/api/routes_records.py](web/api/routes_records.py) 列表端点同步从 `.decision` 子对象读取
- **验证**：API 现返回 `order_type: 限价单 / 不下单`、`diagnosis_confidence: 75`、`trade_confidence: 60`、`support_levels: [62532.9, ...]` 等此前 undefined 的字段

### 4.3 长文字字段截断修复 ✅

- **根因**：[web/static/css/style.css](web/static/css/style.css) 的 `.field-grid .field-val` 设了 `max-width: 60% / overflow: hidden / text-overflow: ellipsis / white-space: nowrap`，强制单行+省略号
- **修复**：移除截断规则，改为 `white-space: normal + word-break: break-word + overflow-wrap: anywhere`；`.field-key` 改为 `flex: 0 0 auto + nowrap` 保持标签紧凑

### 4.4 所有 tab 的 tip / help 文案重新设计 ✅

- **统一格式**：是什么 → 结构 → 视觉/操作 → 使用建议
- **决策 tab help** 重写为新三段式架构说明（VERDICT/VITALS/EVIDENCE）
- **stream / future / tree / tree-viz / raw / debug** 同步更新
- **CSS 增强**：嵌套 ul 添加虚线左边框 + 缩进；`code` 添加背景样式

### 4.5 新增单元测试 ✅

- `tests/unit/test_routes_analyze_incremental.py` — 增量分析路由
- `tests/unit/test_routes_bars_stream.py` — K 线 SSE 流
- `tests/unit/test_routes_records.py` — 历史记录 API
- `tests/unit/test_tradingview_cache.py` — TradingView 缓存

---

## 五、后续可推进的优化（非阻塞）

### 5.1 功能补齐（来自 AGENTS.md 已知缺失）

1. **经验库系统完善** ⭐ 高优
   - 现状：`experience/` 目录无数据文件，经验库检索与应用功能空跑
   - 目标：补齐经验数据文件，实现 Stage1/Stage2 经验库检索
   - 影响范围：[pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py)、`prompt_engineering/`

2. **Web UI Demo 模式**
   - 现状：桌面 GUI 有 demo 模式，Web UI 缺失
   - 目标：前端加 demo 按钮，后端返回内置 sample 数据，便于用户体验
   - 影响范围：[web/api/routes_data.py](web/api/routes_data.py)、[web/static/js/app.js](web/static/js/app.js)

3. **resendLastChat 功能**
   - 现状：`web/static/js/app.js` 中函数存在但无 UI 按钮绑定（死代码）
   - 目标：添加 UI 按钮绑定，实现重新发送最后一条消息

### 5.2 工程优化

4. **前端顶部健康状态指示**
   - 轮询 `/api/health`，`degraded`/`error` 时顶部红条提示
   - 影响范围：[web/static/js/app.js](web/static/js/app.js)、[web/static/css/style.css](web/static/css/style.css)

5. **`_PRACTICAL_UNLIMITED_MAX_TOKENS` 按 provider 动态读模型上限**
   - 当前用静态默认值，DeepSeek 原生 393216、OpenRouter free 32768、其他 128000
   - 影响范围：[pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py)

6. **`max_output_tokens` 前端可配**
   - 当前只能在 `settings.json` / `.env` 配置，前端设置面板未暴露
   - 影响范围：[web/static/js/app.js](web/static/js/app.js) 设置面板

7. **`_validate_snapshot` 性能优化**
   - 大 n（>1000）时 list 遍历可优化（实际场景 n≤100，优先级低）

### 5.3 体验与生态

8. **移动端适配**
   - 现状：当前 UI 为桌面端设计，移动端显示效果差
   - 目标：响应式布局，关键操作在移动端可用

9. **性能优化**
   - 页面加载速度与渲染性能优化
   - 长时间运行的内存泄漏排查（SSE 长连接场景）

10. **国际化支持**
    - 添加多语言支持（中/英）
    - 影响范围：前端所有文案、后端错误消息

---

## 六、建议执行顺序

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
  ├─ P2.2 n+1 约束校验（含休市模式契约）
  ├─ P2.3 max_tokens 可配置化
  ├─ P2.4 日志结构化
  └─ P2.5 上游同步自动化

阶段 4：Web GUI 对齐原 GUI ✅
  ├─ K 线 SSE 实时流 + 历史记录 API
  ├─ 侧边栏可拖拽调宽 + 可折叠 + 历史记录下拉
  ├─ 决策树 tab 拆分 + 可视化 tab 流程图
  └─ 自建指标管理器（indicators.js）

阶段 5：决策 tab 三段式重设计 ✅
  ├─ VERDICT / VITALS / EVIDENCE 三段式架构
  ├─ Stage2 嵌套 schema bug 修复（缺失字段根因）
  ├─ 长文字字段截断修复
  ├─ 所有 tab 的 tip / help 文案重新设计
  └─ 4 个新单元测试

阶段 6：前端体验完善 ✅
  ├─ SSE 倒计时 4 项异常修复
  ├─ FlowBar 失败状态 + 降级轮询模式倒计时
  ├─ 指标移除/切换品种同步
  ├─ 主副图时间轴同步（时间戳范围）
  └─ 决策 tab 字段补齐 + 长文字截断修复

阶段 7：切换性能重构 ✅
  ├─ applySubscribe 并行化 + _inflightSwitch 防重入
  ├─ 品种列表缓存 + 15s 超时
  └─ 后端同源连接复用 + 错误分类 + Cache-Control

阶段 8：休市 K 线序号修复 ✅
  ├─ seconds_until_bar_closes 绝对时间判断
  ├─ _latest_snapshot_inner 休市模式适配
  └─ _validate_snapshot 双模式契约

阶段 9：功能补齐与体验优化（待推进）
  ├─ 经验库系统完善（5.1.1）⭐ 高优
  ├─ Web UI Demo 模式（5.1.2）
  ├─ resendLastChat 功能（5.1.3）
  ├─ 前端顶部健康状态指示（5.2.4）
  ├─ 移动端适配（5.3.8）
  └─ 国际化支持（5.3.10）
```

---

## 七、关键文件索引

| 模块 | 文件 |
| --- | --- |
| Web 入口 | [web/server.py](web/server.py) |
| SSE 分析路由 | [web/api/routes_analyze.py](web/api/routes_analyze.py) |
| K 线 SSE 流路由 | [web/api/routes_bars_stream.py](web/api/routes_bars_stream.py) |
| 历史记录 API | [web/api/routes_records.py](web/api/routes_records.py) |
| 自由对话路由 | [web/api/routes_chat.py](web/api/routes_chat.py) |
| 数据源路由 | [web/api/routes_data.py](web/api/routes_data.py) |
| 配置路由 | [web/api/routes_settings.py](web/api/routes_settings.py) |
| 前端入口 | [web/static/index.html](web/static/index.html) |
| 前端主逻辑 | [web/static/js/app.js](web/static/js/app.js) |
| 前端 API 封装 | [web/static/js/api.js](web/static/js/api.js) |
| 前端图表 | [web/static/js/chart.js](web/static/js/chart.js) |
| 自建指标管理器 | [web/static/js/indicators.js](web/static/js/indicators.js) |
| 前端样式 | [web/static/css/style.css](web/static/css/style.css) |
| 配置加载 | [pa_agent/config/settings.py](pa_agent/config/settings.py) |
| .env 解析 | [pa_agent/config/env_loader.py](pa_agent/config/env_loader.py) |
| 健康检查 | [pa_agent/util/startup_health_check.py](pa_agent/util/startup_health_check.py) |
| 结构化日志 | [pa_agent/util/logging.py](pa_agent/util/logging.py) |
| AI 客户端 | [pa_agent/ai/deepseek_client.py](pa_agent/ai/deepseek_client.py) |
| 两阶段编排 | [pa_agent/orchestrator/two_stage.py](pa_agent/orchestrator/two_stage.py) |
| 数据源工厂 | [pa_agent/data/factory.py](pa_agent/data/factory.py) |
| 市场默认值 | [pa_agent/data/market_defaults.py](pa_agent/data/market_defaults.py) |
| K 线快照 | [pa_agent/data/snapshot.py](pa_agent/data/snapshot.py) |
| forming bar 判断 | [pa_agent/data/bar_close_wait.py](pa_agent/data/bar_close_wait.py) |
| TV 数据源 | [pa_agent/data/tradingview.py](pa_agent/data/tradingview.py) |
| 记录 schema | [pa_agent/records/schema.py](pa_agent/records/schema.py) |
| Docker | [web/Dockerfile](web/Dockerfile) / [web/docker-compose.yml](web/docker-compose.yml) |
| 上游同步 | [.github/workflows/sync-upstream.yml](.github/workflows/sync-upstream.yml) |
| Windows 启动脚本 | [start_pa_agent.bat](start_pa_agent.bat) |

---

## 八、参考文档

- [README.md](README.md) — 项目总览 + 开发者指南 + 已知坑与修复记录
- [CONTRIBUTING.md](CONTRIBUTING.md) — 参与贡献指南（含开发环境、启动方式、PR 建议）
- [AGENTS.md](AGENTS.md) — 项目改动记录（按日期索引，记录每次迭代的细节）
- [PA_Agent使用文档.md](docs/PA_Agent使用文档.md) — 原项目完整功能说明（PyQt6 桌面 GUI）
