# PA_AGENT 项目规范 (AGENTS)

## 项目概述

PA_AGENT 是一个基于 AI 的量化分析工具，提供实时行情数据、智能分析预测、决策树可视化等功能。

---

## 文档分工

| 文档 | 用途 | 内容 |
|---|---|---|
| **AGENTS.md**（本文件） | 规范与约束 | 维护规范、核心约束、技术概念、已知问题、后续需求 |
| [CHANGELOG.md](CHANGELOG.md) | 改动流水账 | 按日期倒序记录每次大改动 / bug 修复 / 新增模块的详细经过 |
| [TODO.md](TODO.md) | 待办规划 | 阶段完成记录与后续可推进的优化 |
| [README.md](README.md) | 用户文档 | 使用说明、踩坑记录 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 | 启动方式、PR 建议、开发者参考约束 |

---

## 维护规范

### 何时更新 CHANGELOG.md

凡属于以下情况必须在 [CHANGELOG.md](CHANGELOG.md) 对应日期节追加条目：

1. **大迭代 / 阶段性交付**：如 Web GUI 阶段、决策 tab 重设计、切换性能重构等
2. **架构性重构**：如 SSE 后台循环改造、DataSource 契约修订、配置系统重构等
3. **关键 bug 修复**：影响可用性或正确性的修复（如休市 K1 序号、时间轴偏移、SSE 倒计时异常等）
4. **新增 / 删除模块**：新增路由、新增前端组件、删除死代码等
5. **文档大范围对齐**：如 README / TODO / CONTRIBUTING / docs 的一次性更新

**不需要更新**的情况（写 commit message 即可）：
- 单文件小修、文案微调、注释补充
- 单测用例的新增 / 调整（除非涉及新模块）
- 临时调试产物的清理

### 何时更新 AGENTS.md

- 新增或修改 **核心约束**（Hard Constraints）
- 新增或修改 **核心技术概念**
- 新增或移除 **已知问题**
- 调整 **后续迭代需求** 优先级
- 维护规范本身有调整

### 追加格式（CHANGELOG.md）

```markdown
## YYYY-MM-DD

### N. 简短标题
- **问题** / **功能** / **目标**：一句话描述动机
- **根因**（仅 bug 修复）：原代码哪里错了
- **修复** / **改动**：做了什么
- **文件**：`path/to/file.py`
- **提交**（可选）：commit hash
```

### 维护节奏

- 每次大改动当天必须追加到 CHANGELOG.md，不要积累到下次
- 每月底可以整体回顾一次，把零散条目归并、提炼新的核心约束到 AGENTS.md
- 若改动涉及踩坑，同步追加到 [README.md](README.md) 的「已知坑与修复记录」
- 若改动涉及开发者约束，同步更新 [CONTRIBUTING.md](CONTRIBUTING.md) 的「开发者参考」

---

## 核心约束 (Hard Constraints)

以下约束从历次改动中沉淀而来，修改相关代码时必须遵守。

### 代码修改后必须重启服务器（最高优先级）

- **修改 Python 后端代码（`web/`、`pa_agent/`）后必须重启 uvicorn 服务器**：uvicorn 默认不监视文件变化，不重启则改动不生效，用户看到的仍是旧代码行为（常被误判为「浏览器缓存」）。
- **推荐启动方式**：`python -m uvicorn web.server:app --host 0.0.0.0 --port 8000 --reload --reload-dir web --reload-dir pa_agent`，`--reload` 模式会自动监视 Python 文件变化并热重载，避免手动重启遗漏。
- **修改前端静态资源（`web/static/`）后必须同步更新 HTML 版本号**：`index.html` 中所有 `?v=N` 引用（CSS/JS）必须递增。虽有 `_NoCacheStaticFiles` 强制 `Cache-Control: no-cache`，但 TRAE 内置 webview 可能忽略 no-cache 头，版本号是兜底失效手段。
- **完成任何代码修改后，必须重启服务器并验证 HTTP 响应**：用 `Invoke-WebRequest http://localhost:8000/` 确认版本号已更新、Cache-Control 头正确，再告知用户「修改完成」。违反此约束会导致用户反复反馈「还是老样子」「缓存问题」，严重损害信任。

### 前端事件绑定

- **`bindEvents()` 必须在所有 `await` 数据加载之前调用**：数据加载失败（如 `loadBars()` throw 异常）不应影响 UI 可交互性。违反此约束会导致所有按钮失去响应。
- **SSE 流 `startSSEBarsStream` 必须在 `loadBars.then()` 中启动**

### 切换操作（交易所/品种/周期）

- 必须使用 `Promise.allSettled` 并行执行 settings 更新、品种列表、首根 K 线
- 需实现 `_inflightSwitch` 防重入机制
- 品种列表缓存：`Map<exchange, symbolList>` + 10 分钟 TTL
- API 请求必须使用 `AbortController` 设置 15 秒超时
- 同数据源切换时需跳过 `connect()` 重连以复用连接
- 后端错误响应必须包含 `error_type: connection|symbol|timeout` 分类
- 后端响应需设置 `Cache-Control: max-age=600` 头
- `applySubscribe` 成功后必须调用 `loadSettings()` 同步 `currentSettings`

### TradingView 数据源认证

- **认证优先级**：`session_id` > `username+password` > `匿名`
- **凭证来源优先级**：`settings.json` > `.env` 环境变量 > 匿名
- 获取美股数据（如 NVDA/NASDAQ）必须配置凭证，匿名访问会被限流
- **auth token 缓存**：TTL=24h；key=`(username, sha256(password))`；只缓存登录成功响应，失败响应不污染缓存
- tvDatafeed 2.1.0 库自身的 `__auth` 已失效（无 session/UA），必须 monkey-patch；失败有 fallback

### K 线按钮联动

- **按钮按域分组**：工具栏=数据流开关（仅「实时」）；侧边栏=分析控制（分析/等待收盘/持续分析/增量）
- **持续分析联动规则**：开启时强制勾选并禁用「实时」+「等待收盘」（依赖 SSE bar_close 事件）；关闭时恢复可编辑
- **哨兵去重**：`keepAnalysisLastClosedTs` 变量，bar_close 事件仅在 `ts_open` 变化时触发分析
- **图表暂停**：分析期间暂停 `bar_update` 的 K线渲染（仍更新 next_close_ts 和状态栏），完成后调用 `loadBars()` 刷新
- **倒计时统一 HMS 格式**：所有倒计时使用 `formatCountdownHMS()` 函数显示 `HH:MM:SS`
- **倒计时共享 tick**：SSE 活跃时「等待收盘」按钮必须复用 `sseStatusExpiryTimer`（由 `updateSSEStatusWithExpiry` 统一更新），不创建独立 setInterval。通过 `waitCloseCountdownResolver` 全局变量在 remaining <= 0 时触发分析。禁止维护两个独立定时器——会导致两个 UI 不同步、算法不一致、sanity check 逻辑分叉

### SSE / 实时刷新

- **SSE 连接 `onopen` 事件中不设置 `sseLastBarUpdateTs`**，仅启动定时器
- **时间剩余计算需通过 `timeframeToSeconds()` 函数与后端对齐**，并进行上限检查（`remaining > tfSecs` 则丢弃值）
- **`updateSSEStatusWithExpiry` 的 `tfSecs` 需优先从 `#ds-timeframe` 读取用户选择值**，fallback 到 `currentSettings`
- **后端 `done` 事件推送前必须检查 `exception` 字段**，有异常时不推进到完成状态
- **休市时（`forming_bar.closed == True`）后端仅推 ping 不推 bar_close 事件**
- **`_compute_next_close_ts()` 必须使用 `elapsed % duration` 取模算法**，不可用简单的 `ts_open + duration`（会产生时区偏移）
- **`seconds_until_bar_closes` 需加入绝对时间判断**：`now_ms >= ts_open_ms + duration_ms` 时返回 0

### 数据快照契约

- **正常模式**：bars 数组包含 `n+1` 个 bar，`bars[0]` 为未收盘 forming bar（seq=0, closed=False）
- **休市模式**：bars 数组包含 `n` 个已收盘 bar，`bars[0].seq=1, closed=True`
- **休市检测必须短路取模算法**：`/api/bars/next-close` 检测 `bars[0].closed == True` 时必须返回 `market_closed: true`、`next_close_ts: null`，不可调用 `_compute_next_close_ts`（取模算法会基于过期 ts_open 返回错误的未来周期边界时间戳）
- **前端休市感知**：`loadBars` 检测 `bars[0].closed === true` 时清空 `sseNextCloseTs = 0`；`fetchAndUpdateNextCloseTs` 收到 `market_closed: true` 时清空；`updateSSEStatusWithExpiry` 检测 `sseNextCloseTs` 已过期时主动调 REST 检测休市

### 品种迁移逻辑

- `migrate_general_gold_defaults` 仅在 symbol 为黄金关键词（XAUUSD/GOLD/XAU）时强制修正为 OANDA/XAUUSD
- 非黄金品种（如 NVDA/AAPL/TSM）保留用户选择配置，不做强制迁移

### 前端进度条

- 需根据分析阶段失败情况调用 `setFlowBarFailed(failedStep)` 标记失败步骤

### 主副图同步

- 主图和副图时间轴同步必须使用**时间范围（时间戳）**而非逻辑范围（数据点索引）

### 侧边栏折叠/展开

- K线图宽度调整需使用缩短的 CSS transition（0.05s linear）
- 在 `requestAnimationFrame` 循环中加入 `void pane.offsetWidth` 强制 reflow
- 进行 150ms 兜底循环确保最终尺寸对齐

### 静态资源

- 所有静态资源响应头必须设置 `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0` 以强制实时加载
- 每次修改前端资源需同步更新 HTML 中的版本号（如 `?v=12`）

### UI 风格

- 按钮视觉风格统一为 **Aurora Quant Console**（4 层阴影 + LED 指示灯 + shimmer 微光）
- UI 字体用 IBM Plex Sans，状态/数值用 JetBrains Mono
- TRAE 内置 webview 中禁用 `alert()`（会触发 React error #185 崩溃），用 `showToast(message, type)` 替代

---

## 核心技术概念

### SSE (Server-Sent Events)
- **端点**：`/api/bars/stream`
- **事件**：`bar_update`、`bar_close`、`ping`
- **关键字段**：`next_close_ts`（下一 bar 收盘时间戳）

### FlowBar 进度条（6-step）
- **步骤**：1=等待数据 → 2=阶段一推理 → 3=阶段一验证 → 4=阶段二推理 → 5=阶段二验证 → 6=完成
- **函数**：`setFlowBarStep(step)`、`setFlowBarFailed(failedStep)`

### 增量分析
- **目的**：减少 token 消耗（约 14.5K tokens），保持 AI 上下文连贯性
- **触发**：手动点击「增量」按钮或「持续分析」在 bar_close 事件触发
- **机制**：重用之前的 Stage1 上下文（system+user+assistant），仅发送新的 bars

### 三层配置覆盖
- **优先级**：shell 环境变量 > .env > settings.json
- **说明**：`.env` 为可选，文件不存在时 `env_loader` 不执行操作

### 降级轮询模式
- **触发**：SSE 连接失败时自动切换
- **间隔**：3 秒轮询 + 5 秒拉取 `next-close`
- **功能**：保证基本的实时数据更新和倒计时显示

---

## 已知问题

- **模型 API 连接失败**：本地模型 API 服务器 `192.168.2.177:8082` 未运行，导致分析失败（已通过 `.env` 配置切换到可用 endpoint 解决，但配置项仍可能被误填回内网地址）
- **Demo 模式缺失**：Web UI 缺乏桌面 GUI 有的 demo 模式
- **resendLastChat 死代码**：`web/static/js/app.js` 中函数存在但无 UI 按钮绑定
- **经验库系统数据为空**：`experience/` 目录无数据文件，经验库检索与应用功能空跑
- **移动端未适配**：当前 UI 为桌面端设计，移动端显示效果差
- **国际化缺失**：所有文案硬编码中文，无多语言支持

---

## 后续迭代需求

1. **经验库系统完善** ⭐ 高优：添加经验数据文件，实现经验库检索和应用功能
2. **Demo 模式**：实现 Web UI 的 demo 模式，便于用户体验
3. **resendLastChat 功能**：添加 UI 按钮绑定，实现重新发送最后一条消息功能
4. **前端顶部健康状态指示**：轮询 `/api/health`，`degraded`/`error` 时顶部红条提示
5. **`_PRACTICAL_UNLIMITED_MAX_TOKENS` 按 provider 动态读模型上限**：当前用静态默认值
6. **`max_output_tokens` 前端可配**：当前只能在 `settings.json` / `.env` 配置，前端设置面板未暴露
7. **移动端适配**：响应式布局，关键操作在移动端可用
8. **性能优化**：页面加载速度、渲染性能、SSE 长连接内存泄漏排查
9. **国际化支持**：添加多语言支持（中/英）

详细规划见 [TODO.md](TODO.md) 第五节「后续可推进的优化」。
