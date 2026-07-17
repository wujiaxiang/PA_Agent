# Tasks

## 阶段一：关键 Bug 修复（P0）

- [x] Task 1: 修复决策卡片字段映射 bug
  - [x] SubTask 1.1: 审查 `pa_agent/orchestrator/two_stage.py` 和 `pa_agent/ai/stage2_normalizer.py` 确认决策 JSON 完整字段名（`stop_loss_price` / `take_profit_price` / `take_profit_price_2` / `estimated_win_rate` / `trade_confidence` / `passes_trader_equation` / `risk_reward_ratio`）
  - [x] SubTask 1.2: 修复 `web/static/js/app.js` 中 `renderDecision` 函数，对齐全部字段名
  - [x] SubTask 1.3: 添加 `no_order` 决策的展示分支（隐藏价格字段，显示理由）
  - [ ] SubTask 1.4: 编写 `tests/unit/test_web_decision_mapping.py` 验证字段映射正确性（移至阶段四统一处理）

- [x] Task 2: 添加取消分析按钮
  - [x] SubTask 2.1: 在 `web/static/index.html` 分析面板添加"取消"按钮元素
  - [x] SubTask 2.2: 在 `web/static/js/app.js` 中绑定 AbortController 到取消按钮，点击时 abort SSE 连接并恢复 UI 状态
  - [x] SubTask 2.3: 在 `web/static/css/style.css` 添加取消按钮样式

- [x] Task 3: 补齐 SSE 重试/失败事件处理
  - [x] SubTask 3.1: 在 `web/api/routes_analyze.py` 中确认 SSE 传递了 Retry / Failed / InsufficientData 事件（若无则补充）
  - [x] SubTask 3.2: 在 `web/static/js/app.js` 的 SSE 事件处理逻辑中添加 Retry / Failed / InsufficientData 分支，展示重试次数和失败原因
  - [x] SubTask 3.3: 后端 `on_prompt` 修复为传递 system/user 全文，前端添加 stage_prompt 展示
  - [ ] SubTask 3.4: 编写 `tests/unit/test_web_sse_events.py` 验证 SSE 事件解析逻辑（移至阶段四统一处理）

## 阶段二：核心可视化迁移（P1）

- [ ] Task 4: K 线图叠加层 — 价格线与 EMA
  - [ ] SubTask 4.1: 在 `web/static/js/chart.js` 添加 EMA20 计算与叠加线渲染
  - [ ] SubTask 4.2: 添加入场价 / 止损价 / TP1 / TP2 横线绘制（实线/虚线区分），颜色按多空方向区分
  - [ ] SubTask 4.3: 添加 forming bar（未收盘 K 线）半透明处理
  - [ ] SubTask 4.4: 添加 fit_view 自动适配功能（最近 20 根 + Y 轴 padding）
  - [ ] SubTask 4.5: 在 `web/api/routes_analyze.py` 分析完成后通过 SSE 推送决策价格到前端触发叠加层更新

- [ ] Task 5: K 线图叠加层 — 支撑阻力位与方向箭头
  - [ ] SubTask 5.1: 从 `pa_agent/gui/support_resistance.py` 移植支撑/阻力位提取逻辑到 web 端（或在后端 API 返回）
  - [ ] SubTask 5.2: 在 `chart.js` 添加支撑/阻力位横线 + 区间填充渲染
  - [ ] SubTask 5.3: 添加方向箭头 marker（在最新 bar 对应 entry_price 位置绘制 ▲/▼）
  - [ ] SubTask 5.4: 添加序号标签（奇数 seq 显示 #1, #3...）

- [ ] Task 6: Token 进度条与置信度展示
  - [ ] SubTask 6.1: 在 `web/static/index.html` 添加 Token 进度条 UI 元素
  - [ ] SubTask 6.2: 在 `app.js` 解析 SSE token_update 事件，更新进度条百分比（context_used / context_window）
  - [ ] SubTask 6.3: 添加黄/红阈值样式（>=80% 黄色，>=95% 红色）
  - [ ] SubTask 6.4: 在决策卡片添加市场判断置信度条 + 交易决策置信度 + 盈亏比 + 预估胜率 + trader equation 状态展示

- [ ] Task 7: 未来走势预期面板
  - [ ] SubTask 7.1: 在 `web/static/index.html` 添加未来走势预期面板区域
  - [ ] SubTask 7.2: 在 `app.js` 解析 `next_bar_prediction` 字段，渲染下一根 K 线方向（多/空/不确定）+ 概率 + reasoning
  - [ ] SubTask 7.3: 在 `app.js` 解析 `next_cycle_prediction` 字段，渲染下一周期 top3 位置 chip + reasoning
  - [ ] SubTask 7.4: 添加不可预测状态的展示分支

- [x] Task 8: Stage prompt 全文展示（已在 P0 Task 3 中完成）
  - [x] SubTask 8.1: 在 `web/api/routes_analyze.py` 的 SSE 流中补充传递 stage_prompt 的 system/user prompt 全文内容
  - [x] SubTask 8.2: 在 `app.js` 解析 stage_prompt 事件，展示 system/user prompt 全文（可折叠）
  - [x] SubTask 8.3: 在 `index.html` 添加 prompt 展示区域（默认折叠）

## 阶段三：交互增强（P2）

- [ ] Task 9: 实时数据刷新
  - [ ] SubTask 9.1: 在 `web/static/js/app.js` 添加基于 setInterval 的后台轮询逻辑（按 `refresh_interval_ms` 配置间隔调用 `/api/bars`）
  - [ ] SubTask 9.2: 在 `index.html` 添加"实时刷新"开关按钮 + 距上次刷新计时显示
  - [ ] SubTask 9.3: 刷新时仅更新 K 线数据，不中断当前分析流程

- [ ] Task 10: 决策树路径回放表格
  - [ ] SubTask 10.1: 在 `web/api/routes_analyze.py` 确认决策树路径数据已通过 SSE 或 record 传递到前端
  - [ ] SubTask 10.2: 在 `index.html` 添加决策树表格区域（6 列：步/阶段/节点/回答/K线依据/理由）
  - [ ] SubTask 10.3: 在 `app.js` 渲染决策树路径为 HTML table，支持折叠展开
  - [ ] SubTask 10.4: 添加终点 banner（trade/wait/reject/proceed 状态标识）

- [ ] Task 11: 飞书通知设置
  - [ ] SubTask 11.1: 在 `web/api/routes_settings.py` 添加飞书设置字段的 GET/PUT 支持（webhook_url / secret / app_id / app_secret / notify_on_order_only / enabled）
  - [ ] SubTask 11.2: 在 `index.html` 设置 modal 添加"飞书通知" tab
  - [ ] SubTask 11.3: 在 `app.js` 添加飞书设置的读写逻辑
  - [ ] SubTask 11.4: 添加"测试发送"按钮调用后端测试接口

- [ ] Task 12: 会话缓存清理
  - [ ] SubTask 12.1: 在 `web/api/routes_chat.py` 为 `_chat_sessions` 字典添加 TTL 机制（如 30 分钟无活动自动清理）
  - [ ] SubTask 12.2: 或在分析完成 / 页面卸载时通过 API 主动通知后端清理 session

## 阶段四：测试保障

- [ ] Task 13: Web API 单元测试
  - [ ] SubTask 13.1: 创建 `tests/unit/test_web_routes_analyze.py`，测试 `/api/analyze/stream` 路由（mock orchestrator）
  - [ ] SubTask 13.2: 创建 `tests/unit/test_web_routes_chat.py`，测试 `/api/chat/stream` 路由（mock FreeChatSession）
  - [ ] SubTask 13.3: 创建 `tests/unit/test_web_routes_data.py`，测试 `/api/bars` / `/api/datasources` / `/api/timeframes`
  - [ ] SubTask 13.4: 创建 `tests/unit/test_web_routes_settings.py`，测试 `/api/settings` GET/PUT + 飞书设置

- [ ] Task 14: 前端逻辑测试
  - [ ] SubTask 14.1: 创建 `tests/unit/test_web_decision_mapping.py`，测试决策字段映射（含 no_order 场景）
  - [ ] SubTask 14.2: 创建 `tests/unit/test_web_sse_events.py`，测试 SSE 事件解析（含 Retry/Failed/InsufficientData）
  - [ ] SubTask 14.3: 创建 `tests/unit/test_web_chart_overlay.py`，测试 EMA 计算、支撑阻力位提取逻辑

- [ ] Task 15: 冒烟测试与本地验证
  - [ ] SubTask 15.1: 创建 `tests/e2e/test_web_smoke.py`，使用 mock AI 响应验证完整 Web 分析流程
  - [ ] SubTask 15.2: 验证 Web 服务器能在云端无 PyQt6 环境下正常启动（`QT_QPA_PLATFORM=offscreen` 或完全不依赖 Qt）
  - [ ] SubTask 15.3: 验证 `web/server.py` 的 lifespan bootstrap 不导入任何 `pa_agent.gui` 模块
  - [ ] SubTask 15.4: 运行全部测试确认通过

# Task Dependencies

- Task 2, Task 3 可与 Task 1 并行
- Task 4 依赖 Task 1（需要正确的决策字段才能画价格线）
- Task 5 依赖 Task 4
- Task 6 依赖 Task 1（需要正确的置信度字段）
- Task 7 依赖 Task 1
- Task 8 独立
- Task 9 独立
- Task 10 依赖 Task 1
- Task 11 独立
- Task 12 独立
- Task 13 依赖 Task 1, Task 3（测试修复后的逻辑）
- Task 14 依赖 Task 1, Task 3
- Task 15 依赖 Task 1-14 全部完成
