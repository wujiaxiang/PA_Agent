"""Helpers for locating prior analysis records for incremental runs."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.datetime_ts import format_epoch_for_display, ts_open_to_ms
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord
from pa_agent.records.pending_writer import _safe_path_segment

logger = logging.getLogger("pa_agent.records.analysis_history")

_TS_EPS_MS = 1.0  # milliseconds tolerance for bar open time matching


@dataclass(frozen=True)
class IncrementalBarDelta:
    """How many closed bars appeared since a previous record."""

    new_count: int
    anchor_ts_open: float
    new_bar_ts_opens: tuple[float, ...]


def format_bar_ts(ts_open: float) -> str:
    """Format bar open time for logs/UI (server-time epoch, no local TZ shift)."""
    return format_epoch_for_display(ts_open, short=False)


def list_record_paths(
    directory: Path | None = None,
    *,
    exchange: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> list[Path]:
    """Return saved analysis record paths, newest modified first.

    Supports both the new partitioned layout
    (``{root}/{exchange}/{symbol}/{timeframe}/{timestamp}.json``) and the
    legacy flat layout
    (``{root}/{timestamp}_{symbol}_{timeframe}.json``).

    If ``exchange``/``symbol``/``timeframe`` are all provided, the narrow
    partition is scanned first; in any case, all ``.json`` files under
    ``root`` are also scanned recursively (via ``rglob``) so that both
    partitioned and flat-layout files are returned.
    """
    root = directory or RECORDS_PENDING_DIR
    if not root.is_dir():
        return []

    paths: list[Path] = []
    seen: set[Path] = set()

    # Narrow by partition if all three are provided.
    if exchange and symbol and timeframe:
        partition = (
            root
            / _safe_path_segment(exchange)
            / _safe_path_segment(symbol)
            / _safe_path_segment(timeframe)
        )
        if partition.is_dir():
            for p in partition.glob("*.json"):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    paths.append(p)

    # Always also scan all .json files recursively. This catches:
    #   - flat-layout files at the top level (legacy)
    #   - partitioned files outside the narrow partition (if any)
    for p in root.rglob("*.json"):
        if p.is_file() and p not in seen:
            seen.add(p)
            paths.append(p)

    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def load_record(path: Path) -> AnalysisRecord | None:
    """Load one AnalysisRecord, returning None for unreadable legacy files.

    ``save_partial`` injects a ``_partial_reason`` marker that is not part of the
    Pydantic schema (``extra="forbid"``), so it must be popped before validating —
    otherwise *every* failed analysis record failed to load and was invisible in
    history. ``routes_records`` already did this on its read path.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("_partial_reason", None)
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def _scan_signature(root: Path, exchange: str, symbol: str, timeframe: str) -> tuple:
    """Cheap change-detector for the directories a scan will touch.

    Returns the sorted (dir_path, mtime) pairs for the narrow partition plus the
    root, so a new record written into any leaf partition changes the signature.
    """
    entries: list[tuple[str, float]] = []
    candidates = [root]
    if exchange and symbol and timeframe:
        candidates.append(
            root
            / _safe_path_segment(exchange)
            / _safe_path_segment(symbol)
            / _safe_path_segment(timeframe)
        )
    for d in candidates:
        try:
            entries.append((str(d), d.stat().st_mtime if d.is_dir() else 0.0))
        except OSError:
            entries.append((str(d), 0.0))
    return tuple(sorted(entries))


def resolve_record_owner(record: AnalysisRecord) -> str:
    """这条记录属于谁 —— ``meta.user_id`` 缺失/空 ⇒ 默认用户（admin）。

    ## 存量记录缺 ``user_id`` 的判定（本条是硬决定，不是待办）

    ``schema.RecordMeta.user_id`` 是**带默认值的后加字段**（``user_id: str = ""``），
    所以任何在该字段加入之前落盘的记录，读出来都是 ``""``，而不是缺失。

    **判定：空/缺失一律归 ``DEFAULT_USER_ID``（admin）。** 理由有两条，缺一不可：

    1. **与写入端逐字一致**。``pending_writer._resolve_owner`` 的解析顺序是
       显式入参 → ``record.meta.user_id`` → ``DEFAULT_USER_ID``。存量 JSON 在
       镜像进 ``analysis_records`` 表时走的正是这同一条链，落库的 ``user_id``
       列就是 ``admin``。若读端把空值判成「不属于任何人」而跳过，这些记录会在
       **列表里可见（读库过滤 user_id='admin'）、增量锚点里不可见（扫盘过滤）**
       —— 同一批记录在两个读端给出两个答案，正是 AGENTS.md「复盘归属必须与条目
       一致」登记过的那类静默分叉。反过来统一判成 admin，两端永远一致。
    2. **「无主」不是可用选项**。漏字段的历史数据既可能是 admin 的（单机部署期
       几乎必然如此），也可能本来就是别人的。判成「谁都能用」是数据泄漏
       （本函数修的就是这个），判成「谁都看不到」是数据丢失且用户无法自查。
       在没有可靠证据区分这两者时，**保守地**归到已被 DB 写入端认定的那一档。

    换句话说：这条判据与 ``analysis_records`` 表当前的写法对齐，是**跟着写入端
    走**的，不是另立一套。若将来给存量行补了真实归属，两端会同时变化。

    与 ``pa_agent/storage/users.py`` 的 ``user_id or ADMIN_USER_ID`` 同源写法
    一致 —— 那正是 AGENTS.md「复盘归属必须与条目一致」要求的形态。
    """
    meta = getattr(record, "meta", None)
    raw = getattr(meta, "user_id", "") if meta is not None else ""
    owner = str(raw or "").strip()
    if owner:
        return owner
    from pa_agent.storage.db import DEFAULT_USER_ID

    return DEFAULT_USER_ID


def _resolve_owner_scope(user_id: str | None) -> str | None:
    """把调用方给的 ``user_id`` 规整成过滤口径。

    **三档语义与经验库一致**（AGENTS.md「三档必须分开」）：

    * ``None`` —— **不过滤**。桌面端 GUI 没有用户概念，这是它的唯一取值；
      保持不过滤才能让 GUI 行为与改造前逐字相同。
    * ``""`` —— 回落默认用户（admin）。调用方明确表达了「我要 admin 的」，
      但身份解析没给出具体值。
    * 其它 —— 按该用户精确过滤。
    """
    if user_id is None:
        return None
    owner = str(user_id).strip()
    if owner:
        return owner
    from pa_agent.storage.db import DEFAULT_USER_ID

    return DEFAULT_USER_ID


def _record_from_payload(payload: dict) -> AnalysisRecord | None:
    """``payload_json`` → :class:`AnalysisRecord`（容忍 ``_partial_reason``）。

    与 :func:`load_record` 同一套处理：``save_partial`` 注入的 ``_partial_reason``
    不在 Pydantic schema 里（``extra="forbid"``），不摘掉就**每一条**都校验失败。
    """
    raw = dict(payload)
    raw.pop("_partial_reason", None)
    try:
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def _is_successful_record(
    record: AnalysisRecord,
    *,
    symbol: str,
    timeframe: str,
    exchange: str,
) -> bool:
    """「成功记录」的记录级判定 —— 库读与盘读**共用同一份**。

    ``find_latest_successful_record_db`` 在 SQL 侧只能读小字段
    （``status``/``has_kline``），stage1/stage2 是否为空必须拿回正文才知道。
    两端各写一份判定 = 迟早分叉，而分叉的后果是「明明有记录却 404」或
    「把半截记录当锚点注入提示词」。
    """
    if symbol and record.meta.symbol != symbol:
        return False
    if timeframe and record.meta.timeframe != timeframe:
        return False
    if exchange and record.meta.exchange != exchange:
        return False
    if record.exception is not None:
        return False
    if not record.stage1_diagnosis or not record.stage2_decision:
        return False
    if not record.kline_data:
        return False
    return True


def _find_latest_via_db(
    *,
    owner: str,
    symbol: str,
    timeframe: str,
    exchange: str,
) -> AnalysisRecord | None:
    """查 ``analysis_records`` 取最新一条属于 ``owner`` 的成功记录。

    **返回 ``None`` 的含义是「不知道」而不是「没有」** —— 调用方必须据此回落
    扫盘。返回 None 的情形：库降级/异常、payload 损坏、``status``/``has_kline``
    之外还得再判 record 级条件时没过。

    之所以把 ``status='ok' AND has_kline=1`` 之后仍要再判一遍，是因为
    ``LIMIT 1`` 只给出**最新那一行**：若它因 stage2 缺失而落选，正确答案在第二
    行。DB 侧无法翻页（本函数签名里没有 limit），所以这一档交回盘扫去取，
    而不是「就当没有」—— 当成没有会让用户凭空多付一次全量分析。
    """
    try:
        from pa_agent.storage.repositories import find_latest_successful_record_db

        payload = find_latest_successful_record_db(
            user_id=owner, exchange=exchange, symbol=symbol, timeframe=timeframe
        )
    except Exception:  # noqa: BLE001
        logger.warning("incremental anchor: DB lookup failed, falling back to disk",
                       exc_info=True)
        return None

    if not isinstance(payload, dict):
        return None
    record = _record_from_payload(payload)
    if record is None:
        logger.warning("incremental anchor: corrupt payload_json, falling back to disk")
        return None
    if not _is_successful_record(
        record, symbol=symbol, timeframe=timeframe, exchange=exchange
    ):
        return None
    # 双保险：DB 侧已按 user_id 过滤，仍按记录自身归属复核一次，
    # 避免任何一端过滤条件走样时静默放行别人的记录。
    if resolve_record_owner(record) != owner:
        logger.error(
            "incremental anchor: DB returned a record owned by %r while querying %r; "
            "treating as not found",
            resolve_record_owner(record), owner,
        )
        return None
    return record


def find_latest_successful_record(
    *,
    symbol: str = "",
    timeframe: str = "",
    exchange: str = "",
    directory: Path | None = None,
    user_id: str | None = None,
) -> AnalysisRecord | None:
    """Find the newest full successful record matching the given filters.

    If ``symbol``/``timeframe``/``exchange`` are empty (default), returns the
    latest successful record across all symbols/timeframes/exchanges.

    Works with both the new partitioned layout and the legacy flat layout —
    iteration does not assume a specific filename format. Filters are applied
    to the loaded record's meta fields (not to the filename) so both layouts
    are handled uniformly.

    ## ``user_id``：分析记录是**每用户私有**，归属在这一层必须参与判断

    这是**跨用户数据泄漏**的修复点。改造前签名里没有 ``user_id``，于是
    ``routes_analyze``（增量锚点 / 增量预检）、``routes_chat``（追问锚点回落）
    在多用户下拿到的是**同一个共享目录里最新的一条**——不管是谁的。而
    ``prompt_assembler.build_incremental_stage1`` 会把
    ``previous_record.stage1_response["content"]`` **原文注入** ``[2] assistant``：
    carol 点「增量」，admin 的完整 stage1 推理就进了 carol 的提示词，新记录还
    继承 admin 的诊断结论。**全程零报错**，只能靠归属判断挡住。

    取值三档（与 :func:`_resolve_owner_scope` 一致，见那里的说明）：

    * ``None``（默认）—— **不过滤**。桌面端 GUI 没有用户概念，它四个调用点
      全部只传 ``symbol``/``timeframe``，因此**逐字不变**，行为零变化。
      Web 端**不得**依赖这个缺省值，必须显式传 ``_request_user_id(request)``。
    * ``""`` —— 回落默认用户（admin）。
    * 具体值 —— 精确按该用户过滤。

    ## 读端走库，扫盘只作降级回落

    传了 ``user_id`` 时**先查 ``analysis_records`` 表**：那张表已有 ``user_id``
    列与 ``(symbol,timeframe,exchange,ts_local_ms)`` 索引，一条 SQL 取代
    「rglob + 全量 JSON parse + 目录 mtime 启发式缓存」，与 AGENTS.md
    「分析记录：读端只查库」的既定方向一致（列表/详情端点早就只查库了）。

    扫盘**保留为降级回落**，因为它是 DB 抖动 / 镜像失败时唯一还能用的数据源；
    直接删掉会把一次库抖动变成「所有人凭空多付一次全量分析」。回落路径带
    **同一套**归属判定（``resolve_record_owner``），所以回落不会把归属判断
    放宽 —— 泄漏点在两条路径上都堵住了。

    GUI 路径（``user_id is None``）**不查库、仍走原来的扫盘 + mtime 缓存**：
    桌面端的历史数据形态与 Web 不同，且本条要求 GUI 行为零变化。

    ## 排序判据的差异（刻意为之）

    库读按 ``ts_local_ms``（分析时刻）排序，盘读按文件 mtime（落盘时刻）。
    「最新」在语义上前者才是对的（回放/重写旧记录时两者可差很久），
    ``find_latest_successful_record_db`` 的 docstring 已就该判据给出同样论证。
    """
    owner = _resolve_owner_scope(user_id)

    if owner is not None:
        hit = _find_latest_via_db(
            owner=owner, symbol=symbol, timeframe=timeframe, exchange=exchange
        )
        if hit is not None:
            return hit

    root = directory or RECORDS_PENDING_DIR
    cache_key = (str(root.resolve()), exchange, symbol, timeframe, owner)
    # Signature must reflect the directories that are ACTUALLY scanned. Records
    # live in nested {exchange}/{symbol}/{timeframe}/ partitions, so writing a new
    # record updates a leaf directory's mtime — never the root's. Keying on the
    # root mtime therefore made the cache permanently stale (verified: a nested
    # write does not change root.stat().st_mtime), so incremental analysis kept
    # reusing an old record — or a cached None — forever.
    dir_mtime = _scan_signature(root, exchange, symbol, timeframe)
    cached = _LATEST_RECORD_CACHE.get(cache_key)
    if cached is not None and cached[0] == dir_mtime:
        return cached[1]

    result: AnalysisRecord | None = None
    for path in list_record_paths(
        directory, exchange=exchange, symbol=symbol, timeframe=timeframe
    ):
        record = load_record(path)
        if record is None:
            continue
        if owner is not None and resolve_record_owner(record) != owner:
            continue
        if not _is_successful_record(
            record, symbol=symbol, timeframe=timeframe, exchange=exchange
        ):
            continue
        result = record
        break
    _LATEST_RECORD_CACHE[cache_key] = (dir_mtime, result)
    return result


_LATEST_RECORD_CACHE: dict[
    tuple[str, str, str, str, str | None], tuple[float, AnalysisRecord | None]
] = {}


def invalidate_latest_record_cache() -> None:
    """Clear cached latest-record lookups (call after saving a new record)."""
    _LATEST_RECORD_CACHE.clear()


def compute_incremental_bar_delta(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> IncrementalBarDelta | None:
    """Return bars newer than the previous record's latest closed bar.

    ``frame.bars`` and ``previous_record.kline_data`` are newest-first. The anchor
    is ``kline_data[0]`` (K1 at the time of the previous analysis). New bars are
    those with ``ts_open`` strictly greater than the anchor — not merely bars
    appearing before the anchor index in the current window.
    """
    if not previous_record.kline_data:
        return None

    anchor_raw = previous_record.kline_data[0]["ts_open"]
    anchor = ts_open_to_ms(anchor_raw)

    anchor_seen = False
    new_ts: list[float] = []
    for bar in frame.bars:
        ts = ts_open_to_ms(bar.ts_open)
        if abs(ts - anchor) <= _TS_EPS_MS:
            anchor_seen = True
            continue
        if ts > anchor + _TS_EPS_MS:
            new_ts.append(bar.ts_open)

    if not anchor_seen:
        return None

    return IncrementalBarDelta(
        new_count=len(new_ts),
        anchor_ts_open=float(anchor_raw),
        new_bar_ts_opens=tuple(new_ts),
    )


def count_new_bars_since_record(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> int | None:
    """Backward-compatible wrapper returning only the new bar count."""
    delta = compute_incremental_bar_delta(frame, previous_record)
    if delta is None:
        return None
    return delta.new_count
