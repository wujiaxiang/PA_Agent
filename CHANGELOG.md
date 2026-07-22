# 变更日志 (CHANGELOG)

本文件按时间倒序记录项目的主要改动与迭代历史。规范的约束、技术概念、已知问题等请见 [AGENTS.md](AGENTS.md)。

---

## 2026-07-22

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
