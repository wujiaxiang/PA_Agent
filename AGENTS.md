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
