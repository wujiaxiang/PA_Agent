"""SSE route for two-stage AI analysis.

Spins up the ``TwoStageOrchestrator`` in a thread-pool and bridges its
callbacks to an SSE stream so the web frontend sees tokens in real time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.data.snapshot import build_display_frame
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.util.threading import CancelToken, OrchestratorEvent

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analyze"])

# Single thread-pool executor shared across analysis requests
_executor = ThreadPoolExecutor(max_workers=2)


def _run_analysis(ctx, bar_count: int, event_queue: asyncio.Queue, loop):
    """Run the two-stage pipeline (synchronous) and push events to *event_queue*."""
    # *loop* is the main-thread event loop, passed from analyze_stream

    bars_raw = ctx.data_source.latest_snapshot(bar_count)
    now_ms = int(time.time() * 1000)
    frame = build_display_frame(
        bars_raw, bar_count,
        ctx.settings.general.last_symbol,
        ctx.settings.general.last_timeframe,
        now_ms=now_ms,
    )

    # Build orchestrator (replicating main_window._build_orchestrator logic)
    orchestrator = TwoStageOrchestrator(
        client=ctx.client,
        assembler=ctx.assembler,
        router=ctx.router,
        validator=ctx.validator,
        pending_writer=ctx.pending_writer,
        exp_reader=ctx.exp_reader,
        settings=ctx.settings,
    )

    cancel_token = CancelToken()

    def on_event(ev: OrchestratorEvent) -> None:
        data = {"type": "orchestrator_event", "event": ev.name}
        loop.call_soon_threadsafe(event_queue.put_nowait, data)

    def on_reasoning(stage: str, chunk: str) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "reasoning_token", "stage": stage, "chunk": chunk,
        })

    def on_content(stage: str, chunk: str) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "content_token", "stage": stage, "chunk": chunk,
        })

    def on_prompt(stage: str, system: str, user: str) -> None:
        # 传递 prompt 全文到前端，供可折叠区域展示
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "stage_prompt", "stage": stage,
            "system": system, "user": user,
        })

    def on_stage2_files(files: list[str]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "strategy_files", "files": files,
        })

    try:
        record = orchestrator.submit(
            frame,
            cancel_token=cancel_token,
            on_event=on_event,
            on_stage1_reasoning=lambda c: on_reasoning("stage1", c),
            on_stage1_content=lambda c: on_content("stage1", c),
            on_stage2_reasoning=lambda c: on_reasoning("stage2", c),
            on_stage2_content=lambda c: on_content("stage2", c),
            on_stage_prompt=on_prompt,
            on_stage2_files=on_stage2_files,
        )

        # Serialize the final record for the frontend
        s1_diag = record.stage1_diagnosis or {}
        s2_resp = record.stage2_response or {}
        # 决策树 trace：stage1_diagnosis.gate_trace + stage2_response.decision_trace/terminal/gate_shortcircuited
        decision_tree_payload = {
            "gate_trace": s1_diag.get("gate_trace"),
            "decision_trace": s2_resp.get("decision_trace"),
            "terminal": s2_resp.get("terminal"),
            "gate_result": s1_diag.get("gate_result"),
            "gate_shortcircuited": bool(s2_resp.get("gate_shortcircuited")),
        }
        # 决策叠加层用价格字段（供前端 chart.js 绘制 entry/TP/SL 横线）
        s2_dec = record.stage2_decision or {}
        decision_overlay = {
            "order_type": s2_dec.get("order_type"),
            "order_direction": s2_dec.get("order_direction"),
            "chart_overlay_active": bool(s2_dec.get("chart_overlay_active", True)),
            "entry_price": s2_dec.get("entry_price"),
            "stop_loss_price": s2_dec.get("stop_loss_price"),
            "take_profit_price": s2_dec.get("take_profit_price"),
            "take_profit_price_2": s2_dec.get("take_profit_price_2"),
        }
        result = {
            "type": "done",
            "record": {
                "symbol": record.meta.symbol,
                "timeframe": record.meta.timeframe,
                "stage1_diagnosis": record.stage1_diagnosis,
                "stage2_decision": record.stage2_decision,
                "strategy_files_used": record.strategy_files_used,
                "usage_total": record.usage_total,
                "exception": record.exception,
                "decision_tree": decision_tree_payload,
                "decision_overlay": decision_overlay,
            },
        }
        loop.call_soon_threadsafe(event_queue.put_nowait, result)
    except Exception as exc:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "error", "message": str(exc),
        })


@router.get("/analyze/stream")
async def analyze_stream(
    request: Request,
    bar_count: int = Query(default=100, ge=2, le=5000),
):
    """SSE endpoint — triggers two-stage analysis and streams every event."""
    ctx = request.app.state.ctx
    event_queue: asyncio.Queue = asyncio.Queue()

    # Kick off analysis in thread pool
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _executor, _run_analysis, ctx, bar_count, event_queue, loop
    )

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
                event_queue.task_done()
                if event["type"] in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                yield {"event": "heartbeat", "data": "{}"}
                continue

    return EventSourceResponse(event_generator())
