# 图表 K 线与「分析快照」说明

本文说明：**K 线序号 K1…KN 的含义、forming bar（未收盘 K 线）的处理方式、市场休市时的特殊行为**，以及图表与 AI 分析数据如何保持序号一致。

适用于 PA Agent 当前版本，覆盖两种运行模式：
- **Web 后端**（推荐）：FastAPI + SSE 实时推送
- **桌面 GUI**：PyQt6 + EventBus 定时轮询

---

## 1. K 线序号约定

程序约定：

| 序号 | 含义 |
|---|---|
| **K1** | 最新一根 **已收盘** K 线 |
| **K2** | K1 的前一根已收盘 |
| **K3** | 再往前，以此类推 |
| **K0**（forming bar） | 当前正在形成、尚未收盘的 K 线，**不参与 AI 分析** |

实时画面最右侧那根浅色空心 K 线就是 forming bar，它没有稳定的 K 序号进入分析 JSON，**收盘后会自动变成下一轮的 K1**。

---

## 2. 为什么 forming bar 不算 K1？

### 2.1 AI 只能分析「已走完」的 K 线

价格行为判断依赖 **完整的 OHLC**（开高低收）。未收盘的 K 线：

- 收盘价还在变，实体大小、阴阳、影线结构都不稳定；
- 若当作「K1」送给模型，分析到一半价格又跳变，结论会前后矛盾；
- 与提示词里「K1 = 最新 **已收盘** K 线」的约定也会冲突。

因此：**提交给 AI 的快照只包含已收盘 K 线**。

### 2.2 图表必须与 AI 使用同一套序号

若在实时画面上把 **未收盘棒** 标成 `#1`，而 AI 表格里 **K1** 却是上一根已收盘棒，你会对不上号，误判信号棒、入场棒属于哪一根。

前端 `setSeqMarkers`（[web/static/js/chart.js](../web/static/js/chart.js)）的实现是：

```javascript
for (let i = 0; i < sorted.length; i++) {
  const b = sorted[i];
  if (b.seq <= 0) continue;  // 0 = forming bar，不画序号
  // ...
}
```

即 `seq <= 0` 一律跳过，确保图上的 `#1` 永远是最新已收盘棒，与 AI JSON 表格一致。

---

## 3. 两种数据帧

[pa_agent/data/snapshot.py](../pa_agent/data/snapshot.py) 提供两种帧构建器：

| 帧类型 | 函数 | 内容 | 用途 |
|---|---|---|---|
| **实时帧** | `build_display_frame` | N 已收盘 + 1 forming | 图表显示、盯盘 |
| **分析帧** | `build_analysis_frame` | 仅 N 已收盘 | 发给 AI 做分析 |

两套帧共用同一套 K 序号（K1 = 最新已收盘），但实时帧多 1 根 forming bar（seq=0，不画序号）。

---

## 4. 两种运行模式的差异

### 4.1 Web 后端（推荐）

- **实时刷新**：SSE 流（`/api/bars/stream`）主动推送，无需前端轮询
- **分析进行中**：图表仍保持实时刷新，不冻结（与桌面 GUI 不同）
- **切换品种/周期**：自动重连 SSE，无需手动点击「获取数据」
- **forming bar 显示**：浅色空心样式（`COLOR_UP_FORMING` / `COLOR_DOWN_FORMING`）

前端关键代码：

| 模块 | 作用 |
|---|---|
| [web/static/js/chart.js](../web/static/js/chart.js) → `setSeqMarkers` | 跳过 `seq <= 0` 的 forming bar，只画 `#1…#N` |
| [web/static/js/chart.js](../web/static/js/chart.js) → forming bar 渲染 | `isLast && b.closed === false` 时用浅色空心样式 |
| [web/api/routes_bars_stream.py](../web/api/routes_bars_stream.py) | SSE 后台循环：forming 期间每 1s 推增量，收盘时推 `bar_close` 事件 |

### 4.2 桌面 GUI（原项目主入口）

- **实时刷新**：`RefreshLoop` 定时轮询（默认 1s）
- **分析进行中**：图表冻结，暂停刷新，未收盘棒消失
- **切换品种/周期**：刷新自动停止，需重新点击「获取数据」
- **forming bar 显示**：`CandleItem` 空心绘制

桌面 GUI 关键代码：

| 模块 | 作用 |
|---|---|
| `pa_agent/gui/main_window.py` → `_start_analysis` | 提交时冻结为分析帧 |
| `pa_agent/gui/widgets/candle_item.py` | 未收盘棒空心绘制 |

---

## 5. 市场休市的特殊行为（2026-07-20 修复）

当市场已收盘（如 NVDA 美股休市、A 股周末）时，数据源只返回已收盘的 K 线，**不返回** forming bar。此时程序的行为：

| 维度 | 正常模式 | 休市模式 |
|---|---|---|
| `latest_snapshot(n)` 返回长度 | `n+1`（1 forming + n closed） | `n`（全 closed） |
| `bars[0]` | `seq=0, closed=False`（forming） | `seq=1, closed=True`（最新已收盘） |
| 图表最右侧 K 线 | 浅色空心（forming） | 实心（已收盘 K1） |
| 最右侧序号 | 无（seq=0 跳过） | `#1` |
| SSE 推送间隔 | 形成中每 1s，收盘推 `bar_close` | 进入 `_FALLBACK_WAIT_S` 兜底等待 |

相关代码：

| 模块 | 作用 |
|---|---|
| [pa_agent/data/bar_close_wait.py](../pa_agent/data/bar_close_wait.py) → `seconds_until_bar_closes` | `now_ms >= ts_open_ms + duration_ms` 时返回 0，触发休市模式 |
| [pa_agent/data/bar_close_wait.py](../pa_agent/data/bar_close_wait.py) → `has_forming_bar_at_head` | 判断 `bars[0]` 是否为真正的 forming bar |
| [pa_agent/data/tradingview.py](../pa_agent/data/tradingview.py) → `_latest_snapshot_inner` | 休市模式下 `bars[0]` 用 `seq=1, closed=True` |
| [pa_agent/data/base.py](../pa_agent/data/base.py) → `_validate_snapshot` | 支持两种模式契约 |
| [web/api/routes_bars_stream.py](../web/api/routes_bars_stream.py) | SSE 后台循环休市时降级到 `_FALLBACK_WAIT_S` |

> ⚠️ **消费方约束**：判断 `bars[0]` 是否为 forming bar 必须用 `has_forming_bar_at_head()`，**不要**硬编码 `bars[0].closed == False`。详见 [README.md 坑 7、13](../README.md#已知坑与修复记录重要)。

---

## 6. 工作原理（数据流）

### 6.1 Web 后端模式

```text
                    数据源 (TradingView / AkShare / MT5)
                              │
                              ▼
                    latest_snapshot (n+1 或 n)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
        SSE 实时推送                      提交分析
        /api/bars/stream                  /api/analyze/stream
        build_display_frame              build_analysis_frame
        (N 已收盘 + 1 forming)           (仅 N 已收盘)
              │                               │
              ▼                               ▼
        前端 chart.js                     TwoStageOrchestrator
        - forming 用浅色空心              → Prompt → AI
        - closed 用实心 + #序号
        - seq <= 0 跳过序号
```

### 6.2 桌面 GUI 模式

```text
                    MT5 行情
                        │
                        ▼
              latest_snapshot（最新在前）
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
  图表 · 实时更新                    提交分析 · 快照
  build_live_frame                   build_analysis_frame
  （N 根已收盘 + 1 根未收盘）         （仅 N 根已收盘）
        │                               │
        ▼                               ▼
  ChartWidget 空心显示               TwoStageOrchestrator
  （可继续刷新）                      → Prompt → AI
        │                               │
        │  点击「提交分析」              │
        └──────────► 暂停刷新 ──────────┘
                     图表改为「仅已收盘」冻结帧
```

---

## 7. 常见场景问答

### Q1：Web 后端分析进行中，图表会冻结吗？

**不会**。Web 后端模式下，SSE 流仍持续推送，图表保持实时更新。这与桌面 GUI「点击提交分析后冻结」不同。

### Q2：休市后最后一根 K 线为什么显示 `#1`？

因为休市时 `latest_snapshot(n)` 返回 `n` 根全 closed 的 K 线，`bars[0]` 是 `seq=1, closed=True`，所以前端会画 `#1` 序号。这是 2026-07-20 修复的正确行为（之前 bug 是被错误标记为 `seq=0, closed=False` 导致不画序号）。

### Q3：「K线数」参数在哪里配？

- **Web 后端**：设置面板的 `analysis_bar_count`，或 `.env` 的 `PA_AGENT_GENERAL_ANALYSIS_BAR_COUNT`
- **桌面 GUI**：控制栏的「K线数」spin 框，或 `config/settings.json` 的 `general.analysis_bar_count`

### Q4：追问时图表和 AI 数据一致吗？

发送追问前会刷新并冻结（桌面 GUI）/ 刷新快照（Web 后端）。追问附带的 K 线文本表仍按 **已收盘** 导出，与阶段分析规则一致，避免把未走完的 K 线写进对话。

### Q5：切换品种/周期后图表为什么自动刷新？

- **Web 后端**：`/api/subscribe` 切换后自动重连 SSE，前端无需手动操作
- **桌面 GUI**：切换后刷新自动停止，需重新点击「获取数据」

---

## 8. 设计原则小结

1. **分析用数据 = 仅已收盘 K 线**，保证 OHLC 完整、结论稳定
2. **K 线序号 K1…KN 在图表与 AI 之间一一对应**：`seq <= 0` 的 forming bar 不画序号
3. **实时盯盘仍可看 forming bar**（空心样式），但不进入分析 JSON
4. **休市模式**：`latest_snapshot(n)` 返回 `n` 根全 closed，`bars[0].seq=1`，正常显示 `#1`
5. **消费方约束**：判断 forming bar 必须用 `has_forming_bar_at_head()`，不要硬编码 `bars[0].closed == False`

---

## 9. 相关文档

- 整体用法：[`PA_Agent使用文档.md`](PA_Agent使用文档.md)
- 项目概述：[`README.md`](../README.md)
- 已知坑与修复记录：[`README.md` §已知坑](../README.md#已知坑与修复记录重要)
- 本地配置：[`config/README.md`](../config/README.md)
- 数据获取流程：[`获取数据功能说明.md`](./获取数据功能说明.md)
