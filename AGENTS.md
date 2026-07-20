# PA_AGENT 项目改动记录

## 项目概述

PA_AGENT 是一个基于 AI 的量化分析工具，提供实时行情数据、智能分析预测、决策树可视化等功能。本文件记录项目的主要改动和迭代历史。

---

## 维护规范（重要）

本文件是项目改动的「日志」，凡属于以下情况必须更新本文件并在对应日期节下追加条目：

1. **大迭代 / 阶段性交付**：如 Web GUI 阶段、决策 tab 重设计、切换性能重构等
2. **架构性重构**：如 SSE 后台循环改造、DataSource 契约修订、配置系统重构等
3. **关键 bug 修复**：影响可用性或正确性的修复（如休市 K1 序号、时间轴偏移、SSE 倒计时异常等）
4. **新增 / 删除模块**：新增路由、新增前端组件、删除死代码等
5. **文档大范围对齐**：如 README / TODO / CONTRIBUTING / docs 的一次性更新

**不需要更新**的情况（写 commit message 即可）：
- 单文件小修、文案微调、注释补充
- 单测用例的新增 / 调整（除非涉及新模块）
- 临时调试产物的清理

### 追加格式

```markdown
### YYYY-MM-DD

#### N. 简短标题
- **问题** / **功能** / **目标**：一句话描述动机
- **根因**（仅 bug 修复）：原代码哪里错了
- **修复** / **改动**：做了什么
- **文件**：`path/to/file.py`
- **提交**（可选）：commit hash
```

### 维护节奏

- 每次大改动当天必须追加，不要积累到下次
- 每月底可以整体回顾一次，把零散条目归并、提炼「核心概念」章节
- 与 [TODO.md](TODO.md) 互补：TODO 是「待办」，AGENTS 是「已办」

---

## 改动记录

### 2026-07-21

#### 1. TradingView 凭证 UI 配置化 + 错误消息按交易所动态化
- **问题**：用户反馈 NVDA/NASDAQ 1d 分析记录没有保存。日志根因是 TradingView 匿名访问美股被限流（`Connection to remote host was lost`），`latest_snapshot` 在分析流程第一步就抛 `DataSourceTransientError`，走不到 `save_full`。同时 `format_tradingview_fetch_error` 的 fallback 分支硬编码"现货黄金请用 OANDA + XAUUSD"，对 NVDA/BTCUSDT 等任何空数据场景都显示这条误导提示。
- **修复**：
  - `Settings` 新增 `TradingViewSettings` 节（`username` / `password`），持久化到 `config/settings.json`
  - `env_loader.get_tv_credentials(settings)` 改为优先读 settings，env vars 作为 fallback（保留无 UI 的服务器部署能力）
  - `factory.create_data_source('tradingview')` 内部读 `SETTINGS_JSON_PATH` 传给 `get_tv_credentials`
  - `routes_settings.put_settings` 的 section 白名单加入 `"tradingview"`
  - 前端 AI 服务 tab 新增「TradingView 凭证」字段集（fieldset），`loadSettings` 回填 + `saveSettingsHandler` 提交
  - `tradingview_errors.py` 新增 `_US_EQUITY_EXCHANGES = {NASDAQ, NYSE, AMEX, SIX, TSX, LSE}`，对美股空数据/超时给出"匿名访问常被限流，请配置凭证"针对性提示；通用 fallback 改为按 `ex` 分类（TVC/CAPITALCOM 给出黄金提示），不再把黄金提示强加给所有失败场景
- **文件**：`pa_agent/config/settings.py`, `pa_agent/config/env_loader.py`, `pa_agent/data/factory.py`, `pa_agent/data/tradingview_errors.py`, `web/api/routes_settings.py`, `web/static/index.html`, `web/static/js/app.js`
- **约束沉淀**：TV 凭证优先级 `settings.json > env > 匿名`；交易所/品种/周期已由 `/api/subscribe` 自动持久化（`routes_data.py:182-190`），无需额外 UI 处理

#### 2. tvDatafeed 2.1.0 登录 monkey-patch + alert→toast
- **问题**：配置 TradingView 凭证后，tvDatafeed 仍报 `error while signin` 回退到匿名模式。根因：tvDatafeed 2.1.0 的 `__auth` 用裸 `requests.post` 调 `/accounts/signin/`，没带 session、没设 User-Agent、没先 GET 首页种 cookies，TradingView 直接返回 `{"error": "...", "code": "rate_limit"}`。同时保存设置时 `alert('设置已 saved')` 同步弹窗会触发 TRAE IDE 内置 webview 的 React error #185 渲染崩溃。
- **修复**：
  - `pa_agent/data/tradingview.py` 新增 `_patch_tvdatafeed_auth()`，monkey-patch `TvDatafeed._TvDatafeed__auth`：用 `requests.Session()` + 浏览器 UA + 先 GET 首页种 cookies 再 POST 登录。失败时 fallback 到原实现。实测能拿到 846 字符 JWT auth_token，NVDA/NASDAQ/1d 成功拉到 100 根 K 线。
  - `web/static/js/app.js` 的 `showToast(message)` 扩展为 `showToast(message, type)`，支持 `success/warning/error` 三种背景色；3 处 `alert()` 全部替换为非阻塞 toast，error 类停留 4s，其余 2s
- **验证**：账号 `laok259@gmail.com` 已成功登录（`anonymous=False`），NVDA/NASDAQ/1d 拉到 101 根 K 线（含 forming bar），`/api/subscribe` 返回 `status=subscribed`
- **文件**：`pa_agent/data/tradingview.py`, `web/static/js/app.js`
- **约束沉淀**：tvDatafeed 2.1.0 库自身的 `__auth` 已失效（无 session/UA），必须 monkey-patch；monkey-patch 风格与已有 `_patch_tvdatafeed_ws_headers` 保持一致，失败有 fallback

#### 3. 刷新页面后交易所/品种/周期回显错误（migrate_general_gold_defaults 误迁移）
- **问题**：用户在 UI 选 NVDA/NASDAQ/1d → 点应用 → `/api/subscribe` 写入 settings.json → 刷新浏览器 → 回显变成 XAUUSD/OANDA/1h，不是刚保存的 NVDA/NASDAQ/1d
- **根因**：`load_settings()` 每次 `GET /api/settings` 时都会调 `migrate_general_gold_defaults(general)` → `resolve_tv_pair("NASDAQ", "NVDA")`。`resolve_tv_pair` 的 6 步分类（公司名/指数/A股/港股/crypto/gold fallback）都没命中 NVDA，最终 fallthrough 到 `resolve_tv_gold_pair`。该函数的最终 fallback 分支 `return GOLD_TV_EXCHANGE, GOLD_TV_SYMBOL, ...` 把任何"非黄金交易所 + 非黄金 symbol"组合强制改成 OANDA/XAUUSD，导致 NVDA/NASDAQ 被误迁移。这是迁移逻辑设计时的过度兜底——把"不认识的 symbol"全当黄金处理。
- **修复**：`pa_agent/data/market_defaults.py` 的 `resolve_tv_gold_pair` 在最终 fallback 前增加判断：若 exchange 非空非 auto、且 symbol 不是黄金关键词（XAUUSD/GOLD/XAU），直接返回 `(ex, sym, False)` 信任用户选择。仅当 symbol 确实是黄金关键词（即使交易所不对）才路由到默认黄金 feed。
- **验证**：8 组用例全部正确 —— NASDAQ/NVDA 保持原值；NYSE/AAPL 保持原值；OANDA/XAUUSD、TVC/GOLD 保持原值；TVC/XAUUSD 修正为 TVC/GOLD；NASDAQ/XAUUSD 异常组合修正为 OANDA/XAUUSD；空值给默认 XAUUSD。settings.json 和 `/api/settings` 返回值现在完全一致
- **文件**：`pa_agent/data/market_defaults.py`
- **影响**：所有美股/欧股/其他字母代码品种（如 AAPL、TSLA、TSM）的 settings 持久化都已修复，刷新页面后能正确回显用户保存的配置

#### 4. TradingView auth_token 缓存（避免频繁登录触发风控）
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
- **约束沉淀**：auth token 缓存 TTL=24h；key=(username, sha256(password))；只缓存登录成功（含 `user.auth_token`）的响应，失败响应不污染缓存



### 2026-07-20

#### 1. 休市后 K1 序号不显示修复
- **问题**：NVDA 美股收盘后，最后一根 K 线是 close bar，但前端不显示 `#1` 序号
- **根因**：`pa_agent/data/bar_close_wait.py` 的 `seconds_until_bar_closes` 用 `elapsed_ms % duration_ms` 取模算法，对已收盘 bar 仍返回正值（"到下一次开盘的剩余时间"），导致 bar 被错误标记为 `seq=0, closed=False`（forming bar），前端 `setSeqMarkers` 跳过 `seq <= 0` 的 bar
- **修复**：
  - `seconds_until_bar_closes` 在 `now_ms >= ts_open_ms + duration_ms` 时直接返回 0（绝对时间判断）
  - `pa_agent/data/tradingview.py` 的 `_latest_snapshot_inner` 在休市模式下 `bars[0]` 用 `seq=1, closed=True`
  - `pa_agent/data/base.py` 的 `_validate_snapshot` 支持两种模式契约（正常 n+1 / 休市 n）
- **文件**：`pa_agent/data/bar_close_wait.py`, `pa_agent/data/tradingview.py`, `pa_agent/data/base.py`, `tests/property/test_snapshot_bijection.py`
- **提交**：5bff91b

#### 2. 交易所/品种切换性能重构
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
- **约束沉淀**：见 project_memory 的「Hard Constraints」与「Engineering Conventions」

#### 3. 主副图时间轴同步修复
- **问题**：添加 MACD/RSI 副图后，滚动/缩放主图时副图时间轴不同步
- **根因**：原同步用逻辑索引（数据点位置），主副图数据点数量不同导致索引错位
- **修复**：`web/static/js/indicators.js` 的 `_syncSubChartTimeScale` 改用时间戳范围同步（`getVisibleRange()` / `setVisibleRange()` / `subscribeVisibleTimeRangeChange`）
- **文件**：`web/static/js/indicators.js`

#### 4. tvDatafeed 缺失与 NumPy 兼容性处理
- **问题**：切换港交所等非加密交易所时 `No module named 'tvDatafeed'`；NumPy 报 `X86_V2` CPU 指令集错误
- **修复**：临时修改 `tradingview.py` 与 `app.js` 在无 tvDatafeed 时能运行并显示友好提示；建议通过 `pip install git+https://github.com/rongardF/tvdatafeed.git` 安装，NumPy 降级到 `1.26.4`
- **文件**：`pa_agent/data/tradingview.py`, `web/static/js/app.js`

#### 5. 品种搜索改造（代码+中文名称）
- **功能**：后端 `/api/tv/symbols` 返回包含名称和代码的字典，前端改为搜索框 + 下拉列表
- **改动**：
  - `pa_agent/data/tradingview.py` 新增 `TV_SYMBOL_NAMES` 字典映射品类代码到中文名称
  - `web/templates/index.html` 改为搜索框 + 下拉列表结构
  - `web/static/js/app.js` 添加搜索相关函数（实时搜索、双字段匹配、键盘导航、清除按钮、自定义输入）
  - `web/static/css/style.css` 添加搜索框样式
- **文件**：`pa_agent/data/tradingview.py`, `web/templates/index.html`, `web/static/js/app.js`, `web/static/css/style.css`

#### 6. 仓库清理与文档对齐
- **清理**：删除工作目录下的临时调试文件（`install_*.py`、`*.whl`、`tvdatafeed.zip`、`dogfood-output/`、`tvdatafeed-main/`、`.tmp_record*.json`）
- **清理**：`records/pending/` 下 11 个旧平铺布局记录（迁移到分区布局后的残留，约 6.9MB）
- **文档对齐**：
  - `README.md` 新增「Web 后端 vs 桌面 GUI 的差异」对比表 + 7 条踩坑记录（坑 7-13）
  - `CONTRIBUTING.md` 重写，新增启动方式、PR 建议、开发者参考约束
  - `TODO.md` 重组阶段 6/7/8 完成记录，阶段 9 待推进规划
  - `docs/图表K线与分析快照说明.md` 重写，新增休市模式章节、双模式对比
  - `docs/获取数据功能说明.md` 重写，新增 SSE 推送、切换性能、错误分类
- **文件**：`README.md`, `CONTRIBUTING.md`, `TODO.md`, `docs/*.md`
- **规范确立**：本文件（AGENTS.md）新增「维护规范」章节，明确哪些改动需追加记录

---

### 2026-07-19

#### 1. 指标移除与品种切换问题修复
- **问题**：指标移除后页面未同步消失，切换品种时仍显示老指标且未重新计算
- **修复**：改用正确的 API 删除序列，切换品种前清空所有指标数据
- **文件**：`web/static/js/app.js`

#### 2. 分析预测功能侧边栏改造
- **功能**：将分析预测功能做成可隐藏的侧边栏
- **改动**：分析按钮移入侧边栏头部，分析和取消按钮合并为单按钮状态切换
- **视觉**：通过图标、文本和颜色区分不同状态
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`, `web/templates/index.html`

#### 3. K线时间轴显示错误修复
- **问题**：TradingView 返回的时间戳被错误处理为 UTC 时间（实际为 UTC+8）
- **修复**：在 `pa_agent/data/tradingview.py` 的 `_row_ts_ms` 函数中先将 naive Timestamp 本地化到服务器时区再转 UTC
- **文件**：`pa_agent/data/tradingview.py`

#### 4. 实时链接断开问题修复
- **问题**：`/api/bars/stream` SSE 端点返回 404
- **修复**：重启服务器加载新代码，确保 SSE 端点正常运行
- **验证**：测试 `/api/settings`、`/api/bars/stream` 端点及相关单元测试

#### 5. Web-GUI 功能完整复刻
- **目标**：完整复刻原 GUI 的设计思想和功能
- **改动**：界面 tab 对齐，重新设计 tab 内 UI 布局，使其更专业、易于普通用户理解
- **文件**：`web/templates/index.html`, `web/static/js/app.js`, `web/static/css/style.css`

#### 6. SSE 实时刷新功能添加
- **功能**：显示最后一次刷新到当前的过期秒数
- **实现**：`updateSSEStatusWithExpiry()` 每秒定时器，更新状态栏文本
- **格式**："● SSE 实时 · 距上次刷新 Ns · 距下次收盘 Ms"
- **文件**：`web/static/js/app.js`

#### 7. 侧边栏宽度可调
- **功能**：侧边栏宽度支持手工拖拽调整
- **修复**：拖拽时实时调用 `resizeChart()` 同步 K线画布尺寸，添加 ResizeObserver 监听
- **文件**：`web/static/js/app.js`（141-175行）

#### 8. 决策 Tab 优化
- **字段精简**：移除冗余字段，保留核心信息
- **字段补充**：添加置信度、支撑位、阻力位等缺失字段
- **布局优化**：合理分组、标题、子标题逻辑，支持折叠
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

#### 9. 追问功能嵌入实时 Tab
- **功能**：在实时 Tab 中支持追问操作
- **文件**：`web/static/js/app.js`

#### 10. 决策树 Tab 拆分
- **功能**：将决策树内容拆分为独立 Tab，便于查看
- **文件**：`web/static/js/app.js`, `web/templates/index.html`

#### 11. 原始 Tab 补齐功能
- **功能**：补齐原始 Tab 的缺失功能
- **文件**：`web/static/js/app.js`

#### 12. 未来 Tab 添加程序补全前缀
- **功能**：为未来 Tab 的字段添加程序补全前缀
- **文件**：`web/static/js/app.js`

#### 13. 调试 Tab 增加内置 JSON 说明提示
- **功能**：在调试 Tab 中添加 JSON 结构说明提示
- **文件**：`web/static/js/app.js`

#### 14. 决策 Tab 长文字截断修复
- **问题**：长文字字段被截断，无法完整阅读
- **修复**：修改 `.field-grid .field-val` 样式，移除 `max-width`、`overflow`、`text-overflow`、`white-space` 属性，允许换行
- **文件**：`web/static/css/style.css`

#### 15. 所有 Tab 的 Tips/Help 重新设计
- **格式**：统一四段式（是什么 → 结构 → 视觉/操作 → 使用建议）
- **优化**：tab-help 视觉效果提升
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

#### 16. SSE 倒计时显示异常修复
- **问题**：选择 1 小时周期时，持续追踪倒计时显示 10000+ 秒
- **根因**：`onopen` handler 立即设置 `sseLastBarUpdateTs`，但此时 `sseNextCloseTs` 尚未被第一个 `bar_update` 事件更新；切换周期时保留旧值
- **修复**：
  - `onopen` 不再设置 `sseLastBarUpdateTs`，仅启动定时器
  - 新增 `timeframeToSeconds()` 辅助函数
  - 在 `updateSSEStatusWithExpiry()` 中添加 sanity check（若 `remaining > tfSecs` 则丢弃值）
- **提交**：2758af9
- **文件**：`web/static/js/app.js`

#### 17. 分析失败后进度条错误修复
- **问题**：阶段一分析失败后，进度条直接跳到完成状态
- **根因**：后端在分析失败时仍推送 `done` 事件，前端无条件调用 `setFlowBarStep(6)`
- **修复**：
  - 新增 `.flow-step.failed` CSS 类
  - 新增 `setFlowBarFailed(failedStep)` 函数
  - `done` 事件检查 `evt.record.exception`，有异常时不推进到完成状态，保留失败状态 10 秒
- **提交**：43bf751
- **文件**：`web/static/js/app.js`, `web/static/css/style.css`

#### 18. 切换周期后 SSE 倒计时不显示修复
- **问题**：切换周期后 SSE 倒计时不显示
- **根因**：`applySubscribe` 切换周期后未刷新 `currentSettings`，导致 sanity check 使用旧的 `tfSecs`
- **修复**：`tfSecs` 优先从 `#ds-timeframe` 读取用户实际选择的值；`applySubscribe` 成功后调用 `loadSettings` 刷新
- **提交**：3a3b10e
- **文件**：`web/static/js/app.js`

#### 19. 持续跟踪倒计时（轮询模式）修复
- **问题**：SSE 连接失败后切换到降级轮询模式，没有倒计时显示
- **修复**：
  - 新增 `fetchAndUpdateNextCloseTs()` 函数，通过低频（5秒）拉取 `/api/bars/next-close` 更新 `sseNextCloseTs`
  - 新增 `startNextClosePolling()` 和 `stopNextClosePolling()` 函数
  - 修改 `updateLiveRefreshStatus()` 在轮询模式下也显示倒计时
- **文件**：`web/static/js/app.js`

#### 20. 上游项目同步
- **操作**：fork 上游项目最新改动并合并到本地 main 分支
- **冲突处理**：修复 `factory.py` 注释行冲突
- **改进**：`start_pa_agent.bat` 改用 `%~dp0` 相对路径，默认启动 Web 后端
- **提交**：d4d611f（Merge upstream/main）, eb519ba（start_pa_agent.bat 改用相对路径）

#### 21. 文档同步更新
- **文件**：`README.md`, `TODO.md`, `PA_Agent使用文档.md`, `智能体部署方法.txt`
- **内容**：反映 Web GUI 变化，更新配置说明（三层配置覆盖：shell env > .env > settings.json）

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
- **触发**：手动点击「增量」按钮或「持续跟踪」在 bar_close 事件触发
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

---

## 维护提醒

- 新增大迭代 / 架构重构 / 关键 bug 修复时，按「维护规范」追加条目到本文件顶部对应日期节
- 同步更新 [TODO.md](TODO.md) 的「阶段完成记录」与「后续可推进」
- 若改动涉及踩坑，同步追加到 [README.md](README.md) 的「已知坑与修复记录」
- 若改动涉及开发者约束，同步更新 [CONTRIBUTING.md](CONTRIBUTING.md) 的「开发者参考」
