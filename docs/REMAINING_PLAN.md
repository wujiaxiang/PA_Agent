> ## ⚠️ 本文件已过时 —— 方案被评审推翻并已实施
>
> 评审（5 位专家）推翻了 P1 的核心决策：**方案 A 不可实施** —— 浏览器原生
> `EventSource` 无法设置请求头，服务端拿不到 session_id，分组无从取值；且会引入
> head-of-line blocking。实际改判 **B（前端按自己游标轮询）**，并已落地。
> P2 的分桶键也由「替换」改为「扩键」。P4 的落点与签名均与原文不同。
>
> **下面保留原文仅为对照**，实施结果见 CHANGELOG 当天条目与 SESSION_CHANGES。

# 剩余改造方案（待评审）

> 上游：`87e5fcc`（数据库为唯一真源 + 多会话推理隔离 + user_id 统一）
> 目标：补齐多会话推理的剩余缺口、完成最后一域、收敛配置层绕过路径。
> **本文件是待评审的方案，不是已落地的实现。**

---

## P1 · SSE 多会话隔离

### 现状问题
`routes_bars_stream._background_bars_loop()` 从 `app.state.ctx` 读**全局**订阅，
拉一次数据后 `_broadcast()` 推给所有订阅者。于是：

- 所有标签页收到**同一条** K 线流，与「每 tab 服务自己的 K 线图」直接冲突
- 前端 `bar_update` 收到非本会话标的的数据 → 图上出现别的品种的 K 线

### 方案选项
| 选项 | 做法 | 取舍 |
|---|---|---|
| **A. 按游标分组广播** | 订阅者按 `(exchange,symbol,timeframe)` 分组；后台循环遍历**各不同游标**分别取数，推给该组 | 上游请求数 = 不同游标数（2 tab = 2 次），与缓存命中率相关；改动集中在 `routes_bars_stream.py` |
| **B. 彻底搬前端轮询** | 删掉后台 loop 与订阅表，前端按自己游标轮询 `/api/bars` | 服务端最薄；但 bar_close 精度从「精确」降到轮询间隔；前端要改倒计时与持续分析判定 |
| **C. 混合** | 只在 bar_close 时按组取数，bar_update 走前端轮询 | 复杂度居中，两头都要改 |

**倾向 A**：精度不变、服务端改动集中、可回退；B 虽更「无状态」但前端改动面大。

### 需要动的
- `_add_subscriber(queue)` → `_add_subscriber(session_id, cursor, queue)`
- 后台循环：`_resolve_symbol_timeframe` 从全局改为「遍历分组」
- 复用已建好的 `ephemeral.SessionRegistry` 每会话队列
- 前端 `startSSEBarsStream` 校验事件里的 `symbol` 与自身游标不符则丢弃（防御）

---

## P2 · 追问会话隔离 + 追问持久化

### 现状问题
`routes_chat._chat_sessions` 按 `record_id|symbol|timeframe|快照标志` 分桶：

- **同一条记录被两个标签页回看 → 共享同一份对话历史**（互相污染）
- 全内存，重启即丢

### 方案
- 分桶键改为 `session_id`（`FreeChatSession` 仍按 record 锚定，两者分离）
- 迁到 `SessionRegistry` 的 `SessionState.chat`，随会话 TTL 回收
- **新增**：`chat_turns` 表落库（已在 schema 里，此前无任何写入），
  使追问历史跨重启存活

### 需要动的
- `routes_chat.py`：分桶键、`_touch_session`/`_get_session` 改走 registry
- 新增 `pa_agent/storage/chat_repo.py`
- `FreeChatSession` 写入时同步落库

---

## P3 · trade_records 最后一域

### 现状问题
`trade_records` 只有建表语句，**无仓储、无导入、无双写**。四域里唯一未完成。

### 方案
- CSV 与 PNG **继续落磁盘**（飞书卡片要发图，不能进 DB）
- 新增 `pa_agent/storage/trade_repo.py`：元数据 + 路径进库
- `trade_logger` 落盘后双写
- 导入器扫既有 `trade_records/*.csv` 补种

### 需要动的
- 新增 `trade_repo.py`；`trade_logger.py` 加双写钩子；`importer.py` 加导入函数

---

## P4 · 配置层绕过路径收敛（B2 / B3 / B4）

### B2 迁移段未复用
`_try_load_from_db` → `resolve()` 直接 `save_baseline(raw)`，**绕过**了
`settings.py` 的 legacy 迁移段（`default_bar_count` → `analysis_bar_count`、
`cost_warning_threshold_pct` → `context_warning_threshold_pct`、
`migrate_general_gold_defaults`）。实测同一份 legacy 文件：文件路径得
`bars=321`，DB 路径得 `bars=100`（静默回落默认值）。

**方案**：抽出 `normalize_raw(raw)` 纯函数，文件路径与 DB 路径都先过它。

### B3 稀疏覆盖失效
`compute_diff(baseline, current)` 里 `baseline` 若是**稀疏的**，
「baseline 缺该键」会被当成 `None != val` → 整份 `model_dump()` 的 63 个叶子
全部写进 user_prefs，模块 docstring 明令禁止的形态重新出现。
现网测试用的是 3-section 手写夹具，结构上发现不了。

**方案**：`load_baseline()` 返回**归一化后的完整快照**（过 `normalize_raw` +
`Settings.model_validate().model_dump()`），使 diff 基准天然完整。

### B4 五条绕过写路径
`routes_data.subscribe` 与 `app_context` 的 5 个 `sync_*_provider_on_load`
直接改 `ctx.settings` 并 `save_settings`。一旦 baseline 存在，文件被完全忽略
——**只写不读**。

**方案**：抽出单一 `persist(settings)`，负责「算 diff → 写 user 覆盖 → 写文件」。
所有写入路径改走它。需逐个确认这些路径应写**用户层**还是**系统兜底层**
（connector 自动同步更像系统层，游标切换更像用户层）。

---

## 并行拆分（评审通过后执行）

| 子代理 | 范围 | 写冲突面 |
|---|---|---|
| **A** | P1 SSE + P2 追问（`routes_bars_stream.py`、`routes_chat.py`、`chat_repo.py`） | 都要碰 `ephemeral.SessionRegistry` → **冲突风险** |
| **B** | P3 trade_records（`trade_repo.py`、`trade_logger.py`、`importer.py`） | `importer.py` 与 A 不重叠 |
| **C** | P4 配置（`settings.py`、`settings_store.py`、`routes_data.py`、`app_context.py`） | `routes_data.py` 与 A 不重叠 |

**已知冲突点，需在开工前约定**：
1. A 与 B 都要改 `importer.py`？→ 否，B 独占
2. A 与 C 都要改 `routes_data.py`？→ A 不碰 routes_data（C 独占）
3. A 内部 P1/P2 共用 registry → **A 自己串行做，不要再拆**