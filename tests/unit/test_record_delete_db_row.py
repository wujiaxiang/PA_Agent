# -*- coding: utf-8 -*-
"""``DELETE /api/records/{id}`` 必须同时删掉**磁盘文件与 SQLite 索引行**。

缺口本体：路由里**一次都没调用过仓储删除**，于是 ``DELETE`` 只删文件。
读取侧靠 ``_db_candidates`` 里的 ``f.is_file()`` 自愈，所以界面看不出异常 ——
行却在库里永久泄漏，且只增不减。

守卫的是三条不变式，各自有对应用例：

1. **两边都删** —— ``test_delete_removes_file_and_db_row``。
2. **文件删不掉时库行必须保留**（先删文件、后删索引行）——
   ``test_unlink_failure_keeps_db_row``。反过来先删库行，失败时会留下一个
   用户看得见、又删不掉的记录（文件还在、索引没了，列表接口回退全盘扫描
   又把它捞回来），比一行静默泄漏糟糕得多。
3. **索引主键是文件 basename，不是 URL 里的 record_id** ——
   ``test_delete_keys_db_row_by_file_stem``。用错键会「一行都没删掉」且
   **不报任何错**，正是本缺口同款。

另有一条「不要好心」的守卫：``test_delete_does_not_cascade``。

**绝不写真实目录**：``RECORDS_DIR`` 与 hub 全部重定向到 ``tmp_path``。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.records.schema import AnalysisRecord
from pa_agent.storage.db import get_hub, reset_hub_for_tests
from pa_agent.storage.repositories import delete_record as repo_delete
from web.api import routes_records
from web.api.routes_records import router as records_router

RID = "GATEIO/BTCUSDT/1d/2026-07-18_14-00-13"
STEM = "2026-07-18_14-00-13"


@pytest.fixture()
def db(tmp_path: Path):
    """Hub 指向 tmp_path。收尾还原到会话级 DB（理由同 test_trade_repo）。"""
    hub = reset_hub_for_tests(tmp_path / "records.db")
    yield hub
    hub.close_all()
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


@pytest.fixture()
def records_dir(tmp_path: Path, monkeypatch) -> Path:
    """把路由的 RECORDS_DIR 指到 tmp_path —— 测试绝不碰真实 ``records/``。"""
    d = tmp_path / "pending"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routes_records, "RECORDS_DIR", d)
    return d


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(records_router, prefix="/api")
    ctx = MagicMock()
    ctx.settings.provider.api_key = "test-secret-key-12345"
    app.state.ctx = ctx
    return app


def _record_dict() -> dict:
    return {
        "meta": {
            "timestamp_local_iso": "2026-07-18T14:00:13",
            "timestamp_local_ms": 1778997613000,
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
            "bar_count": 100,
            "ai_provider": {"provider": "openai"},
            "decision_stance": "conservative",
        },
        "kline_data": [], "htf_text": "",
        "stage1_messages": [], "stage1_response": None, "stage1_diagnosis": None,
        "stage2_messages": [], "stage2_response": None, "stage2_decision": None,
        "strategy_files_used": [], "experience_loaded": [], "exception": None,
        "usage_total": {
            "prompt_tokens": 0, "cached_prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
        },
    }


def _seed(records_dir: Path, *, index: bool = True, stem: str = STEM) -> Path:
    """写一条记录文件，并按 ``upsert_record`` 的真实键规则写入索引行。"""
    path = records_dir / "GATEIO" / "BTCUSDT" / "1d" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _record_dict()
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    if index:
        from pa_agent.storage.repositories import upsert_record

        upsert_record(AnalysisRecord.model_validate(raw), raw=raw, file_path=path)
    return path


def _rows() -> list[dict]:
    return [dict(r) for r in get_hub().query("SELECT record_id FROM analysis_records")]


# ── 1. 两边都删 ──────────────────────────────────────────────────────────────

def test_delete_removes_file_and_db_row(db, records_dir):
    path = _seed(records_dir)
    assert _rows() == [{"record_id": STEM}]

    with TestClient(_make_app()) as c:
        resp = c.delete(f"/api/records/{RID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["record_id"] == RID
    assert body["db_deleted"] is True, "库行必须真的被删掉（缺口本体）"
    assert not path.exists()
    assert _rows() == [], "analysis_records 行仍在泄漏"


def test_delete_keys_db_row_by_file_stem(db, records_dir):
    """主键是 ``Path.stem``，不是 URL 里的 ``exchange/symbol/timeframe/stem``。

    错键的后果是「DELETE 静默无效」：``execute`` 不报告命中行数，
    返回 True、行还在，界面因 ``is_file()`` 自愈看不出任何异常。
    """
    path = _seed(records_dir)
    assert _rows() == [{"record_id": STEM}]

    with TestClient(_make_app()) as c:
        assert c.delete(f"/api/records/{RID}").json()["db_deleted"] is True

    assert not path.exists()
    assert _rows() == [], "用了 URL record_id 当键：一行都没删掉且不报错"


# ── 2. 文件删不掉 → 库行必须保留 ────────────────────────────────────────────

def test_unlink_failure_keeps_db_row(db, records_dir, monkeypatch):
    """``unlink`` 抛错 → 500，且**索引行原样保留**。

    这是「先删文件」的核心理由：先删库行的话，失败后库里会指向一个删不掉
    的文件，用户看到的就是一条反复点删除、反复失败的记录。
    """
    path = _seed(records_dir)

    def boom(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", boom)

    with TestClient(_make_app()) as c:
        resp = c.delete(f"/api/records/{RID}")

    assert resp.status_code == 500
    assert "Failed to delete record" in resp.json()["detail"]
    monkeypatch.undo()
    assert path.exists(), "文件没删掉"
    assert _rows() == [{"record_id": STEM}], "库行被删了：库里指向一个不存在的文件"


# ── 3. 库行删失败不得把已完成的删除报成失败 ──────────────────────────────────

def test_db_row_delete_failure_still_returns_ok(db, records_dir, monkeypatch):
    """磁盘已删、索引删失败 → 仍 200 + ``db_deleted: false``，绝不 500。

    返回 500 会诱导用户重试，而重试只会拿到 404 —— 把「已完成 90% 的删除」
    报成失败比留一行死索引糟糕得多。
    """
    path = _seed(records_dir)

    def boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(routes_records, "logger", MagicMock())
    import pa_agent.storage.repositories as repos

    monkeypatch.setattr(repos, "delete_record", boom)

    with TestClient(_make_app()) as c:
        resp = c.delete(f"/api/records/{RID}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "record_id": RID, "db_deleted": False}
    assert not path.exists()
    assert _rows() == [{"record_id": STEM}], "（模拟的失败让行还在，属预期）"


def test_missing_file_returns_404_without_touching_db(db, records_dir):
    """文件不存在 → 404，且**不动索引行**。

    不在 404 路径上顺手删行：``record_id`` 只是 basename，不同分区可能同名，
    拿一个不存在的文件去按名删行等于开了一条「误删他人记录」的新路径。
    """
    _seed(records_dir, stem="keep-me")
    with TestClient(_make_app()) as c:
        resp = c.delete("/api/records/GATEIO/BTCUSDT/1d/2099-01-01_00-00-00")

    assert resp.status_code == 404
    assert _rows() == [{"record_id": "keep-me"}], "404 路径误删了别人的行"


# ── 4. 不要凭空造级联 ───────────────────────────────────────────────────────

def test_delete_does_not_cascade(db, records_dir):
    """删记录**不得**连带删 ``experience_entries`` / ``trade_records``。

    schema 里没有任何 ``FOREIGN KEY``，且两张表都没有 record_id 列 ——
    经验条目主键是 ``<user_id>_<文件 stem>``、交易主键是 ``sha256``，
    与 ``analysis_records`` 之间**不存在可 join 的键**。经验库更是跨会话共享的
    L2 资产（A tab 的案例 B tab 要读得到），按文件名模糊匹配去删只会误伤。
    """
    _seed(records_dir)
    get_hub().execute(
        "INSERT INTO experience_entries (entry_id, user_id, status, timestamp_ms, "
        "content_json, created_at, updated_at) "
        "VALUES ('admin_some_case', 'admin', 'success', 1, '{}', 1, 1)"
    )
    get_hub().execute(
        "INSERT INTO trade_records (trade_id, user_id, symbol, payload_json, created_at) "
        "VALUES ('deadbeef', 'admin', 'BTCUSDT', '{}', 1)"
    )
    # 先断言基线：两行确实在。否则本测试可能因 INSERT 失败而「空过」。
    assert get_hub().query("SELECT entry_id FROM experience_entries")
    assert get_hub().query("SELECT trade_id FROM trade_records")

    with TestClient(_make_app()) as c:
        assert c.delete(f"/api/records/{RID}").status_code == 200

    assert _rows() == []
    assert get_hub().query("SELECT entry_id FROM experience_entries")
    assert get_hub().query("SELECT trade_id FROM trade_records"), \
        "级联删了共享的经验/交易数据 —— 当前 schema 没有任何外键支撑这种删除"


def test_repository_delete_is_actually_called(db, records_dir, monkeypatch):
    """守卫「路由真的调了仓储」—— 缺口本体就是「调用次数为 0」。

    直接断言调用参数，免得将来重构时把 ``target.stem`` 写回 ``record_id``
    而没有任何测试变红。
    """
    _seed(records_dir)
    seen: list[str] = []
    import pa_agent.storage.repositories as repos

    real = repos.delete_record

    def spy(record_id, **kw):
        seen.append(record_id)
        return real(record_id, **kw)

    monkeypatch.setattr(repos, "delete_record", spy)

    with TestClient(_make_app()) as c:
        c.delete(f"/api/records/{RID}")

    assert seen == [STEM], f"仓储删除未被调用，或用了错误的键：{seen}"
    assert repo_delete is not None  # 导入的符号仍在用
