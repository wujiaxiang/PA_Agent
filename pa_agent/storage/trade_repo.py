"""交易记录仓储（L2 用户级 —— 资金面，多会话共享）。

对应 ``docs/SESSION_STORAGE_DESIGN.md`` §8 第 6 步，是四域里最后一个落地的。

**谁是权威？**
==================
**CSV 文件是权威副本**（``trade_records/<symbol>_<timeframe>.csv``，外加同目录
的图表 PNG）。SQLite 里的 ``trade_records`` 表**只是索引与查询加速层**：

- 飞书卡片要发 PNG 图，DB 里放不了二进制；图表与逐字段的完整上下文也在磁盘上；
- 人工核对、导出、再加工都以 CSV 为准；
- **两者不一致时以 CSV 为准**，DB 可以随时用导入器从 CSV 重建（幂等）。
  删库重来不丢任何交易记录 —— 这正是「写双份、读可回退」策略的全部意义
  （同 ``analysis_records`` / ``experience_entries``，见 §7 迁移策略）。

因此 DB 写失败**只记 warning**，绝不能冒泡（``trade_logger`` 在分析主流程里被调用）。
反过来说，DB 也**不得**成为写盘的前置条件：先落 CSV，再写库。

**分级**：L2 用户级。交易记录是资金面事实，A tab 记下的单子 B tab 必须能查到，
否则「本周期下了几单」这种问题会随标签页分裂（§2.1）。

**索引用小字段、大字段留 JSON**（§5 约定）：``entry_price`` / ``sl_price`` /
``tp_price`` / ``pnl_pct`` 是数值列；CSV 那一整行（含 reasoning、trace、
confidence 全文，动辄几 KB）整体进 ``payload_json``。

``trade_id`` 的构造
====================
**不能用时间戳。** CSV 的 ``record_time`` 只到**秒**（``"%Y-%m-%d %H:%M:%S"``），
而 ``trade_id`` 是 PRIMARY KEY —— 同标的同秒连续出两单（连续分析、
后台 follow-up 线程与手动分析并发）会静默互相覆盖。实测场景：``bar_close``
触发的持续分析与用户手点「分析」可在同一秒内各落一行。

故用 ``sha256("<symbol>|<timeframe>|<record_time>|<row_no>")``，其中
``row_no`` 是**该数据行在其 CSV 文件中的 1 基序号（不含表头）**。

行号为什么必须由「文件内容」决定而不是「内存计数」
----------------------------------------------------
导入器（冷启动扫全量 CSV）与写入端（``trade_logger`` 追加）必须算出**同一个**
``trade_id``，否则每次导入都会给已双写的行再造一份副本。行号取自文件本身
是唯一能让两边自然对齐的定义。

代价是写入端要知道「文件里已有几行数据」。为此本模块提供带 memo 的
:func:`data_row_count` / :func:`peek_next_row_no` / :func:`note_row_appended`：
按 ``(size, mtime_ns)`` 缓存计数，追加后立即刷新缓存 —— 于是稳态下每行都是
O(1)，只有进程重启（缓存冷）或外部改动文件时才重扫一次。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.trade")

#: CSV 里没有这两个列就不是交易记录（同名 CSV 可能是别的导出）。
#: 缺列的文件必须跳过而不是硬解析 —— 那是坏文件，不是待修复数据。
REQUIRED_CSV_COLUMNS = ("record_time", "symbol")

#: ``pnl_pct`` 目前**在 CSV 里没有对应列**（交易记录只在计划落盘时写，没有平仓回填）。
#: 留空 = NULL，绝不用 ``entry_price`` 与 TP/SL 的距离伪造一个「预计收益率」——
#: 那会让「胜率统计」变成猜测。将来真要回填盈亏，加一列即可，下面的映射会自动生效。
_PNL_COLUMNS = ("pnl_pct", "pnl")

#: CSV 计价列 → DB 列。写双份与导入器共用同一份映射（它们本就是同一个入口），
#: 顺序即 INSERT 的位置顺序。
PRICE_COLUMN_MAP = (
    ("entry_price", "entry_price"),
    ("stop_loss_price", "sl_price"),
    ("take_profit_price", "tp_price"),
)


# ── trade_id ──────────────────────────────────────────────────────────────────

def make_trade_id(symbol: str, timeframe: str, record_time: str, row_no: int) -> str:
    """Stable primary key for one trade row.  见模块 docstring 的构造说明。

    ``symbol`` / ``timeframe`` 取自 **CSV 行内容**而非文件名：文件名里
    ``BTC/USDT`` 已被替换成 ``BTC-USDT``，与真正的 ``BTC-USDT`` 标的撞名。
    行内容可以区分二者。
    """
    raw = "|".join(
        [
            str(symbol or "").strip(),
            str(timeframe or "").strip(),
            str(record_time or "").strip(),
            str(int(row_no)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── CSV 数据行计数（行号来源） ─────────────────────────────────────────────────

_count_cache: dict[str, tuple[int, int, int]] = {}   # path -> (size, mtime_ns, data_rows)
_count_lock = threading.Lock()


def _forget(path: Path) -> None:
    with _count_lock:
        _count_cache.pop(str(path), None)


def _scan_data_rows(path: Path) -> int:
    """Count logical data rows (header excluded) in *path*.  0 on any problem.

    用 ``csv.reader`` 而不是数物理行：带引号的字段里可能有换行（模型的 reasoning
    文本），物理行数会把一行算成两行，进而错算行号 → trade_id 错位。
    """
    try:
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            total = sum(1 for _ in reader)
    except (OSError, csv.Error) as exc:
        logger.warning("trade_records: cannot scan %s (%s), assuming empty", path, exc)
        return 0
    # 第一行是表头
    return max(total - 1, 0)


def data_row_count(csv_path: Path) -> int:
    """Existing data-row count of *csv_path*, memoised on ``(size, mtime_ns)``.

    文件不存在即 0（尚未创建的新 CSV）。
    """
    try:
        st = csv_path.stat()
    except OSError:
        _forget(csv_path)
        return 0
    key = str(csv_path)
    with _count_lock:
        hit = _count_cache.get(key)
    if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    count = _scan_data_rows(csv_path)
    with _count_lock:
        _count_cache[key] = (st.st_size, st.st_mtime_ns, count)
    return count


def peek_next_row_no(csv_path: Path) -> int:
    """1-based index the **next appended** data row will occupy.  空文件即 1。

    **必须在 CSV 文件锁内调用** —— 并发写同一文件时锁外取号会撞号。
    """
    return data_row_count(csv_path) + 1


def note_row_appended(csv_path: Path, row_no: int) -> None:
    """Refresh the memo after a successful append (keeps the hot path O(1))."""
    try:
        st = csv_path.stat()
    except OSError:
        _forget(csv_path)
        return
    with _count_lock:
        _count_cache[str(csv_path)] = (st.st_size, st.st_mtime_ns, int(row_no))


# ── 取值与解析 ────────────────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    """``"233.6"`` / ``233.6`` → 233.6；空串、None、NaN、非数字 → ``None``。

    **绝不返回 0**：价格列填 0 会被下游当成「跌到 0」的真值参与计算。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_price(raw: Any) -> float | None:
    """Parse a price that may be a number, a string, or a range (``'5380-5400'``).

    区间取中点 —— 与 ``trade_logger._parse_sr_price`` 同口径。这里刻意**复制**
    而不是 import：存储层不得反向依赖写入端（``experience_repo`` 亦然）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        f = float(raw)
        return f if f > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    m2 = re.search(r"\d+(?:\.\d+)?", text)
    return float(m2.group(0)) if m2 else None


def parse_record_time(record_time: str) -> float | None:
    """``"2026-10-05 06:29:01"`` → epoch 秒（本机时区）。解析不了返回 ``None``。"""
    text = str(record_time or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _text(row: dict, key: str) -> str:
    v = row.get(key)
    return "" if v is None else str(v).strip()


def _resolve_chart_path(csv_path: Path, chart_image: str) -> str:
    """``chart_image`` 列只有文件名，相对 CSV 所在目录解析成绝对路径。

    不判断文件是否存在 —— 路径本身就是信息，是否存在由读取端决定；
    导入时图片可能还没画完，但记录本身不该因此丢字段。
    """
    name = str(chart_image or "").strip()
    if not name:
        return ""
    p = Path(name)
    if not p.is_absolute():
        p = csv_path.parent / p
    return str(p)


# ── 写入 ──────────────────────────────────────────────────────────────────────

def upsert_trade_row(
    row: dict[str, Any],
    *,
    csv_path: Path,
    row_no: int,
    chart_path: str = "",
    user_id: str = DEFAULT_USER_ID,
    created_at: float | None = None,
) -> str | None:
    """Index one CSV row into ``trade_records``.  返回 ``trade_id``，失败 ``None``。

    *row* 是 CSV 的一整行（键为列名、值为字符串）。写双份与导入器**共用**这个
    入口 —— 两条路径若各写一份映射，迟早会漂移。

    ``created_at`` 缺省时取 ``record_time`` 解析出的 epoch（历史导入按交易时刻
    排序，而不是按「今天才被导入」排序），解析失败才退回 ``now()``。
    """
    if not isinstance(row, dict):
        return None
    symbol = _text(row, "symbol")
    if not symbol:
        logger.warning("trade_records: row %d of %s has no symbol, skipped", row_no, csv_path)
        return None

    record_time = _text(row, "record_time")
    trade_id = make_trade_id(symbol, _text(row, "timeframe"), record_time, row_no)
    ts = created_at if created_at is not None else parse_record_time(record_time)
    if ts is None:
        ts = now()

    pnl = None
    for col in _PNL_COLUMNS:
        if col in row:
            pnl = _num(row.get(col))
            if pnl is not None:
                break

    payload = json.dumps(
        {str(k): ("" if v is None else str(v)) for k, v in row.items() if k is not None},
        ensure_ascii=False,
    )
    chart = chart_path or _resolve_chart_path(csv_path, _text(row, "chart_image"))

    ok = get_hub().execute(
        """
        INSERT INTO trade_records
            (trade_id, user_id, symbol, timeframe, order_type,
             entry_price, sl_price, tp_price, pnl_pct,
             csv_path, chart_path, payload_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trade_id) DO UPDATE SET
            symbol=excluded.symbol,
            timeframe=excluded.timeframe,
            order_type=excluded.order_type,
            entry_price=excluded.entry_price,
            sl_price=excluded.sl_price,
            tp_price=excluded.tp_price,
            pnl_pct=excluded.pnl_pct,
            csv_path=excluded.csv_path,
            chart_path=excluded.chart_path,
            payload_json=excluded.payload_json
        """,
        (
            trade_id, user_id, symbol, _text(row, "timeframe"), _text(row, "order_type"),
            *(parse_price(row.get(csv_col)) for csv_col, _db_col in PRICE_COLUMN_MAP),
            pnl,
            str(csv_path), chart, payload, float(ts),
        ),
    )
    if not ok:
        logger.warning("trade_records: index write failed for %s (DB degraded?)", trade_id)
        return None
    return trade_id


# ── 读取 ──────────────────────────────────────────────────────────────────────

def list_trades(
    *,
    user_id: str = DEFAULT_USER_ID,
    symbol: str = "",
    timeframe: str = "",
    order_type: str = "",
    limit: int = 50,
) -> list[dict]:
    """列出交易记录，**过滤条件全部可选** —— 空值即不过滤（跨品种看资金面）。

    按 ``created_at`` 倒序，与 ``ix_trade_recent`` 的索引方向一致。
    """
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    if order_type:
        where.append("order_type = ?")
        params.append(order_type)
    params.append(int(limit))
    return [
        dict(r)
        for r in get_hub().query(
            "SELECT * FROM trade_records WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
    ]


def get_trade(trade_id: str, *, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """取单条记录（含解析后的 ``payload``）。DB 未初始化 / 读失败返回 ``None``。"""
    row = get_hub().query_one(
        "SELECT * FROM trade_records WHERE trade_id = ? AND user_id = ?",
        (trade_id, user_id),
    )
    if row is None:
        return None
    out = dict(row)
    try:
        out["payload"] = json.loads(out.pop("payload_json") or "{}")
    except json.JSONDecodeError:
        logger.warning("trade record %s has corrupt payload_json", trade_id)
        out["payload"] = {}
    return out


def count_trades(
    *,
    user_id: str = DEFAULT_USER_ID,
    symbol: str = "",
    timeframe: str = "",
) -> int:
    """计数。**过滤条件必须跟着走**，否则 UI 上的数字与列表对不上。"""
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    rows = get_hub().query(
        "SELECT COUNT(*) AS n FROM trade_records WHERE " + " AND ".join(where),
        tuple(params),
    )
    return int(rows[0]["n"]) if rows else 0


def delete_trade(trade_id: str, *, user_id: str = DEFAULT_USER_ID) -> bool:
    """只删 DB 索引副本。**CSV 是权威副本，磁盘文件由调用方处理。**"""
    return get_hub().execute(
        "DELETE FROM trade_records WHERE trade_id = ? AND user_id = ?",
        (trade_id, user_id),
    )