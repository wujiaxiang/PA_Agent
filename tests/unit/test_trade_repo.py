"""``trade_records`` 仓储 / 导入器 / 双写钩子的守卫测试。

四条不可回退的约定，各自都有对应用例：

1. **CSV 是权威副本**，DB 只是索引 —— 用例断言「删掉 DB 可从 CSV 幂等重建」
   （``test_import_is_idempotent``）与「DB 故障不阻断 CSV 落盘」
   （``test_index_failure_does_not_block_csv``）。
2. **``trade_id`` 不能用时间戳** —— ``record_time`` 只有秒级精度，同标的同秒两笔
   会撞 PRIMARY KEY。``test_same_second_rows_do_not_collide`` 是这条的回归守卫。
3. **行号定义必须两条路径一致** —— 写入端（trade_logger 追加）与导入端（扫 CSV）
   必须算出同一个 ``trade_id``，否则每次导入都给已双写的行再造副本
   （``test_live_writes_then_import_never_duplicates``）。
4. **坏文件不中断** —— 空文件 / 缺列 / 二进制乱码一律跳过并计数
   （``test_import_skips_unusable_files``）。

**绝不写真实目录**：全部用 ``tmp_path``，``_TRADE_RECORDS_DIR`` 一律 monkeypatch。
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from pa_agent.records.trade_logger import _CSV_FIELDNAMES
from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, reset_hub_for_tests
from pa_agent.storage.importer import import_trade_records
from pa_agent.storage.trade_repo import (
    count_trades,
    delete_trade,
    get_trade,
    list_trades,
    make_trade_id,
    parse_price,
    upsert_trade_row,
)


@pytest.fixture()
def db(tmp_path: Path):
    """Hub 指向 tmp_path。收尾必须还原到会话级 DB —— 否则后续测试会连到一个
    已被 pytest 删掉的路径，``sqlite3.connect`` 会静默重建一个**空库**，
    于是所有人的 ``no such table`` 都被算到这次改动头上。"""
    hub = reset_hub_for_tests(tmp_path / "trades.db")
    yield hub
    hub.close_all()
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


# ── 夹具数据 ──────────────────────────────────────────────────────────────────

def _row(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    record_time: str = "2026-10-05 06:29:01",
    **over: object,
) -> dict[str, str]:
    """一行与 ``trade_logger`` 实际写出的 CSV 完全同构的数据。"""
    row = {k: "" for k in _CSV_FIELDNAMES}
    row.update(
        {
            "record_time": record_time,
            "symbol": symbol,
            "timeframe": timeframe,
            "decision_stance": "balanced",
            "model": "stealth/space-bunny-alpha",
        }
    )
    row.update({k: str(v) for k, v in over.items()})
    return row


def _write_csv(
    directory: Path,
    rows: list[dict[str, str]],
    name: str = "BTCUSDT_1h.csv",
    *,
    mode: str = "a",
) -> Path:
    """按 trade_logger 的方式落盘（utf-8-sig + 表头一次）。``mode="w"`` = 覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, mode, newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
        if mode == "a" and path.stat().st_size > 0:
            pass                      # 追加时表头已写过
        else:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in _CSV_FIELDNAMES})
    return path


def _trade_kwargs(entry: float = 84835.6, symbol: str = "BTCUSDT"):
    return {
        "decision_inner": {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": entry,
            "stop_loss_price": 84826.6,
            "take_profit_price": 84917.9,
            "trade_confidence": 58,
        },
        "stage2_full": {
            "diagnosis_summary": {},
            "bar_analysis": {},
            "terminal": {},
            "next_cycle_prediction": {},
            "decision_trace": [],
        },
        "stage1_diagnosis": None,
        "frame": None,          # 不画图 → 不依赖 matplotlib，快且不落 PNG
        "meta_symbol": symbol,
        "meta_timeframe": "15m",
        "decision_stance": "balanced",
        "model_name": "m",
        "structure_flip_cooldown_bars": 3,
    }


# ── 1. 写入 + 按 user_id 读回 ────────────────────────────────────────────────

def test_upsert_then_read_back_by_user_id(db, tmp_path):
    csv_path = tmp_path / "BTCUSDT_1h.csv"
    trade_id = upsert_trade_row(
        _row(
            order_type="限价单",
            entry_price="233.6",
            stop_loss_price="229.32",
            take_profit_price="237.88",
            reasoning="阶段一为趋势型交易区间",
        ),
        csv_path=csv_path,
        row_no=1,
    )
    assert trade_id, "写入必须成功"

    got = get_trade(trade_id)
    assert got is not None
    assert got["user_id"] == DEFAULT_USER_ID == "admin", "user_id 真源是 admin，不是 'default'"
    assert (got["symbol"], got["timeframe"], got["order_type"]) == ("BTCUSDT", "1h", "限价单")
    assert (got["entry_price"], got["sl_price"], got["tp_price"]) == (233.6, 229.32, 237.88)
    assert got["csv_path"] == str(csv_path)
    assert got["payload"]["reasoning"] == "阶段一为趋势型交易区间", "大字段留在 payload_json"

    assert [r["trade_id"] for r in list_trades()] == [trade_id]
    assert [r["trade_id"] for r in list_trades(symbol="BTCUSDT", timeframe="1h")] == [trade_id]
    assert list_trades(symbol="NVDA") == [], "过滤条件必须真的生效"
    assert count_trades() == 1

    # L2 用户级隔离：别的 user 看不到
    assert list_trades(user_id="someone-else") == []
    assert get_trade(trade_id, user_id="someone-else") is None
    assert count_trades(user_id="someone-else") == 0


def test_created_at_comes_from_record_time_not_import_day(db, tmp_path):
    """历史导入必须按**交易时刻**排序，不是按「今天才被导入」排序。"""
    from datetime import datetime

    trade_id = upsert_trade_row(
        _row(record_time="2026-10-05 06:29:01"), csv_path=tmp_path / "x.csv", row_no=1
    )
    assert get_trade(trade_id)["created_at"] == pytest.approx(
        datetime(2026, 10, 5, 6, 29, 1).timestamp()
    )


def test_price_range_takes_midpoint_and_garbage_becomes_null(db, tmp_path):
    """区间取中点；空/非数字 → NULL。**绝不落 0**（0 会被下游当真价格算）。"""
    assert parse_price("5380-5400") == 5390.0
    assert parse_price("233.6") == 233.6
    assert parse_price("") is None
    assert parse_price("n/a") is None
    assert parse_price(None) is None

    trade_id = upsert_trade_row(
        _row(entry_price="5380-5400", stop_loss_price="", take_profit_price="无"),
        csv_path=tmp_path / "x.csv",
        row_no=1,
    )
    got = get_trade(trade_id)
    assert got["entry_price"] == 5390.0
    assert got["sl_price"] is None and got["tp_price"] is None
    assert got["pnl_pct"] is None, "CSV 无盈亏列时必须留空，不能拿 TP/SL 距离伪造收益率"


# ── 2. 同秒两笔不碰撞（trade_id 不用时间戳的回归守卫） ───────────────────────

def test_same_second_rows_do_not_collide(db, tmp_path):
    csv_path = tmp_path / "BTCUSDT_1h.csv"
    same = "2026-10-05 06:29:01"
    t1 = upsert_trade_row(_row(record_time=same, entry_price="100"), csv_path=csv_path, row_no=1)
    t2 = upsert_trade_row(_row(record_time=same, entry_price="200"), csv_path=csv_path, row_no=2)

    assert t1 and t2 and t1 != t2, "record_time 只有秒级精度，两笔同秒必须靠行号区分"
    assert count_trades() == 2
    # 两笔各自的入场价都得还在 —— 撞主键时后者会静默覆盖前者
    assert {r["entry_price"] for r in list_trades()} == {100.0, 200.0}

    # 换个标的/周期自然也是不同的键
    assert make_trade_id("BTCUSDT", "1h", same, 1) != make_trade_id("BTCUSDT", "4h", same, 1)
    assert make_trade_id("BTCUSDT", "1h", same, 1) != make_trade_id("NVDA", "1h", same, 1)
    # 文件名会 sanitize（BTC/USDT → BTC-USDT），行内容才区分得开
    assert make_trade_id("BTC/USDT", "1h", same, 1) != make_trade_id("BTC-USDT", "1h", same, 1)


def test_same_second_rows_in_one_csv_import_as_two(db, tmp_path):
    """导入侧同一守卫：同秒两行必须是两笔，不是一笔。"""
    same = "2026-10-05 06:29:01"
    _write_csv(
        tmp_path,
        [_row(record_time=same, entry_price="100"), _row(record_time=same, entry_price="200")],
    )
    assert import_trade_records(tmp_path)["imported"] == 2
    assert {r["entry_price"] for r in list_trades()} == {100.0, 200.0}


# ── 3. 导入幂等 ───────────────────────────────────────────────────────────────

def test_import_is_idempotent(db, tmp_path):
    """重复导入不产生重复行 —— 这是「删库可从 CSV 重建」的前提。"""
    _write_csv(
        tmp_path,
        [
            _row(record_time="2026-10-05 06:29:01", entry_price="100"),
            _row(record_time="2026-10-05 06:30:01", entry_price="200"),
            _row(record_time="2026-10-05 06:31:01", entry_price="300"),
        ],
    )
    first = import_trade_records(tmp_path)
    assert first == {"scanned": 1, "imported": 3, "skipped": 0}
    assert count_trades() == 3

    for _ in range(3):
        again = import_trade_records(tmp_path)
        assert again["imported"] == 3
        assert count_trades() == 3, "重复导入产生了重复行"

    # 全量重建：清库后从 CSV 一次导入必须回到 3 行（CSV 是权威副本）
    get_hub().execute("DELETE FROM trade_records")
    assert count_trades() == 0
    assert import_trade_records(tmp_path)["imported"] == 3
    assert count_trades() == 3


def test_import_updates_rather_than_duplicates_on_content_change(db, tmp_path):
    """同一行被改写必须 UPDATE 同一行，而不是多出一行。

    这里顺带覆盖前向兼容：**当前 CSV 没有盈亏列**，所以用一份多带 ``pnl_pct``
    列的 CSV 验证 —— 将来回填平仓价时该列自动生效，无需改导入器。
    """
    fields = [*_CSV_FIELDNAMES, "pnl_pct"]

    def _write(rows: list[dict[str, str]]) -> Path:
        path = tmp_path / "BTCUSDT_1h.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in fields})
        return path

    _write([_row(entry_price="100")])
    import_trade_records(tmp_path)
    assert list_trades()[0]["pnl_pct"] is None

    _write([_row(entry_price="100", pnl_pct="2.5")])
    stats = import_trade_records(tmp_path)
    assert stats["imported"] == 1
    assert count_trades() == 1
    assert list_trades()[0]["pnl_pct"] == 2.5


def test_import_handles_embedded_newline_in_quoted_field(db, tmp_path):
    """带引号字段里可能有换行（模型 reasoning）——行号必须按**逻辑行**算。

    按物理行数算会把一行算成两行，于是该行之后的行号全部错位，
    trade_id 随之错位，导入就会造出一堆副本。
    """
    _write_csv(
        tmp_path,
        [
            _row(record_time="2026-10-05 06:29:01", reasoning="第一行\n第二行"),
            _row(record_time="2026-10-05 06:30:01"),
        ],
    )
    assert import_trade_records(tmp_path)["imported"] == 2
    rows = list_trades()
    assert get_trade(rows[1]["trade_id"])["payload"]["reasoning"] == "第一行\n第二行"
    assert rows[0]["symbol"] == "BTCUSDT"


# ── 4. 坏文件跳过而不中断 ────────────────────────────────────────────────────

def test_import_skips_unusable_files(db, tmp_path):
    (tmp_path / "empty.csv").write_bytes(b"")
    (tmp_path / "header_only.csv").write_text(
        ",".join(_CSV_FIELDNAMES), encoding="utf-8-sig"
    )
    (tmp_path / "wrong_columns.csv").write_text("foo,bar\n1,2\n", encoding="utf-8-sig")
    (tmp_path / "binary.csv").write_bytes(b"\x00\x01\x02 not,a,trade,file\n\xff\xfe")
    _write_csv(tmp_path, [_row()], name="good.csv")

    stats = import_trade_records(tmp_path)
    assert stats["scanned"] == 5
    assert stats["imported"] == 1, "好文件必须导入，坏文件不得中断整批"
    assert stats["skipped"] == 3, "空文件 / 缺列 / 二进制乱码都算跳过"
    assert count_trades() == 1


def test_import_skips_row_without_symbol(db, tmp_path):
    _write_csv(tmp_path, [_row(), _row(symbol=""), _row(record_time="2026-10-05 06:31:01")])
    stats = import_trade_records(tmp_path)
    assert stats["imported"] == 2
    assert stats["skipped"] == 1
    assert all(r["symbol"] == "BTCUSDT" for r in list_trades())


def test_import_missing_dir_is_a_noop(db, tmp_path):
    assert import_trade_records(tmp_path / "does_not_exist") == {
        "scanned": 0,
        "imported": 0,
        "skipped": 0,
    }
    assert import_trade_records(tmp_path)["scanned"] == 0


# ── 5. 双写钩子（挂在 _save_trade_record_impl，CSV 写完之后） ─────────────────

def test_save_trade_record_writes_csv_then_index(db, tmp_path, monkeypatch):
    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    tl.save_trade_record(**_trade_kwargs())

    csv_path = tmp_path / "BTCUSDT_15m.csv"
    assert csv_path.exists(), "CSV 是权威副本，必须先落盘"
    rows = list_trades()
    assert len(rows) == 1
    assert (rows[0]["symbol"], rows[0]["timeframe"]) == ("BTCUSDT", "15m")
    assert rows[0]["order_type"] == "限价单"
    assert rows[0]["entry_price"] == 84835.6
    assert rows[0]["csv_path"] == str(csv_path)
    assert rows[0]["created_at"] > 0


def test_live_writes_then_import_never_duplicates(db, tmp_path, monkeypatch):
    """写入端与导入端必须算出同一个 trade_id —— 否则每次启动都会把双写过的行
    再造一份副本（双写路径的行号若差 1，全部对不上）。"""
    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    tl.save_trade_record(**_trade_kwargs(entry=84835.6))
    tl.save_trade_record(**_trade_kwargs(entry=84900.0))
    assert count_trades() == 2

    assert import_trade_records(tmp_path)["imported"] == 2
    assert count_trades() == 2, "导入把已双写的行又造了一份"
    assert {r["entry_price"] for r in list_trades()} == {84835.6, 84900.0}


def test_concurrent_live_writes_all_land(db, tmp_path, monkeypatch):
    """并发追加的行号必须互不相同 —— 撞号即静默丢单。"""
    import threading

    import pa_agent.records.trade_logger as tl

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    errors: list[BaseException] = []

    def save(i: int) -> None:
        try:
            tl.save_trade_record(**_trade_kwargs(entry=84000.0 + i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors

    assert import_trade_records(tmp_path)["imported"] == 8
    assert count_trades() == 8, "并发追加出现行号碰撞（两笔共用一个 PRIMARY KEY）"


def test_index_failure_does_not_block_csv(db, tmp_path, monkeypatch):
    """索引层故障绝不能让交易记录消失 —— 调用方在分析主流程里。"""
    import pa_agent.records.trade_logger as tl
    import pa_agent.storage.trade_repo as tr

    def boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    monkeypatch.setattr(tr, "upsert_trade_row", boom)

    tl.save_trade_record(**_trade_kwargs())      # 不得抛异常
    assert (tmp_path / "BTCUSDT_15m.csv").exists(), "DB 挂了也必须落 CSV"
    assert count_trades() == 0
    # 数据没丢：DB 修好后重新导入即可 —— 这就是「CSV 是权威副本」的含义
    monkeypatch.undo()
    assert import_trade_records(tmp_path)["imported"] == 1


def test_uninitialized_storage_still_writes_csv(tmp_path, monkeypatch):
    """DB 未初始化（纯 GUI/CLI 模式）时不得抛错，CSV 照写。"""
    from pa_agent.storage.db import reset_hub_for_tests as _reset

    import pa_agent.records.trade_logger as tl

    hub = _reset(tmp_path / "never.db", initialize=False)
    try:
        monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
        tl.save_trade_record(**_trade_kwargs())
    finally:
        hub.close_all()
        _reset(Path(os.environ["PA_AGENT_DB_PATH"]))
    assert (tmp_path / "BTCUSDT_15m.csv").exists()


# ── 6. 路径基准：写入端与导入端必须同一解析基准 ──────────────────────────────

def test_trade_dir_is_project_root_based_not_cwd_relative():
    """曾经的 ``Path("trade_records")`` 是 CWD 相对的：换个工作目录启动，
    写 A 目录、读 B 目录，历史交易凭空消失且无人报错。"""
    from pa_agent.config import paths

    import pa_agent.records.trade_logger as tl

    assert paths.TRADE_RECORDS_DIR == paths.PROJECT_ROOT / "trade_records"
    assert paths.TRADE_RECORDS_DIR.is_absolute()
    assert tl._TRADE_RECORDS_DIR == paths.TRADE_RECORDS_DIR
    # 导入器的默认目录与写入端同源（import_all 默认取 paths.TRADE_RECORDS_DIR）
    assert paths.TRADE_RECORDS_DIR.parent == paths.PROJECT_ROOT


# ── 7. 删除：只删索引，CSV 留给人决定 ────────────────────────────────────────

def test_delete_trade_only_touches_db(db, tmp_path):
    path = _write_csv(tmp_path, [_row()])
    trade_id = upsert_trade_row(_row(), csv_path=path, row_no=1)
    assert delete_trade(trade_id) is True
    assert get_trade(trade_id) is None
    assert path.exists(), "权威副本不由索引层删除"
    assert delete_trade("no-such-id") is True, "删不存在的行不算失败"