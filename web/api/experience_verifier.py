"""Stage 2 of the two-stage experience library: resolve pending records.

Stage 1 (``ExperienceWriter.save_pending``) records a plan the moment the
signal fires. This module decides when that plan is *done*.

Why it cannot just poll the shared data source
----------------------------------------------
``ctx.data_source`` is a **single subscription-bound instance**: ``subscribe()``
rewrites ``self._symbol`` / ``self._timeframe``. A pending record can live for
hours and be verified long after the user navigated to another instrument.
Reusing the shared source would resolve the record against a *different*
symbol's price action — silently fabricating an outcome. (Reproduced: a
BTCUSDT plan, entry 100 / TP 120 / SL 80, evaluated against ETHUSDT bars,
settled as ``('loss', -20.0)``.)

So alignment is the hard invariant here:

* A shared source is reused **only** when ``(exchange, symbol, timeframe)``
  matches the record on all three axes.
* Otherwise a dedicated source is created for that record alone.
* Every fetched bar is sanity-checked against the record's ``entry_price``
  before evaluation — a magnitude mismatch means the bars are not this
  instrument, so the record is left pending rather than wrongly settled.

N-bar rule
----------
``experience_verify_bars`` (N) subsequent bars decide the terminal state:

* TP/SL touched        → ``win`` / ``loss`` with pnl
* N bars, neither hit   → ``unresolved`` (terminal, but no verdict)
* fewer than N bars     → stays ``pending``, keeps waiting

Only ``win`` / ``loss`` are reachable by :class:`ExperienceReader`, so an
undecided setup never gets fed back into the prompts as if it were a loss.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pa_agent.records.experience_writer import (
    STATUS_LOSS,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    STATUS_WIN,
    ExperienceWriter,
    resolve_exit,
)

logger = logging.getLogger("pa_agent.web.experience_verifier")

#: Fetch at least this many bars so the N-bar window is covered even when the
#: pending record is older than the current chart's history.
_FETCH_BARS = 300

#: Guard rail: if the fetched bars sit more than this ratio away from the
#: record's entry price, they cannot be the same instrument.
_PRICE_SANITY_RATIO = 8.0

#: How many records one verification pass may settle.
_DEFAULT_BATCH = 5


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
        return float(v)
    return None


def _bar_rows(bars: Any) -> list[dict[str, Any]]:
    """Normalise bars to dicts carrying ts_open + OHLC."""
    out: list[dict[str, Any]] = []
    for b in bars or []:
        if isinstance(b, dict):
            row = b
        else:
            row = {
                "ts_open": getattr(b, "ts_open", 0),
                "high": getattr(b, "high", None),
                "low": getattr(b, "low", None),
                "close": getattr(b, "close", None),
            }
        ts = row.get("ts_open")
        hi = _num(row.get("high"))
        lo = _num(row.get("low"))
        try:
            ts = int(ts or 0)
        except (TypeError, ValueError):
            continue
        if not ts or hi is None or lo is None:
            continue
        out.append({"ts_open": ts, "high": hi, "low": lo,
                    "close": _num(row.get("close"))})
    return out


def bars_belong_to_instrument(bars: list[dict[str, Any]], entry_price: float) -> bool:
    """Cheap guard against evaluating a record against another instrument.

    A wrong instrument's bars almost never straddle the record's entry price.
    Without this, the subscription-drift bug would sail straight through.
    """
    if not bars or entry_price <= 0:
        return False
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    if lo <= entry_price <= hi:
        return True
    nearest = lo if entry_price < lo else hi
    return nearest > 0 and (entry_price / nearest if entry_price < lo
                            else nearest / entry_price) >= 1.0 / _PRICE_SANITY_RATIO


def _bars_after(anchor_ms: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bars strictly after the entry, oldest-first — the only ones that count."""
    return sorted(
        (r for r in rows if r["ts_open"] > int(anchor_ms or 0)),
        key=lambda r: r["ts_open"],
    )


class _DedicatedSource:
    """Fetch bars for exactly one (exchange, symbol, timeframe), then die.

    Used when the shared source is on a different subscription. Constructing a
    ``TradingViewSource`` is expensive, so instances are never pooled — a
    short-lived object is the only way to guarantee we never mutate the shared
    subscription while another caller is reading it.
    """

    def __init__(self, factory: Callable[[], Any], symbol: str, timeframe: str,
                 exchange: str, logger_: logging.Logger):
        self._factory = factory
        self._symbol = symbol
        self._timeframe = timeframe
        self._exchange = exchange
        self._log = logger_

    def fetch(self) -> list[dict[str, Any]]:
        src = None
        try:
            src = self._factory()
            src.connect()
            # 交易所走 ``set_exchange``、订阅只收 (symbol, timeframe) ——
            # 基类签名就是 ``subscribe(self, symbol, timeframe)``，没有 exchange
            # 形参。此前这里写成 ``subscribe(symbol=..., exchange=..., timeframe=...)``，
            # 对**所有**数据源都抛 TypeError，于是整条「验证」按钮路径（专用数据源）
            # 永远取不到 K 线。之所以从没被发现：单测用的是假源，而假源只走
            # ``_shared_fetch`` 的三轴匹配分支，压根不碰这段。
            setter = getattr(src, "set_exchange", None)
            if callable(setter):
                setter(self._exchange or "")
            src.subscribe(self._symbol, self._timeframe)
            return _bar_rows(src.latest_snapshot(_FETCH_BARS))
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "experience verify: dedicated fetch failed for %s/%s/%s: %s",
                self._exchange, self._symbol, self._timeframe, exc,
            )
            return []
        finally:
            if src is not None:
                try:
                    src.disconnect()
                except Exception:  # noqa: BLE001
                    pass


def _shared_fetch(shared: Any, rec_symbol: str, rec_timeframe: str,
                  rec_exchange: str) -> list[dict[str, Any]]:
    """Reuse the shared source only on a **three-axis** match."""
    cur_symbol = str(getattr(shared, "_symbol", "") or "").strip()
    cur_timeframe = str(getattr(shared, "_timeframe", "") or "").strip()
    cur_exchange = str(getattr(shared, "_exchange", "") or "").strip().upper()
    if cur_symbol != rec_symbol or cur_timeframe != rec_timeframe:
        return []
    if rec_exchange and cur_exchange and cur_exchange != rec_exchange.upper():
        return []
    try:
        return _bar_rows(shared.latest_snapshot(_FETCH_BARS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: shared snapshot failed: %s", exc)
        return []


def settle_record(
    writer: ExperienceWriter,
    entry_id: str,
    content: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    verify_bars: int,
    user_id: str = "",
) -> tuple[str, int]:
    """Apply the N-bar rule to one record. Returns ``(status, bars_seen)``.

    * ``(win|loss, n)``     — a level was touched
    * ``(unresolved, n)``   — N bars elapsed without a touch (terminal)
    * ``(pending, n)``      — fewer than N bars so far; keep waiting
    """
    after = _bars_after(int(content.get("entry_ts_open_ms") or 0), bars)
    seen = len(after)

    info = resolve_exit(
        after,
        entry_price=float(content.get("entry_price") or 0.0),
        take_profit_price=float(content.get("take_profit_price") or 0.0),
        stop_loss_price=float(content.get("stop_loss_price") or 0.0),
        is_long=bool(content.get("is_long", True)),
    )
    if info is not None and info["touched"]:
        status = STATUS_WIN if info["result"] == "win" else STATUS_LOSS
        writer.finalize(entry_id, status=status, pnl_pct=info["pnl_pct"], bars_seen=seen)
        _attach_program_review(writer, entry_id, status, content, info, user_id)
        return status, seen

    if seen >= max(1, int(verify_bars)):
        writer.finalize(entry_id, status=STATUS_UNRESOLVED, bars_seen=seen)
        _attach_program_review(writer, entry_id, STATUS_UNRESOLVED, content, info, user_id)
        return STATUS_UNRESOLVED, seen

    writer.update_pending_progress(entry_id, bars_seen=seen)
    return STATUS_PENDING, seen


#: 连续取不到数据多少次后判定「这条永远结算不了」。
#:
#: 典型触发是交易所/品种组合本身无效（实测见过 ``GATEIO/NVDA`` —— 美股挂在
#: 加密交易所下，TradingView 永远无数据）。原先它会**无限期静默重试**：
#: 每轮都 ``skipped_no_data += 1``，既不改状态也不留痕迹，用户界面上就是
#: 一条永远停在「待验证」的记录，看不出为什么。
MAX_NO_DATA_ATTEMPTS = 5


def _note_no_data(entry_id: str, content: dict[str, Any], logger) -> None:
    """取不到数据时累计次数并告警，不能无限静默重试。

    典型触发是交易所/品种组合本身无效（实测见过 ``GATEIO/NVDA`` —— 美股挂在
    加密交易所下，TradingView 永远无数据）。原先它会**无限期静默重试**：
    每轮都只 ``skipped_no_data += 1``，既不改状态也不留痕迹，用户界面上就是
    一条永远停在「待验证」的记录，看不出为什么。

    **不改状态**：``pending → unresolved`` 的语义是「窗口内未触及价位」，而这里
    是「压根取不到行情」，两者不是一回事；凭空转成 unresolved 会把一条从未
    参与统计的记录混进终态。所以只记次数 + 报错，处置权留给运维/用户。

    计数必须**从库里读当前值**，不能用调用方传进来的 ``content`` —— 那是
    ``list_pending`` 在本轮开始时的快照，每轮都是同一个值，计数器永远停在 1。
    """
    try:
        from pa_agent.storage.db import get_hub

        row = get_hub().query(
            "SELECT content_json FROM experience_entries WHERE entry_id = ?",
            (entry_id,),
        )
        current: dict[str, Any] = {}
        if row:
            try:
                loaded = json.loads(row[0]["content_json"])
                if isinstance(loaded, dict):
                    current = loaded
            except (json.JSONDecodeError, KeyError, TypeError):
                current = {}
        if not current:                      # 读不到就用调用方的，至少别丢这一次
            current = dict(content)

        n = int(current.get("_no_data_attempts") or 0) + 1
        current["_no_data_attempts"] = n
        get_hub().execute(
            "UPDATE experience_entries SET content_json = ? WHERE entry_id = ?",
            (json.dumps(current, ensure_ascii=False), entry_id),
        )
        if n >= MAX_NO_DATA_ATTEMPTS:
            logger.error(
                "experience %s (%s/%s/%s) has failed to fetch bars %d times — "
                "this entry can never settle; exchange/symbol pair is likely invalid",
                entry_id, current.get("exchange"), current.get("symbol"),
                current.get("timeframe"), n,
            )
    except Exception:  # noqa: BLE001
        logger.warning("cannot record no-data attempt for %s", entry_id, exc_info=True)


def _attach_program_review(writer, entry_id, status, content, info, user_id) -> None:
    """结算即生成程序化复盘。

    **归属必须与 :meth:`finalize` 完全一致**。``content["user_id"]`` 只有
    ``save_pending`` 之后的新记录才有；存量行没有这个键 → 空串 → 回落默认用户。
    于是同一次结算里，紧挨着的两个调用给出**不同的归属答案**：``finalize``
    会先不过滤地读出记录再取记录自带的 owner（条目归属对了），而复盘侧不读，
    直接回落 admin —— 复盘挂到 admin 名下，carol 看不到自己的，admin 却读到
    一条不属于自己的。**修法就是让复盘也走同一条解析路径。**

    **失败绝不影响结算** —— 复盘是附加产物，一个附加产物挂掉不该让 TP/SL
    的判定结果丢失。
    """
    if not user_id:
        try:
            from pa_agent.storage.experience_repo import get_entry

            row = get_entry(entry_id, user_id=None)
            if row is not None:
                user_id = str(row.get("user_id") or "")
        except Exception:  # noqa: BLE001
            logger.warning("cannot resolve owner for program review: %s", entry_id)
    try:
        from pa_agent.records.review_program import build_program_review

        built = build_program_review(status=status, content=content, exit_info=info)
        # verdict / criteria 必须从 built 里取出来单独传 —— 列是取用时的热路径，
        # 只写进 payload_json 的话渲染层读不到（曾因此复盘静默地从未进提示词）。
        writer.attach_review(
            entry_id, built, model="", source="program",
            verdict=built["verdict"], reusable_criteria=built["reusable_criteria"],
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("program review attach failed for %s: %s", entry_id, exc)


def verify_pending(
    *,
    shared_source: Any = None,
    source_factory: Callable[[], Any] | None = None,
    settings: Any = None,
    verify_bars: int | None = None,
    batch: int | None = None,
    scope: tuple[str, str] | None = None,
    max_dedicated: int = 2,
    writer: ExperienceWriter | None = None,
) -> dict[str, Any]:
    """Settle as many pending records as the N-bar rule allows.

    Parameters
    ----------
    shared_source:
        The app's data source. Reused only when its subscription matches the
        record on exchange + symbol + timeframe.
    source_factory:
        Zero-arg callable building a fresh data source, used when the shared
        one is on a different subscription. Without it, non-matching records
        are simply left pending (they will be settled once the user switches
        back, or when a factory is supplied).
    scope:
        **不要再从 ``settings.general.last_*`` 取** —— 那是「每次请求从会话
        游标派生、只回给前端」的只读字段，而 ``/api/subscribe`` 早已改写会话
        游标、不再更新它。调度器从 settings 快照读到的是**冻结的旧值**，实测
        切到 NVDA/5m 后点验证得到 ``checked=0`` —— 快照里还是上次订阅的品种。

        留这个参数只给**手工「验证」按钮**用（可显式限定范围）；后台调度
        传 ``None``，改由 :paramref:`max_dedicated` 预算约束。

    max_dedicated:
        本轮**最多**为多少条不匹配当前订阅的记录单独建数据源。用完就跳过，
        下一轮再来 —— ``list_pending`` 是由旧到新，所以不会饿死后面的。

        这才是「别打爆上游」的正确形态：**限制工作量，而不是限制正确性**。
        按品种过滤会让所有非当前品种的记录永久结算不了。

    scope（旧语义，保留兼容）:
        Optional ``(symbol, timeframe)`` — settle only records for this
        instrument. The UI passes the currently-viewed K-line so the library
        panel always reflects what is on screen.

    Returns a summary dict; never raises.
    """
    summary = {"checked": 0, "win": 0, "loss": 0, "unresolved": 0,
               "pending": 0, "skipped_no_data": 0, "skipped_misaligned": 0}
    w = writer or ExperienceWriter(logger=logger)
    try:
        n_bars = int(verify_bars if verify_bars is not None
                     else getattr(getattr(settings, "prompt", None),
                                  "experience_verify_bars", 20))
        limit = int(batch if batch is not None
                    else getattr(getattr(settings, "prompt", None),
                                 "experience_verify_batch", _DEFAULT_BATCH))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: bad settings: %s", exc)
        return summary

    try:
        pending = w.list_pending(limit=max(limit * 5, 50))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: listing failed: %s", exc)
        return summary

    used_dedicated = 0
    for entry_id, content in pending:
        if summary["checked"] >= limit:
            break
        symbol = str(content.get("symbol") or "")
        timeframe = str(content.get("timeframe") or "")
        exchange = str(content.get("exchange") or "")
        if scope and (symbol != scope[0] or timeframe != scope[1]):
            continue

        bars = _shared_fetch(shared_source, symbol, timeframe, exchange) \
            if shared_source is not None else []
        dedicated = False
        if not bars:
            if source_factory is None or used_dedicated >= max(0, int(max_dedicated)):
                summary["skipped_no_data"] += 1
                continue
            used_dedicated += 1
            dedicated = True
            bars = _DedicatedSource(source_factory, symbol, timeframe,
                                    exchange, logger).fetch()
        if not bars:
            summary["skipped_no_data"] += 1
            _note_no_data(entry_id, content, logger)
            continue

        # 价格量级兜底：bars 与本记录价格不在同一量级 → 一定不是这个标的
        if not bars_belong_to_instrument(bars, float(content.get("entry_price") or 0.0)):
            logger.warning(
                "experience verify: bars for %s/%s do not straddle entry %s — "
                "leaving record pending (possible instrument mismatch)",
                exchange, symbol, content.get("entry_price"),
            )
            summary["skipped_misaligned"] += 1
            continue

        summary["checked"] += 1
        try:
            status, _seen = settle_record(
                w, entry_id, content, bars, verify_bars=n_bars,
                # 归属从记录本身取（content["user_id"]，由 save_pending 写入）
                user_id=str(content.get("user_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("experience verify: settle failed for %s: %s",
                           entry_id, exc)
            continue
        summary[status if status in summary else "pending"] += 1

    logger.info("experience verify: %s", summary)
    return summary
