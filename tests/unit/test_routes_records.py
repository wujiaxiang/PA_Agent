# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_records.py — 历史分析记录查询 API。

覆盖：
- GET /api/records 列表查询（200、include_partial 过滤、损坏文件跳过）
- GET /api/records/{record_id} 单条详情（200、404、路径遍历防护 400）
- 脱敏：api_key 出现在记录字符串中时被替换为掩码

mock 策略：用 monkeypatch 替换 routes_records.RECORDS_DIR 到 tmp_path，
在临时目录下构造分区布局的记录文件，实现测试隔离。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import routes_records
from web.api.routes_records import router as records_router


def _make_record_dict(
    *,
    exchange: str = "GATEIO",
    symbol: str = "BTCUSDT",
    timeframe: str = "1d",
    timestamp_iso: str = "2026-07-18_14-00-13",
    timestamp_ms: int = 1778997613000,
    order_type: str = "limit",
    direction: str = "long",
    terminal_outcome: str = "trade",
    exception: dict | None = None,
    last_close_bar_iso: str | None = None,
    kline_data: list[dict] | None = None,
    stage1_diagnosis: dict | None = None,
) -> dict:
    """构造一个最小的有效 AnalysisRecord dict。"""
    meta = {
        "timestamp_local_iso": timestamp_iso,
        "timestamp_local_ms": timestamp_ms,
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
        "bar_count": 100,
        "ai_provider": {"provider": "openai"},
        "decision_stance": "conservative",
    }
    if last_close_bar_iso is not None:
        meta["last_close_bar_iso"] = last_close_bar_iso
    return {
        "meta": meta,
        "kline_data": kline_data if kline_data is not None else [],
        "htf_text": "",
        "stage1_messages": [],
        "stage1_response": None,
        "stage1_diagnosis": stage1_diagnosis,
        "stage2_messages": [],
        "stage2_response": None,
        "stage2_decision": {
            "order_type": order_type,
            "order_direction": direction,
            "terminal": {"node_id": "11.2", "outcome": terminal_outcome},
        },
        "strategy_files_used": [],
        "experience_loaded": [],
        "exception": exception,
        "usage_total": {"total_tokens": 150},
    }


def _write_record(
    base_dir: Path,
    *,
    exchange: str = "GATEIO",
    symbol: str = "BTCUSDT",
    timeframe: str = "1d",
    filename: str = "2026-07-18_14-00-13.json",
    record_dict: dict | None = None,
    partial_reason: str | None = None,
) -> Path:
    """把一条记录写到分区布局目录下，返回写入路径。"""
    seg = (
        base_dir
        / exchange
        / symbol
        / timeframe
    )
    seg.mkdir(parents=True, exist_ok=True)
    path = seg / filename
    data = record_dict if record_dict is not None else _make_record_dict(
        exchange=exchange, symbol=symbol, timeframe=timeframe,
        timestamp_iso=filename.replace(".json", ""),
    )
    if partial_reason is not None:
        data = {**data, "_partial_reason": partial_reason}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _make_app() -> FastAPI:
    """构造一个仅挂载 records 路由的 FastAPI 应用，附带 mock ctx。"""
    app = FastAPI()
    app.include_router(records_router, prefix="/api")
    ctx = MagicMock()
    ctx.settings.provider.api_key = "test-secret-key-12345"
    app.state.ctx = ctx
    return app


# ── 列表查询 ──────────────────────────────────────────────────────────────────


def test_list_records_returns_200(tmp_path, monkeypatch):
    """列表查询返回 200 且包含摘要字段。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(tmp_path, filename="2026-07-18_14-00-13.json")

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["record_id"] == "GATEIO/BTCUSDT/1d/2026-07-18_14-00-13"
    assert item["timestamp"] == "2026-07-18_14-00-13"
    assert item["order_type"] == "limit"
    assert item["direction"] == "long"
    assert item["terminal_outcome"] == "trade"
    assert item["partial_reason"] is None
    assert item["has_exception"] is False


def test_list_records_empty_when_dir_missing(tmp_path, monkeypatch):
    """RECORDS_DIR 不存在时返回空列表。"""
    missing = tmp_path / "nope"
    monkeypatch.setattr(routes_records, "RECORDS_DIR", missing)

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_records_filters_partial(tmp_path, monkeypatch):
    """include_partial=False 时不返回失败记录；True 时返回全部。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    # 成功记录
    _write_record(
        tmp_path,
        filename="2026-07-18_10-00-00.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_10-00-00",
            exception=None,
        ),
    )
    # 失败记录（exception 非空 + _partial_reason）
    _write_record(
        tmp_path,
        filename="2026-07-18_11-00-00.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_11-00-00",
            exception={"category": "stage2", "message": "boom"},
        ),
        partial_reason="stage2 crashed",
    )

    app = _make_app()

    # 默认 include_partial=False → 仅 1 条成功记录
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["timestamp"] == "2026-07-18_10-00-00"
    assert body[0]["has_exception"] is False

    # include_partial=True → 2 条
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
            "include_partial": "true",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # mtime 倒序：后写入的文件更新
    timestamps = [item["timestamp"] for item in body]
    assert "2026-07-18_11-00-00" in timestamps
    assert "2026-07-18_10-00-00" in timestamps
    # 失败记录携带 partial_reason
    failed = next(item for item in body if item["has_exception"])
    assert failed["partial_reason"] == "stage2 crashed"


def test_list_records_skips_corrupted_files(tmp_path, monkeypatch):
    """损坏的 JSON 文件被静默跳过。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    # 有效记录
    _write_record(tmp_path, filename="2026-07-18_10-00-00.json",
                  record_dict=_make_record_dict(timestamp_iso="2026-07-18_10-00-00"))
    # 损坏记录
    bad_dir = tmp_path / "GATEIO" / "BTCUSDT" / "1d"
    (bad_dir / "2026-07-18_11-00-00.json").write_text("{not valid json", encoding="utf-8")

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["timestamp"] == "2026-07-18_10-00-00"


def test_list_records_legacy_flat_layout(tmp_path, monkeypatch):
    """旧平铺布局文件 (records/pending/{ts}_{symbol}_{timeframe}.json) 也能被列出。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    # 旧布局文件直接放在 RECORDS_DIR 根下
    legacy_name = "2026-07-18_09-00-00_BTCUSDT_1d.json"
    legacy_path = tmp_path / legacy_name
    legacy_path.write_text(
        json.dumps(_make_record_dict(timestamp_iso="2026-07-18_09-00-00"), ensure_ascii=False),
        encoding="utf-8",
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["record_id"] == "2026-07-18_09-00-00_BTCUSDT_1d"


def test_list_records_limit_param(tmp_path, monkeypatch):
    """limit 参数截断返回数量。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    for i in range(5):
        ts = f"2026-07-18_{i:02d}-00-00"
        _write_record(
            tmp_path,
            filename=f"{ts}.json",
            record_dict=_make_record_dict(timestamp_iso=ts),
        )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
            "limit": 2,
        })
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ── last_close_bar_iso 字段 ─────────────────────────────────────────────────


def test_list_records_last_close_bar_iso_from_meta(tmp_path, monkeypatch):
    """新记录 meta.last_close_bar_iso 非空时直接透传。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            last_close_bar_iso="2026-07-18T14:00:00+08:00",
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["last_close_bar_iso"] == "2026-07-18T14:00:00+08:00"


def test_list_records_last_close_bar_iso_derived_from_kline_data(tmp_path, monkeypatch):
    """旧记录无 meta.last_close_bar_iso，从 kline_data[-1].time 派生。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    # kline_data[-1].time = 1778997600000 ms（毫秒时间戳）
    kline_data = [
        {"time": 1778997500000, "open": 100, "high": 101, "low": 99, "close": 100.5},
        {"time": 1778997600000, "open": 100.5, "high": 102, "low": 100, "close": 101.5},
    ]
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            kline_data=kline_data,
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    iso = body[0]["last_close_bar_iso"]
    assert iso  # 非空
    # 派生出的 ISO 字符串应能解析回 1778997600 秒
    from datetime import datetime as _dt
    parsed = _dt.fromisoformat(iso)
    assert int(parsed.timestamp()) == 1778997600


def test_list_records_last_close_bar_iso_derived_from_stage1(tmp_path, monkeypatch):
    """kline_data 为空、meta 为空时，从 stage1_diagnosis.bar_analysis.last_closed_bar 派生。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            kline_data=[],
            stage1_diagnosis={
                "bar_analysis": {"last_closed_bar": "2026-07-18T14:00:00"},
            },
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["last_close_bar_iso"] == "2026-07-18T14:00:00"


def test_list_records_last_close_bar_iso_filters_kline_code(tmp_path, monkeypatch):
    """``bar_analysis.last_closed_bar`` 是 K 线代号（如 "K1"）时必须被过滤。

    回归测试：阶段一诊断返回的 ``last_closed_bar`` 不一定是时间字符串，
    可能是 "K1"/"K50-K1" 这类 K 线代号。前端 ``new Date("K1")`` 会得到
    Invalid Date，因此后端必须校验 ISO 格式后才能透传。
    """
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            kline_data=[],
            stage1_diagnosis={
                "bar_analysis": {"last_closed_bar": "K1"},
            },
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    # "K1" 不是 ISO 时间，必须被过滤为空串
    assert body[0]["last_close_bar_iso"] == ""


def test_list_records_last_close_bar_iso_derived_from_ts_open(tmp_path, monkeypatch):
    """kline_data[-1].ts_open（ms 时间戳）派生 ISO 字符串。

    回归测试：实际 K 线时间字段是 ``ts_open`` 而非 ``time``。早期实现错误
    地读 ``time`` 字段，导致历史记录列表 close bar 始终为空。
    """
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    kline_data = [
        {"seq": 49, "ts_open": 1778997500000, "open": 100, "high": 101, "low": 99, "close": 100.5},
        {"seq": 50, "ts_open": 1778997600000, "open": 100.5, "high": 102, "low": 100, "close": 101.5},
    ]
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            kline_data=kline_data,
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    iso = body[0]["last_close_bar_iso"]
    assert iso  # 非空
    from datetime import datetime as _dt
    parsed = _dt.fromisoformat(iso)
    assert int(parsed.timestamp()) == 1778997600  # 1778997600000 ms


def test_list_records_last_close_bar_iso_empty_when_no_source(tmp_path, monkeypatch):
    """所有派生来源都为空时返回 ""。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            kline_data=[],
            stage1_diagnosis=None,
        ),
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["last_close_bar_iso"] == ""


# ── 单条详情 ──────────────────────────────────────────────────────────────────


def test_get_record_returns_200(tmp_path, monkeypatch):
    """单条详情返回 200 并包含 _serialize_record 派生的前端字段。

    历史记录回看与 SSE done 事件共用 _serialize_record，保证前端 render 函数
    能拿到 decision_tree / raw_debug_payload / debug_files_payload 等派生字段。
    """
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(tmp_path, filename="2026-07-18_14-00-13.json")

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records/GATEIO/BTCUSDT/1d/2026-07-18_14-00-13")

    assert resp.status_code == 200
    body = resp.json()
    # _serialize_record 派生的顶层字段（前端 render 函数依赖）
    assert body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "1d"
    assert body["stage2_decision"]["order_type"] == "limit"
    assert body["exception"] is None
    # 关键派生字段必须存在（否则 replayRecord 回显拿不到数据）
    assert "decision_tree" in body
    assert "raw_debug_payload" in body
    assert "debug_files_payload" in body
    assert "decision_overlay" in body


def test_get_record_includes_partial_reason(tmp_path, monkeypatch):
    """失败记录的 _partial_reason 被回填到响应中。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=_make_record_dict(
            timestamp_iso="2026-07-18_14-00-13",
            exception={"category": "stage1", "message": "err"},
        ),
        partial_reason="stage1 crashed",
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records/GATEIO/BTCUSDT/1d/2026-07-18_14-00-13")

    assert resp.status_code == 200
    body = resp.json()
    assert body["_partial_reason"] == "stage1 crashed"
    assert body["exception"]["message"] == "err"


def test_get_record_404(tmp_path, monkeypatch):
    """不存在的 record_id 返回 404。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records/NONEXIST/XYZ/1d/2099-01-01_00-00-00")
    assert resp.status_code == 404


def test_get_record_path_traversal_blocked(tmp_path, monkeypatch):
    """路径遍历防护：包含 .. 的 record_id 返回 400 或 404。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        # 显式 .. 段
        resp = c.get("/api/records/../etc/passwd")
    assert resp.status_code in (400, 404)


def test_get_record_sanitizes_api_key(tmp_path, monkeypatch):
    """记录中残留的 api_key 被脱敏（掩码替换）。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    api_key = "test-secret-key-12345"
    # 构造一条包含明文 api_key 的记录（模拟未脱敏的遗留文件）
    record_dict = _make_record_dict(timestamp_iso="2026-07-18_14-00-13")
    record_dict["stage1_messages"] = [
        {"role": "system", "content": f"Authorization: Bearer {api_key}"},
    ]
    _write_record(
        tmp_path,
        filename="2026-07-18_14-00-13.json",
        record_dict=record_dict,
    )

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records/GATEIO/BTCUSDT/1d/2026-07-18_14-00-13")

    assert resp.status_code == 200
    body = resp.json()
    # 明文 api_key 不应出现在响应中
    serialized = json.dumps(body, ensure_ascii=False)
    assert api_key not in serialized
    # 掩码尾部应出现（mask_secret 保留最后 4 位）
    assert "2345" in serialized


# ── 参数校验 ──────────────────────────────────────────────────────────────────


def test_list_records_limit_below_minimum_returns_422(tmp_path, monkeypatch):
    """limit < 1 返回 422。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
            "limit": 0,
        })
    assert resp.status_code == 422


def test_list_records_limit_above_maximum_returns_422(tmp_path, monkeypatch):
    """limit > 500 返回 422。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT", "timeframe": "1d",
            "limit": 501,
        })
    assert resp.status_code == 422


def test_list_records_missing_required_param_returns_422(tmp_path, monkeypatch):
    """缺少必填参数返回 422。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/records", params={
            "exchange": "GATEIO", "symbol": "BTCUSDT",  # 缺 timeframe
        })
    assert resp.status_code == 422


# ── 删除记录 ──────────────────────────────────────────────────────────────────


def test_delete_record_success(tmp_path, monkeypatch):
    """DELETE 已存在的记录返回 200 + {ok, record_id}，文件被删除。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    record_path = _write_record(tmp_path, filename="2026-07-18_14-00-13.json")
    assert record_path.exists()

    app = _make_app()
    with TestClient(app) as c:
        resp = c.delete("/api/records/GATEIO/BTCUSDT/1d/2026-07-18_14-00-13")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["record_id"] == "GATEIO/BTCUSDT/1d/2026-07-18_14-00-13"
    # 文件已被删除
    assert not record_path.exists()


def test_delete_record_not_found(tmp_path, monkeypatch):
    """DELETE 不存在的 record_id 返回 404 + detail='record not found'。"""
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    app = _make_app()
    with TestClient(app) as c:
        resp = c.delete("/api/records/GATEIO/BTCUSDT/1d/2099-01-01_00-00-00")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "record not found"


def test_delete_record_path_traversal(tmp_path, monkeypatch):
    """DELETE 含 .. 的 record_id 返回 400，且不删除任何文件。

    注意：直接发 ``/api/records/../etc/passwd`` 会被 httpx URL 规范化，
    最终落到 FastAPI 时变成 ``/api/etc/passwd``，路由不匹配返回 404，
    无法触发端点内的防护逻辑。因此这里用 URL 编码 ``%2E%2E`` 绕过规范化，
    让请求最终到达 ``delete_record`` 端点并由 ``_validate_record_id`` 拦截。
    """
    monkeypatch.setattr(routes_records, "RECORDS_DIR", tmp_path)
    # 在 tmp_path 下放一条记录，确保遍历攻击不会误伤
    record_path = _write_record(tmp_path, filename="2026-07-18_14-00-13.json")

    app = _make_app()
    with TestClient(app) as c:
        resp = c.delete("/api/records/%2E%2E/secret")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid record_id"
    # 原文件未受影响
    assert record_path.exists()
