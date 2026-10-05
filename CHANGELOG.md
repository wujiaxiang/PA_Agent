# 变更日志 (CHANGELOG)

本文件按时间倒序记录项目的主要改动与迭代历史。规范的约束、技术概念、已知问题等请见 [AGENTS.md](AGENTS.md)。

---

## 2026-10-05

### 9. 分析记录读端只查库 —— 补上一个用户隔离漏洞

- **问题**：`/api/records` 的列表与详情此前都会**直接读 JSON 文件**，
  两条路径都**不过滤 user_id**：
  1. `_file_candidates` 自愈回退按 exchange/symbol/timeframe 扫盘，唯独没有
     user_id —— DB 一抖动或刚播种完没数据就走那条路
  2. `GET /api/records/{id}` 直接 `open()` 文件，任何人拿到 URL 就能读到
     别人的完整 stage1/stage2 推理（这个系统里最敏感的数据）
  而磁盘 JSON **本身不含任何用户标记**，事后无法补救
- **修复**：列表与详情一律走库（`repositories.list_records` /
  新增 `get_record_detail`，后者强制 user_id 过滤），删除
  `_file_candidates` / `_db_candidates` / `_glob_partitioned` 三个扫描器。
  `record_id` 格式保持逐字不变（前端当 URL 路径段用，改格式会让所有历史回看点不动）
- **实测**（真实服务 + 令牌）：admin 匿名请求看不到 bob 的记录；bob 带令牌只看到自己的
- **顺带暴露**：库主键是文件 stem，依赖文件名唯一。生产命名带 `uuid8` 故安全，
  但同名文件会被后者 UPDATE 掉 —— 测试构造多条记录时必须用不同文件名
- **磁盘仍保留副本**（回放体量大、交易 PNG 要发图），但对记录已是**只写不读**的归档
- **回归**：全量 unit 对比 —— 新增失败 0、新增 error 0

### 8. 经验库废弃文件布局，库成为唯一真源

- **问题**：经验库每个案例是一个 JSON 文件（「目录即状态」），读取靠扫目录。多用户
  下文件系统没有用户概念（A 的案例 B 照样能扫到）、状态流转要搬文件（崩溃留半套状态）、
  每次分析都 rglob 一遍全局目录。写端早已双写进 SQLite，读端却一直没跟上
- **改动**：
  - `ExperienceWriter` **不再写任何文件**：`save`/`save_pending`/`finalize`/
    `list_pending`/`update_pending_progress` 全部以 `entry_id` 为键；状态流转是一条
    UPDATE。`experience_dir` 形参降级为空壳兼容参数
  - `ExperienceReader` 只查库，**删除文件回落**（回落本身即旧设计的产物：同一条
    经验两个真源，静默分叉时没人知道该信哪个）
  - `GET /api/experience` 与复盘取档全部走库；`importer` 的经验导入段删除
  - **根因级修复**：`experience_repo._VALID_STATUSES` 曾是
    `("success","failure",...)` 而写入端发 `"win"/"loss"`，`upsert_entry` 的兜底
    分支把**每一条**都静默改写成 `"pending"` —— 已结算经验在库里全显示为待验证，
    检索端永远取不到，整个经验库静默失效且从不报错。这大概就是 `experience_entries`
    长期 0 行的原因。已统一为 `win/loss/pending/unresolved` 并加词表守护测试
  - **复盘落库**（原 P3）：新增 `experience_reviews` 独立表（可重跑并留历史），
    由**后端**在 SSE `done` 之前落盘 —— 依赖前端回报等于「用户关页面即丢失」
  - **复盘进提示词**（原 P4）：`_render_experience` 改字段感知渲染。原先整条
    content_json 盲截 400 字符，而 `analysis_context` 从第 471 字符才开始，
    且 `experience_max_chars_per_entry` 上限 `le=4000` 装不下实测 7032 字符
    ——**调参不是捷径**
- **测试**：重写 4 个文件、新增 `test_experience_review.py`（复盘状态机 13 例）
- **回归**：以 HEAD 干净 worktree 为基线全量 unit 对比 —— **新增失败 0、新增 error 0**，
  error 由 30 降到 0
- **未纳入本次**：分析记录（回看功能直接读 JSON）与交易记录（飞书卡片要发 PNG）
  仍以文件为权威副本，未动

### 7. 经验库读端切库（P-1 → P2）：修并发降级失效 + 三条读路径接 DB

- **问题**：`650fc8f` 落地了身份/鉴权层，但经验库**读端一行未动** —— 提示词注入、
  浏览 API、复盘取档三条路径全在文件系统扫目录。`experience_repo.list_entries`/
  `get_entry`/`count_by_status` 除测试外**零生产调用者**，`current_user_id` 也零调用者，
  `experience_entries` 表自建起就是死索引，`user_id` 列从未生效。
- **根因**：写端早就双写了，读端没跟上；而直接开写会踩三个更隐蔽的坑（见下）
- **改动**：
  - **P-1 故障隔离（切读的前置条件，三处都是会静默坏掉的缺陷）**：
    - `db.py` 的 `_read_error` 从**普通实例属性**移入 `threading.local`。它曾被
      `query()` 成功时清空，于是 A 线程的读失败标记会被 B 线程任意一次成功读抹掉，
      A 随后读到 `read_failed == False`，把「读不出来」的 `[]` 当成「库里确实没有」。
      本系统是高并发读（分析线程池 + 结算调度器 + 每次分析新建的 followup 线程），
      这不是罕见路径而是**常态路径**，且它放行的正是最需要降级的那一刻
    - `experience_repo` 新增 `QueryResult(list)`，把「本次查询是否失败」**随结果一起**
      交出去，调用方不再「查完再去读共享标志位」
    - `two_stage` 新增 `_load_experience()`：经验检索的任何异常都吞掉并降级成空列表。
      原先 reader 调用裸奔，而当时所有 `save_partial` 都在 `Stage1Done` 之前、
      下一次落盘远在其后 —— reader 一抛，**这次分析连同已经付费成功的 Stage 1 一起消失**
    - `save_partial("stage1_completed")` 前移到 `Stage1Done` 之后
  - **P-2 repo 接口**：`list_entries` 增 `statuses` 多值过滤（单值调两次会得到最多 2N 条
    候选，与「两目录合并取最新 N 条」语义不符且不报错）；`entry_id` 改为
    `<user_id>_<stem>` —— 它曾是全局主键且不在 `ON CONFLICT DO UPDATE SET` 里，
    两个用户写出同 stem 文件时后者会**静默覆盖**前者的 `content_json`
  - **P0 身份统一**：`bind_session` 改走 `current_user_id(request)`（原默认
    `"default"` 是 `users` 表里根本不存在的第三种身份）；`RecordMeta` 增 `user_id`；
    写入链全链透传 `user_id`，且 **`save_pending` 把它写进记录 JSON** —— 阶段二结算跑在
    后台调度器线程上，唯一能知道这条属于谁的地方就是记录本身
  - **P1 读端切读**：`ExperienceReader.read_top5()` 内部改为「先查 SQLite，
    查不到/读不出来再扫文件」（`SESSION_STORAGE_DESIGN` §7 的 C 阶段）。DB 逻辑刻意
    放进 `read_top5` 而不是 `read_for_stage2` —— 前者是唯一漏斗，14 处
    `mock.read_top5` 测试点因此保持有效。**读端仍然严格只读**，不做「顺手补写」：
    那会把读变成写、顶住 `max_workers=2` 的分析池（`busy_timeout=5000`），
    且破坏 AGENTS「写入方唯一入口」。顺带修 `direction` 枚举错配（落盘是
    `做多`、比对的是 `bullish`，**+2 分恒为 0**，检索退化成只看形态交集且毫无报错）
  - **P2 浏览端切读**：`GET /api/experience` 改为先查 DB，抽出 `_row_dict`/
    `_counts_by_cycle`/`_browse_payload` 供两条路径共用（口径必须一致），
    市场周期过滤下沉到共用段
- **测试**：新增 26 条用例（DB 命中不碰文件系统 / DB 空回落 / DB 读不出来回落 /
  多用户隔离 / 点号目录拒绝 / DB 与文件路径给出同一组条目 / direction 归一 /
  浏览端四条路径）。**全部做过反向验证** —— 把守卫拆掉确认测试变红。其中
  `test_read_failure_flag_is_thread_local` 第一版是**假绿**的：它等的是一个自己刚
  `set()` 的 `Event`，立刻返回，在有 bug 的代码上通过；改用两个 Event 后才真正抓到
- **顺带修**：`save()` 是唯一不镜像 SQLite 的写路径（将来复活会写出永远进不了库的条目）
- **文件**：`pa_agent/storage/db.py`、`pa_agent/storage/experience_repo.py`、
  `pa_agent/records/experience_reader.py`、`pa_agent/records/experience_writer.py`、
  `pa_agent/records/schema.py`、`pa_agent/orchestrator/two_stage.py`、
  `web/api/session_ctx.py`、`web/api/routes_analyze.py`、`web/api/order_followup.py`、
  `web/api/routes_data.py`、`docs/EXPERIENCE_READ_CUTOVER.md`
- **接口变更**：`GET /api/experience` 新增 `request` 入参；`list_entries` 增
  `statuses`；`hub.read_failed` 改为线程局部语义（调用方写法不变，并发下不再互相清标志）；
  `/api/records` 中 legacy 记录的 `meta.user_id` 为 `""`（多一个 key）
- **回归**：以 HEAD 的干净 worktree 为基线做全量 unit 对比 —— **新增失败 0、新增 error 0**，
  顺带修好 1 条既有失败
- **方案评审**：见 [docs/EXPERIENCE_READ_CUTOVER.md](docs/EXPERIENCE_READ_CUTOVER.md)，
  v1 经两轮独立评审推翻（5 个 BLOCKER），修订记录保留在文档 §7

### 6. 多会话推理收尾：SSE 下线 / 追问隔离 / 交易域补齐 / 配置层收敛

- **问题**：多标签页虽已能各取各的 K 线，但实时推送、追问、配置仍有全局串味
  1. `/api/bars/stream` 从**全局订阅**取数并广播给所有连接 → 所有标签页收到同一条数据流
  2. 追问按 `record|symbol|tf` 分桶 → 同记录被两个标签页回看时**共享对话历史**
  3. `trade_records` 四域中唯一未完成：只有建表语句
  4. **9 条**配置写路径绕过级联直接写文件，而文件在有系统兜底后**只写不读**
  5. `GET /api/settings` 返回全局游标 → 前端 `loadSettings` 覆盖本 tab 游标，
     **一次 F5 就串味**
- **根因**：SSE 的鉴权天花板 —— 浏览器原生 `EventSource` **无法设置请求头**，
  服务端拿不到会话身份，任何「服务端按会话分组」的方案都无法实施
- **改动**：
  - **SSE 下线改前端轮询**：`routes_bars_stream.py` 357→68 行，只保留
    `_compute_next_close_ts`（`routes_data.py` 的跨模块硬契约）；`startSSEBarsStream`
    改调 `startLiveRefresh(5000)`；持续分析触发从 bar_close 事件改为本地定时器，
    复用 `PAContinuousGate.closedBarTs()`，`triggerSource` 仍传 `'continuous'`
  - 补三处否则静默坏掉的联动：`fetchAndUpdateNextCloseTs` 删 SSE 短路（否则
    next_close_ts 永远拉不到）、`refreshBarsOnly` 补 `chartUpdatePaused`、
    游标 DOM 写入提到启动轮询**之前**（轮询会同步发请求，顺序反了按旧游标取数）
  - **追问隔离**：分桶键**扩键**为 `session_id|record|sym|tf|快照标志`（不是替换 ——
    `FreeChatSession._cached_prefix` 构造时一次性固化，只按 session 分桶会让
    「先追问 A 再回看 B」时 B 携带 A 的上下文**静默错答**）；`_last_record` 读改
    `SessionState.last_record`；per-session 锁移到事件循环侧（等锁不再占线程池
    worker，否则一个挂死的追问会堵死全通道）；页内 nonce 防「复制标签页克隆
    sessionStorage」
  - **trade_records 域**：CSV+PNG 为权威副本，DB 表只作索引；`trade_id` 用
    `sha256(symbol|timeframe|record_time|行号)`（CSV 无盈亏列，`pnl_pct` 恒 NULL，
    绝不拿 TP/SL 距离伪造收益率）
  - **配置层**：`normalize_raw` + `_repair_file_side` 让 DB 路径复用 legacy 迁移
    （原先 DB 路径绕过迁移段，`default_bar_count` 静默回落默认值）；9 条写路径改走
    `persist_patch`（不是 `persist(settings)` —— 那会把 15 个 .env 字段永久烧进
    user_prefs）；`GET /api/settings` 返回本会话游标
- **文件**：`web/api/{routes_bars_stream,routes_chat,routes_analyze,routes_data,routes_settings}.py`、`web/server.py`、`web/static/{js/api.js,js/app.js,index.html}`、`pa_agent/{config/settings.py,config/paths.py,storage/*,records/trade_logger.py,app_context.py,orchestrator/two_stage.py}`、`tests/unit/{test_routes_bars_stream,test_trade_repo,test_settings_cascade,test_followup_and_audit_fixes}.py`
- **验证**：全量 1307 项测试，failures 38 / errors 30 与基线**完全一致**，新增失败 0。
  实机：双标签页各取各的 K 线（tA 85881 / tB 233.9）、`next-close` 跨模块契约正常、
  历史跨会话共享、无 traceback
- **被否决的方案**：① 服务端按游标分组广播 SSE（EventSource 带不了 header，
  不可实施）；② 追问分桶键换成纯 session_id（跨记录静默错答）；③
  `persist(settings)` 整份写（会把 .env 字段烧进用户层）

---

## 2026-10-05

### 5. 多会话隔离 + SQLite 存储层 + 跨品种历史浏览

- **问题**：
  1. 服务端是全局单例（`app.state.ctx` 一次 bootstrap），`POST /api/subscribe` 直接改写全局 `data_source` 与 `settings.json` —— **A 标签页切品种会直接把 B 标签页的图切走**
  2. SSE 是模块级 `_subscribers` 广播给所有连接，**所有标签页只能看到同一条数据流**，无法隔离
  3. `routes_analyze` 的增量锚点读 `ctx.settings.general.last_symbol`（全局），**A 看 NVDA 而全局为 BTCUSDT 时，增量分析会捞到 BTCUSDT 的上一轮上下文**喂给模型（跨标的串味）
  4. `GET /api/records` 的三个过滤参数必填且前端硬编码传当前 tab 的游标 —— **历史面板只能看到当前订阅品种**，多标签页互相看不到对方分析出的结果
  5. `ctx._last_record` 是全局单值，A 的分析结果会成为 B 的追问锚点
  6. 无持久化索引层：增量分析靠 `rglob` + 全量 JSON parse + 目录 mtime 启发式缓存
- **根因**：`data_source` 同时扮演**连接**（TradingView WebSocket，应全局共享）与**游标**（`self._symbol/_timeframe`，应按会话隔离）两个角色；游标寄生在连接上，使「多标签页各自服务自己的 K 线图」不可能实现
- **改动**：
  - 新增 `pa_agent/storage/` 包：`db.py`（WAL + `threading.local` 连接 + 故障降级）、`schema.py`（7 张表 DDL）、`ephemeral.py`（会话注册表：TTL + LRU + `EphemeralBackend` 可替换协议）、`repositories.py`、`sessions.py`、`importer.py`
  - 数据分三级：**L1 全局**（凭证/全局开关，无 user_id）、**L2 用户**（记录/经验/交易/偏好/追问，带 user_id，多会话共享）、**L3 会话**（游标/视图模式/运行时开关，缓存级快照 + TTL）
  - 会话身份走 **`X-Session-Id` 请求头 + 前端 `sessionStorage` UUID**：Cookie 同源共享会让所有标签页拿到同一 id，「一 tab 一会话」直接失效
  - `routes_analyze` 增量锚点改用会话游标；`routes_data.subscribe` 同步写本 tab 游标
  - `GET /api/records` 过滤条件改为可选（留空=跨全部品种，走 SQLite 索引），摘要新增 `symbol`/`timeframe`/`exchange`
  - 前端历史面板新增「全部品种」开关；`api.js` 统一注入会话头
  - lifespan 启动时幂等导入既有记录；`/api/health` 新增 `storage` 字段
  - 选型说明：**PG→SQLite**（LXC 宿主 + AppArmor 构建受限 + 内存/Swap 已紧张）、**Redis 不引入**（用内存注册表 + SQLite 快照自实现等效语义）
- **文件**：`pa_agent/storage/*`、`web/api/{session_ctx,routes_analyze,routes_data,routes_records}.py`、`web/server.py`、`web/static/{js/api.js,js/app.js,index.html,css/style.css}`、`docs/SESSION_STORAGE_DESIGN.md`
- **验证**：新增 `tests/unit/test_storage_layer.py`(41) + `test_session_ctx.py`(19)。既有 27 条记录全量导入（22 ok / 5 partial），重复导入幂等，8 组 `(exchange,symbol,timeframe)` 组合与文件路径**逐例等价、0 处不一致**。全量 `tests/unit`+`tests/property` 对基线：1145→1187 tests，failures 38→38、errors 30→30，**新增失败 0**。实机起服验证 `/api/health` 暴露存储状态、跨品种浏览同时返回 BTCUSDT 与 NVDA
- **接口变更**：新增请求头 `X-Session-Id`（可选，缺失时回落旧行为）；`GET /api/records` 三个参数由必填改可选 + 响应新增 `symbol`/`timeframe`/`exchange`；`GET /api/health` 新增 `storage`

---

## 2026-10-03

### 4. 事件循环阻塞下线 + 记录/交易落盘原子化

- **问题**：async 路由里直接调用阻塞 I/O；记录文件名秒级碰撞；交易 CSV 整文件读改写
- **根因**：
  1. `/api/bars`（前端每秒轮询）与 `/api/bars/next-close` 直接在 `async def` 中调 `latest_snapshot()`，TradingView 源会开 WebSocket + HTTP `get_hist`；而 SSE 后台循环对**同一调用**已正确使用 `asyncio.to_thread`。一次缓存未命中即冻结整个事件循环，连带所有 SSE 流与在途请求。同类问题还有 `/api/subscribe` 的 `connect/disconnect/subscribe` + `save_settings`、`/api/feishu/test` 的 `requests.post`（10s 超时）、`/api/chat/stream` 的记录扫描、`/api/records` 的整目录扫描
  2. SSE 订阅队列 `asyncio.Queue()` 无 `maxsize`，`QueueFull` 分支是不可达死代码，后台标签页无限堆积 `bar_update`
  3. `_build_basename` 只精确到秒且无唯一后缀，同一秒内同品种两次分析写同一路径互相覆盖
  4. `_write_json` 非原子，崩溃会留下被截断的 `.json`，读取端静默跳过 → 整条记录丢失
  5. 交易 CSV 先读全量、内存追加、再以 `mode="w"` 重写：截断先于写入，崩溃或并发保存丢整段历史，且每次追加 O(n²)
- **修复**：全部阻塞调用改 `await asyncio.to_thread(...)`（`routes_analyze._run_analysis` 本就跑在 `run_in_executor`，无需改）；订阅队列 `maxsize=256` 且满时丢弃**最旧**增量事件（`bar_update` 是覆盖式快照，丢旧的安全；丢 `bar_close` 会卡住倒计时）；记录文件名加毫秒 + uuid6；`_write_json` 改同目录临时文件 + `os.replace` 原子落盘；CSV 改 per-file 锁 + 单次追加 + 仅新建时写表头
- **文件**：`web/api/{routes_data,routes_settings,routes_chat,routes_records,routes_bars_stream}.py`、`pa_agent/records/{pending_writer,trade_logger}.py`
- **验证**：新增 `tests/unit/test_record_durability.py`(6)，含 25 线程并发落盘全保留、表头唯一、同秒文件名不碰撞。全量 `tests/unit` 对基线：新增失败 0，修复 2

### 30. 修 CI 基线的两个自伤缺陷（误报 + 基线取自脏工作区）

上一条把 E2E 与单测接进 CI 后排查 72 项失败，发现主体来自**别人未提交的重构**，
转而修自己上一轮引入的两个问题。

- **`ci_diff_baseline.py` 把应用日志误当测试失败**
  - 原正则 `^(FAILED|ERROR)\s+(\S+)` 全文扫描，会命中 **Captured log 段**：
    `ERROR    web.api.routes_data:routes_data.py:451 experience browse: store unreadable`
    被当成名为 `web.api.routes_data:routes_data.py:451` 的测试 → 报成「新增回归」
  - 应用日志里出现 ERROR 是正常运行的一部分，与测试成败无关
  - **修复**：只解析 `short test summary info` 段 + 校验条目形如 `路径.py::用例`（双重约束）
  - 反向验证：注入伪造日志行后解析结果不变
- **基线取自脏工作区（更严重）**
  - 原基线 102 项；用 `git archive HEAD` 导出纯净树复测，**HEAD 上只有 32 项失败**
  - 多出的 **40 项全部来自别人未提交的经验库重构**，被当成「已知失败」记入
  - 后果：**CI 对这 40 项真实回归保持绿灯**，基线替未完成的代码背了书
  - **修复**：基线按纯净 HEAD 重建（102 → 32），文件头写明生成方式必须用
    `git archive HEAD` 导出干净树，禁止用脏工作区
  - 双向验证：HEAD 上 exit 0；脏工作区上 exit 1 并精确点名那 40 项
  - 讽刺之处：上一条修的正是「假绿灯」，却用同样的方式重新造了一遍
- **顺带修好的低风险项**
  - `test_mt5_clock_skew.py`：MT5 是 win32 专属包，CI 跑 ubuntu 必缺 → 逐用例
    `importorskip`（模块级会连带丢掉那条**不依赖 MT5** 的倒计时用例）
  - `test_cursor_sdk_client.py`：`cursor-sdk` 是可选 extra，缺则 skip
  - `tools/stage2_raw_sample.txt`：被 `.gitignore` 排除，而
    `test_json_validator.py` 把它当夹具读 → **CI 上必然失败**。已恢复该文件
    （从 `fb50037` 取回）、解除忽略、加注释放说明它是夹具而非诊断输出

- **查明但未处理（属他人未提交重构，非本会话范围）**
  - `experience_repo.upsert_entry` 的 `file_path → entry_id` 改造**完全在未提交
    工作区**（`git log -S` 无对应 commit）
  - `pa_agent/storage/importer.py:148` 仍传 `file_path=` —— **真实生产 bug**：
    经 `web/server.py:119 → import_all()` 每次启动触发，被宽 `except` 吞成一行
    WARNING，`import_trade_records()` 永不执行
  - `ExperienceWriter.save()` 返回 `Path → str`、`_read_top5_from_files` 已删、
    `EXPERIENCE_DIR` 已删、`schema.sql` 未重新生成
  - 另一会话诊断期间仍在实时改动这些文件，故未代为修改

- **文件**：`tools/ci_diff_baseline.py`、`tests/ci/baseline_failures.txt`、
  `tests/unit/test_mt5_clock_skew.py`、`tests/unit/test_cursor_sdk_client.py`、
  `tools/stage2_raw_sample.txt`（恢复）、`.gitignore`

### 29. CI 的 test job 从「假绿灯」改为真跑单元测试

用户要求 CI 只跑单元测试。动手前先查现状，发现一个必须先说的问题。

- **原 `test` job 一条测试都没跑**：只做 `pip install -e ".[dev]"` + `import pa_agent`，名字叫 test 但不 test
- **而且 `.[dev]` extra 从未在 `pyproject.toml` 里定义过** —— pip 只 warning 后继续，于是「装好了」却没装到 pytest。**这是「CI 显示绿灯却毫无防护」的根因**
- **不能直接加一句 `pytest tests/unit`**：本地 `FAILED=78 ERROR=30`。直接接上去 CI 会**永久红** → 所有人开始忽略 CI → 等于没有 CI
  - 其中 30 个 ERROR 全是缺 `pytest-qt`（纯环境问题）
  - 78 个 FAILED 主体是经验库 / 存储层 / 多会话上下文的重构中代码，属其他会话写入范围，本会话未改其代码
- **改动**
  1. `pyproject.toml` 补 `[dev]` extra（pytest / pytest-qt / pytest-asyncio / playwright）
  2. `test` job 真跑 `pytest tests/unit`；runner 由 `windows-latest` → **ubuntu-latest**（实际部署是 Docker/Linux，且 `MetaTrader5` 是 win32 专属）
  3. 新增 `tools/ci_diff_baseline.py` + `tests/ci/baseline_failures.txt`（102 项存量失败）：**存量降级为警告，新增失败必红**
  4. 补 GitHub 惯例：`concurrency`（取消过期 run）、`timeout-minutes`、`cache: pip`、`permissions: contents: read`、失败时上传 pytest 日志
  5. e2e job 依赖修正：`pip install -e .` → `".[web,dev]"`（fastapi / uvicorn / sse-starlette 都在 `[web]` 里，原写法装不上、服务起不来）
- **脚本做了三场景反向验证**：现状 exit 0 / 注入基线外失败 exit 1 且打印用例名 / 某项被修好时提示清理基线
- **基线的性质**：只是当时快照，**不替代修测试**。取舍理由：不设基线则 CI 永久红（等于关掉），不报则就是本次修掉的假绿灯，设基线才能「新增回归必拦」
- **文件**：`pyproject.toml`、`.github/workflows/ci.yml`、`tools/ci_diff_baseline.py`（新增）、`tests/ci/baseline_failures.txt`（新增）

### 28. 端到端测试接入 CI + E2E 专用播种端点

上一条把 `tests/e2e/test_modes_e2e.py` 建好后，只在本地跑 —— `ci.yml` 中 0 处引用。**测试写了但不接流水线等于没写**：下次谁改了模式切换，CI 不会报警。这与「此前没做端到端」是同一类问题，只是换了个位置。

- **播种必须由服务端做（连续踩四个坑）**
  CI 是空库，核心用例会 `pytest.skip` —— CI 全绿但什么都没测。但最初把播种写成「测试进程写文件 + `upsert_record()` 写 DB」，本地能跑通，容器里连续失败：
  1. `experience_loaded` 写成 `False`，schema 要求 `list` → 被列表接口静默过滤
  2. 只写文件不写 DB → API 查不到（`_list_records` 明确「**数据库是唯一真源**」，磁盘文件只是补种来源）
  3. 补上 `upsert_record()` 仍查不到 → `f.resolve().relative_to(RECORDS_DIR)` 失败。**根因：宿主机与容器是两套文件系统视图**（`/root/.../records/pending` vs `/app/records/pending`，同一 inode、不同挂载点）。任何「测试进程自己写库」的方案在容器下都不成立
  4. `_safe_path_segment()` 返回 `str` 不是 `Path`，`a / b` 报 `TypeError`
- **改为 `POST /api/records/__e2e_seed__`**：仅在 `PA_AGENT_E2E=1` 时**注册路由**（生产环境该路由 404）。播种走服务端自己的 `RECORDS_DIR`、自己的 `upsert_record`，并用服务端同一套 `AnalysisRecord` schema 先自检 —— 不过就 500，免得「播种成功但被列表接口静默过滤」，在 CI 上表现为「什么都没测到」
- **CI 新增独立 `e2e` job**：起服务（带 `PA_AGENT_E2E=1`）→ 等健康检查 → 跑 `pytest tests/e2e` → 失败时打印 `server.log`。与 `test` job 并行、失败必须红
- **验证**：播种端点返回 `seeded:true`，`GET /api/records` 随即查到（`order_type=限价单`）；**清空全部 e2e 记录后重跑 5 passed / 63s，核心用例未 skip**
- **部署踩坑**：本轮两次把另一会话的半成品代码打进镜像 —— 一次 `routes_data.py` 的 `IndentationError`（编辑中间态被 tar 快照捕获），一次容器 `pa_agent/` 未同步导致 `chat_repo` 缺失。教训：部署前必须对 `web/` 与 `pa_agent/` 全量语法自检，且两目录要么都覆盖要么都不覆盖
- **文件**：`tests/e2e/test_modes_e2e.py`、`web/api/routes_records.py`（播种端点）、`.github/workflows/ci.yml`、`SESSION_CHANGES.md`

### 27. 修复：模式切回实时后侧边栏面板残留上一条记录的内容 + 补内容级 E2E

用户报告「历史切到实时，有的页面数据没有重置清空」。复现确认 `实时 → 回看 → 实时` 后 **预测 / 决策树 / 决策** 三个面板仍显示回看记录内容。

- **根因**：返回实时的处理器只做 `clearOverlays` + `loadBars` + `setDataMode('live')`，**从未重置侧边栏各分析面板的 innerHTML**。模式状态机只管 LED / 染色 / 只读，状态与内容是两套东西
- **为什么此前没人修**：想用统一空态入口，但 `renderDecision` / `renderFuturePanel` / `renderDecisionTree` 都直接访问 `record.stage2_decision`，**传 null 会抛 TypeError**；`renderStreamFromRecord(null)` 与 `renderTokenUsage(null)` 则是静默 `return`，同样不清内容
- **修复**
  1. 三个渲染函数补 `record` 为空的早退分支
  2. `renderStreamFromRecord(null)` 改为清理 replay-banner / flow-bar / 消息体并刷新追问锚点
  3. `renderTokenUsage(null)` 改为清空用量行
  4. 新增 `resetAnalysisPanels()` 统一调用空态分支 —— 不就地拼 innerHTML，空态文案只有一处定义
  5. 返回实时处理器里的裸 `lastRecord = null` 换成 `resetAnalysisPanels()`
- **新增 `tests/e2e/test_modes_e2e.py`（5 项）**：断言**面板内容**是否属于当前模式，并把三个模式串成一条完整迁移链
- **做了「测试能否抓到 bug」的反向验证**：把修复回退后 E2E 精确变红，断言信息直接打印残留内容 —— 证明测试有效而非摆设。该验证**顺带发现第二个 bug**：`Demo → 实时` 走另一分支，同样有残留（原判断只覆盖了回看路径）
- **关于此前端到端测试的不足（已记入 SESSION_CHANGES.md）**：旧走查断言的全是状态位（面板可见、dataset 值、classList、消息条数），**没有一个断言「面板当前显示的内容是否属于当前模式」**；`replayRecord()` 与 `btn-live` 在旧脚本里是两个独立步骤，从未串成一次状态迁移 —— 因此「机制都触发正确、结果是错的」这类 bug 全部漏网
- **部署失误记录**：临时验证的 `docker commit` 漏写 `--change CMD`，把 `sleep infinity` 固化成镜像默认启动命令；另一次只覆盖 `web/static` 未覆盖 `pa_agent/`，容器内缺 `persist_patch` 导致 ImportError 起不来。每次 commit 必须显式重设 CMD
- **文件**：`web/static/js/app.js`、`tests/e2e/test_modes_e2e.py`（新增）、`SESSION_CHANGES.md`、`AGENTS.md`、`CHANGELOG.md`
- **版本**：app.js?v=63→64
- **回归**：E2E 5 项全过；单元 FAILED=40 ERROR=30，其中 9 项新增失败全部来自 `tests/unit/test_multisession_contract.py` —— 该文件为**未跟踪的新文件**、属多 Session 会话写入范围，失败原因是其**自身第 180 行 `IndentationError`**，本会话未碰、亦未代为修改

### 26. 处置配置级联事故：settings.json 被写成默认值 → 分析功能整体不可用

用户指出另一会话已完成「重大多用户配置改造」。重新读代码与文档后确认新架构：**DB（SQLite，用户级 admin）为真源**，`settings.json` 降级为「首次播种源 + 灾备兜底」。

- **两处叠加问题**
  1. `config/settings.json` 于 05:04 被写成**代码默认值**（`base_url` 退回 `api.deepseek.com`、`api_key` 清空）→ 容器内构造 OpenAI 客户端直接失败 → **分析功能整体不可用**，`/api/health` 报 `degraded`
  2. DB 侧 `settings.baseline` 为 **NULL**、overrides 为空 —— 系统兜底**从未播种**
- **处置（顺序不能反）**
  1. 留存损坏快照 `config/settings.json.corrupt-20261005-061855`
  2. **合并**而非整体替换 `provider` 段 —— 整体替换会连带弄掉该段里其他会话新增的字段（本次先把 `prompt_cache_prime` 弄丢了一次，改用「以损坏快照为基底 + 从备份补凭证」重做）
  3. 用项目自带的 `settings_store.seed_from_file()` 播种 DB 兜底，不手写 SQL
  4. 部署含新架构的镜像并端到端验证
- **验证**：容器内 `load_settings()` 经级联取到 `base_url=http://192.168.2.128:8087/v1` / `model=stealth/space-bunny-alpha` / `prompt_cache_prime=True`，OpenAI 客户端构造成功，`/api/health` 回到 `ok`
- **回归**：`tests/unit` FAILED=31 / ERROR=30，与本会话基线一致，新增失败 0；新架构的 `test_settings_cascade.py` 19 项全过
- **未改动**：可疑调用点 `web/api/routes_data.py` 的 subscribe 处理器仍用 `save_settings()` 整份写文件（绕过级联），属多 Session 会话的写入范围，本会话未越界修改，已在 `SESSION_CHANGES.md` 的冲突风险中登记
- **文件**：`config/settings.json`、`SESSION_CHANGES.md`、`AGENTS.md`、`CHANGELOG.md`

### 25. 新增多会话协作规范与 SESSION_CHANGES.md 改动记录

用户反馈另一个 Agent 会话正在做多 Session 改造，需要一份规范让并行会话知道别人改了什么、避免写冲突。

- **新增 [SESSION_CHANGES.md](SESSION_CHANGES.md)**：与 CHANGELOG 分工明确
  - **CHANGELOG.md** — 记录*行为*变了什么（面向读者/用户），事后补写，允许合并同类项
  - **SESSION_CHANGES.md** — 记录*谁在改哪个文件*（面向并行会话），开工前必查、完工前必写，禁止合并与省略
  - 分区：🔴 进行中（有人此刻正在写这些文件） / ✅ 已提交
  - 每条含：需求 / 方案 / 改动文件清单 / **接口变更** / **冲突风险**，另附可复制模板
- **AGENTS.md 新增「多会话协作规范」小节**（置于「何时更新 CHANGELOG.md」之后，保证开工前就能读到）：
  - 开工前必查「进行中」区，有则先沟通或避开
  - **开工时立刻占坑** —— 晚写等于没写，别人改完了你才登记，冲突照样发生
  - 完工时状态改 `已提交 <commit>`，核对改动文件清单（开工列的和实际有出入的以实际为准）
  - **被否决的方案也要写进「方案」** —— 后来人不知道你为什么没走那条路，很可能重复踩
  - **既有失败/不稳定测试写进「冲突风险」** —— 否则别人会把自己没改坏的东西算到自己头上
  - 发现别人留下未提交的改动时**不要顺手清理**，只把自己明确认领过的文件纳入提交
- **已登记另一会话的在途工作**：`pa_agent/storage/`、`web/api/session_ctx.py`、
  `docs/SESSION_STORAGE_DESIGN.md`、`tests/unit/test_storage_layer.py`（均未跟踪）
  以及 `routes_analyze.py` / `routes_data.py` / `routes_records.py` / `web/server.py` /
  `test_routes_records.py` 的未提交修改 —— 明确标注 **不要动**，并记下
  `test_routes_records.py` 本就有 2 项既不稳定失败，避免误判
- **文件**：`SESSION_CHANGES.md`（新增）、`AGENTS.md`、`CHANGELOG.md`

### 24. 分析前主动预热 prompt cache —— 缓存率 0.2% → 100%

用户提出「尽量走增量加速推理，前提是推测服务端还有缓存」。先验证这个前提，结果比预期糟：**缓存机制完好，但我们的 prompt 布局让缓存几乎永远落空。**

- **实测：服务端缓存完全正常**
  - 同一 prompt 连发两次 → 缓存率 **100%**（真实 Stage1 prompt，62k tokens）
  - 前缀相同 + 追加尾部 → **99.6%**（追加式扩展能保住缓存）
  - 前缀中途改动 → 掉到 **43.6%**
- **问题不在服务端，在我们的 prompt**
  - 连续两次真实分析记录里 `cached_prompt_tokens = 276 / 134886 = 0.2%`
  - 逐字节比对两份真实 prompt：system(25,944 chars) 完全相同，user 消息前 58,331 chars 也完全相同 —— 理论可缓存 **58.3%**
  - 实测 0.2% 说明**缓存存活期远短于人工分析间隔**（那两条记录相隔 9 小时），靠「等上一轮缓存」根本不可行
- **既然缓存会过期，就在请求前把它重新热起来**（走真实客户端链路实测）

  | | prompt | cached | 命中率 | 耗时 |
  |---|---|---|---|---|
  | 关闭预热 | 62,278 | 138 | 0.2% | 4820ms |
  | 开启预热 | 62,278 | 62,276 | **100%** | **4094ms** |

  - `DeepSeekClient._maybe_prime_cache()`：`chat()` 与 `stream_chat()` **两个入口都要接**（分析实际走流式路径，只改非流式等于没生效）
  - `_primeable_prefix()`：只预热完整消息（半条消息对不齐缓存块边界，且可能写出永远用不上的缓存条目）；system 太小的直接跳过
  - `_should_prime()`：低于 20k chars 不预热 —— 小 prompt 收益不抵一次往返
  - **预热失败必须完全吞掉**：它只是优化，绝不能阻断或搞挂真实请求
  - 新增开关 `provider.prompt_cache_prime`（默认 true）可一键关闭
- **缓存命中率透出到界面**：不显示就看不出预热是否生效。token 明细与「分析」tab 用量行都加上「缓存 N (X%)」，命中染绿、未命中染琥珀
- **一个做错了又撤回的改动**：曾把 prompt 里的稳定尾部提醒从末尾移到变动段之前，想让稳定内容连成整块；实测**只提升 0.1 个百分点**（分叉点本来就在尾部提醒之前，它压根不在断点之后）。属无效改动，已 `git checkout` 撤回
- **测试**：新增 `tests/unit/test_prompt_cache_priming.py`(9) —— 前缀选取（必须有 system、system 过小跳过、纳入首个 large user、不纳入 small user、不改动入参）、阈值与开关、**预热失败不冒泡且仍是 max_tokens=1 的廉价探针**、小 prompt 不预热。写测试时连踩三次夹具坑（`_Exploding` 缺 `chat.completions` 嵌套、`_Cfg` 缺 `model` 字段、位置参数绑错字段），都会让预热在真发请求前 AttributeError
- **文件**：`pa_agent/ai/deepseek_client.py`、`pa_agent/config/settings.py`、`web/static/{js/app.js,css/style.css,index.html}`、`tests/unit/test_prompt_cache_priming.py`
- **版本**：app.js?v=61→62、style.css?v=37→38
- **备注**：`test_routes_records.py` 有 2 项**既有**不稳定失败（失败项在多次运行间漂移，且在干净工作树的 HEAD 上同样失败），与本次改动无关

### 23. 数据源模式状态机（实时/历史/Demo）+ 分析按钮合并 + 非实时只读

用户反馈：「增量」按钮不知何时能点、「重要按钮该常驻右上角」、「Demo/历史要有状态灯让人知道这不是真实数据」。

- **合并「分析」与「增量」为一个按钮**：此前是两个按钮，用户必须理解「有历史记录时点增量、否则点分析」，而「增量」经常灰着且看不出为什么。两者本是同一次分析的两种上下文策略，没有理由让用户做选择
  - 「分析」自动选路：有可复用上下文 → 增量（省 token、上下文连贯）；否则 → 完整
  - 按钮**文案与 tooltip 直接说明会走哪条路**，而不是让用户猜
  - 新增「强制完整」小开关，怀疑存量上下文有问题时可强制重跑
  - `refreshIncrementalButtonState()` 不再维护按钮禁用态，只回答「有没有可复用上下文」，由 `shouldUseIncremental()` 统一决策；持续分析自动选路同步改用同一入口
- **数据源模式状态机**：此前 Demo **完全不在任何状态机里** —— 加载演示数据后界面与真实行情毫无二致，用户会把它当成真实报价
  - 图表右上角常驻 `#data-mode-bar`：●实时 | ●历史 | ●Demo | 自适应。三个模式互斥，LED 点亮者即当前模式（绿=实时 / 琥珀=历史回看 / 紫=Demo）
  - 状态条显示模式名 + 上下文（如「历史回看 BTCUSDT · 1h」「演示数据 模拟行情」）
  - 图表整体染色：回看琥珀框、演示紫框，余光扫一眼就知道在看什么
  - `setDataMode()` 是唯一入口，不要在别处单独改 class
  - 「返回实时」改为「实时」模式按钮：从 Demo / 回看都能一键回到实时，实时态自动禁用
  - 从 Demo 返回必须**无条件重载真实 K 线**：Demo 覆盖主图数据却不改订阅，品种/周期可能和演示内容对不上，只清叠加层不够（此前会直接 return）
- **非实时模式下侧边栏只读**：回看看到的是已归档结论、Demo 是合成行情 —— 对着它们追问、验证经验、重跑分析都没意义，还会把 Demo 数据写进记录或经验库
  - 分析按钮**隐藏**（不是 disabled —— 灰按钮会让人反复去点它为什么点不动）
  - 追问输入/发送、经验库验证/刷新、等待收盘/持续分析/强制完整等一律禁用
  - 侧边栏顶部常驻只读提示条；动态渲染的复盘按钮用 CSS 兜住，不依赖枚举
- **修掉的 3 个真 bug**
  1. `enableChat()` 无条件放开追问框，正好覆盖掉 `setPanelsReadonly()` 的禁用 —— 回看记录时追问框居然可用。改为尊重只读态
  2. `classList.toggle('data-readonly')` 加的是**类名**，CSS 侧却是 `body[data-readonly]` **属性选择器**，选不中 → 降饱和/禁用态样式完全没生效
  3. `#readonly-hint` 放在 `#main` 外面：它是 row flex 的第三个 item，在图表与侧边栏之间占掉 343px 整列且不显示任何内容。已移进 `#sidebar` 内部
- **重命名**：`btn-back-to-live` → `btn-live`（语义从「返回」变为「切到实时模式」，实时态也可见且禁用）；移除 `hideReplayBadge()` 里对它的隐藏逻辑
- **文件**：`web/static/index.html`、`web/static/js/app.js`、`web/static/css/style.css`
- **版本**：app.js?v=56→61、style.css?v=33→37
- **验证**：三模式 × 只读态实测全部符合预期 —— 实时可分析可追问；Demo/回看隐藏分析按钮、禁用追问与验证、显示只读提示；切回实时全部恢复；全量 `tests/unit` 对基线新增失败 0

### 22. K线联动全面审计：修 3 个真 bug + 品种器聚焦体验 + 全控件走查

按「上一轮锚点 bug 的模式」逐条审计 K 线联动链路（硬编码下标 / 用「最新」代替「锚点」/ ms-s 单位 / 逻辑索引代替时间戳），发现并修复 3 个真 bug。

- **`closedBarTs()` 在休市模式下哨兵错位（影响持续分析）**：硬编码 `sorted.length - 2`，假定末位恒为 forming bar。但**休市模式下全部 bar 都已收盘**（数据快照契约：bars[0].seq=1, closed=True），此时「刚收盘」就是最后一根，offset=2 返回的是两根之前的 ts。影响：bar_close 哨兵错位 → 重新开盘后可能误判「这根已处理过」而漏触发，或对同一根重复触发持续分析。修复：按 `closed` 标志倒序查找，无标志时退回 offset 启发式。回归证据：休市数据下旧实现返回 2000、正确值 3000。新增 6 条 Node 断言
- **SSE bar_update 同步 `lastBars` 用了错误下标（`lastBars` 永不更新）**：`/api/bars` 返回 newest-first（bars[0]=forming），`loadBars` 休市检测读的正是 `lastBars[0]`；而 SSE 合并处取 `lastBars[lastBars.length - 1]`（**最老**的一根）去比 ts_open，永远匹配不上。后果：forming bar 的 OHLC 在 lastBars 里一直是旧值，而 `lastBars` 被 `closedBarTs()` / `setSeqMarkers()` / `applyReplayChart` 视窗计算共同依赖。修复：按 ts_open 定位，不依赖数组方向
- **`clearOverlays()` 不清经验回放图例**：`clearExperienceReplay()` 定义并导出了但**从未被调用**。点过经验条目后再切记录/开始分析，价格线与 marker 被清、图例却留着 → 图上出现无对应线条的陈旧说明。修复：`clearOverlays()` 一并清 `#experience-legend`；同时移除 `clearExperienceReplay` 里的 `series.setMarkers([])`（会连 seq 标记一起抹掉）
- **品种选择器聚焦即浏览**：聚焦时把输入框现有值（当前品种）拿去搜，只返回寥寥几条，而「常用 + 分类」清单只能靠点「清空」够得着 —— 用户看到 4 条会以为坏了。修复：聚焦一律展示浏览清单，输入才切到在线搜索
- **`setExperienceReplay` 入场 marker 未校验 bar 是否存在**：LWC 的 marker 时间必须落在真实 bar 上否则被静默丢弃。老案例入场 bar 不在当前窗口时改为不画并在图例注明，而非无声消失
- **子 tab 处理器加防御**：程序化点击隐藏面板里的子 tab 会留下两个 `.active` 面板（用户点不到，但已实测出该路径）
- **走查结果**（Playwright 全控件点击，**30/30 通过，无 JS 错误**）：6 个顶层 tab + 5 个子 tab 任何时刻恰好一个面板可见；追问真实发送收到 user+assistant 两条消息；历史回看/返回实时订阅与周期正确恢复（回看记录是「不下单」，不画叠加层属正确行为，已核对 `decision_overlay`）；品种器聚焦浏览 30 项/2 分组、在线搜 ETH→ETHUSDT、点选、应用订阅；周期切 15m 图例与指标正常重算；三个复选框、经验库刷新/验证、指标图例均正常
- **审计中确认正确**的部分：ms/s 单位换算一致；主副图用 `setVisibleRange` 时间同步且有 `syncing` 防回环；`fitView` 的逻辑范围是有意为之；`setSeqMarkers` 跳过 `seq<=0` 不假设位置；`loadBars` 的 `lastBars[0]` 休市检测正确
- **文件**：`web/static/js/{continuous_gate.js,continuous_gate.test.js,app.js,chart.js}`、`web/static/index.html`
- **版本**：app.js?v=54→56、chart.js?v=8→9、continuous_gate.js?v=1→2；全量 `tests/unit` 对基线新增失败 0

### 21. 修复：历史回看的方向箭头指向错误的 K 线（两个叠加缺陷）

用户实机报告「BTC 1h 选历史后，箭头指向不是当时的 K 线」。查出**两个独立缺陷叠加**，缺一都不会出现该现象。

- **缺陷一（前端）**：箭头锚点用的是「最新一根」而非「当时那一根」。`setDirectionMarker()` 硬取 `window.__PA_LAST_BAR_TIME__`（刚加载数据的最后一根）；回看时视窗已对齐到记录的分析时刻，箭头却画在**今天**的 K 线上 —— 图看着对，语义完全错
  - 改为 `setDirectionMarker(series, decision, anchorTimeSec)`；`applyReplayChart()` 先解析「当时那根 bar」再传给它
  - 锚点落在数据范围外时**显式传 `null` 表示「不要画」** —— 画在不相关的 bar 上比不画更有害
  - ⚠️ 必须用 `=== undefined` 判断是否显式传入，不能用 `!= null`：回看传 `null` 时 `null != null` 为 false，会掉进「回退到最新一根」分支，恰好复现要修的 bug。demo / 实时分析不传参，行为不变
- **缺陷二（后端）**：`two_stage.py` 与 `routes_records.py` 都**硬取 `kline_data[1]`**，假设 `bars[0]` 恒为未收盘 forming bar。但休市、或快照未带 forming bar 时 `bars[0]` 本身就是已收盘的，此时 index 1 指向**倒数第二根**。实测该记录：分析时刻 17:55:13Z，`kd[0].closed=True ts=16:00Z`（真锚点），代码却取 `kd[1]` = 15:00Z
- **修复方式**：新记录修正写入，旧记录在**读取时**修正
  - 抽出 `two_stage._pick_last_closed_bar()`：按 `closed` 标志找第一根已收盘，无标志时退回旧启发式（可单测而不必跑整条流水线）
  - `routes_records._derive_anchor_bar_ts_ms()`：从记录自身 `kline_data` 现算权威锚点，作为 `anchor_bar_ts_ms` 暴露。kline_data 不可变，据此推导不会漂移；旧记录 JSON 里烙着的错值在读取时被修正，**不必也不应改写磁盘上的历史**
  - 前端优先用 `anchor_bar_ts_ms`，回退 `last_close_bar_iso`
- **测试**：新增 `tests/unit/test_last_close_bar_anchor.py`(7) —— 有/无 forming bar、连续多根未收盘、无 closed 标志、单根、空输入
- **验证**：旧记录 2026-10-04_17-10-13（分析 17:55:13Z）存储值 15:00Z(错) → 推导值 16:00Z(对)；新记录 2026-10-05_02-10-38（分析 02:46:38Z）存储值与推导值一致（01:00Z），证明写入端修复生效；前端实测方向箭头 time 与记录锚点逐秒相等
- **文件**：`pa_agent/orchestrator/two_stage.py`、`web/api/routes_records.py`、`web/static/{js/app.js,js/chart.js,index.html}`
- **版本**：app.js?v=52→54、chart.js?v=7→8；全量 `tests/unit` 对基线新增失败 0

### 20. 后台定时结算 + 定时/手工模式开关 + 每条记录独立 LLM 复盘

- **后台定时结算** `web/api/experience_scheduler.py`：此前待验证记录只能靠用户点「验证」才结算，不点就永远停在 pending，两阶段设计等于白做
  - lifespan 启动 daemon 线程，默认每 180s 一轮（`experience_verify_interval_s`，下限 30s 强制）
  - **单飞守卫**：两轮 pass 绝不重叠；任何异常只吞不冒泡且保证守卫释放（一次失败不能让调度器永久卡死）
  - **只结算当前订阅范围 + 只用共享数据源**：结算其它品种需为每条记录单独建 TradingView 连接，定时器上做这个等于打爆上游；那些记录等用户切回去或点按钮时再结算
  - 范围每轮**重读 settings** 而非取快照 —— 用户随时会切品种
  - 首轮在启动后 20s，让重启能结算上次遗留的 pending；分析完成时另起线程顺带结算一轮
- **定时 / 手工模式** `experience_verify_mode`: `auto` | `manual`。manual 时定时器与分析后触发都空操作，但「验证」按钮仍可用（`run_once(force=True)`）—— 用户选手工是不要后台偷偷结算，不是要禁用按钮
- **每条记录独立「复盘」按钮**（不是总按钮）：`GET /api/experience/review/stream` SSE 流式 LLM 复盘，就地展开在该条目下方。不跳到追问 tab —— 复盘针对这一条记录，与当前图表/追问会话是两回事，混在一起会污染追问上下文。prompt 固定五段结构（结论 / 归因 / 当时能否预见 / 改进建议 / 下次判据），并明确要求「不要事后诸葛亮」；`unresolved` 单独说明为正常结局，评价对象是窗口长度是否合理
- **案例库现在记录完整上下文**（复盘的前提）：`save_pending()` 新增 `analysis_context`（阶段一判断要点 + 阶段二决策）与 `bars_snapshot`（入场前后各 20 根 K 线）。只留决策要点、丢弃 prompt/response 原文与冗长叙述（长文本截 600 字、列表取前 12 项），实测单条约 3.8KB
- **实现中修掉的问题**
  1. **阶段一接线整体丢失**：`order_followup` 的 `save_pending_if_resolvable` 替换因后续 assert 失败导致写文件未执行，容器里跑的还是旧 `spawn_experience_watch` —— 两阶段根本没接进分析流程。现把断言放在写入之前
  2. **前端 SSE 解析切不出事件**：sse_starlette 用 CRLF 分隔，前端按 `

` 切，复盘永远停在「生成中…」。已归一化 CRLF
  3. 调度测试直接调用 `_loop` 导致死循环（测试自身缺陷），改为用 `_stop` 收尾
  4. 调度测试 mock 的 `data_source` 写成类而非实例，`latest_snapshot(n)` 把 `self` 变成了 int
- **测试**：新增 `tests/unit/test_experience_scheduler.py`(15) —— 结算正确性、范围跟随 settings、跨品种不结算、单飞不重叠、异常后守卫释放、间隔下限、start 幂等、manual 跳过定时器但按钮仍可用、未知模式回退 auto、循环真跑起来仍空闲
- **文件**：`web/api/{experience_scheduler.py,routes_experience_review.py,routes_data.py,routes_analyze.py,order_followup.py}`、`web/server.py`、`pa_agent/records/experience_writer.py`、`pa_agent/config/settings.py`、`web/static/{js/app.js,css/style.css,index.html}`、`tests/unit/test_experience_scheduler.py`
- **验证**：造一条与真实 BTC 同量级的 pending → 调度器首轮自动结算为 `win / bars_seen=3 / pnl=+0.6%`；复盘按钮流式返回 1257 字并带推理折叠；39 项经验库相关测试全过；全量 `tests/unit` 对基线新增失败 0

### 19. 两阶段经验库：入场即写「待验证」+ 按 N 根 K 线结算 + 点击联动主图

- **动机**：旧实现是一次性判定（起线程轮询到 TP/SL 触发才写一条），进程重启全丢，且只能验证「用户一直没换品种」的那些。改为两阶段，让「记录事实」与「判定结果」解耦
- **状态机（落盘目录即状态）**：`pending_cases/` → `success_cases/`(win) / `failure_cases/`(loss) / `unresolved_cases/`（走满 N 根仍未触及，终态但无盈亏）。**只有 win/loss 被 `ExperienceReader` 读到** —— 未决的 setup 绝不能被当成失败经验喂回提示词
- **阶段一** `save_pending_if_resolvable()`：入场瞬间落盘（「我们做了什么」是事实，不需等结果）。门控拒绝：不下单 / 缺 TP 或 SL / 零价位 / 无多空方向 / 无入场锚点。锚点取最后一根**已收盘** bar（`bars[0]` 是 forming bar，用它会把未收盘走势算进「入场之后」）
- **阶段二** `web/api/experience_verifier.py`：按 N = `experience_verify_bars`（默认 20，可配）结算 —— 触及 TP/SL → win/loss + pnl；走满 N 根未触及 → unresolved；不足 N 根 → 保持 pending 继续等
- **币种/周期/交易所三轴对齐是硬不变量**（此前致命 bug 的根源）：
  - 共享数据源仅在 (exchange, symbol, timeframe) 三者全等时复用；否则为该记录单独建数据源（用完即弃，绝不去改共享订阅）
  - 取到的 bars 再过价格量级兜底 `bars_belong_to_instrument()`：与记录 entry 不在同一量级 → 判为另一个标的，保持 pending
  - 实测：故意造 entry=100 的记录而真实 BTC 在 83589，日志正确输出 `bars for GATEIO/BTCUSDT do not straddle entry 100.0 — leaving record pending`，记录未被错误结算
- **UI**：待验证与终态同列表展示并区分标签；待验证显示「已走 k/N 根」让等待进度可见；「验证」按钮按当前范围结算并 toast 汇报；**点击条目 → 主图联动**（入场线蓝/止盈绿/止损红 + 入场与结算标记 + 视窗对齐入场点），图例展示状态、品种周期、多空方向、价位与判定窗口
- **实现中踩到并修掉的三个坑**
  1. `_status_subdir` 误把 status 当目录名，写出 `pending/` 而非 `pending_cases/` → reader 按 `*_cases` 扫描，结算后的记录读不到
  2. `#experience-legend` 被嵌在 `#chart-legend` **内部**，而 indicators.js 每次刷新指标图例都会整体重写其 innerHTML，把经验图例一并抹掉 → 改为兄弟节点
  3. 一段 `loadExperienceLibrary` 重写因后续 assert 失败导致写文件那步未执行，前端仍是旧模板（class 恒为 `is-failure`、不渲染价位行、点击无反应）。该坑本轮出现两次，已改为写文件前先断言
- **测试**：新增 `tests/unit/test_experience_two_stage.py`(15) —— pending 落盘布局、pending 不被 reader 检索、TP/SL/unresolved/pending 四种 N 根规则、入场前 bar 被忽略、价格量级守卫、共享源在品种/周期/交易所任一不符时被拒、范围过滤、无数据源时保持 pending。其中一条测试一开始用 `entry=100 vs bars 60~70`（0.6 倍）当反例，被正确判为同标的 —— 守卫本身是对的，是例子不成立，已改用真正跨量级的场景
- **文件**：`pa_agent/records/experience_writer.py`、`pa_agent/config/settings.py`、`web/api/{experience_verifier.py,routes_data.py,order_followup.py}`、`web/static/{index.html,js/app.js,js/chart.js,css/style.css}`
- **验证**：阶段一门控 6 种情形逐个验证（合法写入 + 5 种拒绝）；API 返回 `status_counts={'pending':1}`；UI 显示 `待验证 (pending)` / `已走 0/20 根` / `入场 100 · 止盈 120 · 止损 90 · 多头`；点击后主图三条价格线与图例正确渲染；价格量级兜底按预期触发；全量 `tests/unit` 对基线新增失败 0

### 18. 经验库数据核查：36 条全部为合成数据，写入链路实为死链（含 3 个数据完整性缺陷）

用户质疑「经验库的数据是不是真的」。核查结论：**一条真的都没有**。

- **现有 36 条全是合成的种子数据**，证据：
  - 36 个文件 mtime 集中在两分钟内（`21:35:23`×32、`21:36:19`×4）
  - 文件名时间戳严格等差：日期每次 +2 天、小时每次 +1、**秒位恒为 `35-23`**
  - `pnl_pct` 只用了 4 个取值（`-2.1/-1.8/+3.0/+2.5`），各出现 8 次
  - `confidence` 只用了 4 个取值（`65/70/75/80`）
  - `entry_price` 等差递减，每次**恰好 -100**
  - `summary` 为占位文案（「区间内反复」「建议观望」）
  - git 从未跟踪这些 JSON（只有 `.gitkeep` 在版本控制内）
- **根因：写入链路根本没接上（致命）**
  - `spawn_post_order_followup()` 取 `getattr(record, "_data_source", None) or getattr(frame, "data_source", None)`
  - 但 `AnalysisRecord` 字段为 `[meta, kline_data, htf_text, stage1_*, stage2_*, strategy_files_used, experience_loaded, exception, usage_total]`、`KlineFrame` 为 `[symbol, timeframe, bars, indicators, snapshot_ts_local_ms]` —— **两者都没有这两个属性**，全仓也从未给 `_data_source` 赋过值
  - ⇒ `ds` 恒为 `None` ⇒ `spawn_experience_watch()` **从未被调用** ⇒ 写入链路自打通以来就是死的。那些条目不可能是任何一次真实分析的产物
- **另外两个一旦接通就会写入编造数据的缺陷**
  1. **订阅漂移**：`data_source` 是全局共享、订阅绑定的单例（`subscribe()` 会改写 `self._symbol/_timeframe`），watcher 捕获了 `ds` 却没锁订阅。可复现：BTCUSDT 单（entry=100/TP=120/SL=80）→ 用户切到 ETHUSDT → 读到的 ETH 暴跌 bar 把它判成 `('loss', -20.0)` 写入库。**修复**：每轮轮询**前后各校验一次**订阅（只做前置校验仍有竞态窗口）
  2. **入场锚点为 0**：`order_followup` 硬编码 `last_closed_ts_open_ms=0`，过滤条件退化为 `ts_open > 0`，入场**之前**的历史 K 线被当成本单走势。可复现：入场前一根暴跌 bar 触及 SL，第一次轮询（15 秒后）就写下 `pnl=-20%` 的假亏损。**修复**：改传最后一根**已收盘** bar 的 `ts_open`；锚点缺失时直接放弃写入
- **数据处置**：36 条种子移入 `experience/.seed_demo_20260817/`（点号前缀会被 `GET /api/experience` 的目录枚举过滤），避免被 `ExperienceReader` 检索并注入 Stage1/Stage2 提示词当成"参考经验"——否则等于用编造盈亏污染 AI 决策。附 README 记录判定依据。经验库现从干净状态起步（0 条）
- **测试**：新增 `tests/unit/test_experience_watch_integrity.py`(10) —— 锚点缺失/入场前 bar 排除/正常触及仍写入/两次轮询间漂移/取数期间漂移/显式传入 data_source/record 与 frame 确无该属性/routes_analyze 确实传参/可检索库不得含合成数据/隔离目录不得泄漏进 API。其中两条漂移测试是在实现过程中**发现我第一版只做了前置校验**才补上的。另修正 `test_experience_library_loop.py` 中一条按 bug 行为写的旧用例
- **文件**：`web/api/{order_followup.py,routes_analyze.py,experience_watcher.py}`、`tests/unit/{test_experience_watch_integrity.py,test_experience_library_loop.py}`、`.gitignore`
- **验证**：部署后 `GET /api/experience` 返回 0 条；全量 `tests/unit` 对基线新增失败 0

### 17. 追问移到「决策」右侧 + 全部 tab tip 重写 + 经验库范围恒等于当前 K 线

- **tab 顺序调整**：追问从「决策树」右侧移到「决策」右侧 → 分析 / 预测 / 决策树 / 决策 / 追问 / 经验库
- **逐条核对并重写全部 tab tip，发现 1 条过期**：
  - 经验库 tip 写着「浏览经验库」，但该面板上一轮已改为跟随当前 K 线范围、不再展示全库 → 已改为「当前 K 线品种与周期上的历史成败样本」
  - 追问 tip 补充「切换品种或回看历史记录会重设锚点」—— 最易误解的点，不提示会让用户以为还在追问上一份结论
  - 其余 4 条核对无误（预测确实是「下一根 K 线 + 下一市场周期」两段；决策确实是「结论 / 市场状态 / 详细依据」三段 + 置信度/胜率/盈亏比），一并补充更具体的要点
  - 经验库的 tab 内说明补上范围规则与「查看全部品种与周期」入口
- **经验库范围改为恒等于当前 K 线（不可手动选）**：移除「全部交易对」「全部周期」下拉与「跟随当前」复选框，改为只读标签 `#exp-scope`（如 `ETHUSDT · 4h`）。`applySubscribe()` 末尾主动刷新，切品种/周期后面板不会停在旧结果上。空结果时提供「查看全部品种与周期」逃生口，避免严格过滤把用户堵死
- **市场周期下拉改为中英**：`GET /api/experience` 新增 `cycle_options`（`{value,label}` 数组，value 仍是 raw 用于过滤，label 走 `display_labels`）→ `宽通道 (broad_channel)` / `趋势型交易区间 (trending_tr)` / `未知周期 (unknown)`；`_fillExpSelect()` 兼容字符串数组与对象数组
- **文件**：`web/static/{index.html,js/app.js,css/style.css}`、`web/api/routes_data.py`
- **验证**：6 个 tab 逐一点击均只显示单个面板、互不重叠；周期下拉 9 项全中英；范围标签跟随 `ETHUSDT · 4h`；无 JS 错误；全量 `tests/unit` 对基线新增失败 0

### 16. 经验库枚举改为中英展示

- 经验条目里的枚举一直是裸英文 snake_case（`trending_tr` / `up` / `success`），对实际操作者不可读；但只给中文又会丢掉提示词、落盘目录名里真正在用的 raw 值，对不上账。改为 `中文 (raw)`
- 新增 `pa_agent/ai/display_labels.py`（Qt-free，展示层专用）：周期 / 方向 / 结果 / 案例类型四张对照表 + `label_for()`。方向表同时覆盖 AI 决策侧别名（`long/bull/bullish`→上涨、`short/bear/bearish`→下跌）
- **空值返回 `''` 而非「未知 ()」** —— 模板要能整块隐藏该字段；未知枚举回退为裸值，不吞数据
- `GET /api/experience` 每条新增 `cycle_label` / `direction_label` / `result_label` / `case_type_label`；`cycle_position` 改为「条目内容 → 目录名」两级回退
- **顺带修一个写入端不一致**：`ExperienceWriter.save()` 收了 `cycle_position` 参数却只用于拼目录名、**从未写进 JSON** —— 实测库中 36 条的 `cycle_position` 全为 `null`，一直靠 reader 回退父目录名才能显示。现已持久化，条目文件自描述（旧数据仍可回退）
- **文件**：`pa_agent/ai/display_labels.py`(新)、`pa_agent/records/experience_writer.py`、`web/api/routes_data.py`、`web/static/js/app.js`、`tests/unit/test_display_labels.py`(新，13 例)
- **验证**：实机显示 `趋势型交易区间 (trending_tr)` / `上涨 (up)` / `盈利 (success)`；无 JS 错误；全量 `tests/unit` 对基线新增失败 0

### 15. 追问升为独立 tab + 经验库按交易对/周期过滤

- **追问 → 独立顶层 tab**（置于「决策树」之后）：此前追问挂在「分析」面板最末尾的 `.stream-footer`，必须滚过整段流式输出才能看到输入框，而追问恰是最常用的主交互之一
  - 顺序现为：分析 / 预测 / 决策树 / **追问** / 决策 / 经验库
  - 消息区独立成 `#chat-messages` 占满可滚动高度，输入框常驻底部（补 `padding-bottom: 6px`，此前底边紧贴视口）
  - 新增「锚定分析」上下文条，显示当前追问挂在哪次分析上（品种·周期·订单类型·时间）—— 切品种/回看历史会换锚点，不显示出来用户会以为还在追问上一份结论
  - `appendChatMsg` / `clearChatOutput` 改指新容器（保留 `#tab-chat` 兜底）
- **修一个连带 bug**：**demo 下追问输入框始终禁用**。`enableChat()` 只在真实分析的 `done` 事件里调用，demo 路径漏了，等于演示时这条主交互根本用不了。demo 收尾处补 `enableChat()` + `renderChatContext()`
- **经验库先做一层过滤**：
  - `GET /api/experience` 新增 `symbol` / `timeframe`，按**条目内容**过滤（同一代码会出现在不同市场周期下，只看文件名不够）；`counts` 汇总同步跟随过滤，否则前端数字对不上
  - 同时返回全量 `symbols` / `timeframes` 供前端下拉
  - 默认勾选「跟随当前」，只显示当前订阅的品种+周期；用户手动选下拉会自动取消跟随，避免两控件互相覆盖
  - 摘要行显示 范围 · 条数 · 盈利/亏损 · 胜率
- **文件**：`web/static/{index.html,js/app.js,css/style.css}`、`web/api/routes_data.py`
- **验证**：demo 加载后输入框可用、锚定条显示 `BTCUSDT · 1d 限价单`、真实发送收到 AI 回复（user+assistant 两条）；经验库跟随当前 `BTCUSDT 1h`→「共 0 条」（库中仅 1d/4h，正确空集且有引导文案），取消跟随→「共 36 条（盈利 18 / 亏损 18，胜率 50%）」；无 JS 错误；全量 `tests/unit` 对基线新增失败 0

### 14. 接入 TradingView scanner：全市场实时品种搜索

- **修正上一轮的结论**：失效的只有 tvDatafeed 自带的 `search_symbol()`，而 `scanner.tradingview.com` 走另一条路径、无需登录，实测完全可用。实测各市场 `totalCount`：crypto 64412 / futures 52721 / america 20069 / china 7476 / forex 6333 / hongkong 3060 / global 456108（`symbol-search.tradingview.com` v3 则是 403，已弃用）
- **后端**：新增 `search_tv_symbols(query, exchange, limit)`，POST scanner 并返回 `code`（取 `"EXCHANGE:SYMBOL"` 冒号后的部分，格式与 `TV_SYMBOL_PRESETS` 一致，可直接喂 `get_hist`）、`name`/`description`/`exchange`/`volume`/`close`；新增 `GET /api/tv/search`，网络失败返回 `[]` 而非抛错
- **交易所映射**：`GATEIO/BINANCE/BYBIT/OKX/BITSTAMP/COINBASE→crypto`、`NASDAQ/NYSE/SP→america`、`OANDA/FOREXCOM→forex`、`SSE/SZSE→china`、`HKEX→hongkong`、`CBOT/CME_MINI→futures`；`GATEIO` 在 scanner 里叫 **GATE**（不映射则一个都搜不到）
- **结果清洗**（不做完全没法用）：搜索 `BTC` 原本返回 `PUMPBTCUSDT`/`WBTCUSDT`/`BTCUSD.P`；搜索 `EURUSD` 原本返回 `EURUSD.ONE`/`EURUSD.SML.ONE`/`EURUSD.PRO.OTMS`。新增 `_is_derivative()` 识别永续 `.P/.F`、杠杆代币 `.3L/.5S/.2L`、券商变体 `.ONE/.PRO/.ECN/.SML…`、wrapped/staked
  - **顺序是关键**：必须「先按相关性排序、再过滤衍生品」，反过来（先分组后整体重排）等���没过滤 —— 实现时踩到的坑
- **顺带修一个真 bug**：内置表港股代码错误。TradingView 港股**不补前导零**，`0700`/`0388`/`0688`/`0961` 在 TV 上不存在、订阅必然失败；已按 scanner 实测校正为 `700`(腾讯)/`388`(港交所)/`688`(中国海外)/`1193`(华润燃气)
- **前端**：有输入时防抖 260ms 走在线搜索（带竞态守卫，只认最后一次输入）；空查询仍用内置表（瞬时、离线可用、带中文名与分组）；在线失败/无结果自动回退到内置表模糊匹配；结果项右侧显示 `交易所 · code`
- **文件**：`pa_agent/data/tradingview.py`、`web/api/routes_data.py`、`web/static/js/app.js`、`web/static/index.html`、`tests/unit/test_tv_scanner_search.py`(新，24 例)
- **验证**：实机 `BTC→BTCUSDT`、`EURUSD→EURUSD`（变体已清）、`AAPL→AAPL`、`600519→贵州茅台`、`700→腾讯控股`（点击选中生效），无 JS 错误；全量 `tests/unit` 对基线新增失败 0

### 13. tab 重排为 5 个 + 交易对选择器重做

- **tab 顺序**（按用户指定）：分析 / 预测 / 决策树 / 决策 / 经验库。「原始」不再是顶层 tab，降级为「分析」面板内的子 tab
  - 分析 → 流式分析(`stream`) / 原始数据(`raw`) / 文件与经验(`debug`)
  - 决策树 → 问答回放(`tree`) / 流程图(`tree-viz`)
- **交易对选择器**：先说结论 —— **TradingView 的品种搜索接口在本环境不可用**。tvDatafeed 2.1.0 的 `search_symbol()` 对 BTC/AAPL/XAUUSD/苹果 全部返回 `Expecting value: line 1 column 1`（非 JSON），`get_hist` 正常但 `search_symbol` 已失效，与 AGENTS.md 记录的 tvDatafeed `__auth` 失效同源。故无法真正「抓 tv 的数据」，改为把可达成部分做到最好：
  1. 内置表 100 → **195 个品种**，覆盖加密/外汇贵金属/美股/A股/港股/指数/期货农产品；此前多数交易所仅 5–15 个且 109 个没有中文名，现全部有中文名
  2. **真·模糊搜索**：打分排序（完全匹配 > 前缀 > 子串 > 子序列）+ 子序列匹配，「btc」命中「比特币」、「na」命中 Solana/NEAR/Uniswap
  3. 空查询按分组展示（常用/加密/外汇/美股/A股/港股/期货），聚焦即可浏览
  4. 超出显示上限时提示「共 N 个匹配」而非静默截断
  5. **修掉注入风险**：结果项原用内联 `onclick="selectSymbol('${code}')"`，字符串插值遇引号即破坏 HTML → 改为 data 属性 + 事件委托，并补 mousemove 同步键盘高亮
- **新增测试**：`tests/unit/test_symbol_presets.py`(5) —— 每交易所规模下限、无重复、每个品种必须有中文名、`list_symbols` 未知交易所兜底（当场抓到我自己写重复的 BYBIT `ARBUSDT`）
- **文件**：`pa_agent/data/tradingview.py`、`web/static/{index.html,js/app.js,css/style.css}`
- **验证**：全量 `tests/unit` 对基线新增失败 0；实机确认顶层 5 个 tab、子 tab 分组互斥、OANDA 空查询 20 项 / GATEIO 30 项均带分组头

### 12. 历史回看联动主图 + 侧边栏 tab 收敛为 6 个

- **历史回看联动**：此前 `replayRecord()` 只重渲染侧边栏，**完全不碰图表** —— 不画该记录的 Entry/SL/TP1/TP2 横线、不切换品种。于是回看别的品种的历史记录时，图上是当前品种的 K 线、面板里是历史记录的数字，两边彻底对不上；「返回实时」也只是切 tab，K 线还停在回看的品种上。
  - 新增 `applyReplayChart(record)`：按记录 meta 无条件 `POST /api/subscribe` → `loadBars()` → `clearOverlays` → `setDecisionOverlays` + `setDirectionMarker` + `_renderTradeLegend`，工具栏品种/周期同步
  - 「返回实时」恢复回看前订阅并清空叠加层；回看期间自动取消「持续分析」勾选，避免复盘时误触发新一轮分析
  - 视窗对齐：锚点在数据范围内时以该记录最后一根已收盘 bar 为中心；**落在范围外回退到近期窗口**（回看 8 月 ETH 而当前只有 10 月数据时，硬对齐会被钳到序列边界、视窗退化成两三根超宽 K 线）
  - 两处实现要点均为实测踩出：切换与否**不能按工具栏标签判断**（标签与后端订阅可能不一致，按标签判断会跳过切换）；暂存的「实时订阅」取 `settings.general` 真实状态而非标签
  - 实测往返：NVDA 1h(200根 207-238) → 回看 ETHUSDT 1h(201根 2636-2777，价位图例 4 条) → 返回 NVDA 1h(叠加层清空)
- **tab 收敛**：侧边栏顶层 tab 由 8 个降到 6 个 ——「可视化」并入「决策树」（问答回放 / 流程图）、「调试」并入「原始」（原始数据 / 文件与经验）。
  - **不做 DOM 嵌套**：两组面板本是同级兄弟、共享侧边栏同一槽位（`.tab-panel` 默认 `display:none`，`.active` 才占位），只需面板内插入子 tab 条、点击时转移 `.active`。`#btn-tree-viz-*`、`#tab-tree-viz` 等 id 全部不变，既有 JS 引用零改动
  - 修掉实现 bug：最初 `SUBTAB_GROUPS[target]` 按键查组，而组键是 `tree`、target 是 `tree-viz` → 查不到 → 旧面板未移除，切到「流程图」时两面板同时占位。改为按成员反查 `groupOf(target)`
  - 实测任意时刻只有一个面板可见：决策树→问答 10 张卡片 / 流程图 53 个 SVG 节点；原始→文件与经验正常；经验库 36 条
- **文件**：`web/static/js/app.js`、`web/static/index.html`、`web/static/css/style.css`
- **验证**：全量 `tests/unit` 对基线新增失败 0；实机无 JS 错误

### 11. Demo 决策树/可视化渲染成空壳

- **问题**：Demo 模式下「决策树」卡片只有节点号没有内容，「可视化」节点显示 `→ — —`
- **结论先行**：**真实分析链路未被改坏** —— 历史记录回放实测 47 张卡片全部带 `question`+`reason`、可视化 200 个 SVG 节点、无 JS 错误。问题只出在 demo 数据
- **根因**：上一轮重写 `routes_demo.py` 时，决策树 trace 是「凭想象编的形状」，与真实流水线不同构：
  | 字段 | 真实记录 | demo（改前） |
  |---|---|---|
  | `gate_trace` | dict 列表 ×8 | 纯字符串列表 |
  | `decision_trace` | dict ×15 | dict ×3，缺 `question`/`bar_range`/`skipped`/`section` |
  | `terminal.outcome` | `reject` 等真实取值 | `trade`（不在真实集合内） |
  
  前端 `renderTraceCard` 与决策树可视化读 `item.question` / `item.reason`，demo 缺失 → 渲染为空壳
- **修复**：按真实记录结构重建 demo trace —— gate_trace 4 节点（含 `branch`/`section`）、decision_trace 6 节点覆盖四个 section 并含一个 `skipped=True` 未走分支、`terminal.outcome` 改为 `accept`
- **文件**：`web/api/routes_demo.py`、`tests/unit/test_demo_decision_tree_shape.py`(新，6 例)
- **验证**：新增测试含一条直接读取真实历史记录做**逐字段对齐**，demo 再漂移即失败；实机截图确认决策树 10 张卡片全部带问题/理由/K线依据，可视化节点显示完整问题与回答。全量 `tests/unit` 对基线：新增失败 0

### 10. 经验库闭环 + 指标图例配色修正 + 图表数据入口收口

- **问题**：经验库（素材库）没有写入方、没有浏览入口、读取默认关闭；主图指标图例色块全黄；图表数据更新存在「只做一半」的隐患
- **根因与修复**：
  1. **经验库是条死路**：`ExperienceReader` 文档明写 *strictly read-only*，全仓无任何写入代码；`PromptSettings.experience_max_entries` 默认 **0** 导致检索链路空跑（实测 `experience_loaded` 恒为 `[]`）；Web 端也没有浏览入口。另修正 AGENTS.md 过时记载——`experience/` 实际有 59 条数据
     - 新增 `pa_agent/records/experience_writer.py`：`ExperienceWriter.save()` 原子落盘（tmp + `os.replace`），同秒多次写入不覆盖，`symbol`/`cycle` 经 `_safe_segment` 防路径穿越
     - 新增 `evaluate_outcome()`：按后续 K 线判定 TP1/SL 先后；**同一根 bar 同时触及两者时保守按止损计**（OHLC 无法还原 intrabar 路径，按乐观计会把经验库偏向虚高胜率）
     - 新增 `web/api/experience_watcher.py`：下单信号时起 daemon 线程轮询数据源，TP/SL 触达即回写一条经验；超时未了结则不写入。与通知线程一致：失败只记 warning，绝不冒泡进分析主流程
     - 接入 `order_followup.spawn_post_order_followup()`（AGENTS.md 单一入口）；新增 `GET /api/experience` 与侧边栏「经验库」tab
     - `experience_max_entries` 0 → 3，新增 `experience_auto_write` / `experience_max_wait_s`
  2. **指标图例全黄**：图例用 `series.applyOptions()` 无参调用想读回颜色，但该 API 是写入型的，无参调用抛 `Cannot read properties of undefined (reading 'priceScaleId')`，被 catch 吞掉后退回 registry 声明色（`ema` 对所有周期均为 `#ffc800`），故 6 个色块全黄、与主图实际 6 色不符。改用 `series.options().color`
  3. **图表数据入口不收口**：`setBars` + `setSeqMarkers` + 指标重算 + 时间锚点是成套动作，只做一半就会让指标持有上个品种数据（上一轮 demo 即如此）。收口为唯一入口 `applyBarsToChart(bars)`
- **文件**：`pa_agent/records/experience_writer.py`(新)、`web/api/experience_watcher.py`(新)、`web/api/order_followup.py`、`web/api/routes_data.py`、`pa_agent/config/settings.py`、`web/static/{index.html,js/app.js,js/indicators.js,css/style.css}`、`AGENTS.md`
- **验证**：新增 `tests/unit/test_experience_library_loop.py`(19)。写→读闭环实测成立（写出的文件立刻被 `ExperienceReader` 检索到）；胜负判定含同根双触保守按止损；`/api/experience` 返回 36 条 / 9 个周期；图例色块实测 `rgb(178,108,255) / rgb(255,152,0) / rgb(255,82,82) / rgb(255,235,59) / rgb(38,166,154) / rgb(41,182,246)`；无 JS 错误。全量 `tests/unit` 对基线：新增失败 0，修复 2

### 9. 功能键联动 review：demo 三方脱钩 / 交易价位图例 / 死代码

- **背景**：用 Playwright 逐个操作功能键并量取 DOM/canvas，核对 AGENTS.md 的联动规则
- **控件联动实测（均符合预期，未改动）**：等待收盘 ⇄ 分析按钮 waiting/idle；持续分析强制勾选并锁定「实时」「等待收盘」、关闭后恢复可编辑；取消实时只关数据流
- **发现并修复**：
  1. **Demo 的「决策 ↔ K 线 ↔ 指标」三方脱钩（High，四层成因）**
     - `/api/demo/sample` 缺 `kline_data`：上一轮 demo 改用 `_serialize_record`，而该函数是「真实分析流」序列化器，K 线随 SSE 单独下发，刻意不含此字段
     - **契约不匹配**：demo handler 把 bar 预映射成 `{time,...}`，但 `setBars` 的契约是**原始 bar**（`ts_open`/`closed`），于是 `a.ts_open === undefined` → 排序退化、time 为 `NaN`，LightweightCharts 抛 `Value is null` / `right should be >= left`
     - **指标不重算**：`loadBars()` 是 `setBars` + `onBarsUpdated` 两步，demo 只做第一步。实测切到 BTCUSDT 1d 后 EMA 仍是 NVDA 的 210~234，蜡烛却是 48000~64000，自动缩放把两个数量级一起纳入 → 价格轴被拉到 **-8000~66000**，K 线压成顶部一条
     - `__PA_LAST_BAR_TIME__` 未更新，方向箭头仍指向切换前的品种
  2. **入场/止损/止盈在图上不可读（Med）**：`fitView` 只看最近 20 根，TP2/SL 常落在可见区间外。新增 `#trade-legend` 固定列出四个价位与颜色，超出区间时标注「视野外 ↑/↓」；位置由右上改为左上（右上与价格轴数值标签叠字）
  3. **两处死代码（Low）**：`#token-progress-bar`（页面只有同名 class 无该 id，95% 告警变红从未生效，且 toast 反复触发）；`#ds-symbol-select`（HTML 中不存在，永不触发）
- **文件**：`web/static/js/app.js`、`web/static/js/chart.js`、`web/static/index.html`、`web/static/css/style.css`、`web/api/routes_demo.py`
- **验证**：Demo 实机截图确认蜡烛、EMA、MACD、Entry/SL/TP1/TP2 横线与图例、方向箭头全部对齐同一价格轴；指标值域由 `210~234` 变为 `56605~75385`（随品种）；工具栏品种同步为 BTCUSDT；无 JS 错误。全量 `tests/unit` 对基线：新增失败 0

### 8. UI 实机排查：追问入口不可见 / MACD 副图缺失 / 指标无图例 / 标记淹没 K 线

- **背景**：改用 Playwright + headless Chromium 对部署实例（`:8005`）实机截图与 DOM 度量排查 UI，而非只读代码
- **问题**：
  1. `.chat-input-row` 位于 `#tab-stream` 滚动流末尾，`getBoundingClientRect().top = 1011` > 视口 1000 → **追问输入框完全不可见**，而「分析完成后继续追问」是主交互之一；免责声明同时被折线切成一半（top 982 / bottom 1011）
  2. `initIndicators()` 注释声明「默认启用 6 条 EMA + MACD 副图」，实际循环中 `addIndicator('macd')` 调用数为 **0** → `#chart-osc-wrap` 永远 `display:none` / 高度 0，副图功能形同虚设
  3. 首屏叠加 EMA5/10/20/40/60/120 共 6 条线，**全程无任何图例**，无法区分紫=EMA5 与橙=EMA10
  4. `_seqStep` 按固定档位取步长，200 根落在 step=5 → 屏幕上铺 **40 个** `#N` 圆点+文本，蜡烛被完全遮盖
- **修复**：
  - 免责声明 + 输入框包入 `.stream-footer` 整体 sticky 到底部（JS 不依赖二者兄弟关系，仅按 id 取元素）
  - 补 `addIndicator('macd', {})`，恢复副图
  - 新增 `#chart-legend`：读取各 overlay series 的**实际 `applyOptions` 颜色**渲染，EMA 加粗配色与自定义指标均如实反映；点击可临时隐藏单条线
  - `_seqStep` 改为「标记总数上限 16 + 整数步长表 `[1,2,5,10,20,25,50,100]`」，任意长度标记数 ≤16 且序号取整
- **文件**：`web/static/index.html`、`web/static/css/style.css`、`web/static/js/{chart.js,indicators.js}`
- **验证**：实机度量 `footer/chatInput/disclaimer.visible = true`（top 915 / 944，bottom 990 ≤ 1000）；`osc-wrap` 变为 `flex` / 140px，MACD(12,26,9) 正常绘制；图例 6 项且像素级验证点击 EMA5 → `#b26cff` 像素 518 → 0 → 恢复 518；序号标记 200 根下由 40 个降至 10 个；页面无 JS 错误。版本号 `chart.js v4→5`、`indicators.js v3→4`、`style.css v18→20`、`app.js v27→29`

### 7. 持续分析 / 等待收盘 前端联动缺陷修复

- **问题**：持续分析在 SSE 模式下无法真正发起分析；用户取消「等待收盘」勾选会永久挂起等待中的分析
- **根因**：
  1. 「持续分析」按联动规则强制勾选「等待收盘」，而 `startAnalysis` / `startIncrementalAnalysis` 开头无条件检查 `cbWaitClose.checked` 并 `await startWaitCloseCountdown()`。于是 `bar_close` 触发的持续分析会**再次等待一根收盘**——与触发它的语义直接冲突。倒计时归零（`remaining<=0`）与 `bar_close` 几乎同时到达：倒计时先到则正常发起；`bar_close` 先到则 `startWaitCloseCountdown()` 内的 `stopWaitCloseCountdown()` 把 pending resolver 取消成 `resolve(false)`，本根 bar 的分析被跳过
  2. `#cb-wait-close` 的 change handler 只调用 `stopWaitingCountdownDisplay()`（停显示定时器），没有停 `stopWaitCloseCountdown()`（停 resolver）。此刻若 `startAnalysis` 正 await 在倒计时上，`refreshAnalyzeButtonWaitingState()` 把按钮置回 `idle`，而 `updateSSEStatusWithExpiry` 里 `if (btn.dataset.state !== 'waiting') return` 提前返回 → resolver 再无人 resolve → 分析永不发起、按钮卡住
- **修复**：
  - 新增 `triggerSource`（`'user'` / `'continuous'`）参数，判定收敛到 `continuous_gate.shouldWaitForClose()`；`'continuous'` 触发时不再等待，因为 bar 刚刚收盘
  - 取消勾选时同时调用 `stopWaitCloseCountdown()`，`resolve(false)` 让 `startAnalysis` 正常 return
  - 把「刚收盘 bar 的 `ts_open`」从 `app.js` 里重复的 3 份抽为 `continuous_gate.closedBarTs()` 唯一实现——三份必须永远一致，任一份漂移都会让 `bar_close` 与倒计时路径的哨兵去重互相打架
- **文件**：`web/static/js/continuous_gate.js`（新增）、`web/static/js/continuous_gate.test.js`（新增）、`web/static/js/app.js`、`web/static/index.html`
- **验证**：新增 Node 单测（`node web/static/js/continuous_gate.test.js`，无 DOM 依赖的纯逻辑可直接 require 求值），覆盖 `closedBarTs` 的正常/休市/乱序/空数组边界与 `shouldWaitForClose` 契约。`app.js?v=27→28`、新增 `continuous_gate.js?v=1`。全量 `tests/unit` 对基线：新增失败 0。已重建镜像并部署至 `:8005`，确认 `continuous_gate.js` 返回 200 且 `app.js` 中有 10 处 `PAContinuousGate` 引用

### 6. 设置接口凭据跨域暴露修复 + Docker 重新部署

- **问题**：`GET /api/settings` 只脱敏 `provider.api_key`，其余通知凭据明文返回；配合 `allow_origins=["*"]` 且全站无鉴权，运营者访问的任意网页即可跨域读取并可驱动写接口
- **根因**：
  1. 脱敏范围仅覆盖 `provider.api_key`，飞书 `secret`/`app_secret`/`webhook_url`、PushPlus token、Tushare token、TradingView 密码/session 均明文返回（且注释明确写着「保持明文以适配表单回填」）
  2. `CORSMiddleware allow_origins=["*"]`，而 WebUI 与 API 本就同源，CORS 完全不需要
- **修复**：
  - 新增 `_SECRET_FIELDS` 覆盖 8 个凭据字段，GET 统一输出 `abcd****wxyz`；非凭据字段（`app_id`/`enabled`/`username`）不受影响
  - PUT 的占位值保护从「仅 api_key」推广到全部 `_SECRET_FIELDS`，表单回填仍可安全保存；判定同时收紧为「含 `****` 才算占位」——此前 `<16` 字符即视为占位的启发式会让用户保存其它设置时**静默丢掉较短的飞书/Tushare 凭据**
  - `provider.api_key` 空提交按「保持原值」处理，其余字段空提交视为主动清空
  - CORS 默认不启用；确需分离前端时用 `PA_AGENT_CORS_ORIGINS` 显式列出并记录告警
  - 前端为脱敏字段加虚线边框与「已配置」提示，避免用户误把占位值当真实值重输
- **Docker 重建与部署**：
  - 修复镜像烘焙凭据（`COPY config/` → 只拷 `*.example.json`），`.dockerignore` 追加凭据与备份排除
  - 补 `matplotlib`（此前交易图 PNG 永远静默跳过）、显式声明 `requests`、补 `fonts-noto-cjk`（否则图表中文全是豆腐块）
  - 镜像内创建 `trade_records/`，compose 增加 `trade_records`/`experience` 挂载与 healthcheck（原先交易记录随容器重建丢失）
  - 该 LXC 主机无法在 buildkit RUN 阶段加载 apparmor profile，按 `docker-compose.yml` 既有说明改用 `docker run --security-opt apparmor=unconfined` + `docker commit` 构建
- **文件**：`web/api/routes_settings.py`、`web/server.py`、`web/static/{js/app.js,css/style.css,index.html}`、`web/Dockerfile`、`web/docker-compose.yml`、`.dockerignore`、`pyproject.toml`、`uv.lock`
- **验证**：新增/更新 21 例凭据脱敏与往返测试。部署后容器运行于 `0.0.0.0:8005`，镜像内确认无 PyQt6、20 条路由注册、CORS 默认关闭、无 `settings.json` 进镜像层；端到端跑通 subscribe → 201 根 K 线 → 完整两阶段分析（160,416 tokens，无异常），记录以「时间戳_毫秒_uuid」写入宿主机挂载目录且无 `.tmp` 残留；跨域请求不再返回 `Access-Control-Allow-Origin`

### 5. 追问链路优化 + 记录缓存/健康探测/demo 契约修复

- **问题**：追问（`/api/chat/stream`）跨品种串味、并发追问损坏会话；增量分析的记录缓存永久失效；失败记录读不出来；未鉴权健康端点驱动无上限 LLM 调用；demo 模式渲染错位
- **根因与修复**：
  1. **追问跨品种串味**：`ctx._last_record` 跨品种切换仍存活，切换后追问静默锚定到上一个品种的分析。新增 `_record_matches_subscription()`，不匹配时按当前 `(symbol, timeframe, exchange)` 从历史取最新记录；比较仅在两侧均为非空字符串时生效
  2. **追问会话串味**：`session_key` 缺省时所有会话共用 `'latest'`。改为 `record_id|品种|周期|K线快照开关`，不同品种对话互不污染，且 `attach_kline_snapshot` 复用时真正生效（此前被静默忽略）
  3. **并发损坏**：`FreeChatSession.send()` 会改 `_turn` 与 `_history_full` 且内部无锁，两个并发追问会交错历史、重复轮次号 → per-session 锁串行化
  4. **内存记录被覆盖**：任何一次磁盘回退都会无条件写回 `ctx._last_record`，覆盖刚分析出的记录。改为仅在确实取到更新记录时提升
  5. **记录缓存永久失效**：`find_latest_successful_record` 缓存键取**根目录** mtime，但记录写在嵌套 `{exchange}/{symbol}/{timeframe}/` 分区，嵌套写入不改根目录 mtime（已实测），且 `None` 也会被缓存 → 增量分析一直复用旧记录。新增 `_scan_signature()`，签名包含实际扫描的分区目录
  6. **失败记录不可见**：`load_record` 未 `pop("_partial_reason")`，schema 为 `extra="forbid"` → 每条 `save_partial` 记录都读不出来（`routes_records` 侧已有 pop）
  7. **健康探测成本可滥用**：`/api/health/check` 未鉴权却驱动一次**不限 token、默认 600s 超时**的真实 LLM 调用。`chat()` 新增 `max_tokens` 覆盖，探测改为 `max_tokens=1` + `timeout_s=10`
  8. **demo 契约错位**：payload 与前端渲染器有 6 处不匹配（决策区恒显「不下单」、概率芯片全 0%、`terminal` 渲染为空串）。改为构造真实 `AnalysisRecord` 并复用 `_serialize_record()`，从结构上杜绝漂移
- **文件**：`web/api/routes_chat.py`、`web/api/routes_demo.py`、`pa_agent/records/analysis_history.py`、`pa_agent/util/startup_health_check.py`、`pa_agent/ai/deepseek_client.py`
- **验证**：新增 `tests/unit/test_followup_and_audit_fixes.py`(12)。线上实测两轮连续追问均正常返回 reasoning + content + done；全量 `tests/unit` 对基线：新增失败 0，修复 2

### 3. 逐功能审计修复：交易静默否决 / 凭据泄露 / 上下文溢出

- **问题**：对推理链、`web/` 后端、`data`+`records`+`notify` 三路逐功能审计后发现 4 个 High 级缺陷，其中一个会**静默吃掉真实交易**
- **根因与修复**：
  1. **RR 上限自造否决（吃掉交易）**：`MIN/MAX_RISK_REWARD_RATIO` 同为 1.0，上限靠拉宽止损把 TP1 盈亏比压到 1.0，于是 reward == risk，交易方程 `p*reward > (1-p)*risk` 退化为 `p > 0.5`——**任何盈亏比超限且胜率 ≤50% 的单子必然被否**。线上实录：BTCUSDT 15m 盈亏比 9.14:1、胜率 50%、期望值 +36.6 被改成「不下单」；同一决策内节点 10.2 报 risk=9.0 而 10.3 报 risk=82.3，互相矛盾。修复：方程改用上限调整**前**的模型原始几何判定，盈亏比上下限仍在调整后几何上检查
  2. **凭据明文进日志/记录**：`requests` 的 `ConnectionError` 把完整 URL（含飞书 `bot/v2/hook/<TOKEN>`）塞进异常串 → 明文写进 `logs/pa_agent.log`；`JsonlFormatter` 不脱敏 `exc`；`PendingWriter._api_key` 构造后不刷新，换密钥后新 key 明文落盘。修复：`mask_secret` 升级为进程级 `SecretRegistry`（`register_secret`/`scrub`），两个 formatter 统一过滤，`register_settings_secrets()` 在载入/保存时登记全部凭据，落盘时一并 scrub
  3. **KV cache 命中率恒读 0**：`_extract_cached_prompt_tokens` 只认两种字段名，遇到 `cache_read_input_tokens`/顶层 `cached_tokens`/`model_extra` 一律返回 0（实测 0.18%），使 prefix-chain 优化收益不可见。修复：补齐各形状
  4. **大 `analysis_bar_count` 直接 400**：允许至 5000 根，但提示词约 283 字符/根，约 2500 根即撑爆 1M 上下文。修复：`submit()` 发起任何 API 调用前做 context-budget 预检，超限 fail fast
- **文件**：`pa_agent/util/{trade_metrics,mask_secret,logging}.py`、`pa_agent/ai/deepseek_client.py`、`pa_agent/orchestrator/two_stage.py`、`pa_agent/notify/feishu_notifier.py`、`pa_agent/records/pending_writer.py`、`web/api/routes_settings.py`
- **验证**：新增 `tests/unit/test_secret_scrubbing.py`(12)、`test_context_budget_and_cache_meter.py`(13)，`test_trade_metrics_validation.py` 补 4 例 RR 上限回归。全量 `tests/unit` 对基线：新增失败 0，修复 2

### 2. 补齐 GUI→WebUI 移植缺口：下单推送 / 交易落盘 / 失效开关

- **问题**：对照 `pa_agent/gui/` 逐模块审计 WebUI 后发现，桌面 GUI 的下单信号推送与交易记录在 Web 端**完全没有实现**
- **根因**：全仓 `save_trade_record` / `feishu.send_order_signal` / `pushplus.send_order_signal` 的调用者**只有** `pa_agent/gui/main_window.py:4016-4096`，`web/` 零命中。即配了飞书/PushPlus 配置 UI、且「测试发送」按钮可用，但服务端从不真正发送，交易 CSV 也从不落盘。Docker 常驻部署没有浏览器标签页可依赖，告警实际是死的
- **修复**：
  1. 把 `has_order_opportunity()` 等纯门控逻辑从 `pa_agent/gui/order_opportunity.py` 抽到 Qt-free 的 `pa_agent/ai/order_opportunity.py`；原文件改为转出同名符号的兼容层，GUI import 不变。抽出后测试不再需要 pyqtgraph（原先 `pa_agent/gui/__init__.py` 会 `import MainWindow → ChartWidget → pyqtgraph`，无 Qt 环境直接 ImportError）
  2. 新增 `web/api/order_followup.py`：门控通过后在 daemon 线程执行 `save_trade_record()`（CSV + 图表 PNG）与 Feishu/PushPlus 推送，每步独立 try/except，异常只记 warning 不冒泡进分析流程
  3. 在 `web/api/routes_analyze.py::_run_analysis` 成功提交记录后调用 `spawn_post_order_followup()`
- **附带修复**：
  1. **`cancel_keep_analysis_on_retry` 开关失效**：前端只在保存设置时写盘，从未在重试事件读取该开关 —— 界面上是个完全不起作用的复选框。新增 `applyCancelKeepAnalysisOnRetry()`，在 `Stage1Retry`/`Stage2Retry`（全量与增量两条流共 4 处）按开关关闭持续分析并持久化，对齐 GUI 的 `_on_retry_occurred`
  2. **切换品种后的增量提示**：桌面 GUI 在切换后自动跑一次增量；Web 端改为提示（`refreshIncrementalButtonState()` 返回可用性 + toast）。因为每次分析都调 LLM 且现在可能推送下单信号，浏览品种时自动触发会反复烧 token 并打扰通知渠道
- **文件**：`pa_agent/ai/order_opportunity.py`（新增）、`pa_agent/gui/order_opportunity.py`、`web/api/order_followup.py`（新增）、`web/api/routes_analyze.py`、`web/static/js/app.js`、`web/static/index.html`（app.js `?v=23`→`?v=25`）、`tests/unit/test_web_order_followup.py`（新增 14 例）
- **验证**：`tests/unit` 全量对比同步前 `main` 基线：**新增失败 0，修复 2**。实测分析完成后生成 `trade_records/BTCUSDT_15m.csv` 且正确调用飞书（未配置时优雅跳过）

### 1. 同步上游 1.31 ~ 1.39（11 个提交）

- **目标**：把上游 `upstream/main`（`rosemarycox5334-debug/PA_Agent`）从 `bb7c7d3`（1.3）推进到 `cd0aca2`（1.39），共 11 个提交、28 个文件、+614/-242
- **上游主要内容**：
  1. `pa_agent/ai/rate_limit.py`（新增）：429 限流的指数退避重试 `call_with_rate_limit_backoff()`
  2. `pa_agent/ai/incremental_shift.py`（新增）：增量分析时机械平移上一轮 JSON 的 K 线序号引用
  3. `pa_agent/ai/deepseek_client.py`：B.AI 网关限流、Packy/DeepSeek 上限细化、RateLimit 错误分类
  4. `pa_agent/ai/prompt_assembler.py`：增量场景 prompt 裁剪优化
  5. `pa_agent/ai/session_ledger.py`、`pa_agent/data/akshare_source.py`、`ashare_common.py`、`pa_agent/gui/*` 等配套调整
  6. 默认值调整：`GeneralSettings.last_data_source` → `mt5`、`last_symbol` → `XAUUSDm`、`context_warning_threshold_pct` → `99999999.0`
- **合并前保护**：本地未提交工作先落到 `wip/local-work-20261003`，再合入同步分支，避免冲突中丢失
- **冲突解决（3 处）**：
  1. `pa_agent/ai/deepseek_client.py`：保留上游 `call_with_rate_limit_backoff` 包装 + 本仓库 `top_p` 例外注释
  2. `config/settings.example.json`：采用上游 `context_warning_threshold_pct`（与已合并的 `settings.py` 代码默认值一致）；保留本仓库 Web 优先的 `tradingview`/`GATEIO`/`BTCUSDT` 默认与新增字段
  3. `README.md`：上游把「安装内容：PyQt6...」误放进 Web 章节，已移回「桌面 GUI」章节；同时恢复上游 `b81cf2d` 误删的「### uv 隔离环境（可选）」小标题
- **二次冲突（并入 WIP 时）**：`deepseek_client.py::_provider_max_output_tokens` 取两侧并集 —— 保留上游新增的 B.AI 8192 分支，叠加本仓库 OpenRouter 32768 分支；unknown 分支仍走上游 `_GLOBAL_MAX_OUTPUT_TOKENS`(384000) 全局 clamp；移除已无引用的 `_PRACTICAL_UNLIMITED_MAX_TOKENS` 与 `_UNKNOWN_PROVIDER_MAX_OUTPUT_TOKENS`
- **附带修复**：`tests/unit/test_settings_round_trip.py::test_round_trip` —— 上游 1.31 重写该测试并断言 `BTCUSDT` 被迁移为 `XAUUSDm`，这是本仓库早已移除的行为（非黄金品种保留用户选择，见 AGENTS.md「品种迁移逻辑」），按本仓库约定更新断言
- **验证**：与同步前 `main` 基线（worktree）逐条对比 `tests/unit`：**新增失败 0 个，修复 1 个**（上游修好了 `test_completion_max_tokens_packy_claude_cap`）。其余 32 失败 / 30 error 均为同步前既有（Linux 缺 MetaTrader5、缺 cursor-sdk、Qt 无显示环境等）
- **分支**：`chore/sync-upstream-20261003`

---

## 2026-07-22

### 7. closebar 时间错误修复 + 增量/持续分析标识

- **问题**：历史分析记录中 closebar 时间显示错误（显示最早 bar 的时间而非刚收盘 bar 的时间）；无法区分增量分析和持续分析
- **根因**：
  1. `two_stage.py` 和 `routes_records.py` 中 `kline_data` 是 newest-first 顺序，代码却用 `kline_data[-1]` 取最早的 bar 时间，应取 `kline_data[1]`（K1，刚收盘的 bar）
  2. 记录元数据缺少 `incremental` 和 `continuous` 字段，无法追溯分析类型
- **修复**：
  1. `pa_agent/records/schema.py`：`RecordMeta` 添加 `incremental: bool = False`、`continuous: bool = False`
  2. `pa_agent/orchestrator/two_stage.py`：`_build_empty_record` 和 `submit` 方法添加参数，取 `kline_data[1]` 计算 closebar 时间
  3. `web/api/routes_analyze.py`：`_run_analysis` 添加 `continuous` 参数，路由支持 `continuous=true` 查询参数
  4. `web/api/routes_records.py`：`_list_records` 返回 `incremental` 和 `continuous` 字段，修复 `_derive_last_close_bar_iso` 取 `kline_data[1]`
  5. `web/static/js/app.js`：持续分析触发时传递 `continuous=true`，历史记录列表显示「增量」「持续」标签
  6. `web/static/css/style.css`：添加 `.history-tag`、`.history-tag-incremental`、`.history-tag-continuous` 样式
- **文件**：`pa_agent/records/schema.py`、`pa_agent/orchestrator/two_stage.py`、`web/api/routes_analyze.py`、`web/api/routes_records.py`、`web/static/js/app.js`、`web/static/css/style.css`

### 6. 休市（美股已收盘）时倒计时显示错误

- **问题**：美股收盘后（如北京时间 04:00 后），「等待收盘」按钮和状态栏仍显示错误的倒计时（取模算法返回的未来周期边界时间戳），倒计时归零后还会错误触发分析
- **根因**：`/api/bars/next-close` 后端端点没有检测休市状态。休市时 `bars[0].closed == True`（无 forming bar），但代码仍把它当 forming bar，用取模算法 `_compute_next_close_ts(ts_open, tf)` 计算出一个**未来的周期边界时间戳**。前端 `sseNextCloseTs` 被设成错误值，SSE 休市时只推 ping 不推 bar_update，`sseNextCloseTs` 永远不被纠正
- **修复（后端）**：`/api/bars/next-close` 检测 `forming.closed == True`，返回 `market_closed: true`，`next_close_ts: null`，短路取模算法
- **修复（前端）**：
  - `fetchAndUpdateNextCloseTs`：收到 `market_closed: true` 或 `next_close_ts` 为 null 时清空 `sseNextCloseTs = 0`
  - `loadBars`：检测 `bars[0].closed === true` 时清空 `sseNextCloseTs`（数据刷新时主动感知休市）
  - `updateSSEStatusWithExpiry`：改进休市检测——`sseNextCloseTs` 已过期（`>0 && < Date.now()`）时主动调 `fetchAndUpdateNextCloseTs(true)` 检测休市；`sseNextCloseTs === 0` 且 1 个 timeframe 无更新时显示「休市中」（从 `tfSecs * 2` 缩短为 `tfSecs`）
- **文件**：`web/api/routes_data.py`、`web/static/js/app.js`、`web/static/index.html`（版本号 v=19 → v=20）
- **验证**：41 个单元测试通过；HTTP 验证 `/api/bars/next-close` 返回 `{"market_closed": true, "next_close_ts": null}`；`bars[0].closed == True` 确认休市检测正确

### 5. 等待按钮倒计时与状态栏共享同一 tick（消除两个 setInterval 不同步）

- **问题**：用户反馈「等待收盘」按钮倒计时与 K线状态栏不同步、显示慢
- **根因**：前端维护了两个独立的 setInterval：
  - `sseStatusExpiryTimer`（L1680）：每秒更新状态栏 `#live-refresh-status`
  - `waitCloseCountdownTimer`（L2209）：每秒更新等待按钮 `#btn-analyze-toggle`
  - 两者都从 `sseNextCloseTs` 读相同数据，但分别调度，导致：
    1. 两个 UI 不同帧更新（按钮落后状态栏近 1 秒）
    2. 算法不一致：状态栏用 `Math.max(0, ...)` 不 ceil（显示 "4.2s"），按钮用 `Math.ceil()`（显示 "5s"），数字对不上
    3. sanity check 逻辑只有状态栏有（`remaining > tfSecs` 时清零并拉取 REST），按钮没有，导致状态栏已清零但按钮还在跑旧值
- **修复**：SSE 活跃时等待按钮复用 `sseStatusExpiryTimer`，不创建独立 setInterval
  - 新增全局变量 `waitCloseCountdownResolver`，由 `updateSSEStatusWithExpiry` 在 remaining <= 0 时触发
  - `updateSSEStatusWithExpiry` 末尾增加等待按钮更新逻辑：同一 tick、同一 remaining、同一 sanity check
  - `startWaitCloseCountdown` SSE 活跃时只存 resolver，不启动 setInterval
  - SSE 不活跃（fallback 轮询）时仍走独立 setInterval（`updateSSEStatusWithExpiry` 此模式不跑）
  - `stopWaitCloseCountdown` 清理 resolver 时先取出再清，防止 resolve 空指针
- **效果**：状态栏和等待按钮完全同步，同一帧渲染、同一数字、同一归零时机
- **文件**：`web/static/js/app.js`、`web/static/index.html`（版本号 v=18 → v=19）
- **验证**：41 个单元测试通过；HTTP 验证 `app.js?v=19` + `Cache-Control: no-cache, no-store, must-revalidate`

### 4. 倒计时触发系统稳定化（TDD）+ 服务器重启规范固化

- **问题**：用户多次反馈「等待收盘」按钮倒计时与状态栏不同步、归零后 K线序号/指标不刷新、持续分析模式重复触发；且每次修改后用户反馈「还是老样子」，误判为浏览器缓存问题
- **根因（倒计时 bug）**：
  1. `startWaitCloseCountdown` 启动时捕获 `targetMs = sseNextCloseTs` 快照，setInterval 内不重新读取，SSE 推新 bar 后倒计时仍指向旧值
  2. 倒计时归零后 `loadBars()` 无 await，分析在数据刷新前启动
  3. SSE 未启动时 REST fallback 只调用一次，不检查恢复
  4. 倒计时归零触发分析时不更新 `keepAnalysisLastClosedTs`，SSE bar_close 重复触发
  5. `_compute_next_close_ts` 硬编码 `time.time()`，测试无法注入固定时间
  6. `TradingViewSource.latest_snapshot()` TTL 缓存（5m=8s）导致 bar_close 推送旧数据
- **根因（"缓存问题"真相）**：uvicorn 未开 `--reload`，修改 `routes_bars_stream.py` 等后端代码后未重启服务器，用户看到的仍是旧代码行为，被误判为浏览器缓存
- **修复（倒计时）**：
  - 移除 `targetMs` 快照，`computeRemaining` 每秒动态读取全局 `sseNextCloseTs`
  - SSE 未就绪时每 5 秒调 `fetchAndUpdateNextCloseTs(true)` 检查恢复
  - 倒计时归零后 `loadBars().then(...).finally(() => resolve(true))` 确保 K线刷新完成
  - loadBars 完成后更新 `keepAnalysisLastClosedTs` 为 `sorted[last].ts_open`（与 SSE handler 同公式）
  - `_compute_next_close_ts` 添加 `now_ms` 可选参数
  - `_push_bar_close` 调用 `source.clear_snapshot_cache()` 后再 `latest_snapshot`
  - 新增 `tests/unit/test_countdown_consistency.py`（8 测试）验证两函数一致性
- **修复（流程规范）**：
  - AGENTS.md 新增「代码修改后必须重启服务器」最高优先级硬约束
  - CONTRIBUTING.md 启动方式推荐 `--reload --reload-dir web --reload-dir pa_agent`
  - 服务器已用 `--reload` 模式重启，未来 Python 改动自动热重载
- **文件**：`web/static/js/app.js`、`web/api/routes_bars_stream.py`、`pa_agent/data/base.py`、`pa_agent/data/tradingview.py`、`pa_agent/data/eastmoney_source.py`、`tests/unit/test_countdown_consistency.py`、`tests/unit/test_routes_bars_stream.py`、`AGENTS.md`、`CONTRIBUTING.md`
- **验证**：41 个单元测试通过；HTTP 验证 `app.js?v=18` + `Cache-Control: no-cache, no-store, must-revalidate` 正确返回

### 3. 倒计时触发系统稳定化 spec 起草

- **问题**：用户多次反馈倒计时触发时机不一致，要求用 TDD 方式系统性自查
- **改动**：起草 spec（`spec.md` / `tasks.md` / `checklist.md`），深度自查发现 6 个潜在 bug
- **文件**：`.trae/specs/stabilize-countdown-trigger/`

### 2. 按钮视觉重设计（Aurora Quant Console）+ bindEvents 未调用根因修复
- **问题**：用户反馈侧边栏按钮设计太丑；同时测试发现「持续分析」change 事件 handler 不触发（所有按钮失去响应）
- **根因（重大 bug）**：`DOMContentLoaded` async handler 中 `bindEvents()` 调用在 `await loadBars()` 之后。而 `loadBars()` 在 catch 块中 `throw e` 重新抛出异常（line 326），任何数据加载失败（如 TradingView 连接超时、品种不存在等）都会导致整个 async handler 中断，`bindEvents()` 永远不执行 → 所有按钮的 click/change handler 都未注册
- **视觉重设计**：采用 **Aurora Quant Console** 设计方向（深色 obsidian + 4 层阴影立体感 + LED 指示灯 + shimmer 微光动画）
  - **字体升级**：UI 用 IBM Plex Sans，状态/数值用 JetBrains Mono（Google Fonts CDN）
  - **分析按钮 CTA**：44px 高度，4 层阴影（顶部高光 + 底部暗影 + 近距投影 + 外发光），shimmer 微光扫过动画（6s 慢速循环，hover 时加速至 1.8s），analyzing 状态红色脉冲外发光
  - **Console Toggle 替代 Pill Toggle**：锐利 6px 圆角（非 999px 胶囊），左侧 LED 指示灯（7px 圆点，开启时绿色脉冲发光 + led-pulse 2.4s 动画），锁定态用对角条纹叠加（非单纯降透明度）
  - **大气背景**：sidebar-header 径向渐变 + SVG feTurbulence 噪点纹理叠加（opacity 0.6, mix-blend-mode overlay）
  - **工具图标**：统一 16px viewBox / stroke 1.75 的 refined outline SVG 风格，hover 时立体阴影
- **bindEvents 修复**：将 `bindEvents()` 提前到所有 `await` 之前调用，确保 UI handler 在任何数据加载失败时也能注册。原顺序：`await loadSettings → await loadExchanges → await loadSymbols → await loadTimeframes → await loadBars → bindEvents`；新顺序：`bindEvents → await loadSettings → ... → await loadBars`
- **验证**：17 项 Playwright 视觉验证 16/17 通过（唯一「失败」是测试脚本自身的 regex bug，实际 LED 发光阴影正确）；change 事件 handler 注册数从 0 变为 1；真实点击触发 disabled 锁定生效
- **文件**：`web/static/index.html`, `web/static/css/style.css`, `web/static/js/app.js`

### 1. K线按钮联动与实时更新产品完成度修复（按钮按域分组 + 持续分析 + 倒计时 + 哨兵去重）
- **问题**：Web 版 K线按钮联动逻辑有多处 bug 和产品完成度问题：①休市时持续分析每 60s 重复触发分析浪费 token；②持续分析不使用增量分析；③倒计时显示为裸秒数不直观；④倒计时 sanity check 失败无 fallback；⑤持续分析/等待收盘按钮错放在工具栏（实属分析领域能力）；⑥按钮无联动锁定关系；⑦缺少图表暂停/恢复机制
- **修复**：
  - **按钮按域分组**：工具栏仅保留「实时」1个数据流开关；「等待收盘」「持续分析」移到侧边栏头部，与分析/增量同属分析控制区。「持续跟踪」改名为「持续分析」
  - **侧边栏三行布局**：第1行工具图标（历史/恢复图表/返回实时）；第2行主操作「分析」占满宽度；第3行子选项横排（等待收盘/持续分析/增量）
  - **倒计时时分秒格式**：新增 `formatCountdownHMS(seconds)` 函数，所有倒计时（距下次收盘、等待收盘）统一显示 `HH:MM:SS` 格式
  - **持续分析联动规则**：开启时强制勾选并禁用「实时」+「等待收盘」（持续分析依赖 SSE bar_close 事件来自实时流）；关闭时恢复可编辑
  - **哨兵去重**：新增 `keepAnalysisLastClosedTs` 变量，bar_close 事件仅在 ts_open 变化时触发分析，避免同一根 bar 重复触发
  - **自动增量**：持续分析触发时检查增量按钮可用性，有增量基础记录则用 `startIncrementalAnalysis()`，否则 `startAnalysis()`
  - **图表暂停**：新增 `chartUpdatePaused` 状态，分析期间暂停 `bar_update` 的 K线渲染（仍更新 next_close_ts 和状态栏），分析完成后 `loadBars()` 刷新
  - **后端休市修复**：`routes_bars_stream.py` 检测 `forming_bar.closed == True` 时仅推 ping 不推 bar_close，避免休市每 60s 重复推送
  - **next_close_ts 统一**：`routes_data.py` 的 REST 端点改为复用 `routes_bars_stream._compute_next_close_ts`（取模算法），消除 SSE 和 REST 结果不一致
  - **SSE 倒计时 fallback**：sanity check 失败时调 `fetchAndUpdateNextCloseTs()` 拉取正确值
  - **休市显示**：`sseNextCloseTs === 0` 且超过 2 倍 timeframe 无 bar 更新时显示「休市中」
  - **恢复图表按钮**：侧边栏第1行新增 `#btn-fit-view`（⤢），点击调 `chart.timeScale().fitContent()`
- **文件**：`web/static/index.html`, `web/static/css/style.css`, `web/static/js/app.js`, `web/api/routes_bars_stream.py`, `web/api/routes_data.py`

---

## 2026-07-21

### 1. TradingView 凭证 UI 配置化 + 错误消息按交易所动态化
- **问题**：用户反馈 NVDA/NASDAQ 1d 分析记录没有保存。日志根因是 TradingView 匿名访问美股被限流（`Connection to remote host was lost`），`latest_snapshot` 在分析流程第一步就抛 `DataSourceTransientError`，走不到 `save_full`。同时 `format_tradingview_fetch_error` 的 fallback 分支硬编码"现货黄金请用 OANDA + XAUUSD"，对 NVDA/BTCUSDT 等任何空数据场景都显示这条误导提示。
- **修复**：
  - `Settings` 新增 `TradingViewSettings` 节（`username` / `password`），持久化到 `config/settings.json`
  - `env_loader.get_tv_credentials(settings)` 改为优先读 settings，env vars 作为 fallback（保留无 UI 的服务器部署能力）
  - `factory.create_data_source('tradingview')` 内部读 `SETTINGS_JSON_PATH` 传给 `get_tv_credentials`
  - `routes_settings.put_settings` 的 section 白名单加入 `"tradingview"`
  - 前端 AI 服务 tab 新增「TradingView 凭证」字段集（fieldset），`loadSettings` 回填 + `saveSettingsHandler` 提交
  - `tradingview_errors.py` 新增 `_US_EQUITY_EXCHANGES = {NASDAQ, NYSE, AMEX, SIX, TSX, LSE}`，对美股空数据/超时给出"匿名访问常被限流，请配置凭证"针对性提示；通用 fallback 改为按 `ex` 分类（TVC/CAPITALCOM 给出黄金提示），不再把黄金提示强加给所有失败场景
- **文件**：`pa_agent/config/settings.py`, `pa_agent/config/env_loader.py`, `pa_agent/data/factory.py`, `pa_agent/data/tradingview_errors.py`, `web/api/routes_settings.py`, `web/static/index.html`, `web/static/js/app.js`

### 2. tvDatafeed 2.1.0 登录 monkey-patch + alert→toast
- **问题**：配置 TradingView 凭证后，tvDatafeed 仍报 `error while signin` 回退到匿名模式。根因：tvDatafeed 2.1.0 的 `__auth` 用裸 `requests.post` 调 `/accounts/signin/`，没带 session、没设 User-Agent、没先 GET 首页种 cookies，TradingView 直接返回 `{"error": "...", "code": "rate_limit"}`。同时保存设置时 `alert('设置已 saved')` 同步弹窗会触发 TRAE IDE 内置 webview 的 React error #185 渲染崩溃。
- **修复**：
  - `pa_agent/data/tradingview.py` 新增 `_patch_tvdatafeed_auth()`，monkey-patch `TvDatafeed._TvDatafeed__auth`：用 `requests.Session()` + 浏览器 UA + 先 GET 首页种 cookies 再 POST 登录。失败时 fallback 到原实现。实测能拿到 846 字符 JWT auth_token，NVDA/NASDAQ/1d 成功拉到 100 根 K 线。
  - `web/static/js/app.js` 的 `showToast(message)` 扩展为 `showToast(message, type)`，支持 `success/warning/error` 三种背景色；3 处 `alert()` 全部替换为非阻塞 toast，error 类停留 4s，其余 2s
- **验证**：账号 `laok259@gmail.com` 已成功登录（`anonymous=False`），NVDA/NASDAQ/1d 拉到 101 根 K 线（含 forming bar），`/api/subscribe` 返回 `status=subscribed`
- **文件**：`pa_agent/data/tradingview.py`, `web/static/js/app.js`

### 3. 刷新页面后交易所/品种/周期回显错误（migrate_general_gold_defaults 误迁移）
- **问题**：用户在 UI 选 NVDA/NASDAQ/1d → 点应用 → `/api/subscribe` 写入 settings.json → 刷新浏览器 → 回显变成 XAUUSD/OANDA/1h，不是刚保存的 NVDA/NASDAQ/1d
- **根因**：`load_settings()` 每次 `GET /api/settings` 时都会调 `migrate_general_gold_defaults(general)` → `resolve_tv_pair("NASDAQ", "NVDA")`。`resolve_tv_pair` 的 6 步分类（公司名/指数/A股/港股/crypto/gold fallback）都没命中 NVDA，最终 fallthrough 到 `resolve_tv_gold_pair`。该函数的最终 fallback 分支 `return GOLD_TV_EXCHANGE, GOLD_TV_SYMBOL, ...` 把任何"非黄金交易所 + 非黄金 symbol"组合强制改成 OANDA/XAUUSD，导致 NVDA/NASDAQ 被误迁移。这是迁移逻辑设计时的过度兜底——把"不认识的 symbol"全当黄金处理。
- **修复**：`pa_agent/data/market_defaults.py` 的 `resolve_tv_gold_pair` 在最终 fallback 前增加判断：若 exchange 非空非 auto、且 symbol 不是黄金关键词（XAUUSD/GOLD/XAU），直接返回 `(ex, sym, False)` 信任用户选择。仅当 symbol 确实是黄金关键词（即使交易所不对）才路由到默认黄金 feed。
- **验证**：8 组用例全部正确 —— NASDAQ/NVDA 保持原值；NYSE/AAPL 保持原值；OANDA/XAUUSD、TVC/GOLD 保持原值；TVC/XAUUSD 修正为 TVC/GOLD；NASDAQ/XAUUSD 异常组合修正为 OANDA/XAUUSD；空值给默认 XAUUSD。settings.json 和 `/api/settings` 返回值现在完全一致
- **文件**：`pa_agent/data/market_defaults.py`
- **影响**：所有美股/欧股/其他字母代码品种（如 AAPL、TSLA、TSM）的 settings 持久化都已修复，刷新页面后能正确回显用户保存的配置

### 4. TradingView auth_token 缓存（避免频繁登录触发风控）
- **问题**：tvDatafeed 2.1.0 在每次 `TvDatafeed(username, password)` 构造时都调用 `__auth` 撞 TradingView 的 `/accounts/signin/`。我们的 `connect()` 在服务器启动、数据源切换、断线重连时都会触发，多次失败后 TradingView 风控升级为 `recaptcha_required`，账号被锁定 12+ 小时无法登录。
- **修复**：`pa_agent/data/tradingview.py` 新增 auth token 双层缓存：
  - 内存 dict `_tv_token_cache: dict[(username, sha256(password)), (token, saved_at)]`
  - 磁盘 JSON `config/.tv_token_cache.json`（进程重启后仍可用）
  - 24h TTL（TV JWT 实际有效期 ~7 天，保守取 24h 避免使用陈旧 token）
  - `_patched_auth` 先查缓存命中直接返回，未命中走网络登录，成功后写入双层缓存；失败（如 `recaptcha_required`）不写入缓存
  - 路径优先级：`PA_AGENT_TV_TOKEN_CACHE` 环境变量 > `config/.tv_token_cache.json`
  - 修复 `Path("") or default` 不工作的问题（`Path("")` 是 truthy，需用显式条件判断）
- **安全**：
  - 密码不落盘，只存 `sha256(password)` 作为 cache key 一部分
  - `config/.gitignore` 排除 `.tv_token_cache.json`（含 JWT，禁止提交）
- **验证**：新增 `tests/unit/test_tv_auth_token_cache.py`，9 项单元测试全部通过 —— 覆盖缓存命中跳过网络、缓存未命中写双层缓存、过期条目驱逐重取、登录失败不缓存、空凭证短路、磁盘往返、模块重载后磁盘缓存恢复等场景
- **文件**：`pa_agent/data/tradingview.py`, `config/.gitignore`, `tests/unit/test_tv_auth_token_cache.py`

### 5. TradingView Session ID 直接登录（绕过 reCAPTCHA）
- **问题**：tvDatafeed 账号密码登录频繁触发 TradingView 的 reCAPTCHA 风控，导致登录失败且账号被锁定 12+ 小时。
- **功能**：新增 Session ID 直接登录方式，用户在浏览器登录 TradingView 后复制 sessionid cookie 即可绕过登录接口，完全避免触发机器人验证。
- **改动**：
  - `TradingViewSettings` 新增 `session_id` 字段，支持三种认证模式（session_id > 账号密码 > 匿名）
  - `get_tv_credentials()` 返回值从 `(username, password)` 改为 `(session_id, username, password)`，优先级：settings > env vars
  - `TradingViewSource.__init__` 和 `connect()` 支持 `session_id` 参数，设置后直接赋值 `self._tv.token = session_id` 跳过登录
  - `factory.create_data_source()` 适配新的三参数返回值
  - 前端设置面板新增「Session ID」输入框及详细使用说明，优先推荐此方式
- **文件**：`pa_agent/config/settings.py`, `pa_agent/config/env_loader.py`, `pa_agent/data/tradingview.py`, `pa_agent/data/factory.py`, `web/static/index.html`, `web/static/js/app.js`

---

## 2026-07-20

### 1. 休市后 K1 序号不显示修复
- **问题**：NVDA 美股收盘后，最后一根 K 线是 close bar，但前端不显示 `#1` 序号
- **根因**：`pa_agent/data/bar_close_wait.py` 的 `seconds_until_bar_closes` 用 `elapsed_ms % duration_ms` 取模算法，对已收盘 bar 仍返回正值（"到下一次开盘的剩余时间"），导致 bar 被错误标记为 `seq=0, closed=False`（forming bar），前端 `setSeqMarkers` 跳过 `seq <= 0` 的 bar
- **修复**：
  - `seconds_until_bar_closes` 在 `now_ms >= ts_open_ms + duration_ms` 时直接返回 0（绝对时间判断）
  - `pa_agent/data/tradingview.py` 的 `_latest_snapshot_inner` 在休市模式下 `bars[0]` 用 `seq=1, closed=True`
  - `pa_agent/data/base.py` 的 `_validate_snapshot` 支持两种模式契约（正常 n+1 / 休市 n）
- **文件**：`pa_agent/data/bar_close_wait.py`, `pa_agent/data/tradingview.py`, `pa_agent/data/base.py`, `tests/property/test_snapshot_bijection.py`
- **提交**：5bff91b

### 2. 交易所/品种切换性能重构
- **问题**：切换交易所（如港交所）报错、响应慢、品种列表重复拉取
- **改动（前端）**：
  - `applySubscribe` 使用 `Promise.allSettled` 并行执行 settings 更新、品种列表、首根 K 线
  - 新增 `_inflightSwitch` 防重入机制
  - 品种列表缓存：`Map<exchange, symbolList>` + 10 分钟 TTL
  - 新增 `showSwitchError` 错误提示组件
  - API 请求带 `AbortController` 15 秒超时
- **改动（后端）**：
  - `web/api/routes_data.py` 同数据源切换时跳过 `connect()` 复用连接
  - 错误响应包含 `error_type: connection|symbol|timeout` 分类
  - 响应头 `Cache-Control: max-age=600`
- **测试**：webapp-testing 19 项 checklist 全部通过，报告保存于 `dogfood-output/switch-refactor`（临时目录，已清理）
- **文件**：`web/static/js/app.js`, `web/api/routes_data.py`

### 3. 主副图时间轴同步修复
- **问题**：添加 MACD/RSI 副图后，滚动/缩放主图时副图时间轴不同步
- **根因**：原同步用逻辑索引（数据点位置），主副图数据点数量不同导致索引错位
- **修复**：`web/static/js/indicators.js` 的 `_syncSubChartTimeScale` 改用时间戳范围同步（`getVisibleRange()` / `setVisibleRange()` / `subscribeVisibleTimeRangeChange`）
- **文件**：`web/static/js/indicators.js`

### 4. tvDatafeed 缺失与 NumPy 兼容性处理
- **问题**：切换港交所等非加密交易所时 `No module named 'tvDatafeed'`；NumPy 报 `X86_V2` CPU 指令集错误
- **修复**：临时修改 `tradingview.py` 与 `app.js` 在无 tvDatafeed 时能运行并显示友好提示；建议通过 `pip install git+https://github.com/rongardF/tvdatafeed.git` 安装，NumPy 降级到 `1.26.4`
- **文件**：`pa_agent/data/tradingview.py`, `web/static/js/app.js`

### 5. 品种搜索改造（代码+中文名称）
- **功能**：后端 `/api/tv/symbols` 返回包含名称和代码的字典，前端改为搜索框 + 下拉列表
- **改动**：
  - `pa_agent/data/tradingview.py` 新增 `TV_SYMBOL_NAMES` 字典映射品类代码到中文名称
  - `web/templates/index.html` 改为搜索框 + 下拉列表结构
  - `web/static/js/app.js` 添加搜索相关函数（实时搜索、双字段匹配、键盘导航、清除按钮、自定义输入）
  - `web/static/css/style.css` 添加搜索框样式
- **文件**：`pa_agent/data/tradingview.py`, `web/templates/index.html`, `web/static/js/app.js`, `web/static/css/style.css`

### 6. 仓库清理与文档对齐
- **清理**：删除工作目录下的临时调试文件（`install_*.py`、`*.whl`、`tvdatafeed.zip`、`dogfood-output/`、`tvdatafeed-main/`、`.tmp_record*.json`）
- **清理**：`records/pending/` 下 11 个旧平铺布局记录（迁移到分区布局后的残留，约 6.9MB）
- **文档对齐**：
  - `README.md` 新增「Web 后端 vs 桌面 GUI 的差异」对比表 + 7 条踩坑记录（坑 7-13）
  - `CONTRIBUTING.md` 重写，新增启动方式、PR 建议、开发者参考约束
  - `TODO.md` 重组阶段 6/7/8 完成记录，阶段 9 待推进规划
  - `docs/图表K线与分析快照说明.md` 重写，新增休市模式章节、双模式对比
  - `docs/获取数据功能说明.md` 重写，新增 SSE 推送、切换性能、错误分类
- **文件**：`README.md`, `CONTRIBUTING.md`, `TODO.md`, `docs/*.md`
- **规范确立**：`AGENTS.md` 新增「维护规范」章节，明确哪些改动需追加记录

---

## 2026-07-19

### 1. 指标移除与品种切换问题修复
- **问题**：指标移除后页面未同步消失，切换品种时仍显示老指标且未重新计算
- **修复**：改用正确的 API 删除序列，切换品种前清空所有指标数据
- **文件**：`web/static/js/app.js`

### 2. 分析预测功能侧边栏改造
- **功能**：将分析预测功能做成可隐藏的侧边栏
- **改动**：分析按钮移入侧边栏头部，分析和取消按钮合并为单按钮状态切换
- **视觉**：通过图标、文本和颜色区分不同状态
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`, `web/templates/index.html`

### 3. K线时间轴显示错误修复
- **问题**：TradingView 返回的时间戳被错误处理为 UTC 时间（实际为 UTC+8）
- **修复**：在 `pa_agent/data/tradingview.py` 的 `_row_ts_ms` 函数中先将 naive Timestamp 本地化到服务器时区再转 UTC
- **文件**：`pa_agent/data/tradingview.py`

### 4. 实时链接断开问题修复
- **问题**：`/api/bars/stream` SSE 端点返回 404
- **修复**：重启服务器加载新代码，确保 SSE 端点正常运行
- **验证**：测试 `/api/settings`、`/api/bars/stream` 端点及相关单元测试

### 5. Web-GUI 功能完整复刻
- **目标**：完整复刻原 GUI 的设计思想和功能
- **改动**：界面 tab 对齐，重新设计 tab 内 UI 布局，使其更专业、易于普通用户理解
- **文件**：`web/templates/index.html`, `web/static/js/app.js`, `web/static/css/style.css`

### 6. SSE 实时刷新功能添加
- **功能**：显示最后一次刷新到当前的过期秒数
- **实现**：`updateSSEStatusWithExpiry()` 每秒定时器，更新状态栏文本
- **格式**："● SSE 实时 · 距上次刷新 Ns · 距下次收盘 Ms"
- **文件**：`web/static/js/app.js`

### 7. 侧边栏宽度可调
- **功能**：侧边栏宽度支持手工拖拽调整
- **修复**：拖拽时实时调用 `resizeChart()` 同步 K线画布尺寸，添加 ResizeObserver 监听
- **文件**：`web/static/js/app.js`（141-175行）

### 8. 决策 Tab 优化
- **字段精简**：移除冗余字段，保留核心信息
- **字段补充**：添加置信度、支撑位、阻力位等缺失字段
- **布局优化**：合理分组、标题、子标题逻辑，支持折叠
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

### 9. 追问功能嵌入实时 Tab
- **功能**：在实时 Tab 中支持追问操作
- **文件**：`web/static/js/app.js`

### 10. 决策树 Tab 拆分
- **功能**：将决策树内容拆分为独立 Tab，便于查看
- **文件**：`web/static/js/app.js`, `web/templates/index.html`

### 11. 原始 Tab 补齐功能
- **功能**：补齐原始 Tab 的缺失功能
- **文件**：`web/static/js/app.js`

### 12. 未来 Tab 添加程序补全前缀
- **功能**：为未来 Tab 的字段添加程序补全前缀
- **文件**：`web/static/js/app.js`

### 13. 调试 Tab 增加内置 JSON 说明提示
- **功能**：在调试 Tab 中添加 JSON 结构说明提示
- **文件**：`web/static/js/app.js`

### 14. 决策 Tab 长文字截断修复
- **问题**：长文字字段被截断，无法完整阅读
- **修复**：修改 `.field-grid .field-val` 样式，移除 `max-width`、`overflow`、`text-overflow`、`white-space` 属性，允许换行
- **文件**：`web/static/css/style.css`

### 15. 所有 Tab 的 Tips/Help 重新设计
- **格式**：统一四段式（是什么 → 结构 → 视觉/操作 → 使用建议）
- **优化**：tab-help 视觉效果提升
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

### 16. SSE 倒计时显示异常修复
- **问题**：选择 1 小时周期时，持续追踪倒计时显示 10000+ 秒
- **根因**：`onopen` handler 立即设置 `sseLastBarUpdateTs`，但此时 `sseNextCloseTs` 尚未被第一个 `bar_update` 事件更新；切换周期时保留旧值
- **修复**：
  - `onopen` 不再设置 `sseLastBarUpdateTs`，仅启动定时器
  - 新增 `timeframeToSeconds()` 辅助函数
  - 在 `updateSSEStatusWithExpiry()` 中添加 sanity check（若 `remaining > tfSecs` 则丢弃值）
- **提交**：2758af9
- **文件**：`web/static/js/app.js`

### 17. 分析失败后进度条错误修复
- **问题**：阶段一分析失败后，进度条直接跳到完成状态
- **根因**：后端在分析失败时仍推送 `done` 事件，前端无条件调用 `setFlowBarStep(6)`
- **修复**：
  - 新增 `.flow-step.failed` CSS 类
  - 新增 `setFlowBarFailed(failedStep)` 函数
  - `done` 事件检查 `evt.record.exception`，有异常时不推进到完成状态，保留失败状态 10 秒
- **提交**：43bf751
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

### 18. 切换周期后 SSE 倒计时不显示修复
- **问题**：切换周期后 SSE 倒计时不显示
- **根因**：`applySubscribe` 切换周期后未刷新 `currentSettings`，导致 sanity check 使用旧的 `tfSecs`
- **修复**：`tfSecs` 优先从 `#ds-timeframe` 读取用户实际选择的值；`applySubscribe` 成功后调用 `loadSettings` 刷新
- **提交**：3a3b10e
- **文件**：`web/static/js/app.js`

### 19. 持续跟踪倒计时（轮询模式）修复
- **问题**：SSE 连接失败后切换到降级轮询模式，没有倒计时显示
- **修复**：
  - 新增 `fetchAndUpdateNextCloseTs()` 函数，通过低频（5秒）拉取 `/api/bars/next-close` 更新 `sseNextCloseTs`
  - 新增 `startNextClosePolling()` 和 `stopNextClosePolling()` 函数
  - 修改 `updateLiveRefreshStatus()` 在轮询模式下也显示倒计时
- **文件**：`web/static/js/app.js`

### 20. 上游项目同步
- **操作**：fork 上游项目最新改动并合并到本地 main 分支
- **冲突处理**：修复 `factory.py` 注释行冲突
- **改进**：`start_pa_agent.bat` 改用 `%~dp0` 相对路径，默认启动 Web 后端
- **提交**：d4d611f（Merge upstream/main）, eb519ba（start_pa_agent.bat 改用相对路径）

### 21. 文档同步更新
- **文件**：`README.md`, `TODO.md`, `PA_Agent使用文档.md`, `智能体部署方法.txt`
- **内容**：反映 Web GUI 变化，更新配置说明（三层配置覆盖：shell env > .env > settings.json）
