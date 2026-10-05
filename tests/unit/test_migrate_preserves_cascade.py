"""迁移**不得**删掉级联子表的行。

**回归守卫**：``db.connect()`` 开了 ``PRAGMA foreign_keys=ON``，而
``_rebuild_without_default_user_id`` 要 ``DROP TABLE`` 重建父表 —— SQLite 把
DROP TABLE 当作删除全部行，``ON DELETE CASCADE`` 于是把子表**整个清空**。
实测：重建 ``experience_entries`` 会静默删光 ``experience_reviews``，而
``migrate()`` 照样返回 True —— 数据没了，日志里一行警告都没有。

触发条件是「该表 user_id 仍带 DEFAULT 'default'」，即**尚未跑过迁移的存量库**。
生产库已迁移过所以当下安全，但风险窗口一直开着：老容器、老备份、CI 里的旧
fixture，以及任何将来需要重建的迁移。

本文件从**老库**出发验证 —— 现有测试只测「新库跑两次」，从未验证过升级路径，
上一轮「列没建出来」正是因此溜过去的。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pa_agent.storage.db import _ConnectionHub
from pa_agent.storage.schema import create_table_ddl


def _make_legacy_db(path: Path) -> None:
    """造一个 user_id 仍带 ``DEFAULT 'default'`` 的老库（会触发重建）。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 当前 DDL 的 user_id 早已没有默认值（迁移跑过），必须显式造回「未迁移」
    # 的形态 —— 否则重建路径根本不会被触发，测试静默地什么都没验。
    conn.execute(create_table_ddl("experience_entries").replace(
        "user_id       TEXT NOT NULL,",
        "user_id       TEXT NOT NULL DEFAULT 'default',", 1))
    dflt = conn.execute(
        "SELECT dflt_value FROM pragma_table_info('experience_entries') "
        "WHERE name='user_id'").fetchone()[0]
    assert dflt == "'default'", f"老库没造出来（dflt_value={dflt!r}），重建不会触发"
    conn.execute(create_table_ddl("experience_reviews"))
    conn.execute(
        "INSERT INTO experience_entries (entry_id,user_id,cycle_position,status,"
        "symbol,timeframe,content_json,created_at,updated_at,timestamp_ms) "
        "VALUES ('e1','default','trending_tr','win','BTC','1h','{}',1.0,1.0,1)")
    conn.execute(
        "INSERT INTO experience_reviews (entry_id,user_id,verdict,"
        "reusable_criteria,payload_json,created_at) "
        "VALUES ('e1','default','判断成立但运气不佳','判据','{}',1.0)")
    conn.commit()
    conn.close()


@pytest.fixture()
def migrated(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    hub = _ConnectionHub(db)
    assert hub.migrate() is True, "老库升级必须成功"
    yield hub
    hub._disable("test")
    hub.close_all()


def _counts(hub):
    row = hub.connect().execute(
        "SELECT (SELECT COUNT(*) FROM experience_entries),"
        " (SELECT COUNT(*) FROM experience_reviews)").fetchone()
    return row[0], row[1]


def test_rebuild_does_not_cascade_delete_child_rows(migrated):
    entries, reviews = _counts(migrated)
    assert entries == 1
    assert reviews == 1, f"复盘数据被级联删除，只剩 {reviews} 行"


def test_child_row_content_survives(migrated):
    row = migrated.connect().execute(
        "SELECT verdict, reusable_criteria FROM experience_reviews").fetchone()
    assert row["verdict"] == "判断成立但运气不佳"
    assert row["reusable_criteria"] == "判据"


def test_foreign_keys_are_restored_after_rebuild(migrated):
    """关掉是为了保护数据，**用完必须开回来** —— 留着 OFF 等于全局失去级联保护。"""
    assert migrated.connect().execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_is_idempotent(migrated):
    before = _counts(migrated)
    assert migrated.migrate() is True
    assert _counts(migrated) == before
