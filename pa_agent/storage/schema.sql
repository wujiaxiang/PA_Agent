-- PA_AGENT 存储层：表结构
-- 由 pa_agent/storage/schema.py::render_sql() 生成，**请勿手改**
-- 改表结构请改 schema.py 后跑 `python -m pa_agent.storage.schema` 重建
-- 用法：sqlite3 records/pa_agent.db < schema.sql
-- 全部 IF NOT EXISTS，可重复执行。
-- ===== 新建库 =====
CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS users (
        user_id      TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL DEFAULT 'admin',
        is_default   INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        last_seen    REAL NOT NULL,
        password_hash TEXT NOT NULL DEFAULT ''
    );
CREATE TABLE IF NOT EXISTS global_config (
        key        TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS user_prefs (
        user_id    TEXT NOT NULL,
        key        TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, key)
    );
CREATE TABLE IF NOT EXISTS analysis_records (
        record_id       TEXT PRIMARY KEY,
        user_id         TEXT NOT NULL,
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
    );
CREATE INDEX IF NOT EXISTS ix_rec_lookup
        ON analysis_records (user_id, exchange, symbol, timeframe, ts_local_ms DESC);
CREATE INDEX IF NOT EXISTS ix_rec_recent
        ON analysis_records (user_id, ts_local_ms DESC);
CREATE TABLE IF NOT EXISTS experience_entries (
        entry_id      TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
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
    );
CREATE INDEX IF NOT EXISTS ix_exp_browse
        ON experience_entries (user_id, status, symbol, timeframe, timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS ix_exp_recent
        ON experience_entries (user_id, timestamp_ms DESC);
CREATE TABLE IF NOT EXISTS experience_reviews (
        review_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id     TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        model        TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT 'llm',   -- 'program' | 'llm'
        verdict      TEXT NOT NULL DEFAULT '',
        reusable_criteria TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        created_at   REAL NOT NULL,
        FOREIGN KEY (entry_id) REFERENCES experience_entries(entry_id)
            ON DELETE CASCADE
    );
-- 试过把列序改成对齐 ``program_review`` / ``latest_llm_review`` 的
    -- ORDER BY（(…, source, verdict, created_at DESC)），实测**并没有**消除
    -- 临时 B-tree：ORDER BY 末尾的 ``review_id DESC`` 本身就无法被索引覆盖，
    -- 去掉它才能 TEMP=False，而去掉就放弃了同毫秒并列时的确定性仲裁。
    -- 每条经验只有 1–3 条复盘，这个排序代价可忽略，故保持原样。
    CREATE INDEX IF NOT EXISTS ix_review_entry
        ON experience_reviews (user_id, entry_id, created_at DESC);
CREATE TABLE IF NOT EXISTS trade_records (
        trade_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
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
    );
CREATE INDEX IF NOT EXISTS ix_trade_recent
        ON trade_records (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS chat_turns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT NOT NULL,
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
    );
CREATE INDEX IF NOT EXISTS ix_chat_thread
        ON chat_turns (user_id, thread_key, turn);
CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
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
    );
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON sessions (expires_at);

-- ===== 旧库增量：不写进本文件 =====
-- CREATE TABLE IF NOT EXISTS 对**已存在**的表是空操作，所以老库补列靠
-- MIGRATIONS 里的 ALTER。那些 ALTER 由应用启动时的 db.py::migrate() 执行
-- （它显式容忍 duplicate column，可重复运行）。
--
-- **刻意不写进本文件**：上面 CREATE 已含这些列，写进来会让空库执行到
-- ALTER 时立刻报 duplicate column —— 手工重建直接失败。
--
-- [users] ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT '';
-- [sessions] drop_default_user_id;
-- [chat_turns] drop_default_user_id;
-- [user_prefs] drop_default_user_id;
-- [analysis_records] drop_default_user_id;
-- [experience_entries] drop_default_user_id;
-- [trade_records] drop_default_user_id;
-- [experience_reviews] drop_default_user_id;
-- [experience_reviews] ALTER TABLE experience_reviews ADD COLUMN source TEXT NOT NULL DEFAULT 'llm';

-- schema 版本: 1
-- 表: users, global_config, user_prefs, analysis_records, experience_entries, experience_reviews, trade_records, chat_turns, sessions

