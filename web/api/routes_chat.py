"""SSE route for post-analysis free-chat (追问)."""
from __future__ import annotations

import asyncio
import json
import logging
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


def _touch_session(key: str, session: FreeChatSession) -> None:
    _chat_sessions[key] = {"session": session, "last_touch": time.time()}


def _get_session(key: str) -> FreeChatSession | None:
    entry = _chat_sessions.get(key)
    if entry is None:
        return None
    entry["last_touch"] = time.time()
    return entry["session"]


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
):
    """SSE endpoint for post-analysis free-chat."""
    ctx = request.app.state.ctx
    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Create or reuse a FreeChatSession anchored to the last analysis record
    record = getattr(ctx, "_last_record", None)
    if record is None:
        # Try to load latest from history
        from pa_agent.records.analysis_history import find_latest_successful_record
        record = find_latest_successful_record()

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

    ctx._last_record = record

    session_key = record_id or getattr(record, "_basename", "latest")

    session = _get_session(session_key)
    if session is None:
        session = FreeChatSession(
            base_record=record,
            client=ctx.client,
            assembler=ctx.assembler,
            pending_writer=ctx.pending_writer,
            ledger=ctx.ledger,
            settings=ctx.settings,
            kline_snapshot_fn=_kline_snapshot_fn(ctx),
        )
        _touch_session(session_key, session)

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
