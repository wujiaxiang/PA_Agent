# Checklist

## 阶段一：关键 Bug 修复（P0）

- [x] 决策卡片字段映射 bug 已修复：`renderDecision` 正确读取 `stop_loss_price` / `take_profit_price` / `take_profit_price_2` / `estimated_win_rate` / `trade_confidence` / `passes_trader_equation` / `risk_reward_ratio`
- [x] `no_order` 决策正确展示：隐藏价格字段，显示"本轮不下单"及理由
- [x] 取消分析按钮已添加并绑定：点击后中止 SSE 连接，UI 恢复可发起新分析状态
- [x] SSE 重试事件已处理：`Stage1Retry` / `Stage2Retry` 事件展示重试次数和原因
- [x] SSE 失败事件已处理：`Failed` / `InsufficientData` 事件展示错误信息
- [x] `tests/unit/test_web_decision_mapping.py` 通过
- [x] `tests/unit/test_web_sse_events.py` 通过

## 阶段二：核心可视化迁移（P1）

- [x] EMA20 叠加线在 K 线图上正确渲染
- [x] 入场价 / 止损价 / TP1 / TP2 横线正确绘制（实线/虚线区分，颜色按多空方向区分）
- [x] forming bar（未收盘 K 线）半透明处理生效
- [x] fit_view 自动适配功能工作（最近 20 根 + Y 轴 padding）
- [x] 支撑/阻力位横线 + 区间填充正确渲染
- [x] 方向箭头 marker 在最新 bar 正确位置绘制
- [x] 序号标签（奇数 seq #1, #3...）正确显示
- [x] Token 进度条展示 context_used / context_window 百分比
- [x] Token 进度条黄/红阈值样式生效（>=80% 黄，>=95% 红）
- [x] 市场判断置信度条正确展示
- [x] 交易决策置信度正确展示
- [x] 盈亏比 + 预估胜率 + trader equation 通过/不通过状态正确展示
- [x] 下一根 K 线预期面板正确渲染（方向 + 概率 + reasoning）
- [x] 下一周期预期面板正确渲染（top3 chip + reasoning）
- [x] 不可预测状态正确展示
- [x] Stage prompt 全文（system/user）通过 SSE 传递到前端
- [x] Stage prompt 展示区域可折叠展开

## 阶段三：交互增强（P2）

- [x] 实时数据刷新功能工作：按 `refresh_interval_ms` 间隔轮询 `/api/bars` 更新图表
- [x] 实时刷新开关按钮可切换
- [x] 距上次刷新计时正确显示
- [x] 刷新时仅更新 K 线数据，不中断当前分析
- [x] 决策树路径回放表格正确渲染 6 列（步/阶段/节点/回答/K线依据/理由）
- [x] 决策树表格支持折叠展开
- [x] 终点 banner（trade/wait/reject/proceed）正确显示
- [x] 飞书通知设置 tab 在设置 modal 中可用
- [x] 飞书设置字段（webhook_url / secret / app_id / app_secret / notify_on_order_only / enabled）可读写
- [x] "测试发送"按钮工作正常
- [x] 会话缓存 TTL 机制生效（30 分钟无活动自动清理）
- [x] 无 `_chat_sessions` 内存泄漏

## 阶段四：测试保障

- [x] `tests/unit/test_web_routes_analyze.py` 通过（mock orchestrator 测试分析路由）
- [x] `tests/unit/test_web_routes_chat.py` 通过（mock FreeChatSession 测试追问路由）
- [x] `tests/unit/test_web_routes_data.py` 通过（测试数据源路由）
- [x] `tests/unit/test_web_routes_settings.py` 通过（含飞书设置）
- [x] `tests/unit/test_web_decision_mapping.py` 通过（含 no_order 场景）
- [x] `tests/unit/test_web_sse_events.py` 通过（含 Retry/Failed/InsufficientData）
- [x] `tests/unit/test_web_chart_overlay.py` 通过（EMA 计算、支撑阻力位提取）
- [x] `tests/e2e/test_web_smoke.py` 通过（mock AI 响应的完整 Web 分析流程）
- [x] Web 服务器在无 PyQt6 环境下正常启动（`QT_QPA_PLATFORM=offscreen` 或完全不依赖 Qt）
- [x] `web/server.py` 的 lifespan bootstrap 不导入任何 `pa_agent.gui` 模块
- [x] 全部测试通过：`pytest tests/ -q`
