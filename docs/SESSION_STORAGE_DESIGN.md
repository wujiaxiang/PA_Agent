# 会话与存储分层设计（SQLite + 会话快照）

> 状态：已采纳，进入实现。相关约束同步维护在 AGENTS.md。
> 背景：解决「服务端全局单例导致多标签页互相踩踏」，并为多用户/多会话打基础。

---

## 1. 为什么不是「加 Redis」

原始设想是引入 Redis + PG 持久化会话。落地时按环境约束做了替换：

| 原始设想 | 实际选型 | 原因 |
|---|---|---|
| PostgreSQL | **SQLite**（内嵌进程内） | 「内嵌」+ LXC 宿主 + Dockerfile 已注明 AppArmor 导致构建失败；SQLite 零运维、支持真 SQL 与索引 |
| Redis | **不引入**，用「内存注册表 + SQLite 快照」自实现 | 宿主机 10GB 内存已用 6.6G、Swap 已占 4G；Redis 常驻进程有 OOM 风险，且不带来持久化价值 |

**结论：本设计的会话层具备 Redis 语义（TTL / 惰性过期 / LRU / 可替换 backend），但不引入外部依赖。**

---

## 2. 三级数据粒度

用三个正交属性定义边界，避免「目录式」定义反复扯皮：

| 级别 | 生命周期 | 归属 | 变更频率 | 判据 |
|---|---|---|---|---|
| **全局级** | 部署级 | 这台机器 / 这套账号 | 极低，操作者手动改 | 改了影响**所有**会话，且与凭证/成本/资金相关 |
| **用户级** | 永久 | 这个人 | 每次分析追加 | **累积型知识或资产**，多会话必须共享同一份 |
| **会话级** | 标签页级（TTL） | 一个 tab | 高频 | 描述「**此刻在看什么**」，tab 间必须互不可见 |

### 2.1 「浏览历史」与「增量锚点」是两种模式，不可混谈

需求原文：**「一个用户可以有多个会话（多个网页），可以开 2 个网页分开推理，但是历史数据应该都能看」**。

这句话里藏着两条**方向相反**的规则：

| 用途 | 级别 | 过滤条件 | 理由 |
|---|---|---|---|
| **浏览历史记录** | **L2 用户级** | 过滤条件**可选**，默认跨全部品种，按时间倒序 | 历史是跨会话共享的累积资产；A tab 分析出的记录，B tab 必须能查到 |
| **增量分析锚点** | **L3 会话级** | 强制 = **本会话**的 symbol/timeframe/exchange | A tab 的 BTCUSDT 增量链不能串到 B tab 的 NVDA 上下文 |

**现状缺陷（两处，均已核实）：**

1. `routes_records.list_records` 的 `exchange/symbol/timeframe` 三个参数是 `Query(...)` **必填**，且前端硬编码传当前 tab 的游标（`app.js:4858`）。→ 记��面板**只能看到当前订阅品种**，无法跨会话浏览。
2. `routes_analyze` 调 `find_latest_successful_record(symbol=ctx.settings.general.last_symbol, ...)`（`routes_analyze.py:415-423`）读的是**全局**设置而非本会话游标。→ A tab 看 NVDA、全局为 BTCUSDT 时，**A 的增量分析会捞到 BTCUSDT 的上一轮上下文**喂给模型。

第 2 条是铁律 1（「凡影响 L2 写入内容的配置本身必须是 L2」）的反向应用：增量锚点虽不写 L2，但它**读取** L2，必须按会话隔离，否则跨标的串味。

### 2.2 分级铁律

1. **凡影响 L2 写入内容的配置，本身必须是 L2。**
   反例：`decision_stance`（风险姿态）若做成会话级，两个 tab 用不同姿态分析同一标的，会往**共享的**经验库写出互相矛盾的案例。故归 L2。
2. **凡「丢了要重新跑一次分析 / 重新下载一次」的数据，不得进 L3。**
3. **L3 永远不是权威来源**，丢失后可由快照恢复或回落默认值。
4. **动钱/风控的开关必须是 L1**，不允许按 tab 分开。

---

## 3. 会话身份：为什么不能用 Cookie

需求是「每个网页标签页服务自己的 K 线图」，即 **1 tab = 1 会话**。

但 **Cookie 同源共享**，同一浏览器所有 tab 拿到同一个 session_id，直接废掉隔离。

> **会话身份 = 前端 `sessionStorage` 内的 UUID → `X-Session-Id` 请求头**

- `sessionStorage` 语义天生是「每 tab 独立」
- 能扛住 F5 刷新（关 tab 才销毁）
- 服务端不存任何内存指针，只把 id 当不透明 key 查表 → 满足「服务端无状态」

---

## 4. 表结构

```sql
-- ═══ L1 全局级：无 user_id / session_id ═══
CREATE TABLE global_config (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);

-- ═══ L2 用户级：带 user_id，多会话共享 ═══
CREATE TABLE user_prefs (
  user_id    TEXT NOT NULL DEFAULT 'default',
  key        TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE analysis_records (
  record_id      TEXT PRIMARY KEY,        -- 沿用磁盘 JSON 的 basename
  user_id        TEXT NOT NULL DEFAULT 'default',
  exchange       TEXT NOT NULL DEFAULT '',
  symbol         TEXT NOT NULL,
  timeframe      TEXT NOT NULL,
  ts_local_ms    INTEGER NOT NULL,        -- 排序/筛选主键
  status         TEXT NOT NULL DEFAULT 'ok',   -- ok | partial | error
  incremental    INTEGER NOT NULL DEFAULT 0,
  continuous     INTEGER NOT NULL DEFAULT 0,
  decision_stance TEXT NOT NULL DEFAULT '',
  has_kline      INTEGER NOT NULL DEFAULT 0,     -- 增量分析准入条件
  payload_json   TEXT NOT NULL,           -- 完整 AnalysisRecord（大字段留此）
  file_path      TEXT NOT NULL DEFAULT '', -- 双写期磁盘副本，用于回退
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX ix_rec_lookup ON analysis_records
  (user_id, exchange, symbol, timeframe, ts_local_ms DESC);

CREATE TABLE experience_entries (         -- ★ L2 共享：跨 tab 可见
  entry_id    TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL DEFAULT 'default',
  status      TEXT NOT NULL,              -- success | failure | unresolved
  symbol TEXT NOT NULL DEFAULT '', timeframe TEXT NOT NULL DEFAULT '',
  exchange TEXT NOT NULL DEFAULT '', cycle_position TEXT NOT NULL DEFAULT '',
  timestamp_ms INTEGER NOT NULL,
  pnl_pct REAL, entry_price REAL,
  content_json TEXT NOT NULL,
  file_path    TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX ix_exp_browse ON experience_entries
  (user_id, status, symbol, timeframe, timestamp_ms DESC);

CREATE TABLE trade_records (              -- ★ L2 资金记录，多 tab 共享
  trade_id TEXT PRIMARY KEY,
  user_id  TEXT NOT NULL DEFAULT 'default',
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '',
  order_type TEXT NOT NULL DEFAULT '',
  entry_price REAL, sl_price REAL, tp_price REAL, pnl_pct REAL,
  csv_path TEXT NOT NULL DEFAULT '', chart_path TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

CREATE TABLE chat_turns (                 -- ★ L2 持久，对话历史跨刷新存活
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT NOT NULL DEFAULT 'default',
  session_id TEXT NOT NULL DEFAULT '',   -- 隔离 tab；会话清理时按策略回收
  thread_key TEXT NOT NULL DEFAULT '',   -- (record_id|snapshot flag)
  record_id TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL DEFAULT '',
  timeframe TEXT NOT NULL DEFAULT '',
  turn INTEGER NOT NULL, ts_ms INTEGER NOT NULL,
  role TEXT NOT NULL,                    -- user | assistant
  content TEXT NOT NULL, reasoning TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_chat_thread ON chat_turns(user_id, thread_key, turn);

-- ═══ L3 会话级：缓存级，快照持久化，TTL 过期即清 ═══
CREATE TABLE sessions (
  session_id  TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL DEFAULT 'default',
  -- 游标：今天藏在 settings.json 的那三个
  symbol TEXT NOT NULL DEFAULT '', timeframe TEXT NOT NULL DEFAULT '',
  exchange TEXT NOT NULL DEFAULT '',
  -- 视图模式
  data_mode      TEXT NOT NULL DEFAULT 'live',   -- live | replay | demo
  replay_record_id TEXT NOT NULL DEFAULT '',
  -- 运行时开关（重连后不还原，属缓存语义）
  keep_analysis INTEGER NOT NULL DEFAULT 0,
  wait_close    INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, last_seen REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX ix_sessions_expiry ON sessions(expires_at);
```

**设计要点**：大字段留在 `payload_json`，索引用小字段。记录 JSON 平均数百 KB（含 `kline_data` + 两阶段 messages），塞进 BLOB 会让索引页膨胀；拆出来后 `find_latest_successful_record` 从「rglob + 全量 JSON parse + 目录 mtime 启发式」变为一条带索引的 SQL。

---

## 5. `settings.json` 字段归级全景

`GeneralSettings` 今日把游标与全局配置混在同一文件，这是切 tab 互相踩的**根源**。

### 5.1 今日即为错，必须迁 L3
| 字段 | 问题 |
|---|---|
| `keep_analysis`（持续分析） | **A tab 开持续分析会触发 B tab 的分析**——双份 token 且结果互相污染 |
| `last_symbol` / `last_timeframe` / `last_tradingview_exchange` | 即「游标」，必须每 tab 独立 |

### 5.2 判定正确，保持 L1（勿动）
| 字段 | 理由 |
|---|---|
| `alert_on_order_opportunity` | 动钱开关，多 tab 各开各的危险 |
| `decision_confidence_threshold` | 资金面策略 |
| `incremental_max_new_bars` | 成本策略，全局统一才可控 token |
| `experience_verify_mode` / `experience_max_wait_s` | 运维策略 |
| `kline_adjust` | 数据语义，改了历史数据含义就变 |
| `cancel_keep_analysis_on_retry` / `structure_flip_cooldown_bars` | 策略 |
| 全部凭证（provider / feishu / pushplus / tushare / tradingview） | 单机账号 |
| `stream_pane_font_pt` / `chart_seq_label_font_pt` | 纯外观 |

### 5.3 迁 L2（用户级持久偏好）
`decision_stance`（见铁律 1）、`analysis_bar_count`、`refresh_interval_ms`、`display_timezone`、`enable_next_bar_prediction`

### 5.4 迁 L3（会话级游标 / 运行时）
`symbol` / `timeframe` / `exchange`、`data_mode`、`replay_record_id`、`keep_analysis`、`wait_close`

---

## 6. 会话注册表（内嵌 mini-Redis）

### 6.1 定位与铁律

| | 职责 | 丢了会怎样 |
|---|---|---|
| **SQLite** | 持久化 + 快照：L1/L2 权威、L3 快照 | **不可丢** |
| **Registry** | 热层：SSE 队列、追问对象、游标镜像 | 重新建即可 |

> **铁律：任何不能丢的东西绝对不能进 Registry。**

判据一句话：*丢了要不要重新跑一次分析 / 重新下载一次？* 要 → 可以放。

### 6.2 收编的三处现有内存全局

| 现状 | 问题 | 收编后 |
|---|---|---|
| `_subscribers` 模块级 list（`routes_bars_stream.py:38`） | 广播给所有连接，无法隔离 tab | 每 session 独立队列 |
| `_chat_sessions` 模块级 dict（`routes_chat.py:55`） | 按 `record\|symbol\|tf` 分，同记录两 tab **共享对话** | 按 session_id 隔离 |
| `ctx._last_record` | **全局单值**，A tab 分析结果污染 B tab 追问锚点 | 每 session 一份 |

第三条是此前未记录的坑：`_last_record` 挂在全局 ctx 上，`routes_chat.py:134-137` 靠 `_record_matches_subscription` 打补丁才没出错，但那是兜底，不是隔离。

### 6.3 Redis 对应关系

| 本地 | Redis 等价 |
|---|---|
| `SessionRegistry` | keyspace |
| `SessionState`（固定槽位 + 键值） | HASH |
| `set/get/delete(key, ttl_s)` | HSET/HGET + EXPIRE |
| `sweep()` 惰性过期 | 惰性过期 |
| `max_sessions` + LRU | `maxmemory-policy` |
| `sse_queue` | **不对应**——连接态不适合放 Redis |

### 6.4 写穿透 + 快照

```
请求 → registry.get_or_create(sid)      # 热层，命中即返回
        └─ miss → 读 sessions 表 → 命中则回填热层
                 └─ miss → 建默认行（写快照）
变更 → registry（内存，立即）+ sessions 表（快照，持久）
过期 → sweep() 清热层；快照由 expires_at 索引批量清
重启 → 从 sessions 表快照恢复热层（游标可复原；运行时开关归默认）
```

### 6.5 三条硬限制

1. **多进程即失效**：`Dockerfile` 的 `CMD` 不带 `--workers`（单进程）故当前安全。一旦加 `--workers 2`，session 按 hash 落到不同进程 → 表现为「追问偶尔丢历史」。这正是真 Redis 要解决的问题，故 `backend=` 抽象须从第一版就留。
2. **重启即丢（热层）**：Registry 内容不扛重启；游标靠快照恢复，运行时开关归默认。可接受，但须写进接口文档。
3. **不可替代 DB**：若把分析记录塞进 Registry，会造出**既会丢又难查**的状态层，比现状更糟。

### 6.6 内存边界

- `max_sessions=128`，超出按 LRU 踢（被踢的会话连带丢追问历史与 last_record，
  所以上限调的是内存与用户可见数据损失的取舍，不是纯性能参数）
- 每 session 的 `sse_queue` 有界 256，沿用 `SUBSCRIBER_QUEUE_MAXSIZE` 的「丢最旧」策略
- `chat` 对象带 TTL，沿用 `_CHAT_SESSION_TTL_SEC`
- `stats()` 暴露给 `/api/health`，让泄漏**可观测**而非等它炸（对应 AGENTS.md 遗留需求 3「SSE 长连接内存泄漏排查」）
- **不新增后台常驻任务**：把 session sweep 挂到已有 `_chat_cleanup_loop`（改造成通用 housekeeping），避免常驻任务从 4 个变 5 个

---

## 7. 迁移策略：双轨制，零回归

不做一次性大爆炸切换。

| 阶段 | 写 | 读 | 风险 |
|---|---|---|---|
| **A 导入** | 文件 | 文件 | 无 |
| **B 双写** | 文件 + SQLite | 文件 | 无（SQLite 写失败仅记 warning） |
| **C 切读** | 文件 + SQLite | **SQLite，miss 回退文件并顺手补写** | 低 |
| **D 归档** | SQLite only | SQLite | 高，最后做，可不做 |

C 阶段的回退是保险：SQLite 查不到就读文件并补写，导入漏任何一条也不丢历史。

### 降级
- DB 放 `records/pa_agent.db` —— `records/` 已在 docker-compose bind mount，**零部署改动**；`rglob("*.json")` 不会匹配 `.db`
- WAL 模式 + `threading.local` 连接（项目大量使用 `to_thread`，SQLite 连接不可跨线程）
- **DB 不可用时整体降级回纯文件**，仅记 warning —— 绝不让数据库故障导致 K 线出不来

---

## 8. 落地顺序

1. `storage/` 包：`db.py` + DDL + `ephemeral.py`
2. `analysis_records` 仓储 + 幂等导入器（收益最大：消灭 rglob+parse）
3. `sessions` 仓储 + `X-Session-Id` 中间件
4. `experience_entries` 仓储
5. `global_config` / `user_prefs` 拆分 `settings.json`
6. `trade_records` 仓储
