"""SSE route for post-analysis free-chat (追问)."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.orchestrator.free_chat import FreeChatSession
from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

_executor = ThreadPoolExecutor(max_workers=2)

# In-memory session cache: keyed by session_key -> {"session": FreeChatSession, "last_touch": ts}
# 通过 TTL 机制（默认 30 分钟无活动）自动清理，避免内存泄漏
_CHAT_SESSION_TTL_SEC = 30 * 60
#: Per-request SSE queue depth (see routes_bars_stream for the same reasoning).
_CHAT_QUEUE_MAXSIZE = 256
_chat_sessions: dict[str, dict] = {}

# 后台清理 task 句柄，避免重复启动
_chat_cleanup_task: asyncio.Task | None = None


async def _chat_cleanup_loop():
    """周期清理过期 chat session。"""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [
            k for k, v in _chat_sessions.items()
            if now - v.get("last_touch", 0) > _CHAT_SESSION_TTL_SEC
        ]
        for k in expired:
            _chat_sessions.pop(k, None)
            logger.info("chat session expired, key=%s", k)


@router.on_event("startup")
async def _ensure_chat_cleanup():
    global _chat_cleanup_task
    if _chat_cleanup_task is None or _chat_cleanup_task.done():
        _chat_cleanup_task = asyncio.create_task(_chat_cleanup_loop())


def _touch_session(
    key: str, session: FreeChatSession, lock: threading.Lock | None = None
) -> None:
    _chat_sessions[key] = {
        "session": session,
        "last_touch": time.time(),
        # FreeChatSession.send() mutates _turn / _history_full without any internal
        # lock, so two concurrent follow-ups on the same session would interleave
        # history and duplicate turn numbers. One lock per session serialises them.
        "lock": lock if lock is not None else threading.Lock(),
    }


def _get_session(key: str) -> FreeChatSession | None:
    entry = _chat_sessions.get(key)
    if entry is None:
        return None
    entry["last_touch"] = time.time()
    return entry["session"]


def _record_matches_subscription(record, ctx) -> bool:
    """True when *record* was produced for the (symbol, timeframe) now subscribed.

    Without this check a follow-up asked after switching instruments silently
    anchored to the *previous* symbol's analysis, so the AI answered about a
    different market than the chart the user was looking at.
    """
    if record is None:
        return False
    meta = getattr(record, "meta", None)
    general = getattr(getattr(ctx, "settings", None), "general", None)
    if meta is None or general is None:
        return True  # no way to compare — keep prior behaviour
    # Only trust a comparison when both sides are real non-empty strings; any
    # other value means we cannot reliably tell, so stay permissive.
    def _mismatch(want: object, got: object) -> bool:
        if not isinstance(want, str) or not isinstance(got, str):
            return False
        want, got = want.strip(), got.strip()
        return bool(want) and bool(got) and want != got

    if _mismatch(getattr(general, "last_symbol", ""), getattr(meta, "symbol", "")):
        return False
    if _mismatch(getattr(general, "last_timeframe", ""), getattr(meta, "timeframe", "")):
        return False
    return True


def _kline_snapshot_fn(ctx):
    """Capture current kline snapshot for the chat context."""
    def _inner() -> str:
        try:
            bars_raw = ctx.data_source.latest_snapshot(20)
            lines = ["seq  | 开       | 高       | 低       | 收       | 量"]
            for b in reversed(bars_raw[-10:]):
                lines.append(f"{b.seq:>3}  | {b.open:>8.2f} | {b.high:>8.2f} | {b.low:>8.2f} | {b.close:>8.2f} | {b.volume:>.0f}")
            return "\n".join(lines)
        except Exception:
            return ""
    return _inner


@router.get("/chat/stream")
async def chat_stream(
    request: Request,
    text: str = Query(..., description="User question text"),
    record_id: str = Query(default="", description="Sidecar basename for followups"),
    attach_kline_snapshot: bool = Query(default=False, description="附加最新 K 线快照到追问 prompt"),
):
    """SSE endpoint for post-analysis free-chat."""
    ctx = request.app.state.ctx
    # Bounded: a paused tab must not accumulate tokens forever (same reasoning as
    # routes_bars_stream.SUBSCRIBER_QUEUE_MAXSIZE).
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=_CHAT_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()

    # Anchor the follow-up to an analysis of the *currently subscribed* instrument.
    # ctx._last_record is only a hint: it survives symbol switches, so using it
    # blindly answered questions about the previous market.
    record = getattr(ctx, "_last_record", None)
    if not _record_matches_subscription(record, ctx):
        record = None

    if record is None:
        # Try to load latest from history for the current instrument. Offloaded:
        # on a cache miss this rglobs + parses records — blocking file I/O.
        from pa_agent.records.analysis_history import find_latest_successful_record

        general = getattr(ctx.settings, "general", None)
        record = await asyncio.to_thread(
            find_latest_successful_record,
            symbol=getattr(general, "last_symbol", "") or "",
            timeframe=getattr(general, "last_timeframe", "") or "",
            exchange=getattr(general, "last_tradingview_exchange", "") or "",
        )
        # Only promote to the shared hint when it is genuinely the newest one;
        # previously any fallback clobbered a fresh in-memory record.
        if record is not None:
            ctx._last_record = record

    if record is None:
        event_queue.put_nowait({"type": "error", "message": "没有已完成的交易分析记录，请先进行一次分析"})
        async def _error_gen():
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
                    break
                except asyncio.TimeoutError:
                    continue
        return EventSourceResponse(_error_gen())

    meta = getattr(record, "meta", None)

    def _key_part(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    # Scope the session to the instrument so follow-ups on AAPL never inherit
    # BTCUSDT's conversation, and include the snapshot flag so toggling
    # attach_kline_snapshot actually takes effect instead of being ignored on reuse.
    session_key = "|".join(
        [
            record_id or _key_part(getattr(record, "_basename", "")) or "latest",
            _key_part(getattr(meta, "symbol", "")),
            _key_part(getattr(meta, "timeframe", "")),
            "k" if attach_kline_snapshot else "n",
        ]
    )

    session = _get_session(session_key)
    if session is None:
        # 仅当 attach_kline_snapshot=true 时附加最新 K 线快照（Phase C Task 3 SubTask 3.8）
        kline_fn = _kline_snapshot_fn(ctx) if attach_kline_snapshot else None
        session = FreeChatSession(
            base_record=record,
            client=ctx.client,
            assembler=ctx.assembler,
            pending_writer=ctx.pending_writer,
            ledger=ctx.ledger,
            settings=ctx.settings,
            kline_snapshot_fn=kline_fn,
        )
        _touch_session(session_key, session, threading.Lock())
    entry_lock = _chat_sessions[session_key]["lock"]

    def on_reasoning(c: str) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "reasoning_token", "chunk": c,
        })

    def on_content(c: str) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "content_token", "chunk": c,
        })

    def _run():
        try:
            cancel_token = CancelToken()
            with entry_lock:
                reply = session.send(text, cancel_token=cancel_token,
                                     on_reasoning_token=on_reasoning,
                                     on_content_token=on_content)
            loop.call_soon_threadsafe(event_queue.put_nowait, {
                "type": "done",
                "content": reply.content,
                "reasoning": reply.reasoning_content or "",
            })
        except Exception as exc:
            loop.call_soon_threadsafe(event_queue.put_nowait, {
                "type": "error", "message": str(exc),
            })

    loop.run_in_executor(_executor, _run)

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
                event_queue.task_done()
                if event["type"] in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield {"event": "heartbeat", "data": "{}"}
                continue

    return EventSourceResponse(event_generator())
