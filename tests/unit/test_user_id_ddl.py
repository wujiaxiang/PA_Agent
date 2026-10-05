"""`user_id` 的 DDL 默认值必须不存在，且迁移不得丢数据或索引。

## 为什么

7 张表的 user_id 曾是 ``TEXT NOT NULL DEFAULT 'default'``，而 ``users`` 表里
只有 ``admin`` —— ``default`` 是个**不存在的用户**。写入不报错，按 user_id
过滤时却永远查不到，且没有任何提示。同源事故：``bind_session`` 曾把 user_id
硬编码成 ``"default"``，``db.py`` 与 ``users.py`` 又各定义一份
``DEFAULT_USER_ID`` —— 三方分裂，用户级查询集体返回空。

## 迁移为什么必须重建表

SQLite 没有「删掉列默认值」这种语句，唯一手段是 CREATE 新表 → 拷数据 →
DROP 旧表 → RENAME。而重建会丢掉该表上所有**显式索引**（漏一个的代价是
查询退化成全表扫，表现为「莫名其妙慢了 100 倍」），所以索引 DDL 必须从
``sqlite_master`` 动态抓取后补回 —— 本测试逐张表核对。
"""

import re
import sqlite3
from pathlib import Path

import pytest

from pa_agent.storage.db import _ConnectionHub
from pa_agent.storage.schema import SCHEMA_VERSION, all_statements, create_table_ddl

_TABLES = (
    "sessions", "chat_turns", "analysis_records", "trade_records",
    "experience_entries", "experience_reviews", "user_prefs",
)


def _snapshot(conn: sqlite3.Connection, table: str) -> tuple[int, set[str], str | None]:
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        )
        if not r[0].startswith("sqlite_autoindex")
    }
    default = next(
        (r[4] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
         if r[1] == "user_id"),
        "NO-COL",
    )
    return n, idx, default


def _build_old_schema(path: Path) -> None:
    """造一个「还带 DEFAULT 'default'」的库，模拟存量部署。"""
    conn = sqlite3.connect(str(path))
    # 把 user_id 的 DEFAULT 注入回去，还原成迁移前的 DDL —— 直接用当前
    # all_statements() 建出来的库本来就没默认值，迁移也就无从测起。
    for stmt in all_statements():
        conn.execute(re.sub(
            r"(user_id\s+TEXT NOT NULL)(,)",
            r"\1 DEFAULT 'default'\2",
            stmt,
        ))
    for table in _TABLES:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in info]
        if "user_id" in cols:
            conn.execute(
                f"UPDATE {table} SET user_id='default' WHERE rowid "
                f"IN (SELECT rowid FROM {table} LIMIT 2)"
            )
    conn.commit()
    conn.close()


def test_no_ddl_declares_a_user_id_default():
    """新库建表语句里不得再出现 user_id 的 DEFAULT。"""
    for stmt in all_statements():
        if "user_id" in stmt and "DEFAULT 'default'" in stmt:
            pytest.fail(f"user_id 又带上了不存在的 DEFAULT 'default'：{stmt[:90]}")


def test_create_table_ddl_covers_every_migrated_table():
    for t in _TABLES:
        ddl = create_table_ddl(t)
        assert ddl is not None, f"{t} 找不到 CREATE TABLE 语句"
        assert f"IF NOT EXISTS {t} " in ddl


@pytest.mark.parametrize("table", _TABLES)
def test_migration_drops_default_without_losing_rows_or_indexes(table):
    db = Path("/tmp") / f"uid_mig_{table}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    _build_old_schema(db)

    conn = sqlite3.connect(str(db))
    before_n, before_idx, before_dflt = _snapshot(conn, table)
    assert before_dflt == "'default'", "前置条件不成立：建出来的库没有默认值"
    conn.close()

    hub = _ConnectionHub(path=db)
    assert hub.migrate() is True

    conn = sqlite3.connect(str(db))
    after_n, after_idx, after_dflt = _snapshot(conn, table)
    conn.close()

    assert after_dflt is None, f"{table} 仍带默认值 {after_dflt}"
    assert after_n == before_n, f"{table} 行数 {before_n} → {after_n}"
    assert after_idx == before_idx, f"{table} 索引丢失：{before_idx - after_idx}"


def test_migration_is_idempotent():
    db = Path("/tmp/uid_mig_idem.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    _build_old_schema(db)
    hub = _ConnectionHub(path=db)
    assert hub.migrate() is True
    conn = sqlite3.connect(str(db))
    snap = {t: _snapshot(conn, t) for t in _TABLES}
    conn.close()
    assert hub.migrate() is True
    conn = sqlite3.connect(str(db))
    for t, (n, idx, dflt) in snap.items():
        n2, idx2, dflt2 = _snapshot(conn, t)
        assert (n, idx, dflt2) == (n2, idx2, dflt), f"{t} 二次迁移有变化"
    conn.close()


def test_existing_default_rows_are_not_rewritten_to_admin():
    """存量 `'default'` 行不得被改名成 `'admin'`。

    把「身份未知」的记录谎报成管理员，比查不到更糟：用户会看到一批根本不属于
    自己的历史。按 user_id 过滤时这种行本来就查不到，改写反而制造出
    「查得到但归属错误」的数据。
    """
    db = Path("/tmp/uid_mig_norename.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    _build_old_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chat_turns (user_id, thread_key, turn, role, content, ts_ms) "
        "VALUES ('default', 'k', 1, 'user', 'hi', 0)"
    )
    conn.commit()
    conn.close()

    assert _ConnectionHub(path=db).migrate() is True
    conn = sqlite3.connect(str(db))
    rows = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM chat_turns")}
    conn.close()
    assert "admin" not in rows, "存量 default 行被谎报成了 admin"
    assert "default" in rows


def test_schema_version_unchanged_after_migration():
    """迁移不该顺手改版本号 —— 版本号是给人看的，migrate() 自己会写它。"""
    db = Path("/tmp/uid_mig_ver.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    _build_old_schema(db)
    hub = _ConnectionHub(path=db)
    assert hub.migrate() is True
    assert hub.schema_version() == SCHEMA_VERSION
