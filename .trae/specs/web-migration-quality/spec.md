# Web 端迁移质量保障 Spec

## Why

本项目从 PyQt6 桌面应用二次开发为 Web 端应用，目前 Web 端仅搭通了最小可用骨架（AppContext → orchestrator → SSE），但存在关键 bug（决策字段映射错误导致决策卡片几乎全空）、核心可视化缺失（K 线叠加层、决策树、未来走势预期等全部未实现）、无法自测（无 Web 端自动化测试）。需要系统性修复并补齐核心功能，使 Web 端能在云端服务器跑起来调试，最终替代 PyQt6 壳子。

## What Changes

### P0 — 关键 Bug 修复与基础可用性
- **修复决策字段映射 bug**：`web/static/js/app.js` 中 `renderDecision` 读取 `d.stop_loss` / `d.take_profit` / `d.confidence`，但 orchestrator 实际输出 `stop_loss_price` / `take_profit_price` / `take_profit_price_2` / `estimated_win_rate` / `trade_confidence`，导致决策卡片除 order_type/direction 外几乎全部显示为空。
- **添加取消分析按钮**：前端 `api.js` 已声明 `AbortController` 但未绑定任何 UI 元素，用户无法取消进行中的分析。
- **补齐 SSE 事件处理**：前端未处理 `Stage1Retry` / `Stage2Retry` / `Failed` / `InsufficientData` 事件，用户看不到重试和失败原因。

### P1 — 核心可视化迁移
- **K 线图叠加层**：迁移 PyQt6 `chart_widget.py` 的 EMA20 叠加线、入场/TP1/TP2/SL 横线、支撑/阻力位横线+区间填充、方向箭头、forming bar 半透明处理、序号标签、fit_view 自动适配。
- **Token 进度条**：迁移 PyQt6 的 QProgressBar，实现百分比进度条 + 黄/红阈值样式 + context_used/context_window 展示。
- **置信度展示**：迁移市场判断置信度条、交易决策置信度、盈亏比、预估胜率、trader equation 通过/不通过状态。
- **未来走势预期面板**：迁移 `future_trend_panel.py` 的下一根 K 线预期（direction + 概率 + reasoning）和下一个市场周期预期（top3 chip + reasoning）。
- **Stage1/Stage2 prompt 全文展示**：后端 SSE 当前只发 stage 标签，需传递 system/user prompt 内容，前端展示。

### P2 — 交互增强
- **实时数据刷新**：实现后台轮询机制（Web 端用 setInterval + `/api/bars` 轮询替代 Qt RefreshLoop），支持暂停/恢复。
- **决策树路径回放表格**：迁移 `decision_tree_panel.py` 的 6 列表格（步/阶段/节点/回答/K线依据/理由），Web 端用 HTML table 实现（不做科幻流程图动画）。
- **飞书通知设置**：迁移 `feishu_settings_dialog.py` 到 Web 设置 modal。
- **会话缓存清理**：`routes_chat._chat_sessions` 无销毁机制，添加 TTL 或分析完成后清理。

### 测试保障
- **Web API 单元测试**：为 `routes_analyze` / `routes_chat` / `routes_data` / `routes_settings` 添加 pytest 测试。
- **前端字段映射测试**：测试 `renderDecision` 能正确解析 orchestrator 输出的所有字段。
- **SSE 事件流测试**：测试 SSE 事件解析逻辑能处理全部事件类型（含 Retry/Failed/InsufficientData）。
- **冒烟测试**：添加 Web 端 e2e 冒烟测试（mock AI 响应，验证完整流程）。

### 环境清理
- **PyQt6 运行时依赖**：在 Web 端运行模式下不再需要 PyQt6/pyqtgraph，但 `pa_agent/gui/` 代码保留不删除（桌面端仍可独立使用）。Web 端启动时不导入任何 `pa_agent.gui` 模块。

## Impact

- **Affected code**:
  - `web/static/js/app.js` — 决策渲染、SSE 事件处理、取消按钮
  - `web/static/js/chart.js` — K 线叠加层
  - `web/static/js/api.js` — SSE 解析
  - `web/static/index.html` — UI 结构
  - `web/static/css/style.css` — 样式
  - `web/api/routes_analyze.py` — SSE 事件补充、prompt 传递
  - `web/api/routes_chat.py` — 会话缓存清理
  - `web/api/routes_settings.py` — 飞书设置
  - `web/server.py` — 启动流程
  - `tests/` — 新增 Web 端测试

- **NOT affected**:
  - `pa_agent/gui/` — 保留不动，桌面端代码独立
  - `pa_agent/orchestrator/` — 核心编排逻辑不动
  - `pa_agent/ai/` — AI 客户端不动
  - `pa_agent/data/` — 数据源不动

## ADDED Requirements

### Requirement: 决策卡片完整字段映射
Web 端 `renderDecision` 函数 SHALL 正确解析 orchestrator 输出的全部决策字段，包括 `order_type`、`direction`、`entry_price`、`stop_loss_price`、`take_profit_price`、`take_profit_price_2`、`estimated_win_rate`、`trade_confidence`、`passes_trader_equation`、`risk_reward_ratio`、`reasoning`。

#### Scenario: 完整决策展示
- **WHEN** orchestrator 返回包含 `order_type=limit`、`direction=long`、`entry_price=2650.0`、`stop_loss_price=2640.0`、`take_profit_price=2670.0`、`estimated_win_rate=0.65`、`trade_confidence=0.7` 的决策
- **THEN** Web 端决策卡片应展示全部价格字段、置信度条、盈亏比、胜率，且数值正确

#### Scenario: 不下单决策展示
- **WHEN** orchestrator 返回 `order_type=no_order`
- **THEN** Web 端应显示"本轮不下单"状态及理由，隐藏价格字段

### Requirement: K 线图叠加层
Web 端 K 线图 SHALL 支持 EMA20 叠加线、入场/TP/SL 横线、支撑/阻力位、方向箭头等可视化元素，与 PyQt6 `chart_widget.py` 功能对齐。

#### Scenario: 决策价格线叠加
- **WHEN** 分析完成后决策包含 `entry_price` 和 `stop_loss_price`
- **THEN** K 线图上应绘制入场价（实线）和止损价（虚线），颜色区分多空方向

#### Scenario: EMA20 叠加
- **WHEN** K 线数据加载完成
- **THEN** 图表应叠加 EMA20 均线

### Requirement: Token 进度条
Web 端 SHALL 展示 AI 分析的 Token 使用进度条，包括 context_used 百分比、黄/红阈值警告。

#### Scenario: 上下文占用预警
- **WHEN** 分析过程中 context_used 占用超过 context_window 的 80%
- **THEN** 进度条应变红色并显示警告提示

### Requirement: 取消分析功能
Web 端 SHALL 提供取消进行中分析的按钮，点击后通过 `AbortController` 中止 SSE 连接。

#### Scenario: 用户取消分析
- **WHEN** 用户在分析过程中点击"取消"按钮
- **THEN** SSE 连接断开，UI 恢复到可发起新分析的状态

### Requirement: SSE 重试事件处理
Web 端 SHALL 处理 `Stage1Retry` / `Stage2Retry` / `Failed` / `InsufficientData` 事件，向用户展示重试次数和失败原因。

#### Scenario: Stage1 校验失败重试
- **WHEN** SSE 推送 `Stage1Retry` 事件
- **THEN** 前端应显示"阶段一第 N 次重试"提示及失败原因

### Requirement: 未来走势预期面板
Web 端 SHALL 展示下一根 K 线方向预期（含概率和理由）和下一个市场周期位置预期。

#### Scenario: 下一根 K 线预测展示
- **WHEN** 分析结果包含 `next_bar_prediction` 字段
- **THEN** 面板应显示预测方向（多/空/不确定）+ 概率 + 理由

### Requirement: Web 端自动化测试
Web 端 SHALL 包含 API 层单元测试和前端逻辑测试，覆盖决策字段映射、SSE 事件解析、核心 API 路由。

#### Scenario: 字段映射测试
- **WHEN** 运行 `pytest tests/unit/test_web_decision_mapping.py`
- **THEN** 应验证 `renderDecision` 逻辑能正确解析所有 orchestrator 输出字段

## MODIFIED Requirements

### Requirement: SSE 事件流
原有 SSE 仅传递 5 类事件（orchestrator_event / reasoning_token / content_token / done / error），现需扩展传递 stage_prompt 全文（system/user prompt 内容）、retry 事件、failed 事件、insufficient_data 事件。

### Requirement: 设置接口
原有设置接口仅含 AI 服务和通用设置两个 tab，现需添加飞书通知设置 tab，包含 webhook_url / secret / app_id / app_secret / notify_on_order_only 字段。
