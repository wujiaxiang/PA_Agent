"""SQLite DDL 与 schema 版本管理。

## 为什么真源是 Python 常量而不是 .sql 文件

运行时读的是本模块的字符串常量。独立 ``.sql`` 文件看着更「正统」，但 Docker
镜像按目录 COPY（``web/Dockerfile:26``），``.sql`` 需要额外 COPY 步骤并引入
包内路径解析问题 —— 而这里的 DDL 是**启动期必经路径**，解析失败等于整库不可用。
拿可用性换一个形式上的统一，不划算。

## 那 schema.sql 是什么

``schema.sql`` 是**派生产物**，由 :func:`render_sql` 从本模块生成，供
「手工重建 / 换机 / 审阅」使用。``test_storage_layer.py::test_schema_sql_matches_python``
会逐句比对两者 —— **漂移即测试失败**，所以它不可能悄悄过期。

要改表结构：**只改本模块**，然后跑 ``python -m pa_agent.storage.schema`` 重新生成。

分级语义见 ``docs/SESSION_STORAGE_DESIGN.md`` §2：
L1 无 user_id；L2 带 user_id 且多会话共享；L3 带 session_id 且为缓存级快照。
"""
from __future__ import annotations

# ── Schema 版本 ───────────────────────────────────────────────────────────────
# 每次 DDL 变更递增。``db.migrate()`` 据此做幂等升级。
SCHEMA_VERSION = 1

# ── L1 全局级 ────────────────────────────────────────────────────────────────
# 单一全局配置。不带 user_id：凭证与全局开关本就是单机属性，分了反而危险
# （alert_on_order_opportunity 逐用户分开会让多个 tab 各自发单）。
DDL_GLOBAL_CONFIG = (
    """
    CREATE TABLE IF NOT EXISTS global_config (
        key        TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
)

# ── L2 用户级 ─────────────────────────────────────────────────────────────────
# 用户偏好。同样带 user_id —— decision_stance 影响写入经验库的内容，
# 必须是用户级统一，否则两个 tab 用不同姿态会往共享经验库写出互相矛盾的案例。
DDL_USER_PREFS = (
    """
    CREATE TABLE IF NOT EXISTS user_prefs (
        user_id    TEXT NOT NULL DEFAULT 'default',
        key        TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, key)
    )
    """,
)

# 分析记录。大字段（kline_data + 两阶段 messages，数百 KB）留在 payload_json，
# 索引只用小字段 —— 塞进 BLOB 会让索引页膨胀。
DDL_ANALYSIS_RECORDS = (
    """
    CREATE TABLE IF NOT EXISTS analysis_records (
        record_id       TEXT PRIMARY KEY,
        user_id         TEXT NOT NULL DEFAULT 'default',
        exchange        TEXT NOT NULL DEFAULT '',
        symbol          TEXT NOT NULL,
        timeframe       TEXT NOT NULL,
        ts_local_ms     INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'ok',
        incremental     INTEGER NOT NULL DEFAULT 0,
        continuous      INTEGER NOT NULL DEFAULT 0,
        decision_stance TEXT NOT NULL DEFAULT '',
        has_kline       INTEGER NOT NULL DEFAULT 0,
        payload_json    TEXT NOT NULL,
        file_path       TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    )
    """,
    # 「找这个标的上一次成功的记录」是增量分析的唯一热路径；索引方向按过滤前缀 + 时间倒序。
    """
    CREATE INDEX IF NOT EXISTS ix_rec_lookup
        ON analysis_records (user_id, exchange, symbol, timeframe, ts_local_ms DESC)
    """,
    # 「跨全部品种浏览历史」—— 过滤条件可选时走这条（EXPLAIN 需确认能跳过前导列）。
    """
    CREATE INDEX IF NOT EXISTS ix_rec_recent
        ON analysis_records (user_id, ts_local_ms DESC)
    """,
)

# 经验库。★ 用户级共享：A tab 写出的案例，B tab 看同一标的时必须能读到，
# 否则经验库失去意义（AGENTS.md「经验库范围恒等于当前 K 线」）。
DDL_EXPERIENCE_ENTRIES = (
    """
    CREATE TABLE IF NOT EXISTS experience_entries (
        entry_id      TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL DEFAULT 'default',
        status        TEXT NOT NULL,
        symbol        TEXT NOT NULL DEFAULT '',
        timeframe     TEXT NOT NULL DEFAULT '',
        exchange      TEXT NOT NULL DEFAULT '',
        cycle_position TEXT NOT NULL DEFAULT '',
        timestamp_ms  INTEGER NOT NULL,
        pnl_pct       REAL,
        entry_price   REAL,
        content_json  TEXT NOT NULL,
        file_path     TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_exp_browse
        ON experience_entries (user_id, status, symbol, timeframe, timestamp_ms DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_exp_recent
        ON experience_entries (user_id, timestamp_ms DESC)
    """,
)

# 交易记录。★ 资金面，多会话共享；PNG/CSV 仍落磁盘，库里只存元数据与路径。
DDL_TRADE_RECORDS = (
    """
    CREATE TABLE IF NOT EXISTS trade_records (
        trade_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL DEFAULT 'default',
        symbol       TEXT NOT NULL,
        timeframe    TEXT NOT NULL DEFAULT '',
        order_type   TEXT NOT NULL DEFAULT '',
        entry_price  REAL,
        sl_price     REAL,
        tp_price     REAL,
        pnl_pct      REAL,
        csv_path     TEXT NOT NULL DEFAULT '',
        chart_path   TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at   REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_trade_recent
        ON trade_records (user_id, created_at DESC)
    """,
)

# 追问历史。★ 持久（L2）但按 thread_key 隔离线程：同一记录被两个 tab 回看时
# 各有各的对话，不能互相污染。thread_key = (record_id, snapshot flag)。
DDL_CHAT_TURNS = (
    """
    CREATE TABLE IF NOT EXISTS chat_turns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT NOT NULL DEFAULT 'default',
        session_id  TEXT NOT NULL DEFAULT '',
        thread_key  TEXT NOT NULL DEFAULT '',
        record_id   TEXT NOT NULL DEFAULT '',
        symbol      TEXT NOT NULL DEFAULT '',
        timeframe   TEXT NOT NULL DEFAULT '',
        turn        INTEGER NOT NULL,
        ts_ms       INTEGER NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        reasoning   TEXT,
        usage_json  TEXT NOT NULL DEFAULT '{}',
        cancelled   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_chat_thread
        ON chat_turns (user_id, thread_key, turn)
    """,
)

# ── L3 会话级（缓存级快照） ───────────────────────────────────────────────────
# 只有「游标 + 视图模式 + 运行时开关」。运行时开关（keep_analysis/wait_close）
# 重连后刻意不还原 —— 那是缓存语义，不是需要跨重启保留的状态。
DDL_SESSIONS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL DEFAULT 'default',
        symbol      TEXT NOT NULL DEFAULT '',
        timeframe   TEXT NOT NULL DEFAULT '',
        exchange    TEXT NOT NULL DEFAULT '',
        data_mode      TEXT NOT NULL DEFAULT 'live',
        replay_record_id TEXT NOT NULL DEFAULT '',
        keep_analysis INTEGER NOT NULL DEFAULT 0,
        wait_close    INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_seen  REAL NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON sessions (expires_at)
    """,
)

# 迁移元信息
DDL_SCHEMA_META = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

# ── 用户 ──────────────────────────────────────────────────────────────────────
# 单机部署下恒为 1 行（admin）。建表是为了让「多用户」不是一次性大改：
# L2/L3 表的 user_id 已是外键语义，今天恒等于 admin。UI 暂不做登录，
# 解析时统一取默认用户即可（见 users.default_user_id()）。
DDL_USERS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id      TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL DEFAULT 'admin',
        is_default   INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        last_seen    REAL NOT NULL,
        password_hash TEXT NOT NULL DEFAULT ''
    )
    """,
)

#: 旧库的增量迁移。``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表是空操作，
#: 所以新增列必须靠 ALTER 补。逐条独立执行，且允许「列已存在」失败 ——
#: 这样重复运行安全，不必维护版本号分支。
#:
#: 2026-10-05: 预留给注册登录。旧库一律补空串（表示「未设口令」），
#: authenticate() 对空散列恒失败，不存在误放行。
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("users", "ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"),
)

_ALL_DDL: tuple[tuple[str, ...], ...] = (
    DDL_SCHEMA_META,
    DDL_USERS,
    DDL_GLOBAL_CONFIG,
    DDL_USER_PREFS,
    DDL_ANALYSIS_RECORDS,
    DDL_EXPERIENCE_ENTRIES,
    DDL_TRADE_RECORDS,
    DDL_CHAT_TURNS,
    DDL_SESSIONS,
)


def all_statements() -> tuple[str, ...]:
    """Flatten every DDL group into individual statements.

    必须逐条 ``execute`` 而非 ``executescript``：后者会先隐式 COMMIT，
    会把 ``db.migrate()`` 的事务拆开，中途失败留下半套表。DDL 里全是
    ``IF NOT EXISTS``，单条执行同样幂等。
    """
    return tuple(s for group in _ALL_DDL for s in group)


def tables() -> tuple[str, ...]:
    """Table names only — 供迁移器与诊断使用。"""
    return (
        "users", "global_config", "user_prefs", "analysis_records",
        "experience_entries", "trade_records", "chat_turns", "sessions",
    )


def render_sql() -> str:
    """把全部 DDL 渲染成一个可直接执行的 SQL 脚本。

    与 :func:`all_statements` 同源，故不存在「两份定义漂移」的可能。
    输出含新建库与旧库增量两段，整体幂等。
    """
    head = (
        "-- PA_AGENT 存储层：表结构\n"
        "-- 由 pa_agent/storage/schema.py::render_sql() 生成，**请勿手改**\n"
        "-- 改表结构请改 schema.py 后跑 `python -m pa_agent.storage.schema` 重建\n"
        "-- 用法：sqlite3 records/pa_agent.db < schema.sql\n"
        "-- 全部 IF NOT EXISTS，可重复执行。\n"
    )
    body = ["-- ===== 新建库 ====="]
    body += [s.strip() + ";" for s in all_statements()]
    body += [
        "",
        "-- ===== 旧库增量：不写进本文件 =====",
        "-- CREATE TABLE IF NOT EXISTS 对**已存在**的表是空操作，所以老库补列靠",
        "-- MIGRATIONS 里的 ALTER。那些 ALTER 由应用启动时的 db.py::migrate() 执行",
        "-- （它显式容忍 duplicate column，可重复运行）。",
        "--",
        "-- **刻意不写进本文件**：上面 CREATE 已含这些列，写进来会让空库执行到",
        "-- ALTER 时立刻报 duplicate column —— 手工重建直接失败。",
        "--",
    ]
    for table, stmt in MIGRATIONS:
        body += [f"-- [{table}] {stmt.strip()};"]
    body += [
        "",
        f"-- schema 版本: {SCHEMA_VERSION}",
        f"-- 表: {', '.join(tables())}",
        "",
    ]
    return head + "\n".join(body) + "\n"


if __name__ == "__main__":  # pragma: no cover - 维护脚本
    import pathlib

    out = pathlib.Path(__file__).with_name("schema.sql")
    out.write_text(render_sql(), encoding="utf-8")
    print(f"regenerated {out} ({len(tables())} tables, version {SCHEMA_VERSION})")
