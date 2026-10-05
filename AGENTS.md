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
- **持续分析触发时禁止再次等待收盘**：`startAnalysis` / `startIncrementalAnalysis` 必须接受 `triggerSource`（`'user'` / `'continuous'`），由 `web/static/js/continuous_gate.js::shouldWaitForClose` 判定。`'continuous'` 表示本次调用本身就是被 `bar_close` 触发的，此时 bar 刚刚收盘，**再等一根必然出错**——与「持续分析强制勾选等待收盘」的联动规则叠加后会形成自等待，被下一次 `bar_close` 内的 `stopWaitCloseCountdown()` 取消成 `resolve(false)`，表现为持续分析整周期延迟或时灵时不灵。新增触发路径时必须透传 `'continuous'`
- **纯逻辑抽到 `continuous_gate.js`**：「刚收盘 bar 的 ts_open」与「是否需要等待收盘」是无 DOM 依赖的纯逻辑，禁止再内联回 `app.js`。三处哨兵计算曾重复三份且必须永远一致，抽成唯一实现由 Node 单测 `continuous_gate.test.js` 守护
- **取消「等待收盘」勾选必须调用 `stopWaitCloseCountdown()`**：只停显示定时器不够。`refreshAnalyzeButtonWaitingState()` 会把按钮置回 `idle`，而 `updateSSEStatusWithExpiry` 中 `if (btn.dataset.state !== 'waiting') return` 会提前返回，导致 pending resolver 无人 resolve，`startAnalysis` 永久 await
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
- 加密货币代码（BTCUSDT/ETHUSDT…）同样**保留原样**，不迁移为黄金默认品种

### 分层约束：Web 层禁止依赖 PyQt（最高优先级）

- **`web/` 下的任何模块禁止 import `pa_agent.gui.*`**：`pa_agent/gui/__init__.py` 会 `import MainWindow → ChartWidget → pyqtgraph`，而 Docker 镜像**不安装 PyQt6/pyqtgraph**（见 `web/Dockerfile`），一旦引用就是启动期 ImportError
- **需要被 Web 复用的纯逻辑必须放在 Qt-free 包内**：决策/门控类助手统一放 `pa_agent/ai/`（如 `order_opportunity.py`、`decision_stance.py`、`decision_continuity.py`）
- **GUI 侧保留兼容层**：抽离后 `pa_agent/gui/<mod>.py` 改为 re-export 同名符号，GUI 现有 import 不受影响
- **默认数据源必须是 `tradingview`**：`pa_agent/data/factory.py::DATA_SOURCE_CHOICES` 只暴露 tradingview，MT5 仅 Windows 可用；而 `config/settings.json` 在 `.gitignore` 中，全新 Docker volume 无该文件会走代码默认值，若默认 `mt5` 则 `create_data_source()` 抛 `DataSourceTransientError` 且被 `AppContext.bootstrap()` 的 `except Exception` 吞掉 → **应用静默启动但完全没有数据源**。修改 `GeneralSettings` 默认值时必须同步这条

### 经验库闭环（写入端）

- **两阶段状态机**：目录即状态 —— `pending_cases/`（入场瞬间写）→ `success_cases/`(win) / `failure_cases/`(loss) / `unresolved_cases/`（走满 N 根仍未触及，终态无盈亏）。**只有 win/loss 会被 `ExperienceReader` 读到**，未决 setup 不得当成失败经验喂回提示词
- **写入方唯一入口**：`pa_agent.records.experience_writer.ExperienceWriter`（阶段一 `save_pending_if_resolvable()`，阶段二 `finalize()`/`update_pending_progress()`）。`_status_subdir()` 必须用 `STATUS_DIRS[status]` 取目录名 —— 误把 status 本身当目录名会写出 `pending/` 而非 `pending_cases/`，reader 读不到
- **阶段二必须自备数据源**：`ctx.data_source` 是订阅绑定的单例，待验证记录可能挂几小时后才结算。共享源仅在 (exchange, symbol, timeframe) **三者全等**时复用，否则为该记录单独建源并用完即弃；再叠一道 `bars_belong_to_instrument()` 价格量级兜底。宁可保持 pending，也绝不用别的标的判定
- **阶段二接线**：`web/api.experience_verifier.verify_pending()`；`POST /api/experience/verify/once` 为 UI 的「验证」按钮与后台调度器的共用入口（走 `experience_scheduler.run_once` 的单飞守卫）
- **后台结算必须有调度器**：`experience_scheduler` 在 lifespan 启动 daemon 线程。单飞守卫保证 pass 不重叠；范围每轮**重读 settings**（用户随时会切品种）；**只结算当前订阅范围且只用共享数据源** —— 结算其它品种要每条建一个 TradingView 连接，放定时器上会打爆上游
- **`experience_verify_mode`**：`manual` 只关掉**定时器**，不关掉「验证」按钮（`run_once(force=True)`）
- **复盘要求案例自描述**：`save_pending()` 必须写 `analysis_context`（阶段一判断要点 + 阶段二决策）与 `bars_snapshot`（入场前后各 20 根）。只有 `pnl_pct` 的记录会让模型事后诸葛亮。压缩时丢 prompt/response 原文，长文本截 600 字、列表取前 12 项
- **前端解析 SSE 必须归一化 CRLF**：sse_starlette 用 `\r\n\r\n` 分隔事件，按 `\n\n` 切会永远切不出完整事件、流式输出停在「生成中…」
- **胜负判定**：`evaluate_outcome()` 按后续 K 线判定 TP1/SL 谁先触及。**同一根 bar 同时触及两者时必须按止损计** —— OHLC 无法还原 intrabar 路径，按乐观计会把经验库偏向虚高胜率
- **未了结的计划不写入**：触及任一价位前超时（`experience_max_wait_s`，默认 24h）即丢弃
- **触发点**：`order_followup.spawn_post_order_followup()`（与通知同一入口，AGENTS.md 单一入口约束）
- **必须 daemon 线程 + 分步 try/except**：轮询数据源可能失败/超时，任何异常只记 warning，**绝不能冒泡进分析主流程**
- **数据来源必须可追溯**：`experience/` 下的条目只有两种来源 —— ① `ExperienceWriter.save()` 真实写入；② 早期手工种子数据。**不得手写 JSON 造经验**。真实盈亏是连续分布，若发现 `pnl_pct` 取值高度重复、`entry_price` 成等差数列、文件 mtime 集中在同一分钟，即为合成数据，必须隔离（`experience/.seed_demo_*/`，点号前缀会被 API 目录枚举过滤）并告知用户
- **watcher 必须在轮询前后各校验一次订阅**：`data_source` 是全局共享、订阅绑定的单例，用户随时会切品种/周期。只做前置校验仍有竞态窗口（取数过程中被改掉）→ 两种情况都会拿**另一个标的**的 K 线判定本单，凭空写出胜负
- **入场锚点不得为 0**：`after_ts_open_ms` 必须晚于最后一根**已收盘** bar（`bars[0]` 是 forming bar，取 `bars[1]`）。为 0 时过滤条件退化成 `ts_open > 0`，入场**之前**的历史 K 线会被当成本单走势。锚点缺失一律放弃写入
- **`data_source` 必须显式传参**：不要用 `getattr(record, "_data_source")` / `getattr(frame, "data_source")` —— `AnalysisRecord` 与 `KlineFrame` 都没有这些属性，会恒为 `None` 导致整条链路静默变死（曾如此）
- **枚举展示统一走 `pa_agent.ai.display_labels`**：格式 `中文 (raw)` —— 中文给操作者读，括号里的 raw 值用于和提示词、落盘目录名对账。新增枚举展示字段必须用 `label_for()`，不要在模板里就地翻译
- **空值必须返回 `''`**：`label_for('')` 返回空串而非「未知 ()」，这样模板能整块隐藏该字段
- **经验库范围恒等于当前 K 线**：`GET /api/experience` 的 `symbol` / `timeframe` **始终**取自 `#ds-symbol` / `#ds-timeframe`，前端不提供手动选择控件（只有「市场周期」可筛）。经验库的意义是「我正在看的这个标的、这个周期上历史上怎么走」，让用户另选等于把它变成另一个功能。`applySubscribe()` 末尾必须调 `loadExperienceLibrary()`，否则切品种后面板停在旧结果上
- **经验库浏览必须先过滤**：`GET /api/experience` 支持 `symbol` / `timeframe`，按**条目内容**过滤而非文件名（同一代码会出现在不同市场周期下）。前端默认勾选「跟随当前订阅」；用户手动选下拉会自动取消跟随，避免两控件互相覆盖。`cycles` 汇总计数必须跟着过滤，否则前端显示的数字对不上
- **读取端默认必须 > 0**：`experience_max_entries` 默认 0 会让整条检索链路空跑；新增/修改 PromptSettings 时注意该默认值

### 侧边栏 tab 分组与子 tab

- **顶层只有 6 个 tab**：分析 / 预测 / 决策树 / 决策 / 追问 / 经验库（顺序固定，不可随意调换）
- **两组通过面板内子 tab 合并**：「分析」= 流式分析(`stream`) + 原始数据(`raw`) + 文件与经验(`debug`)；「决策树」= 问答回放(`tree`) + 流程图(`tree-viz`)
- **可被 innerHTML 重写的容器不能包固定子节点**：`#chart-legend` 每次刷新指标图例都会被 indicators.js 整体重写，任何常驻子节点（如 `#experience-legend`）必须放在**兄弟**位置，否则会被静默抹掉
- **写文件前先断言**：本轮三次出现「后续 assert 失败导致 `open(p,'w')` 未执行」，改动静默丢失、部署的是旧模板 —— 有一次 `order_followup` 的整段替换因此没落地，容器里跑的还是旧调用却毫无察觉。编辑脚本必须把断言放在写入之前，并在写入后回读校验关键符号
- **只有单视图的面板不要画子 tab 条**：单项子 tab 是纯噪音（如「追问」）
- **任何解锁「追问」的路径都必须调 `enableChat()` + `renderChatContext()`**：此前只在真实分析的 `done` 事件里调，demo 路径漏掉 → demo 下追问输入框永远禁用。新增演示/回放/加载入口时务必一并调用
- **子 tab 不做 DOM 嵌套**：两组面板是同级兄弟、共享侧边栏同一槽位（`.tab-panel` 默认 `display:none`，`.active` 才占位）。切换时必须在**同组全部面板**间转移 `.active`，否则会出现两个面板同时占位
- **品种搜索走 scanner，不要用 tvDatafeed 的 search_symbol**：后者对任意查询都返回非 JSON（已失效）。可用的是 `search_tv_symbols()`（POST `scanner.tradingview.com/{market}/scan`，无需登录），覆盖 crypto 64k / futures 52k / america 20k / china 7.4k / forex 6.3k / hongkong 3k。`symbol-search.tradingview.com` v3 已 403 不可用
- **scanner 交易所名与我们的不一致**：`GATEIO` 在 scanner 里叫 **GATE**（见 `TV_SEARCH_EXCHANGE_ALIASES`），漏了映射会一个都搜不到
- **scanner 返回值必须清洗**：原始结果里大量永续 `.P`、杠杆代币 `.3L/.5S`、外汇券商变体 `.ONE/.PRO.OTMS/.SML…`、wrapped/staked 币。`_is_derivative()` 负责识别；**顺序必须是「先相关性排序、再过滤」**，反过来（先分组后整体重排）等于没过滤
- **TradingView 港股代码不补前导零**：`0700`/`0388` 等在 TV 上不存在，必须写 `700`/`388`。新增港股代码前先用 scanner 验证
- **内置表完整性由测试守护**：`tests/unit/test_symbol_presets.py` 要求每个交易所 ≥8 个品种、无重复、且**每个品种都必须有中文名**。新增品种必须同步 `TV_SYMBOL_NAMES`，否则测试会失败
- **结果项禁止内联 onclick**：品种代码/名称来自外部数据，字符串插值进 HTML 有注入风险且遇引号即破坏结构。统一用 `data-symbol` + 事件委托
- **按成员反查分组**：`SUBTAB_GROUPS[target]` 在 target 不是组键时查不到，必须 `find(g => g.includes(target))`——曾因此导致切到流程图时 `#tab-tree` 未被移除
- **控件 id 必须保持不变**（`#btn-tree-viz-*`、`#tab-tree-viz`、`#tab-debug` 等），否则既有 JS 引用要大面积改动

### 历史回看必须联动主图

- `replayRecord()` 除重渲染侧边栏外，**必须**调用 `applyReplayChart(record)`：切订阅 → `loadBars()` → `clearOverlays` → `setDecisionOverlays` + `setDirectionMarker` + `_renderTradeLegend`
- **判断是否切换订阅不得依赖工具栏标签**：标签与后端订阅可能不一致（别处直接调过 `/api/subscribe`），按标签判断会跳过切换导致图上是错误品种。回看一律**无条件按记录对齐**
- **方向箭头必须用显式锚点**：`setDirectionMarker(series, decision, anchorTimeSec)`。不传参时回退到最新一根 bar（实时/demo 正确），但历史回看**必须**传「该记录分析当时那根 bar」的时间，否则箭头会画在今天的 K 线上
- **「没传」与「明确不画」必须区分**：用 `anchorTimeSec === undefined` 判断是否显式传入，**不能用 `!= null`** —— 回看在锚点落到数据范围外时会传 `null` 表示不要画，`null != null` 为 false 会掉进回退分支，恰好复现要修的 bug
- **锚点按 `closed` 标志找，不要硬编码 `kline_data[1]`**：`bars[0]` 并不总是 forming bar（休市或快照未带时它本身就是已收盘的），index 1 会指向倒数第二根。用 `two_stage._pick_last_closed_bar()`
- **历史记录的锚点在读取时修正，不改写磁盘**：`_derive_anchor_bar_ts_ms()` 从记录自身 `kline_data` 现算权威锚点（kline_data 不可变，不会漂移），修复早期记录 JSON 里烙错的值
- **视窗锚点可能在数据范围外**：老记录（如回看 8 月 ETH 而当前只有 10 月数据）硬对齐会被钳到序列边界、视窗退化成两三根超宽 K 线。锚点落在 `[首根, 末根]` 之外时改为回退到近期窗口
- 「返回实时」必须恢复回看前订阅（取 `settings.general` 真实状态，非标签）并 `clearOverlays`
- 回看期间应取消「持续分析」勾选，避免复盘时误触发新一轮分析

### 图表数据唯一入口

- **必须经 `applyBarsToChart(bars)`**：`setBars` + `setSeqMarkers` + 指标重算（`_indicatorsAPI.onBarsUpdated`）+ `__PA_LAST_BAR_TIME__` 更新是**成套**动作
- 只调 `setBars` 会让 EMA/MACD 继续持有**上一个品种**的数据（例如切到 BTCUSDT 后 EMA 仍是 NVDA 的 210~234，而蜡烛是 48000~64000），主图自动缩放把两个数量级一起纳入 → 价格轴被拉到 -8000~66000，K 线被压成顶部一条、指标线贴地
- `setBars` 的契约是**原始 bar**（带 `ts_open`/`closed`），由它内部升序排序并换算 LWC 的秒级 `time`；**不要**预先映射成 `{time,...}`，否则 `a.ts_open === undefined` 会让时间变成 NaN 并被 LWC 抛 `Value is null`

### 下单信号推送（交易记录 + Feishu/PushPlus）

- **判定门控唯一来源**：`pa_agent.ai.order_opportunity.has_order_opportunity()`（Qt-free）。判定「是否下单机会」必须调用它，禁止在前端或路由里另写一份
- **执行入口**：`web/api/order_followup.spawn_post_order_followup()`，在 `_run_analysis` 成功提交记录后调用；桌面 GUI 的等价实现是 `MainWindow._spawn_post_order_followup`
- **必须后台线程 + 分步 try/except**：落盘与推送都可能耗时/失败，任何异常只记 warning，**绝不能冒泡进分析主流程**
- **`decision_inner` 传扁平化后的 stage2**：落盘/通知/门控都读 `order_type`、`trade_confidence`、`entry_price` 等顶层字段；原始嵌套结构在 `.decision` 里，需合并（`_flat_stage2()`）
- **总开关是 `alert_on_order_opportunity`**：关闭时既不落盘也不推送（与 GUI 语义一致）

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

- **模型 API 连接失败**：本地模型 API 服务器 `192.168.2.177:8082` 未运行，导致分析失败（已通过 `.env` 配置切换到可用 endpoint 解决，但配置项仍可能被误填回内网地址）。注意 2026-10 实测还存在「模型免费期结束」类 404（`base_url` 可达但模型不可用），`/api/health` 会显示 `degraded`/`model_api: error`
- ~~**经验库系统数据为空**~~（2026-10 已闭环）：`experience/` 实际有 59 条数据（此前文档记载有误）。
  缺失的是**写入方**（`ExperienceReader` 文档明写 strictly read-only，全仓无写入代码）
  与**浏览入口**，且 `experience_max_entries` 默认为 0 导致读取链路长期空跑。
  现已补齐：`experience_writer.ExperienceWriter` + `experience_watcher`（TP/SL 触达后回写）
  + `GET /api/experience` + 侧边栏「经验库」tab + `experience_max_entries` 默认 3
- **移动端未适配**：当前 UI 为桌面端设计，移动端显示效果差
- **国际化缺失**：所有文案硬编码中文，无多语言支持
- **`/api/bars` 忽略查询参数**：该端点只接受 `count`，实际数据取自当前订阅状态（`settings.general.last_symbol/last_timeframe`）；而 `/api/bars/next-close` 却接受并回显 `symbol/timeframe/exchange`。两个端点对同一请求会返回不同品种，属于**已知接口不一致**，前端必须先 `POST /api/subscribe` 再 `GET /api/bars`。待统一
- **`_build_incremental_stage1_user_prompt` 为死代码**：`pa_agent/ai/prompt_assembler.py` 中该方法全仓无调用者（增量路径走 `build_incremental_stage1` 的续写变体）
- **429 耗尽后被归类为网络错误**：`two_stage._stream_chat_resilient` 中 `_is_network_error` 匹配 `openai.APIStatusError`（`RateLimitError` 的父类），持续限流时会静默轮换 provider fallback 而不是直接报错

---

## 后续迭代需求

1. **经验库系统完善** ⭐ 高优：添加经验数据文件，实现经验库检索和应用功能
2. **移动端适配**：响应式布局，关键操作在移动端可用
3. **性能优化**：页面加载速度、渲染性能、SSE 长连接内存泄漏排查
4. **国际化支持**：添加多语言支持（中/英）
5. **统一 `/api/bars` 与 `/api/bars/next-close` 的参数契约**：要么都接受 `symbol/timeframe/exchange`，要么都只读订阅状态，避免前端拿错品种
6. **AI provider 预设与字段校验**：桌面 GUI 有 cursor/qclaw/workbuddy/trae_cn 等预设和「model / BaseURL 填反了」的守卫（`gui/settings_dialog.py:335-446`），Web 端目前是裸文本框，Docker 用户需手填
7. **SummaryStrip 指标条**：GUI 的 5 项指标条（趋势/市场周期/下周期/支撑/阻力，`gui/widgets/summary_strip.py:7-16`）未移植，数据其实已在 Web payload 里
8. **TradingView 连通性诊断**：GUI 的 `tv_connectivity_dialog` 提供 MT5/云端回退建议与 wiki 链接，Web 端只有 toast（错误分类 `error_type` 反而更好）
9. **Demo 模式增强**：当前 `web/api/routes_demo.py` 是随机游走的合成数据，不支持真实记录回放与自动串联（真实记录回放已由历史记录功能覆盖）
10. **删除 `pa_agent/gui/`**：桌面 GUI 代码 1.7 万行已成死代码，且上游仍在积极修改它（每次同步都产生删除冲突；`.github/workflows/sync-upstream.yml` 已预设「keep our deletion」策略）。前置条件：把 `pa_agent/gui/stage2_payload.py` 等无 Qt 依赖的模块移出 `gui/`（`tests/unit/test_validation_retry.py` 目前依赖它）

详细规划见 [TODO.md](TODO.md) 第五节「后续可推进的优化」。
