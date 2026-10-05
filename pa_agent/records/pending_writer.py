"""PendingWriter — persists AnalysisRecord and FollowupTurn to disk.

Storage layout (partitioned):
    {pending_dir}/{exchange}/{symbol}/{timeframe}/{YYYY-MM-DD_HH-mm-ss}.json
    {pending_dir}/{record_id}.followups.jsonl  (sidecar stays at top level)

Legacy flat layout ({pending_dir}/{ts}_{symbol}_{timeframe}.json) is still
readable by analysis_history.list_record_paths().

Disk failures are logged and emitted to the event bus but never propagated.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pa_agent.records.schema import AnalysisRecord, FollowupTurn
from pa_agent.util.mask_secret import mask_secret, register_secret, scrub

# Characters that are illegal in Windows/Linux path segments.
_ILLEGAL_PATH_CHARS = ('/', '\\', ':', '*', '?', '"', '<', '>', '|')


def _default_logger() -> logging.Logger:
    return logging.getLogger(__name__)


def _ms_to_local_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to local datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()


def _safe_path_segment(s: str) -> str:
    """Replace illegal path characters with ``-``.

    Applied to exchange/symbol/timeframe before joining into a path so that
    malformed values (e.g. ``BTC/USDT``) do not break the directory structure.
    """
    result = s
    for ch in _ILLEGAL_PATH_CHARS:
        result = result.replace(ch, '-')
    return result


def _build_basename(record: AnalysisRecord) -> str:
    """Build the filename stem (without extension) for a record.

    In the partitioned layout this is just the timestamp portion; the
    exchange/symbol/timeframe go into the directory path (see
    ``_build_record_path``).
    """
    dt = _ms_to_local_datetime(record.meta.timestamp_local_ms)
    ts_str = dt.strftime("%Y-%m-%d_%H-%M-%S")
    # Second resolution collided: two analyses of the same
    # (exchange, symbol, timeframe) inside one second produced identical paths and
    # the second truncated the first. Append milliseconds + a short uuid so the
    # stem stays human-sortable while being unique.
    ms = int(record.meta.timestamp_local_ms) % 1000
    return f"{ts_str}_{ms:03d}_{uuid.uuid4().hex[:6]}"


def _resolve_owner(record: AnalysisRecord, user_id: str) -> str:
    """决定这条记录落库时的 ``user_id``。

    解析顺序（先到先得，**任一非空即止**）：

    1. 调用方显式传的 ``user_id``；
    2. ``record.meta.user_id`` —— 记录**自己**带的归属；
    3. ``DEFAULT_USER_ID``（admin），即改造前的行为。

    ## 为什么第 2 步是主要通道，而不是「每个调用点都得记得传」

    ``TwoStageOrchestrator.submit(user_id=...)`` 在构造记录时就把同一个
    ``user_id`` 盖进了 ``record.meta.user_id``（``_build_empty_record``）。
    归属因此**随记录本身走**，而不是依赖十几次 ``save_partial`` 调用点各自
    传参是否漏了 —— 漏一个就是一条落到 admin 名下、用户永远看不到的记录，
    而且**没有任何报错**。

    这与经验库的口径完全一致（AGENTS.md「``user_id`` 必须一路落库」）：
    结算跑在调度器线程上、结算的是几小时前的记录，那时没有请求上下文，
    **记录本身是唯一依据**。

    显式入参仍然保留，是给「同一个 writer 实例被不同用户共用」的场景用的
    —— ``ctx.pending_writer`` 是**进程级单例**，靠实例字段存归属会在
    并发分析下互相串写；把它做成参数（且默认取记录自带的）就没有这个竞态。
    """
    explicit = str(user_id or "").strip()
    if explicit:
        return explicit

    meta = getattr(record, "meta", None)
    carried = str(getattr(meta, "user_id", "") or "").strip()
    if carried:
        return carried

    from pa_agent.storage.db import DEFAULT_USER_ID

    return DEFAULT_USER_ID


def _build_record_path(record: AnalysisRecord, pending_dir: Path) -> Path:
    """Construct the partitioned storage path for a record.

    Layout: ``{pending_dir}/{exchange}/{symbol}/{timeframe}/{timestamp}.json``.
    Empty exchange collapses to ``{pending_dir}/{symbol}/{timeframe}/...``
    (pathlib normalises empty segments away).
    """
    exchange_seg = _safe_path_segment(record.meta.exchange)
    symbol_seg = _safe_path_segment(record.meta.symbol)
    timeframe_seg = _safe_path_segment(record.meta.timeframe)
    basename = _build_basename(record)
    return pending_dir / exchange_seg / symbol_seg / timeframe_seg / f"{basename}.json"


class PendingWriter:
    """Writes analysis records and followup turns to the pending directory."""

    def __init__(
        self,
        pending_dir: Optional[Path] = None,
        event_bus=None,
        logger: Optional[logging.Logger] = None,
        api_key: str = "",
    ) -> None:
        if pending_dir is None:
            from pa_agent.config.paths import RECORDS_PENDING_DIR
            pending_dir = RECORDS_PENDING_DIR

        self._pending_dir = pending_dir
        self._event_bus = event_bus
        self._logger = logger or _default_logger()
        self._api_key = api_key
        # 密钥可能在运行期被用户改过（PUT /api/settings 会调 update_api_key →
        # register_secret）。落盘脱敏时一并兜底所有已注册密钥，避免旧 key
        # 失效后新 key 以明文写进 records/pending/*.json。
        register_secret(api_key)

        try:
            self._pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.error(
                "PendingWriter: failed to create pending directory %s: %s",
                self._pending_dir,
                exc,
            )

    def _mirror_to_sqlite(
        self,
        record: AnalysisRecord,
        data: dict,
        path: Path,
        user_id: str = "",
    ) -> None:
        """把刚写盘的分析记录同步一份到 SQLite（索引层）。

        策略是**写双份**：文件仍是权威副本，SQLite 只做索引/快照。两条硬约束：

        1. **SQLite 写失败绝不能影响文件写**。落盘已成功，这里任何异常都只记
           warning —— 索引层故障不该让一条分析记录消失（AGENTS.md 已有先例：
           一次静默失败让整条链路变死）。
        2. **传脱敏后的 data**，不传原始 record —— 否则 API key 会绕过
           ``_sanitize`` 进入数据库。``_partial_reason`` 必须带上，否则失败
           记录会被误标为 ``ok`` 并混入增量分析的锚点候选。

        ``user_id`` 必须解析出真实归属并透传给 ``upsert_record``：不传就是
        ``DEFAULT_USER_ID``，于是**所有非 admin 用户的记录恒落 admin 名下**，
        而读端按 ``current_user_id`` 过滤 ⇒ 用户「分析成功、文件落盘、接口
        200」，列表却永远为空。见 :func:`_resolve_owner`。
        """
        try:
            from pa_agent.storage.repositories import upsert_record

            upsert_record(
                record,
                raw=data,
                file_path=path,
                user_id=_resolve_owner(record, user_id),
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "PendingWriter: SQLite mirror failed for %s (file already saved): %s",
                path.name,
                exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_full(self, record: AnalysisRecord, *, user_id: str = "") -> Path:
        """Serialize and save a complete analysis record.

        ``user_id`` 是**可选**的落库归属；留空时按 :func:`_resolve_owner` 的
        顺序回落（先看记录自带的 ``meta.user_id``，再回落默认用户）。

        Returns the path written to, or a best-effort path on failure.
        """
        if not record.meta.exchange:
            self._logger.warning(
                "PendingWriter: record.meta.exchange is empty; writing record "
                "to symbol/timeframe partition without exchange segment."
            )
        path = _build_record_path(record, self._pending_dir)
        data = record.model_dump()
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        self._mirror_to_sqlite(record, data, path, user_id)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def save_partial(self, record: AnalysisRecord, reason: str, *, user_id: str = "") -> Path:
        """Serialize and save a partial analysis record with a reason field.

        The ``_partial_reason`` key is injected into the serialized dict
        (it is not part of the Pydantic model). When ``record.exception`` is
        set, ``partial_reason`` is also copied into that dict for easier
        filtering without reading ``_partial_reason``.

        ``user_id`` 语义同 :meth:`save_full`。

        Returns the path written to, or a best-effort path on failure.
        """
        if not record.meta.exchange:
            self._logger.warning(
                "PendingWriter: record.meta.exchange is empty; writing record "
                "to symbol/timeframe partition without exchange segment."
            )
        path = _build_record_path(record, self._pending_dir)
        data = record.model_dump()
        data["_partial_reason"] = reason
        if isinstance(data.get("exception"), dict):
            data["exception"] = {**data["exception"], "partial_reason": reason}
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        self._mirror_to_sqlite(record, data, path, user_id)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def append_followup(self, record_id: str, turn: FollowupTurn) -> None:
        """Append a single followup turn to the JSONL sidecar file.

        ``record_id`` is the basename (without extension) of the record file,
        e.g. ``"2026-05-18_14-00-13_XAUUSD_1h"``.

        The sidecar file stays at the top level of the pending directory
        (``records/pending/{record_id}.followups.jsonl``) for backward
        compatibility with existing callers.
        """
        path = self._pending_dir / f"{record_id}.followups.jsonl"
        line = json.dumps(turn.model_dump(), ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            self._handle_disk_error(exc, path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize(data: dict, api_key: str) -> dict:
        """Recursively redact secrets in string values at any depth.

        Covers *api_key* explicitly plus every secret in the process-wide
        registry (``pa_agent.util.mask_secret``), so a rotated key — which
        replaces ``self._api_key`` on disk but not in this instance — still gets
        masked instead of being written in plaintext.
        Handles nested dicts, lists, and plain string values at any depth.
        """
        masked = mask_secret(api_key) if api_key else None

        def _walk(node):
            if isinstance(node, str):
                if masked is not None:
                    node = node.replace(api_key, masked)
                return scrub(node)
            if isinstance(node, dict):
                return {k: _walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        return _walk(data)

    def _write_json(self, path: Path, data: dict) -> None:
        """Write *data* as pretty-printed JSON to *path*, handling errors.

        Parent directories are created automatically (supports the partitioned
        layout where records live under ``{exchange}/{symbol}/{timeframe}/``).
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            # Atomic: write a unique temp file in the same directory, then rename.
            # A crash mid-write previously left a truncated/empty .json that the
            # reader then silently skipped — losing the record entirely.
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError as exc:
            self._handle_disk_error(exc, path)

    def _handle_disk_error(self, exc: OSError, path: Path) -> None:
        """Log the error and optionally emit to the event bus."""
        self._logger.error(
            "PendingWriter: disk error writing %s: %s", path, exc
        )
        if self._event_bus is not None:
            try:
                self._event_bus.emit("disk_error", {"path": str(path), "error": str(exc)})
            except Exception as bus_exc:  # noqa: BLE001
                self._logger.error(
                    "PendingWriter: event_bus emit failed: %s", bus_exc
                )
