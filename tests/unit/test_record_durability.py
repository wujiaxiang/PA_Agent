"""Regression tests for record-filename collisions and trade-CSV durability."""
from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from pa_agent.records.pending_writer import PendingWriter, _build_basename, _build_record_path


def _record_with_ms(ts_ms: int, exchange="GATEIO", symbol="BTCUSDT", timeframe="15m"):
    """A schema-valid record whose meta carries *ts_ms* (for path uniqueness)."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    meta = RecordMeta(
        timestamp_local_iso="2026-10-03T12:00:00+08:00",
        timestamp_local_ms=ts_ms,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        bar_count=100,
        ai_provider={"model": "test"},
    )
    from tests.unit.test_pending_writer_sanitize import _make_record  # noqa: F401

    base = _make_record("")
    return base.model_copy(update={"meta": meta})


# ── filename uniqueness ───────────────────────────────────────────────────────


def test_same_second_records_get_distinct_filenames():
    """Two analyses finishing in the same second must not collide."""
    base = 1_760_000_000_000
    a = _build_record_path(_record_with_ms(base), Path("records"))
    b = _build_record_path(_record_with_ms(base + 1), Path("records"))
    assert a != b, f"collision: {a.name} == {b.name}"


def test_basename_keeps_human_sortable_prefix():
    rec = _record_with_ms(1_760_000_000_123)
    name = _build_basename(rec)
    assert name.startswith("2025-") or name.startswith("2026-")
    assert "_123_" in name, name  # millisecond component preserved


def test_record_write_is_atomic_no_temp_left_behind(tmp_path):
    """A successful write must leave no stray temp files in the record dir."""
    rec = _record_with_ms(1_760_000_000_500)
    writer = PendingWriter(pending_dir=tmp_path, event_bus=None, api_key="")
    written = _build_record_path(rec, tmp_path)
    writer._write_json(written, {"ok": True})
    assert written.exists()
    assert written.read_text(encoding="utf-8") == '{\n  "ok": true\n}'
    leftovers = [p.name for p in written.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temp files left behind: {leftovers}"


# ── trade CSV durability ──────────────────────────────────────────────────────


def _trade_kwargs():
    return {
        "decision_inner": {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 84835.6,
            "stop_loss_price": 84826.6,
            "take_profit_price": 84917.9,
            "trade_confidence": 58,
            "estimated_win_rate": "0.55",
        },
        "stage2_full": {
            "diagnosis_summary": {},
            "bar_analysis": {},
            "terminal": {},
            "next_cycle_prediction": {},
            "decision_trace": [],
        },
        "stage1_diagnosis": None,
        "frame": None,
        "meta_symbol": "BTCUSDT",
        "meta_timeframe": "15m",
        "decision_stance": "balanced",
        "model_name": "m",
        "structure_flip_cooldown_bars": 3,
    }


def test_concurrent_trade_records_are_all_preserved(tmp_path, monkeypatch):
    """25 simultaneous saves must yield 25 rows (was lossy read-modify-write)."""
    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    errors: list[BaseException] = []

    def save() -> None:
        try:
            tl.save_trade_record(**_trade_kwargs())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    csv_path = tmp_path / "BTCUSDT_15m.csv"
    assert csv_path.exists()
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 25, f"lost rows: {len(rows)}/25"


def test_csv_header_written_exactly_once(tmp_path, monkeypatch):
    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    for _ in range(5):
        tl.save_trade_record(**_trade_kwargs())
    text = (tmp_path / "BTCUSDT_15m.csv").read_text(encoding="utf-8-sig")
    assert text.count("record_time,symbol") == 1


def test_append_preserves_existing_rows(tmp_path, monkeypatch):
    """Appending must not drop earlier rows (the old 'w' mode truncated first)."""
    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    tl.save_trade_record(**_trade_kwargs())
    first = (tmp_path / "BTCUSDT_15m.csv").read_text(encoding="utf-8-sig")

    kwargs = _trade_kwargs()
    kwargs["decision_inner"] = {**kwargs["decision_inner"], "entry_price": 85000.0}
    tl.save_trade_record(**kwargs)

    with (tmp_path / "BTCUSDT_15m.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert "84835.6" in rows[0]["entry_price"]
    assert "85000.0" in rows[1]["entry_price"]
    assert first  # original content still present