# PA_AGENT 项目规范 (AGENTS)

## 项目概述

PA_AGENT 是一个基于 AI 的量化分析工具，提供实时行情数据、智能分析预测、决策树可视化等功能。

---

## 文档分工

| 文档 | 用途 | 内容 |
|---|---|---|
| **AGENTS.md**（本文件） | 规范与约束 | 维护规范、核心约束、技术概念、已知问题、后续需求 |
| [SESSION_CHANGES.md](SESSION_CHANGES.md) | 改动记录（协作） | **开工前必查、完工前必写**：谁正在改哪些文件、接口变更、冲突风险 |
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

### 多会话协作规范（多人/多 Agent 并行时强制）

仓库可能同时有多个会话（多个 Agent、多个开发者）在改。**不写改动记录就会静默覆盖别人的未提交工作。**

- **开工前必查 [SESSION_CHANGES.md](SESSION_CHANGES.md)**：看「🔴 进行中」区有没有人正在改你要改的文件。有则先沟通或避开，不要直接写。
- **开工时立刻占坑**：在「进行中」区加条目，把**打算写**的文件列全。晚写等于没写 —— 别人已经改完了你才登记，冲突照样发生。
- **完工时补齐**：状态改为 `已提交 <commit>`，核对改动文件清单（开工时列的和实际有出入的，以实际为准），并写明**接口变更**（API 路径/字段、配置项、事件名、DOM id）。
- **被否决的方案也要写进「方案」**：后来人不知道你为什么没走那条路，很可能重复踩。
- **既有失败/不稳定测试要写进「冲突风险」**：否则别人会把自己没改坏的东西算到自己头上。
- **检查是自动的，别只靠自觉**：`tools/check_write_scope.py` 已实现两层检查
  - **本地 pre-commit**（`.githooks/pre-commit`，本仓库已启用 `core.hooksPath=.githooks`）：
    暂存文件命中他人「进行中」范围 → **硬阻断**。这是唯一还来得及挽回的时刻
  - **CI**（`.github/workflows/ci.yml` 的 `write-scope` job）：改了代码却没更新
    SESSION_CHANGES.md → **失败**；命中他人「进行中」范围 → 仅告警
  - 确认无冲突要绕过：`SKIP_WRITE_SCOPE_CHECK=1 git commit ...`
- **条目标题用 `###`，其下小节必须用 `####`**：解析器取「最近的 `###`」作为会话名，
  小节也写 `###` 会让报错指不出是谁占用
- **不要用子串替换改 Markdown 层级**：`"### 冲突风险"` 是 `"#### 冲突风险 ⚠️"` 的
  子串（从第 2 个字符起），会把 `####` 变成 `#####`。必须用行锚定的正则
- **与 CHANGELOG.md 的分工**：CHANGELOG 记「行为变了什么」，面向读者、事后补写、允许合并同类项；**SESSION_CHANGES 记「谁在改哪个文件」，面向并行会话、实时写、禁止合并与省略**。两者都要写，不可互相替代。
- **`git add` 改的是共享暂存区，不是会话私有的**：并行会话在同一工作树下
  `git add` 后、自身 `git commit` 前，另一会话的提交会把**本会话暂存的文件
  一并带走**。2026-10-05 实测：`app.js` 被对方的 auth 提交捎走（代码完好、
  归属混乱），E2E 文件被误提交又被对方 revert 删除。
  规避：① 每会话用**独立 worktree**（最彻底）；② 提交用
  `git commit -- <显式文件列表>`，不依赖暂存区，且 add 与 commit 之间不穿插
  他人操作；③ 至少 commit 后立即 `git show --stat HEAD` 核对归属
- **发现别人留下未提交的改动时不要顺手清理**：要么沟通确认，要么原样保留。只把自己明确认领过的文件纳入提交。

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

### 禁止用通配符删除数据路径（2026-10-05 实际事故）

- **绝不写 `rm -f records/pa_agent.db*` 这类通配符删除**：`*` 会一并匹配
  `-wal` / `-shm`，**以及你自己事先做的备份**（`pa_agent.db.pre-recover-*`）。
  2026-10-05 实测：一条为「确认测试没污染真实库」而写的
  `rm -f records/pa_agent.db*` 直接删掉了生产库和自己的备份。所幸
  `config/settings.json` 完好，baseline 能重新播种，27 条记录与 TV 凭证全部
  自动重建 —— **但这是运气，不是设计**。
- **判断「测试有没有写脏真实库」的正确做法是比对 mtime/size**，不是删完再查
  在不在：删除本身就是破坏，且「文件不存在」无法区分「测试没碰」与「刚被我删了」
- **`tests/conftest.py` 已在导入期把 `PA_AGENT_DB_PATH` 重定向到临时目录**，
  测试根本不碰生产库。因此验证命令里**不需要任何清理动作**；
  用 `stat -c "%Y:%s"` 前后比对即可
- 备份文件**不要与被备份对象同名前缀**（`x.db.pre-*` 会被 `x.db*` 命中）。
  要么放独立目录，要么用 `.bak/` 子目录

### 代码修改后必须重启服务器（最高优先级）

- **修改 Python 后端代码（`web/`、`pa_agent/`）后必须重启 uvicorn 服务器**：uvicorn 默认不监视文件变化，不重启则改动不生效，用户看到的仍是旧代码行为（常被误判为「浏览器缓存」）。
- **推荐启动方式**：`python -m uvicorn web.server:app --host 0.0.0.0 --port 8000 --reload --reload-dir web --reload-dir pa_agent`，`--reload` 模式会自动监视 Python 文件变化并热重载，避免手动重启遗漏。
- **修改前端静态资源（`web/static/`）后必须同步更新 HTML 版本号**：`index.html` 中所有 `?v=N` 引用（CSS/JS）必须递增。虽有 `_NoCacheStaticFiles` 强制 `Cache-Control: no-cache`，但 TRAE 内置 webview 可能忽略 no-cache 头，版本号是兜底失效手段。
- **完成任何代码修改后，必须重启服务器并验证 HTTP 响应**：用 `Invoke-WebRequest http://localhost:8000/` 确认版本号已更新、Cache-Control 头正确，再告知用户「修改完成」。违反此约束会导致用户反复反馈「还是老样子」「缓存问题」，严重损害信任。

### 前端事件绑定

- **`bindEvents()` 必须在所有 `await` 数据加载之前调用**：数据加载失败（如 `loadBars()` throw 异常）不应影响 UI 可交互性。违反此约束会导致所有按钮失去响应。
- **`startSSEBarsStream` 必须在 `loadBars.then()` 中启动**（现已改为轮询流，函数名保留）

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

- **数据源模式只有三种，且必须一眼可辨**：`live` / `replay` / `demo`。入口唯一是 `setDataMode()`，由它驱动右上角模式条（`#data-mode-bar`）的三个 LED、状态条文案、图表染色与只读态。**禁止在别处单独改 class 或 dataset**
- **非实时模式（回看 / Demo）侧边栏只读**：追问、验证经验、重跑分析都没有意义，还会把 Demo 数据写进记录或经验库。分析按钮**隐藏**而非 disabled —— 灰按钮会让人反复去点它为什么点不动
- **`enableChat()` 必须尊重只读态**：无条件 `input.disabled = false` 会覆盖掉 `setPanelsReadonly()` 的禁用，导致回看记录时追问框仍可用
- **CSS 属性选择器要用 attribute 设置**：用 `classList.toggle('data-x')` 加类名，CSS 侧 `body[data-x]` 属性选择器选不中，样式静默失效
- **`#readonly-hint` 必须在 `#sidebar` 内部**：`#main` 是 row flex，放在外面会成为图表与侧边栏之间的第三个 flex item，占掉一整列宽度且不显示任何内容
- **从 Demo 返回实时必须无条件重载 K 线**：Demo 覆盖主图数据却不改订阅，品种/周期可能与演示内容对不上，只清叠加层不够
- **模式切换必须重置面板内容**：`setDataMode()` 只管 LED / 染色 / 只读，**不管内容** —— 状态与内容是两套东西。回到实时时必须调 `resetAnalysisPanels()`，它统一走各渲染函数的空态分支。**只置 `lastRecord = null` 是不够的**：面板 innerHTML 里仍留着上一条记录的渲染结果
- **渲染函数必须能接受空记录**：`renderDecision` / `renderFuturePanel` / `renderDecisionTree` 直接访问 `record.stage2_decision`，传 null 会抛 TypeError；`renderStreamFromRecord(null)` / `renderTokenUsage(null)` 静默 return，同样不清内容。新增/修改任何「渲染某条记录」的函数都要显式处理 `!record`
- **可选链只对「已声明为 undefined」生效**：写成 `updateFlowBarIdle?.()` 而该函数根本不存在时，仍会抛 ReferenceError。函数是否存在要用 `typeof x === 'function'` 判断，不要靠加 `?.` 蒙混
- **端到端测试必须断言「内容」而非「状态位」**：面板可见 / dataset 值 / classList / 消息条数只能证明**机制触发**，不能证明**结果正确**。断言要看面板当前显示的内容是否属于当前模式，并把多个操作串成一条**状态迁移链**逐段验证（`replay → 返回实时` 必须是一次连续走查，不能拆成两个独立步骤）
- **CI 的绿灯必须对应真的跑了测试**：`test` job 曾只做 `pip install` + `import pa_agent` 就算过，且 `pip install -e ".[dev]"` 里的 **`[dev]` extra 从未定义** —— pip 只 warning 后继续，pytest 根本没装上。**加 CI 前先确认被装的 extra 真的存在**，否则是「假绿灯」，比没有 CI 更危险
- **失败基线必须从「已提交的干净状态」生成，绝不能用脏工作区**：2026-10-05 实测
  ——在多会话并行、别人正重构未提交时生成基线，把 **40 项「别人半成品造成的失败」**
  当成存量记了进去。纯净 `HEAD` 实测只有 32 项，脏工作区却是 102 项。结果是
  **CI 对这 40 项真实回归保持绿灯，基线替未完成的代码背了书**。
  正确做法：`git archive HEAD | tar -x -C /tmp/head` 导出纯净树再跑 pytest。
  判据：若工作区有他人未提交改动，基线一律不可信
- **只解析 pytest 的 `short test summary info` 段**：全文扫描会命中
  **Captured log 段**里的应用日志行（`ERROR web.api.routes_data:...`），
  把它当成名为 `web.api.routes_data:...` 的测试报成「新增回归」。应用日志里
  出现 ERROR 是正常运行的一部分，与测试成败无关
- **存量测试失败会让 CI 永久红，必须用基线**：`tools/ci_diff_baseline.py` 比对
  `tests/ci/baseline_failures.txt`，**存量失败降级为警告、新增失败才红**。不设基线
  → CI 永远红 → 没人再看 CI；不报 → 就是假绿灯。基线是快照，**不替代修测试**；
  存量修好后 CI 会提示哪些项可从基线移除
- **`pip install -e .` 装不上 Web 依赖**：fastapi / uvicorn / sse-starlette 都在
  `[web]` extra 里，不是核心依赖。CI 里起服务必须 `".[web,dev]"`
- **runner 要贴近生产**：单测跑 `windows-latest` 但部署是 Docker/Linux，
  且 `MetaTrader5` 是 win32 专属 —— Linux 上行为不同，等于测了个非生产环境
- **测试必须接进 CI 才算数**：只写在仓库里、本地手动跑一遍，等于没写 —— 下次改动照样没人被拦。`tests/e2e` 由独立的 `e2e` job 承载（起真实服务 + Playwright）
- **E2E 播种必须由服务端做**：CI 是空库，核心用例会静默 `skip`（全绿但什么都没测）。但**绝不能让测试进程自己写文件/写库** —— 宿主机与容器是两套文件系统视图（`/root/.../records/pending` vs `/app/records/pending`，同一 inode、不同挂载点），`_db_candidates` 的 `f.resolve().relative_to(RECORDS_DIR)` 必然失败，表现为「文件在、库里有、API 就是查不到」。正确做法是请求仅在 `PA_AGENT_E2E=1` 时注册的服务端播种端点，由它用自己的 `RECORDS_DIR` 与 `upsert_record`
- **播种后要用服务端同一套 schema 自检**：不通过就 500。列表接口会**静默过滤**校验不过的记录，CI 上表现为「播种成功但测试没测到」，极难定位
- **部署前必须全量语法自检**：`web/` 与 `pa_agent/` 两个目录要么都覆盖、要么都不覆盖。本轮两次把另一会话的半成品代码打进镜像（`IndentationError` 的编辑中间态、容器缺 `chat_repo`）
- **新写的测试要做「能否抓到 bug」的反向验证**：把修复回退，确认测试变红。没做过这一步的测试，无法区分「真的没问题」与「根本没测到」
- **「分析」按钮自动选路**：有可复用上下文走增量、否则走完整，按钮文案与 tooltip 必须说明它会走哪条路。「强制完整」开关供用户覆盖
- **按钮按域分组**：工具栏=数据流开关（仅「实时」）；侧边栏=分析控制（分析/等待收盘/持续分析/增量）
- **持续分析联动规则**：开启时强制勾选并禁用「实时」+「等待收盘」（依赖 bar 收盘判定）；关闭时恢复可编辑
- **哨兵去重**：`keepAnalysisLastClosedTs` 变量，仅在 `ts_open` 变化时触发分析（触发源已由 bar_close 事件改为本地定时器，见「K 线实时刷新」节）
- **持续分析触发时禁止再次等待收盘**：`startAnalysis` / `startIncrementalAnalysis` 必须接受 `triggerSource`（`'user'` / `'continuous'`），由 `web/static/js/continuous_gate.js::shouldWaitForClose` 判定。`'continuous'` 表示本次调用本身就是被 `bar_close` 触发的，此时 bar 刚刚收盘，**再等一根必然出错**——与「持续分析强制勾选等待收盘」的联动规则叠加后会形成自等待，被下一次 `bar_close` 内的 `stopWaitCloseCountdown()` 取消成 `resolve(false)`，表现为持续分析整周期延迟或时灵时不灵。新增触发路径时必须透传 `'continuous'`
- **`closedBarTs()` 必须按 `closed` 标志查找**：硬编码 offset=2 假定末位恒为 forming bar；休市模式下全部 bar 已收盘，此时「刚收盘」就是最后一根，offset 会返回两根之前的 ts → bar_close 哨兵错位（重新开盘后漏触发或重复触发持续分析）
- **`lastBars` 是 newest-first**：`/api/bars` 返回 bars[0]=forming bar（数据快照契约）。按 ts_open 定位元素，**不要**用 `lastBars[length-1]` 当最新根 —— 那是**最老**的一根
- **品种选择器：聚焦 = 浏览，输入 = 搜索**：聚焦时展示「常用 + 分类」清单；拿输入框里已有的当前品种去搜只返回寥寥几条，看起来像功能坏了
- **纯逻辑抽到 `continuous_gate.js`**：「刚收盘 bar 的 ts_open」与「是否需要等待收盘」是无 DOM 依赖的纯逻辑，禁止再内联回 `app.js`。三处哨兵计算曾重复三份且必须永远一致，抽成唯一实现由 Node 单测 `continuous_gate.test.js` 守护
- **取消「等待收盘」勾选必须调用 `stopWaitCloseCountdown()`**：只停显示定时器不够。`refreshAnalyzeButtonWaitingState()` 会把按钮置回 `idle`，而 `updateSSEStatusWithExpiry` 中 `if (btn.dataset.state !== 'waiting') return` 会提前返回，导致 pending resolver 无人 resolve，`startAnalysis` 永久 await
- **图表暂停**：分析期间暂停 K 线渲染（仍更新 `lastBars`、next_close_ts 和状态栏），完成后调用 `loadBars()` 刷新
- **倒计时统一 HMS 格式**：所有倒计时使用 `formatCountdownHMS()` 函数显示 `HH:MM:SS`
- **倒计时共享 tick**：「等待收盘」按钮必须复用 `sseStatusExpiryTimer`（由 `updateSSEStatusWithExpiry` 统一更新），不创建独立 setInterval。通过 `waitCloseCountdownResolver` 全局变量在 remaining <= 0 时触发分析。禁止维护两个独立定时器——会导致两个 UI 不同步、算法不一致、sanity check 逻辑分叉

### K 线实时刷新（2026-10-05 起为前端轮询，非 SSE）

- **服务端 SSE 已下线**：`GET /api/bars/stream` 及后台广播循环全部移除，
  `routes_bars_stream.py` 只剩 `_compute_next_close_ts`（`routes_data.py` 的
  跨模块硬契约，**不得删**）。前端改调 `startLiveRefresh(5000)` 轮询
  `/api/bars`，**按各自会话游标**取数
- **为什么不用服务端 SSE 分组**：浏览器原生 `EventSource` **无法设置请求头**，
  服务端拿不到会话身份，任何按会话分组的方案都无法实施；且坏品种的 auto-probe
  会持 `_snapshot_lock` 数十秒，全站 `/api/bars` 排队。轮询路径本就存在，且
  隐藏标签页自动停，一个坏 tab 不会传染别人
- **轮询化后必须成立的约束**（缺一个就静默坏掉）：
  - 游标 DOM（`#ds-symbol`/`#ds-exchange`/`#ds-timeframe`）必须在**启动轮询之前**
    写好 —— 轮询会同步发请求，顺序反了会按旧游标取数
  - `fetchAndUpdateNextCloseTs` 不得有「SSE 活跃就不拉」的短路，否则
    `next_close_ts` 永远拿不到，倒计时与「等待收盘」全废
  - `refreshBarsOnly` 必须尊重 `chartUpdatePaused`：仍更新 `lastBars`，
    只跳过 `applyBarsToChart`，否则分析期间图表每 5s 跳一次
  - 休市判定不能靠「一个周期无 bar_update」（SSE 断流判据）——轮询下
    `liveRefreshLastTs` 每 5s 都更新，该判据恒不成立，须用 `market_closed` 标志
- **持续分析触发**：由本地 3000ms 定时器调 `PAContinuousGate.closedBarTs()`
  判定，**不再依赖 bar_close 事件**；`triggerSource` 仍必须透传 `'continuous'`
- **`_compute_next_close_ts()` 必须使用 `elapsed % duration` 取模算法**，
  不可用简单的 `ts_open + duration`（会产生时区偏移）

### 数据快照契约

- **正常模式**：bars 数组包含 `n+1` 个 bar，`bars[0]` 为未收盘 forming bar（seq=0, closed=False）
- **休市模式**：bars 数组包含 `n` 个已收盘 bar，`bars[0].seq=1, closed=True`
- **休市检测必须短路取模算法**：`/api/bars/next-close` 检测 `bars[0].closed == True` 时必须返回 `market_closed: true`、`next_close_ts: null`，不可调用 `_compute_next_close_ts`（取模算法会基于过期 ts_open 返回错误的未来周期边界时间戳）
- **前端休市感知**：`loadBars` 检测 `bars[0].closed === true` 时清空 `sseNextCloseTs = 0`；`fetchAndUpdateNextCloseTs` 收到 `market_closed: true` 时清空；`updateSSEStatusWithExpiry` 检测 `sseNextCloseTs` 已过期时主动调 REST 检测休市

### 品种迁移逻辑

- `migrate_general_gold_defaults` 仅在 symbol 为黄金关键词（XAUUSD/GOLD/XAU）时强制修正为 OANDA/XAUUSD
- 非黄金品种（如 NVDA/AAPL/TSM）保留用户选择配置，不做强制迁移
- 加密货币代码（BTCUSDT/ETHUSDT…）同样**保留原样**，不迁移为黄金默认品种

### Prompt cache 预热

- **分析前必须预热**：服务端对**逐字相同的前缀**做缓存（实测同一 prompt 连发两次 → 100%），但**存活期远短于人工分析间隔**（相隔 9 小时的两次真实分析缓存率仅 0.2%，而它们有 58.3% 的 prompt 逐字相同）。靠「等上一轮缓存」不可行，必须在真实请求前发一条同前缀、`max_tokens=1` 的廉价请求主动预热
- **必须接 `chat()` 和 `stream_chat()` 两个入口**：分析走的是流式路径，只改非流式等于没生效
- **预热失败必须完全吞掉**：它只是优化，绝不能阻断或搞挂真实请求。低于 20k chars 不预热（小 prompt 收益不抵一次往返）
- **只预热完整消息**：半条消息对不齐缓存块边界，且可能写出永远用不上的缓存条目
- **缓存命中率必须透出到界面**：否则无法判断预热是否生效。开关为 `provider.prompt_cache_prime`（默认 true）

### 分层约束：Web 层禁止依赖 PyQt（最高优先级）

- **`web/` 下的任何模块禁止 import `pa_agent.gui.*`**：`pa_agent/gui/__init__.py` 会 `import MainWindow → ChartWidget → pyqtgraph`，而 Docker 镜像**不安装 PyQt6/pyqtgraph**（见 `web/Dockerfile`），一旦引用就是启动期 ImportError
- **需要被 Web 复用的纯逻辑必须放在 Qt-free 包内**：决策/门控类助手统一放 `pa_agent/ai/`（如 `order_opportunity.py`、`decision_stance.py`、`decision_continuity.py`）
- **GUI 侧保留兼容层**：抽离后 `pa_agent/gui/<mod>.py` 改为 re-export 同名符号，GUI 现有 import 不受影响
- **默认数据源必须是 `tradingview`**：`pa_agent/data/factory.py::DATA_SOURCE_CHOICES` 只暴露 tradingview，MT5 仅 Windows 可用；而 `config/settings.json` 在 `.gitignore` 中，全新 Docker volume 无该文件会走代码默认值，若默认 `mt5` 则 `create_data_source()` 抛 `DataSourceTransientError` 且被 `AppContext.bootstrap()` 的 `except Exception` 吞掉 → **应用静默启动但完全没有数据源**。修改 `GeneralSettings` 默认值时必须同步这条

### 经验库闭环（写入端）

- **两阶段状态机**：`pending`（入场瞬间写）→ `win` / `loss`（触及 TP/SL）/ `unresolved`（走满 N 根仍未触及，终态无盈亏）。**只有 win/loss 会被 `ExperienceReader` 读到**，未决 setup 不得当成失败经验喂回提示词。状态是**列**不是目录，详见下节
- **写入方唯一入口**：`pa_agent.records.experience_writer.ExperienceWriter`（阶段一 `save_pending_if_resolvable()`，阶段二 `finalize()`/`update_pending_progress()`），**只写库、不写文件**
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
- **数据来源必须可追溯**：经验条目只有一种来源 —— `ExperienceWriter` 真实写入。**不得手写行造经验**。真实盈亏是连续分布，若发现 `pnl_pct` 取值高度重复、`entry_price` 成等差数列、创建时间集中在同一分钟，即为合成数据，必须隔离并告知用户。磁盘上遗留的 `experience/.seed_demo_*/`（2026-10-05 文件布局废弃前的手工种子）**已彻底隔离，永不入库**
- **watcher 必须在轮询前后各校验一次订阅**：`data_source` 是全局共享、订阅绑定的单例，用户随时会切品种/周期。只做前置校验仍有竞态窗口（取数过程中被改掉）→ 两种情况都会拿**另一个标的**的 K 线判定本单，凭空写出胜负
- **入场锚点不得为 0**：`after_ts_open_ms` 必须晚于最后一根**已收盘** bar（`bars[0]` 是 forming bar，取 `bars[1]`）。为 0 时过滤条件退化成 `ts_open > 0`，入场**之前**的历史 K 线会被当成本单走势。锚点缺失一律放弃写入
- **`data_source` 必须显式传参**：不要用 `getattr(record, "_data_source")` / `getattr(frame, "data_source")` —— `AnalysisRecord` 与 `KlineFrame` 都没有这些属性，会恒为 `None` 导致整条链路静默变死（曾如此）
- **枚举展示统一走 `pa_agent.ai.display_labels`**：格式 `中文 (raw)` —— 中文给操作者读，括号里的 raw 值用于和提示词、落盘目录名对账。新增枚举展示字段必须用 `label_for()`，不要在模板里就地翻译
- **空值必须返回 `''`**：`label_for('')` 返回空串而非「未知 ()」，这样模板能整块隐藏该字段
- **经验库范围恒等于当前 K 线**：`GET /api/experience` 的 `symbol` / `timeframe` **始终**取自 `#ds-symbol` / `#ds-timeframe`，前端不提供手动选择控件（只有「市场周期」可筛）。经验库的意义是「我正在看的这个标的、这个周期上历史上怎么走」，让用户另选等于把它变成另一个功能。`applySubscribe()` 末尾必须调 `loadExperienceLibrary()`，否则切品种后面板停在旧结果上
- **经验库浏览必须先过滤**：`GET /api/experience` 支持 `symbol` / `timeframe`，按**条目内容**过滤而非文件名（同一代码会出现在不同市场周期下）。前端默认勾选「跟随当前订阅」；用户手动选下拉会自动取消跟随，避免两控件互相覆盖。`cycles` 汇总计数必须跟着过滤，否则前端显示的数字对不上
- **读取端默认必须 > 0**：`experience_max_entries` 默认 0 会让整条检索链路空跑；新增/修改 PromptSettings 时注意该默认值

### 经验库：库是唯一真源（2026-10-05 起，不再有文件）

- **状态即列，不是目录**：`experience_entries.status ∈ {pending, win, loss, unresolved}`。
  流转是一条 UPDATE（`entry_id` 不变），不再是「把文件搬到另一个目录」——
  搬文件在崩溃时会留下半套状态。旧目录名 `*_cases/` 仅作为
  `STATUS_DIRS` 兼容字典保留，**不得再据它推导状态**
- **写入方唯一入口**：`ExperienceWriter`。它**不写任何文件**，`experience_dir`
  形参已是兼容用的空壳。读端（`ExperienceReader`）同样只查库，**没有文件回落**
- **状态词表必须与 `experience_writer.STATUS_*` 完全一致**：
  曾是 `_VALID_STATUSES=("success","failure",...)` 而写入端发 `"win"/"loss"`，
  `upsert_entry` 的兜底分支把**每一条**都静默改写成 `"pending"` —— 已结算的
  经验在库里全显示为待验证，检索端永远取不到，整个经验库静默失效且从不报错。
  由 `test_storage_dualwrite.py::test_status_vocabulary_guard` 守护
- **只有 `win`/`loss` 可被检索**（`RETRIEVABLE_STATUSES`）。`unresolved` 是
  终态但**无盈亏结论**，把它当失败经验喂回提示词会凭空制造大量不存在的错误经验
- **`entry_id = <user_id>_<秒级时间戳>_<uuid8>`**：全局主键，而
  `ON CONFLICT DO UPDATE SET` 的列清单里**没有** `user_id`。曾用裸文件名做主键，
  跨用户同记录时后者静默覆盖前者
- **`user_id` 必须一路落库**：`save_pending()` 写进 content，`finalize()` 不传
  user_id 时**沿用记录自带的**（先不过滤地读出来才知道归属）。结算跑在调度器
  线程上、结算的是几小时前的记录，那时没有请求上下文 —— 记录本身是唯一依据。
  传一个**不匹配**的 user_id 去结算必须干净失败（否则 A 能改写 B 的经验结论）
- **`hub.query()` 返回 `[]` 有两种含义**：「表里确实没有」与「读不出来」同形。
  不得用「查完再去读 hub 标志位」判断（读后时序）；`experience_repo` 的查询
  返回 `QueryResult(list)`，`.failed`/`.error` **随结果一起**交出
- **`hub.read_failed` 是线程局部的**（`_read_error` 在 `threading.local` 里）。
  曾是普通实例属性且被 `query()` 成功时清空，A 线程的失败标记会被 B 线程的
  任意一次成功读抹掉 —— 高并发读下那放行的正是最需要降级的那一刻
- **DB 逻辑必须放在 `ExperienceReader.read_top5()` 内部**（唯一漏斗），
  不要改 `read_for_stage2`；绕过它会让 14 处 `mock.read_top5` **静默失效**
- **`direction` 比较前必须归一**：阶段一输出 `bullish/bearish/neutral`，
  阶段二 `order_direction` 输出 `做多/做空`。直接比字符串则永远不等，
  +2 分恒为 0，检索退化成「只看形态交集」且毫无报错
### 复盘是混合的：程序层（必做）+ LLM 层（可选）

- **程序层**：`review_program.build_program_review()`，结算时由
  `settle_record` 自动生成，`source='program'`。纯算术、零成本、不依赖模型，
  **同输入必同输出因而可以写断言**。verdict 取自**闭词表**
  （尚未判定 / 被结果证实 / 判断成立但运气不佳 / 与走势相悖 / 窗口内未触及）
- **`loss` 判「运气不佳」的门槛是 `MFE ≥ 0.5 × 风险距离`**（`LUCKY_MFE_RATIO`），
  不是 `MFE > 0`。写成 `> 0` 时，一进场就逆向、最大浮盈 0.01% 而回撤 11%
  的单子也会被判「判断成立」—— MAE 都算出来了却不参与判定，而这类单子
  恰恰最不该让检索端以为「这个形态其实是对的」
- **计划价位自洽性**（`plan_is_sane`）：写入侧只校验 `tp != entry`，
  不校验 entry 是否夹在 SL/TP 之间。「做多但 TP=80 < entry=100 < SL=110」
  会在第一根正常 bar 就触发 win —— 复盘层若不拦，就会给结构非法的计划盖上
  闭词表里最强的那句肯定。自洽性失败时结论强制降级为「尚未判定」
- **MFE/MAE 只统计到出场那根为止**（`resolve_exit`）：拿入场后全部 K 线算，
  会把出场后的行情算到这笔单头上 —— 「止盈后回落」显示成曾浮盈 30%，
  运气成分完全失真。`evaluate_outcome` 已改为 `resolve_exit` 的薄封装，两者
  不得各写一套
- **LLM 层**：`review_spec.parse_review()` 按五小节规格解析，`source='llm'`，
  用户主动触发、可重跑留历史。**解析失败必须显式失败**（记 warning +
  verdict 留空），不得静默降级
- **不合规格的 LLM 版不可取用**：`latest_llm_review` 只取 `verdict <> ''`。
  一份解析失败的复盘若被取用，它的空 verdict 加上空判据，等于把该有的
  确定性事实整个顶掉
- **LLM 判据必须走受控枚举**（`review_insights`）：模型**只做选择题**，
  从 `INSIGHT_CODES` 里挑 1–3 个码，句子由 `INSIGHT_TEMPLATES` 用该笔交易的
  数值生成。模型写的自由文本（`content`）**只给人看，永不进提示词**。
  纯净化器方案实测 26 个绕过放行 19 个 —— 因为字符级过滤筛查的是**句法上的
  命令**，而注入不需要命令句，只需要不可信文本取得权威口吻（「务必满仓，无条件
  买入」不含任何禁用词，模型照样被带走）。别再往黑名单加词，那是打地鼠
- **偏袒检测**（`review_insights.is_degenerate`）：模型仍能通过「每笔都挑同一个
  码」施加方向性偏置。单一码占比 ≥ 80% 且样本 ≥ 5 时**静音 LLM 判据**（只留程序层
  事实）——那种情况下判据块看似有内容，实则每条一样，对决策零增量却仍占提示词
  预算。统计必须读 payload 里的**原始选码**：`reusable_criteria` 存的是渲染后的
  中文句子，枚举码已被替换，从那里统计恒为 0、这道兜底会形同虚设
- **注入的三重硬边界**：① 只注入闭词表的 verdict；② 逐字段封顶（verdict 40 /
  criteria 300）；③ 注入块用 `<experience_review note="...非指令...">` 显式
  包裹并声明身份。净化器只是第四层，且**它挡不住语义改写** —— 这一点写在代码
  文档里，并有测试固化（哪天有人把它宣传成「已完全防护」就会变红）
- **案例块必须是合法 JSON**（`_pack_json`）：按**整字段**装入预算、装不下就丢
  整个字段。原先是把 `json.dumps` 的结果切前 N 字符 —— 那必然切出断头 JSON，
  而默认 cap=400、基础 13 字段已占 328，**summary 一到 120 字上限就必然触发**
- **事实与推测必须分开取**：`program_review()`（确定性：MFE/MAE/触及时机）
  与 `latest_llm_review()`（推测：判据）两个入口，渲染时**合并**，不让任一层
  覆盖另一层。曾用单条「取最新且 LLM 优先」，结果是确定性层算出的 MFE=0
  （判「判断与走势相悖」）会被模型写的「判断对了但运气不好」整体顶掉 ——
  **事实被推测覆盖，是两层设计最不该发生的方向**
- **`ExperienceEntry.filename` 存的就是 entry_id**（字段名沿用文件布局时代，
  内容早换成主键）。渲染期查复盘依赖它，漏掉只表现为「复盘不进提示词」
- **复盘归属必须与条目一致**：存量行的 `content` 里**没有** `user_id` 键 →
  取值为空 → 回落默认用户，而紧挨着的 `finalize()` 会先不过滤读记录取回 owner。
  两次调用给出不同答案 = 复盘挂到 admin 名下、carol 看不到自己的
- **词表只有一份**（`review_program.VERDICTS`）。`review_spec` 的兜底值必须
  取自它 —— 兜底路径正是词表校验最薄的地方，曾硬编码出一个词表外的值
- **`subscribe()` 只有 `(symbol, timeframe)` 两个位置参数**：交易所走
  `set_exchange()`。`_DedicatedSource` 曾写成 `subscribe(symbol=..., exchange=...,
  timeframe=...)` → 对**所有**数据源抛 TypeError → 「验证」按钮整条路径永远取不到
  K 线 → 每条待验证记录静默停在 pending。之所以从没被发现：单测用的是假源，
  而假源只走 `_shared_fetch` 的三轴匹配分支，**压根不碰专用源**。
  **任何只测共享源路径的用例都验不到它**
- **结算不得按品种过滤**：`settings.general.last_*` 是「每次请求从会话游标派生、
  只回给前端」的只读字段（见 `routes_settings._CURSOR_FIELDS` 注释），
  `/api/subscribe` 早已不更新它 —— 调度器读到的是**冻结的旧值**。实测切到
  NVDA/5m 后点「验证」得到 `checked=0`（快照里还是 BTCUSDT），等于所有非该
  品种的记录**永久结算不了**
- **保护上游要限制「工作量」而不是「正确性」**：正确形态是 `max_dedicated`
  预算 —— 共享源三轴匹配时复用（零成本），不匹配才建专用源，每轮最多 N 条，
  剩下的下一轮再来（`list_pending` 由旧到新，不会饿死后面的）。按品种过滤是用
  「永久结算不了」换「不超预算」，后者有预算就能解决
- **取不到数据必须计数并告警**：交易所/品种组合无效时 TradingView 永远无数据，
  原先每轮只 `skipped_no_data += 1`，**无限静默重试**，界面上就是一条永远停在
  「待验证」却看不出为什么的记录。`_note_no_data` 记 `_no_data_attempts`，
  达 `MAX_NO_DATA_ATTEMPTS` 打 ERROR。**刻意不转 unresolved** —— 那个状态
  的语义是「窗口内未触及价位」，与「压根取不到行情」不是一回事
- **`_no_data_attempts` 必须从库里读当前值**，不能用调用方传的 `content` ——
  那是 `list_pending` 在本轮开始时的快照，每轮都是同一个值，计数器永远停在 1
- **经验条目的交易所必须与 symbol/timeframe 同源**：`routes_analyze` 刻意用
  **本次分析**的 `view_symbol`/`view_timeframe`（不能用全局订阅），而交易所若
  在 `spawn_post_order_followup` 内部从 `settings.general.last_tradingview_exchange`
  读，就与前两者**取自两个真相源** —— 三轴不一致会写出**永远结算不了**的条目。
  真机实测：`GATEIO/NVDA`（美股挂在加密交易所下），取数失败累计 6 次仍 pending
- **后台结算必须遍历所有用户**：`list_pending(user_id="")` 会经 `_owner("")`
  回落成 `DEFAULT_USER_ID`，而 `list_entries` 原本恒为 `user_id = ?` —— 结算
  跑在调度器线程上没有请求上下文，于是**只有 admin 的记录会被结算**，其他用户
  的经验写进去了却永远停在 pending、永远进不了检索端。三档必须分开：
  `None` = 不过滤（结算用），`""` = 回落默认用户，`有值` = 按该用户
- **成功路径的日志也会撒谎**：`save_pending_if_resolvable` 返回 entry_id（str），
  调用点却写 `staged.name` → **每次成功写入都抛 AttributeError** 并被外层
  except 报成「experience stage-1 failed」。记录写进去了，日志说失败，排查直接
  被带偏。**写入成功与否不能只看日志**
- **测试 fixture 不得 `close_all()` 全局 hub**：早先某用例自建 hub 后
  `close_all()`，同进程后续测试拿到已关闭的连接，一批无关用例随机变红
  （实测 `test_record_user_isolation` 5 条）。复用 `conftest` 的
  `db_path_isolated` 就没这问题
- **反向验证要看「撤掉后哪条变红」，而不是「全部都红」**：断言写成
  `"exchange=view_exchange" in getsource(module)` 时，删掉出问题的那一处仍绿 ——
  因为该串在文件里出现 3 次。必须断言**那一个调用块**
- **重建表必须先 `PRAGMA foreign_keys=OFF`**：`connect()` 开着 FK，而重建要
  `DROP TABLE`，SQLite 视为删全部行 → `ON DELETE CASCADE` **静默清空子表**，
  `migrate()` 还返回 True（实测重建 `experience_entries` 会删光
  `experience_reviews`）。且该 PRAGMA **在事务内是 no-op**，关闭与恢复
  都必须先 `commit()` —— 恢复那句静默无效会让全局一直停在 OFF
- **曾经复盘从未进过提示词**：`_persist_review` 没传 `verdict`/`criteria`，
  渲染层 `if crit or verdict` 恒为假，全链无报错。根因是测试直接调
  `attach_review(..., verdict=...)` 走了只有测试会走的分支 ——
  **集成测试必须走生产路径**（`settle_record` / `_persist_review`）
- **`CREATE TABLE IF NOT EXISTS` 对已存在的表完全无效**，加列必须写进
  `schema.MIGRATIONS` 的 ALTER。漏了的后果特别隐蔽：INSERT 报
  "no column named source"，而 `db.execute` 把它当 transient 吞掉 ——
  结算照常成功、复盘一条都没写进库，只留一行 warning
- **`experience_max_chars_per_entry` 上限 `le=4000` 装不下真实 payload**
  （实测 7032 字符，`analysis_context` 从第 471 字符才开始）。**调参不是捷径**：
  `_render_experience` 必须字段感知地挑字段，并把复盘结论**单独追加**
- **经验库测试必须自带库隔离**：落库后不再有「只写 tmp_path」这回事，
  不隔离就会读到别的用例写的行 —— 断言照样绿，验的不是它声称的东西

### 分析记录：读端只查库（2026-10-05），用户隔离已补上

- **列表与详情都只查库**，正文取 `payload_json`，**不再读任何文件**。
  磁盘仍保留副本（回放体量大、删除顺序仍先删文件），但那已是**只写不读**的归档
- **`record_id` 必须与改造前逐字一致**（相对 `RECORDS_DIR` 的路径、无 `.json`）
  —— 前端把它当 URL 路径段回放，改格式等于让所有历史回看点不动。
  由 `repositories.get_record_detail(user_id, file_path)` 精确匹配，**必须带
  user_id**
- **补上一个真实的用户隔离漏洞**：旧的 `_file_candidates` 自愈回退按
  exchange/symbol/timeframe 扫盘，**唯独没有 user_id 判断** —— DB 一抖动或刚
  播种完没数据就走那条路，A 能看到 B 的历史记录；而磁盘 JSON 本身不含任何
  用户标记，事后无法补救。详情端点此前也是直接 `open()` 且完全不过滤用户，
  任何人拿到 URL 就能读到别人的完整 stage1/stage2 推理。该回退已整体删除
- **主键是文件 stem**（`repositories._record_basename`），依赖文件名唯一。
  生产命名带 `uuid8` 后缀故实际安全，但**测试里写同名文件会被第二条 UPDATE
  掉** —— 构造多条记录时文件名必须各不相同（曾因此让「跨品种浏览」用例
  只看到一条而无从解释）
- **测试播种必须双写**：只 `write_text` 不走 `upsert_record`，读端只查库后会
  全部拿到空列表。`tests/unit/test_routes_records.py::_write_record` 已内置镜像
- **写入归属必须走「记录自带」这条通道**（2026-10-06 修漏洞）：
  `PendingWriter._resolve_owner()` 的解析顺序 = 显式入参 →
  `record.meta.user_id` → `DEFAULT_USER_ID`。主通道是第 2 步
  （`two_stage._build_empty_record` 早就把 `submit()` 的 user_id 盖进了
  `RecordMeta.user_id`）。**不要**改成「要求每处 `save_partial` 都记得传
  `user_id=`」：漏一个就是一条静默落到 admin 名下、用户列表永远为空、
  **且不报错**的记录。`ctx.pending_writer` 是进程级单例，把归属挂在实例
  属性上会在并发分析下互相串写，故只能做成参数
- **`upsert_record` 的 `ON CONFLICT` 列清单必须含 `user_id`**：归属只在 INSERT
  时生效的话，同 record_id 复用时永远卡在首次写入的那个用户
- **`DELETE /api/records/{id}` 必须判归属**：`request: Request` 是必需的，
  删之前用 `get_record_detail(user_id=…, file_path=…)` 过一遍，取不到即 404。
  「不属于你」与「不存在」必须**逐字同形**（同一 status + 同一 detail）——
  区分开等于向探测者确认某个 record_id 确实存在
- **`hub.execute()` 返回的是「SQL 执行成功」，不是「影响了多少行」**：
  `DELETE ... WHERE` 匹配 0 行同样返回 `True`。**绝不可**拿它当「删掉了」的
  证据上报 —— 那会让接口在跨用户删除时**谎报** `db_deleted: true`。需要行数
  请用新增的 `execute_count()`（返回 `int | None`，`None`=写失败、`0`=成功但
  没匹配，两者必须分开看）。**不要**改 `execute()` 的返回类型：全仓 90+
  调用点几乎全是 `ok = execute(...)` / `return execute(...)`，`0` 与 `False`
  同为假会把「0 行匹配」与「执行失败」永远焊在一起
- ⚠️ **尚未修的同类漏洞（2026-10-06 登记）**：
  `pa_agent/records/analysis_history.py::find_latest_successful_record()`
  的签名是 `(symbol, timeframe, exchange, directory)` —— **没有 `user_id`**。
  它是「增量分析锚点」（`routes_analyze.py`）与「追问锚点回落」
  （`routes_chat.py`）的取数入口，而 `prompt_assembler.build_incremental_stage1`
  会把 `previous_record.stage1_response["content"]` **原文注入**提示词
  ⇒ 非 admin 用户点「增量」会把 admin 的完整 stage1 推理灌进自己的上下文。
  新增任何「按标的找上一条记录」的入口时，**必须先确认它带 user_id 过滤**

### 侧边栏 tab 分组与子 tab

- **顶层只有 6 个 tab**：分析 / 预测 / 决策树 / 决策 / 追问 / 经验库（顺序固定，不可随意调换）
- **两组通过面板内子 tab 合并**：「分析」= 流式分析(`stream`) + 原始数据(`raw`) + 文件与经验(`debug`)；「决策树」= 问答回放(`tree`) + 流程图(`tree-viz`)
- **清理函数要清干净所有派生 UI**：`clearOverlays()` 必须一并清 `#experience-legend` —— 价格线被清了、图例留着就成了无对应线条的陈旧说明。派生**图例/标记**的函数若定义了就要有人调用，否则就是死代码
- **LWC marker 的时间必须落在真实 bar 上**：不存在的时间点会被静默丢弃。老案例的入场 bar 不在当前窗口时应不画并在图例注明，而不是无声消失
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

### K 线实时刷新
- **形态**：前端按会话游标轮询 `GET /api/bars`（间隔 5000ms）+ 拉 `next-close`
- **已下线**：SSE 端点 `/api/bars/stream` 与后台广播循环（2026-10-05，
  原因见硬约束节）。`bar_update` / `bar_close` / `ping` 三个事件不再存在
- **会话隔离**：取数入参化（`latest_snapshot(n, exchange=, symbol=, timeframe=)`），
  缓存按 `(exchange,symbol,timeframe,n)` 分键

### FlowBar 进度条（6-step）
- **步骤**：1=等待数据 → 2=阶段一推理 → 3=阶段一验证 → 4=阶段二推理 → 5=阶段二验证 → 6=完成
- **函数**：`setFlowBarStep(step)`、`setFlowBarFailed(failedStep)`

### 增量分析
- **目的**：减少 token 消耗（约 14.5K tokens），保持 AI 上下文连贯性
- **触发**：手动点击「增量」按钮，或持续分析定时器判定新收盘后触发
- **机制**：重用之前的 Stage1 上下文（system+user+assistant），仅发送新的 bars

### 配置真源在 DB（2026-10-05 重大改造）

- **`pa_agent.storage.settings_store` 的级联是唯一权威**：
  `baseline`（系统兜底，SQLite）← `overrides`（用户级 admin）。
  `config/settings.json` 已**降级**为「首次播种源 + 灾备兜底」，不再是运行时真源
- **`load_settings()` 走 DB 优先、文件回退**；文件回退时会把内容
  **首次**升格为 baseline（`seed_from_file()`）
- **用户改动必须走 `apply_user_change()`**，它只把**差异**存进用户配置区。
  禁止用 `save_settings()` 整份写文件 —— 绕过级联，且若传入默认值构造的对象会
  直接摧毁配置（2026-10-05 实际发生：base_url 被重置为 api.deepseek.com、
  api_key 清空，容器内 OpenAI 客户端构造失败，分析功能整体不可用）
- **字段归级见 `docs/SESSION_STORAGE_DESIGN.md` §5**：游标（symbol/timeframe/
  exchange/data_mode/keep_analysis/wait_close）属 L3 会话级；凭证属 L1 勿动；
  `experience_verify_mode` / `experience_max_wait_s` 判定正确，保持 L1
- **播种顺序不能反**：先把 `settings.json` 修对，再 `seed_from_file()`。
  反过来会把损坏的文件升格成系统兜底，污染所有用户
- **改 `settings.json` 用「合并」而非整体替换某个段**：整体替换会连带弄掉
  该段里其他会话新增的字段（曾把 `prompt_cache_prime` 一并弄丢）

### 三层配置覆盖
- **优先级**：shell 环境变量 > .env > settings.json
- **说明**：`.env` 为可选，文件不存在时 `env_loader` 不执行操作

## 已知问题

- **模型 API 连接失败**：本地模型 API 服务器 `192.168.2.177:8082` 未运行，导致分析失败（已通过 `.env` 配置切换到可用 endpoint 解决，但配置项仍可能被误填回内网地址）。注意 2026-10 实测还存在「模型免费期结束」类 404（`base_url` 可达但模型不可用），`/api/health` 会显示 `degraded`/`model_api: error`
- ~~**经验库系统数据为空**~~（2026-10 已闭环）：`experience/` 实际有 59 条数据（此前文档记载有误）。
  缺失的是**写入方**（`ExperienceReader` 文档明写 strictly read-only，全仓无写入代码）
  与**浏览入口**，且 `experience_max_entries` 默认为 0 导致读取链路长期空跑。
  现已补齐：`experience_writer.ExperienceWriter` + `experience_watcher`（TP/SL 触达后回写）
  + `GET /api/experience` + 侧边栏「经验库」tab + `experience_max_entries` 默认 3
- **移动端未适配**：当前 UI 为桌面端设计，移动端显示效果差
- **国际化缺失**：所有文案硬编码中文，无多语言支持
- **`/api/bars` 不接受品种参数**：该端点只接受 `count`，数据取自**本会话游标**（2026-10-05 起，不再读全局 `settings.general.last_*`）；而 `/api/bars/next-close` 仍接受并回显 `symbol/timeframe/exchange`。两个端点对同一请求可能返回不同品种，属于**已知接口不一致**，前端必须先 `POST /api/subscribe` 再 `GET /api/bars`。待统一
- **`_build_incremental_stage1_user_prompt` 为死代码**：`pa_agent/ai/prompt_assembler.py` 中该方法全仓无调用者（增量路径走 `build_incremental_stage1` 的续写变体）
- **429 耗尽后被归类为网络错误**：`two_stage._stream_chat_resilient` 中 `_is_network_error` 匹配 `openai.APIStatusError`（`RateLimitError` 的父类），持续限流时会静默轮换 provider fallback 而不是直接报错

---

## 后续迭代需求

1. **经验库系统完善** ⭐ 高优：添加经验数据文件，实现经验库检索和应用功能
2. **移动端适配**：响应式布局，关键操作在移动端可用
3. **性能优化**：页面加载速度、渲染性能、会话注册表内存占用排查
4. **国际化支持**：添加多语言支持（中/英）
5. **统一 `/api/bars` 与 `/api/bars/next-close` 的参数契约**：要么都接受 `symbol/timeframe/exchange`，要么都只读订阅状态，避免前端拿错品种
6. **AI provider 预设与字段校验**：桌面 GUI 有 cursor/qclaw/workbuddy/trae_cn 等预设和「model / BaseURL 填反了」的守卫（`gui/settings_dialog.py:335-446`），Web 端目前是裸文本框，Docker 用户需手填
7. **SummaryStrip 指标条**：GUI 的 5 项指标条（趋势/市场周期/下周期/支撑/阻力，`gui/widgets/summary_strip.py:7-16`）未移植，数据其实已在 Web payload 里
8. **TradingView 连通性诊断**：GUI 的 `tv_connectivity_dialog` 提供 MT5/云端回退建议与 wiki 链接，Web 端只有 toast（错误分类 `error_type` 反而更好）
9. **Demo 模式增强**：当前 `web/api/routes_demo.py` 是随机游走的合成数据，不支持真实记录回放与自动串联（真实记录回放已由历史记录功能覆盖）
10. **删除 `pa_agent/gui/`**：桌面 GUI 代码 1.7 万行已成死代码，且上游仍在积极修改它（每次同步都产生删除冲突；`.github/workflows/sync-upstream.yml` 已预设「keep our deletion」策略）。前置条件：把 `pa_agent/gui/stage2_payload.py` 等无 Qt 依赖的模块移出 `gui/`（`tests/unit/test_validation_retry.py` 目前依赖它）

详细规划见 [TODO.md](TODO.md) 第五节「后续可推进的优化」。
