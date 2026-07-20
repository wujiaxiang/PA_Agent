# PA_AGENT 项目改动记录

## 项目概述

PA_AGENT 是一个基于 AI 的量化分析工具，提供实时行情数据、智能分析预测、决策树可视化等功能。本文件记录项目的主要改动和迭代历史。

---

## 改动记录

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

- **模型 API 连接失败**：本地模型 API 服务器 `192.168.2.177:8082` 未运行，导致分析失败
- **Demo 模式缺失**：Web UI 缺乏桌面 GUI 有的 demo 模式
- **resendLastChat 死代码**：函数存在但无 UI 按钮绑定
- **经验库系统数据为空**：`experience/` 目录无数据文件

---

## 后续迭代需求

1. **经验库系统完善**：添加经验数据文件，实现经验库检索和应用功能
2. **Demo 模式**：实现 Web UI 的 demo 模式，便于用户体验
3. **resendLastChat 功能**：添加 UI 按钮绑定，实现重新发送最后一条消息功能
4. **移动端适配**：优化移动端显示效果
5. **性能优化**：优化页面加载速度和渲染性能
6. **国际化支持**：添加多语言支持
