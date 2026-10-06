"""SSE route for two-stage AI analysis.

Spins up the ``TwoStageOrchestrator`` in a thread-pool and bridges its
callbacks to an SSE stream so the web frontend sees tokens in real time.
"""
from __future__ import annotations

import asyncio
import threading
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.data.snapshot import build_display_frame
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.analysis_history import (
    count_new_bars_since_record,
    find_latest_successful_record,
)
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.util.threading import CancelToken, OrchestratorEvent

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analyze"])

# Single thread-pool executor shared across analysis requests
_executor = ThreadPoolExecutor(max_workers=2)


def _extract_message_content(messages, index: int) -> str:
    """从 OpenAI 风格 messages list 中安全取出第 index 条的 content 字符串。

    messages 形如 ``[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]``。
    越界、非 dict、缺 content 字段时返回空串。
    """
    if not isinstance(messages, list) or index < 0 or index >= len(messages):
        return ""
    item = messages[index]
    if not isinstance(item, dict):
        return ""
    content = item.get("content", "")
    if content is None:
        return ""
    # content 可能是 str 或 list（多模态），统一转字符串
    return content if isinstance(content, str) else str(content)


def _build_raw_debug_payload(record) -> dict:
    """构造 raw_debug_payload：阶段一/二 Prompt + 原始 AI 响应 + 验证 + 异常。

    用于「原始」tab 展示 AI 请求/响应的完整原始数据。
    """
    s1_messages = record.stage1_messages or []
    s2_messages = record.stage2_messages or []

    # 验证字段：优先从 record.exception 中提取 missing/invalid_fields；
    # 若无显式记录，则用 stage1_diagnosis / stage2_decision 是否为 None 作为 valid 判断。
    exception = record.exception
    stage1_valid = record.stage1_diagnosis is not None
    stage2_valid = record.stage2_decision is not None
    stage1_missing_fields: list = []
    stage1_invalid_fields: list = []
    stage2_missing_fields: list = []
    stage2_invalid_fields: list = []
    if isinstance(exception, dict):
        stage_str = str(exception.get("stage", "")).lower()
        mf = exception.get("missing_fields") or []
        ifo = exception.get("invalid_fields") or []
        if not isinstance(mf, list):
            mf = []
        if not isinstance(ifo, list):
            ifo = []
        # exception.stage 形如「阶段一-诊断」或「阶段二-决策」
        if "阶段一" in stage_str or "stage1" in stage_str or stage_str.endswith("1"):
            stage1_missing_fields = list(mf)
            stage1_invalid_fields = list(ifo)
        elif "阶段二" in stage_str or "stage2" in stage_str or stage_str.endswith("2"):
            stage2_missing_fields = list(mf)
            stage2_invalid_fields = list(ifo)

    validation = {
        "stage1_valid": stage1_valid,
        "stage2_valid": stage2_valid,
        "stage1_missing_fields": stage1_missing_fields,
        "stage2_missing_fields": stage2_missing_fields,
        "stage1_invalid_fields": stage1_invalid_fields,
        "stage2_invalid_fields": stage2_invalid_fields,
    }

    # KV Cache 命中率（Phase E1 Task 5 SubTask 5.6）
    # 优先从 usage.cache_hit_rate_pct 读取，缺失则从 cached/prompt tokens 计算。
    stage1_cache_hit_pct = _compute_cache_hit_pct(record.stage1_response)
    stage2_cache_hit_pct = _compute_cache_hit_pct(record.stage2_response)

    return {
        "stage1_system_prompt": _extract_message_content(s1_messages, 0),
        "stage1_user_prompt": _extract_message_content(s1_messages, 1),
        "stage1_raw_response": record.stage1_response,
        "stage2_system_prompt": _extract_message_content(s2_messages, 0),
        "stage2_user_prompt": _extract_message_content(s2_messages,  1),
        "stage2_raw_response": record.stage2_response,
        "validation": validation,
        "exception": exception,
        "stage1_cache_hit_pct": stage1_cache_hit_pct,
        "stage2_cache_hit_pct": stage2_cache_hit_pct,
    }


def _compute_cache_hit_pct(raw_response) -> float | None:
    """从 LLM 原始响应中提取 KV Cache 命中率百分比。

    数据结构（参考 pa_agent.gui.main_window / debug_widget）：
        raw_response = {
            "usage": {
                "cache_hit_rate_pct": 85.0,        # 直接读取
                "prompt_tokens": 1000,
                "cached_prompt_tokens": 850,
                "cache_miss_tokens": 150,
                "completion_tokens": 200,
            },
            ...
        }

    优先级：
      1. ``usage.cache_hit_rate_pct``（已是百分比数值）
      2. ``cached_prompt_tokens / prompt_tokens * 100`` 计算得到
      3. 都缺失则返回 None
    """
    if not isinstance(raw_response, dict):
        return None
    usage = raw_response.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        hit_pct = usage.get("cache_hit_rate_pct")
        if hit_pct is not None:
            return round(float(hit_pct), 1)
        prompt_tokens = usage.get("prompt_tokens") or 0
        cached_tokens = usage.get("cached_prompt_tokens") or 0
        if prompt_tokens > 0:
            return round(cached_tokens / prompt_tokens * 100.0, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _build_debug_files_payload(record) -> dict:
    """构造 debug_files_payload：阶段一/二策略文件 + 经验库条目。

    用于「调试」tab 展示本次分析加载的策略文件与经验库。
    """
    # strategy_files_used 在 schema 中是 list[str]（扁平文件名列表）。
    # 兼容历史/外部实现可能返回 dict {stage1: [...], stage2: [...]} 的情形。
    strategy_files = record.strategy_files_used
    if isinstance(strategy_files, dict):
        stage1_files = list(strategy_files.get("stage1", []) or [])
        stage2_files = list(strategy_files.get("stage2", []) or [])
    elif isinstance(strategy_files, list):
        # 扁平 list：用 prompt_assembler.stage1_prompt_txt_files() 拆分出 stage1 静态文件，
        # 剩余归为 stage2 动态文件。失败时全部归入 stage2。
        try:
            from pa_agent.ai.prompt_assembler import stage1_prompt_txt_files
            stage1_set = set(stage1_prompt_txt_files() or [])
            stage1_files = [f for f in strategy_files if f in stage1_set]
            stage2_files = [f for f in strategy_files if f not in stage1_set]
        except Exception:  # noqa: BLE001
            stage1_files = []
            stage2_files = list(strategy_files)
    else:
        stage1_files = []
        stage2_files = []

    # experience_loaded 在 schema 中是 list[dict]，每项含 filename + case_type ('success'/'failure')。
    experience = record.experience_loaded or []
    experience_loaded_files: list[str] = []
    success_count = 0
    failure_count = 0
    for entry in experience:
        if isinstance(entry, dict):
            filename = entry.get("filename", "") or ""
            case_type = str(entry.get("case_type", "")).lower()
        else:
            # 兼容 Pydantic model 对象
            filename = getattr(entry, "filename", "") or ""
            case_type = str(getattr(entry, "case_type", "")).lower()
        if filename:
            experience_loaded_files.append(filename)
        if case_type == "success":
            success_count += 1
        elif case_type == "failure":
            failure_count += 1

    return {
        "stage1_files": stage1_files,
        "stage2_files": stage2_files,
        "experience_loaded": experience_loaded_files,
        "experience_count": {
            "success": success_count,
            "failure": failure_count,
        },
    }


def _serialize_record(record) -> dict:
    """Serialize an AnalysisRecord to the JSON payload sent via SSE done event."""
    s1_diag = record.stage1_diagnosis or {}
    s2_dec = record.stage2_decision or {}
    # 程序补全标记透传：next_bar_prediction / next_cycle_prediction 可能由程序
    # 补全（非 AI 原始输出），需把 is_program_filled 标记透传到前端，前端据此
    # 在 reasoning 文本前加「【程序补全】」前缀提示。字段不存在时默认 false。
    stage2_for_payload = record.stage2_decision
    if isinstance(stage2_for_payload, dict):
        next_bar = stage2_for_payload.get("next_bar_prediction")
        next_cycle = stage2_for_payload.get("next_cycle_prediction")
        stage2_for_payload = {**stage2_for_payload}
        if isinstance(next_bar, dict):
            stage2_for_payload["next_bar_prediction"] = {
                **next_bar,
                "is_program_filled": bool(next_bar.get("is_program_filled", False)),
            }
        if isinstance(next_cycle, dict):
            stage2_for_payload["next_cycle_prediction"] = {
                **next_cycle,
                "is_program_filled": bool(next_cycle.get("is_program_filled", False)),
            }
        # 关键修复：Stage2 实际存储结构为 {decision: {...flat decision...}, diagnosis_summary, ...}
        # 但前端 renderDecision / decision_overlay 期望扁平字段（order_type, entry_price,
        # diagnosis_confidence 等）直接在 stage2_decision 顶层。将内层 decision 的字段
        # 合并到顶层（不覆盖已有顶层字段），同时保留原 .decision 引用以防其他消费者使用。
        # 参考：tests/integration/test_gate_shortcircuit.py:54 验证了嵌套结构。
        inner_decision = stage2_for_payload.get("decision")
        if isinstance(inner_decision, dict):
            for k, v in inner_decision.items():
                if k not in stage2_for_payload:
                    stage2_for_payload[k] = v
    # 决策树 trace：stage1_diagnosis.gate_trace + stage2_decision.decision_trace/terminal/gate_shortcircuited
    decision_tree_payload = {
        "gate_trace": s1_diag.get("gate_trace"),
        "decision_trace": s2_dec.get("decision_trace"),
        "terminal": s2_dec.get("terminal"),
        "gate_result": s1_diag.get("gate_result"),
        "gate_shortcircuited": bool(s2_dec.get("gate_shortcircuited")),
    }
    # 决策叠加层用价格字段（供前端 chart.js 绘制 entry/TP/SL 横线）
    # 修复：从 stage2_for_payload（已扁平化）读取，而非原始 s2_dec（嵌套结构）
    s2_flat = stage2_for_payload if isinstance(stage2_for_payload, dict) else {}
    decision_overlay = {
        "order_type": s2_flat.get("order_type"),
        "order_direction": s2_flat.get("order_direction"),
        "chart_overlay_active": bool(s2_flat.get("chart_overlay_active", True)),
        "entry_price": s2_flat.get("entry_price"),
        "stop_loss_price": s2_flat.get("stop_loss_price"),
        "take_profit_price": s2_flat.get("take_profit_price"),
        "take_profit_price_2": s2_flat.get("take_profit_price_2"),
    }
    return {
        "symbol": record.meta.symbol,
        "timeframe": record.meta.timeframe,
        # 前端用 lastRecord.timestamp_local_iso 拼追问会话的 record_id
        # （app.js: buildChatRecordId），缺失会退化成 chat_${Date.now()}，
        # 导致每条消息都开新会话、追问上下文全部丢失。
        "timestamp_local_iso": _meta_str(record.meta, "timestamp_local_iso"),
        # 历史回看 banner 直接读 meta.timestamp_local_iso（app.js）。这里只显式
        # 拷贝前端需要的标量字段并强制转 str：整块 model_dump() 在测试的
        # MagicMock 记录下会产出不可 JSON 序列化的对象。
        "meta": {
            "timestamp_local_iso": _meta_str(record.meta, "timestamp_local_iso"),
            "symbol": _meta_str(record.meta, "symbol"),
            "timeframe": _meta_str(record.meta, "timeframe"),
            "exchange": _meta_str(record.meta, "exchange"),
        },
        "last_close_bar_iso": _derive_last_close_bar_iso(record),
        "stage1_diagnosis": record.stage1_diagnosis,
        "stage2_decision": stage2_for_payload,
        "strategy_files_used": record.strategy_files_used,
        "usage_total": record.usage_total,
        "exception": record.exception,
        "decision_tree": decision_tree_payload,
        "decision_overlay": decision_overlay,
        "raw_debug_payload": _build_raw_debug_payload(record),
        "debug_files_payload": _build_debug_files_payload(record),
    }


def _meta_str(meta: object, field: str) -> str:
    """Read *field* off a record meta object as a plain JSON-safe string."""
    value = getattr(meta, field, "")
    return "" if value is None else str(value)


def _derive_last_close_bar_iso(record) -> str:
    """提取 last_close_bar_iso，优先 meta；为空则从 kline_data / stage1 派生。

    派生链：
      1. ``record.meta.last_close_bar_iso``（新记录由 orchestrator 写入）
      2. ``record.kline_data[-1].time``（ms 时间戳）→ 本地 ISO 字符串
      3. ``record.stage1_diagnosis.bar_analysis.last_closed_bar``（直接字符串）
      4. 全部失败则返回 ""（前端不渲染 close bar span）
    """
    last_close_bar_iso = getattr(record.meta, "last_close_bar_iso", "") or ""
    if last_close_bar_iso:
        return last_close_bar_iso

    if record.kline_data:
        try:
            ts_ms = int(record.kline_data[-1].get("time", 0))
            if ts_ms > 0:
                return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, AttributeError, IndexError):
            pass

    if record.stage1_diagnosis:
        try:
            bar_analysis = record.stage1_diagnosis.get("bar_analysis", {}) or {}
            lcb = bar_analysis.get("last_closed_bar", "")
            if lcb:
                return str(lcb)
        except (TypeError, ValueError, AttributeError):
            pass

    return ""


def _build_orchestrator(ctx) -> TwoStageOrchestrator:
    """Build a TwoStageOrchestrator from the AppContext."""
    return TwoStageOrchestrator(
        client=ctx.client,
        assembler=ctx.assembler,
        router=ctx.router,
        validator=ctx.validator,
        pending_writer=ctx.pending_writer,
        exp_reader=ctx.exp_reader,
        settings=ctx.settings,
    )


def _install_callbacks(event_queue: asyncio.Queue, loop):
    """Bridge orchestrator callbacks into the SSE event queue."""
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
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "stage_prompt", "stage": stage,
            "system": system, "user": user,
        })

    def on_stage2_files(files: list[str]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, {
            "type": "strategy_files", "files": files,
        })

    return {
        "on_event": on_event,
        "on_stage1_reasoning": lambda c: on_reasoning("stage1", c),
        "on_stage1_content": lambda c: on_content("stage1", c),
        "on_stage2_reasoning": lambda c: on_reasoning("stage2", c),
        "on_stage2_content": lambda c: on_content("stage2", c),
        "on_stage_prompt": on_prompt,
        "on_stage2_files": on_stage2_files,
    }


def _request_user_id(request: Request) -> str:
    """本次请求的 user_id，取不到就回落默认用户。

    身份判定本身在 ``web.api.auth_ctx``，那里「任何情况下都不抛」。
    这里再包一层是因为本函数在**派发线程之前**被调用 —— 拿不到身份绝不能
    让整次分析起不来，回落成默认用户即可（单机部署下它本就等于正确答案）。
    """
    try:
        from web.api.auth_ctx import current_user_id

        return current_user_id(request)
    except Exception as exc:  # noqa: BLE001
        logger.debug("user identity unavailable, using default: %s", exc)
        from pa_agent.storage.db import DEFAULT_USER_ID

        return DEFAULT_USER_ID


def _run_analysis(
    ctx,
    bar_count: int,
    event_queue: asyncio.Queue,
    loop,
    incremental: bool = False,
    continuous: bool = False,
    session_id: str = "",
    user_id: str = "",
):
    """Run the two-stage pipeline (synchronous) and push events to *event_queue*.

    When ``incremental=True`` the function looks up the latest successful
    record for the current (exchange, symbol, timeframe) and passes it as
    ``previous_record`` to the orchestrator. The orchestrator then routes
    Stage 1 through ``build_incremental_stage1`` so the AI sees the prior
    conclusion as context and only reasons about newly-closed bars.

    *session_id* 解析本次请求属于哪个标签页：游标与增量锚点都按会话取，
    不读全局 settings —— 否则 A tab 看 NVDA 而全局为 BTCUSDT 时，A 的增量
    分析会捞到 BTCUSDT 的上一轮上下文喂给模型（跨标的串味）。
    空 session_id 时回落到全局 settings，行为与改造前一致。

    *user_id* 同样必须由调用方（持有 request 的异步入口）在**派发前**取好传进来：
    本函数跑在线程池里，没有 request 可问。而经验记录要靠它确定归属 ——
    经验库的写入方在后台线程，事后无从追问「这条是谁的」。
    """
    # *loop* is the main-thread event loop, passed from analyze_stream

    try:
        from web.api.session_ctx import resolve_view

        view_symbol, view_timeframe, view_exchange = resolve_view(ctx, session_id)

        # **必须按本会话游标取数**。不传则回落到进程级共享订阅（最后一次
        # POST /api/subscribe 的结果），于是 A tab 的分析会拿到 B tab 的 K 线，
        # 再被下面 build_display_frame 贴上 A 的标的标签 —— AI 分析错标的、
        # 记录还带着「对」的标签，零异常零日志。
        # 与 /api/bars（web/api/routes_data.py）保持逐字一致，别再各漏各的。
        bars_raw = ctx.data_source.latest_snapshot(
            bar_count,
            exchange=view_exchange or None,
            symbol=view_symbol or None,
            timeframe=view_timeframe or None,
        )
        now_ms = int(time.time() * 1000)
        frame = build_display_frame(
            bars_raw, bar_count,
            view_symbol,
            view_timeframe,
            now_ms=now_ms,
        )

        orchestrator = _build_orchestrator(ctx)
        cancel_token = CancelToken()
        callbacks = _install_callbacks(event_queue, loop)

        previous_record = None
        incremental_new_bar_count: int | None = None
        if incremental:
            try:
                # 游标与 frame 用的是同一组 view_* —— 锚点与数据必须同标的，
                # 否则增量上下文会串到别的品种上。
                symbol = view_symbol
                timeframe = view_timeframe
                exchange = view_exchange
                # **必须传 user_id**：分析记录是每用户私有的。不传则取数层
                # 无从判断归属，会在共享目录里挑出**最新的一条**——不管是谁的。
                # 而 `build_incremental_stage1` 会把上一条记录的诊断**原文**
                # 注入 `[2] assistant` 与 `[3] user` 的 previous_summary ⇒
                # 非 admin 点「增量」会把 admin 的完整 stage1 推理灌进自己的
                # 提示词，新记录还继承 admin 的诊断结论，且全程零报错。
                #
                # 用形参 `user_id` 而**不是** `_request_user_id(request)`：
                # 本函数跑在线程池里，没有 request 可问 —— 身份必须由持有
                # request 的异步入口在派发前取好传进来（见本函数 docstring）。
                previous_record = find_latest_successful_record(
                    symbol=symbol,
                    timeframe=timeframe,
                    exchange=exchange,
                    user_id=user_id,
                )
                if previous_record is not None:
                    incremental_new_bar_count = count_new_bars_since_record(
                        frame, previous_record
                    )
                    if incremental_new_bar_count is None:
                        # Anchor not found in current window → cannot do safe
                        # incremental; fall back to full analysis.
                        previous_record = None
                        logger.info(
                            "Incremental analysis: anchor not found in current "
                            "window, falling back to full analysis"
                        )
                    else:
                        # 阈值保护：超过 incremental_max_new_bars 则降级为完整分析
                        #
                        # **刻意保留读全局 settings**（它不是游标字段）：这是用户
                        # 偏好的调参项（L1 用户级），语义是「增量最多跨几根新
                        # K 线」，与「本 tab 在看哪个标的」无关。游标三件套
                        # （last_symbol/last_timeframe/last_tradingview_exchange）
                        # 才是会话级、必须走 resolve_view —— 见
                        # `routes_settings._CURSOR_FIELDS` 与下方增量 POST 端点。
                        max_new = int(
                            getattr(
                                ctx.settings.general,
                                "incremental_max_new_bars",
                                10,
                            )
                            or 10
                        )
                        if max_new > 0 and incremental_new_bar_count > max_new:
                            logger.info(
                                "Incremental analysis: new bars %d > threshold %d, "
                                "falling back to full analysis",
                                incremental_new_bar_count,
                                max_new,
                            )
                            previous_record = None
                            incremental_new_bar_count = None
                else:
                    logger.info(
                        "Incremental analysis: no previous successful record, "
                        "falling back to full analysis"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Incremental lookup failed: %s", exc)
                previous_record = None
                incremental_new_bar_count = None

        record = orchestrator.submit(
            frame,
            cancel_token=cancel_token,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
            incremental=incremental,
            continuous=continuous,
            user_id=user_id,
            # **落库的 exchange 只能来自本次会话游标**。KlineFrame 没有
            # exchange 字段（pa_agent/data/base.py），build_display_frame 也不收
            # 该参数，所以 view_exchange 到 record.meta.exchange 的唯一通道就是
            # 这里。以前不传 → _build_empty_record 回落全局冻结值
            # `settings.general.last_tradingview_exchange` → 用户订阅
            # NASDAQ/NVDA 却被记成 GATEIO，历史弹窗按三元组过滤一条都查不到。
            exchange=view_exchange,
            **callbacks,
        )
        record_payload = _serialize_record(record)
        # 保存最近记录引用，供追问（/api/chat/stream）路由使用。
        # **必须会话级**：挂在全局 ctx._last_record 上时，A tab 刚跑完的分析会
        # 成为 B tab 追问的锚点 —— 追问的多标签页隔离就成了装饰。
        # 无 session_id（老客户端 / 直连 API）时才回落全局，行为与改造前一致。
        if session_id:
            try:
                from pa_agent.storage.ephemeral import get_registry

                get_registry().get_or_create(session_id).last_record = record
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to store session last_record: %s", exc)
                ctx._last_record = record
        else:
            ctx._last_record = record
        # 下单机会：落 trade_records + 推 Feishu/PushPlus（后台线程，失败不影响主流程）。
        # 桌面 GUI 的等价逻辑在 MainWindow._spawn_post_order_followup；此前 Web 端
        # 完全没有调用方，导致服务端部署下告警与交易落盘都是死的。
        try:
            from web.api.order_followup import spawn_post_order_followup

            if spawn_post_order_followup(
                record=record,
                frame=frame,
                settings=ctx.settings,
                # 同上：必须用**本次分析的**品种，而不是全局订阅。
                # 分析对了而落盘/推送用错标的，用户看到的通知与记录会对不上。
                symbol=view_symbol,
                timeframe=view_timeframe,
                # 交易所必须与 symbol/timeframe 同源：内部回落的是**冻结**的
                # 全局字段，三轴不一致会写出永远结算不了的经验条目。
                exchange=view_exchange,
                data_source=ctx.data_source,
                user_id=user_id,
            ):
                logger.info(
                    "order signal dispatched for %s/%s", view_symbol, view_timeframe,
                )
            # 刚写下的 pending 最可能马上够 N 根 K 线，顺带结算一轮。
            # 后台线程，不阻塞本次分析响应；与定时器共用单飞守卫不会重叠。
            # experience_verify_mode="manual" 时这里是空操作 —— 用户选了
            # 手工验证，就不要在后台偷偷结算。
            try:
                from web.api import experience_scheduler

                threading.Thread(
                    target=experience_scheduler.run_once, args=(ctx,),
                    name="experience-verify-once", daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("experience verify kickoff failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-order followup dispatch failed: %s", exc)
        # 脱敏：递归替换 payload 中出现的 api_key（含 raw_debug_payload 内的 prompt/response）
        api_key = ""
        try:
            raw_key = ctx.settings.provider.api_key
            if isinstance(raw_key, str) and raw_key:
                api_key = raw_key
        except AttributeError:
            api_key = ""
        if api_key:
            record_payload = PendingWriter._sanitize(record_payload, api_key)
        result = {
            "type": "done",
            "record": record_payload,
            # 增量模式元数据（前端可显示是否走了增量路径）
            "incremental": previous_record is not None,
            "incremental_new_bar_count": incremental_new_bar_count,
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
    continuous: bool = Query(default=False),
):
    """SSE endpoint — triggers two-stage analysis and streams every event."""
    ctx = request.app.state.ctx
    from web.api.session_ctx import session_id_of

    session_id = session_id_of(request)
    user_id = _request_user_id(request)
    event_queue: asyncio.Queue = asyncio.Queue()

    # Kick off analysis in thread pool
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _executor, _run_analysis, ctx, bar_count, event_queue, loop, False,
        continuous, session_id, user_id,
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


@router.get("/analyze/incremental/stream")
async def analyze_incremental_stream(
    request: Request,
    bar_count: int = Query(default=100, ge=2, le=5000),
    continuous: bool = Query(default=False),
):
    """SSE endpoint — incremental analysis based on the last successful record.

    Looks up the latest successful record for the current
    (exchange, symbol, timeframe), computes how many new closed bars have
    appeared since, and passes that context to the orchestrator. The AI
    then only reasons about the new bars (Stage 1 uses
    ``build_incremental_stage1``). When no prior record exists or the new
    bar count exceeds ``incremental_max_new_bars``, falls back to a full
    analysis transparently.
    """
    ctx = request.app.state.ctx
    from web.api.session_ctx import resolve_view, session_id_of

    session_id = session_id_of(request)
    # Fast-fail when no prior record exists: 404 with a clear message so the
    # frontend can fall back to a full analysis call without paying the
    # thread-pool dispatch cost. (Frontend already disables the button via
    # /api/records?limit=1 precheck, so this is a defensive double-check.)
    symbol, timeframe, exchange = resolve_view(ctx, session_id)
    previous_record = None
    if symbol and timeframe:
        try:
            previous_record = find_latest_successful_record(
                symbol=symbol,
                timeframe=timeframe,
                exchange=exchange,
                user_id=_request_user_id(request),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("incremental precheck failed: %s", exc)
    if previous_record is None:
        raise HTTPException(
            status_code=404,
            detail="无可用历史记录，请使用完整分析",
        )

    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _executor,
        _run_analysis,
        ctx,
        bar_count,
        event_queue,
        loop,
        True,  # incremental=True
        continuous,
        session_id,
        _request_user_id(request),
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
                yield {"event": "heartbeat", "data": "{}"}
                continue

    return EventSourceResponse(event_generator())


# ── Non-SSE helper endpoint for explicit POST trigger ───────────────────
# Used by tests / programmatic callers that want a synchronous response
# rather than an SSE stream. The frontend uses the SSE endpoint above.
@router.post("/analyze/incremental")
async def analyze_incremental_post(request: Request):
    """Non-streaming POST endpoint that triggers an incremental analysis.

    Returns a small JSON status indicating whether an incremental analysis
    could be started. The actual analysis is dispatched in the same way as
    the SSE endpoint; callers wanting streamed tokens should use
    ``GET /api/analyze/incremental/stream`` instead.
    """
    ctx = request.app.state.ctx
    # **必须走 resolve_view，不能读 settings.general.last_***。
    # `last_symbol/last_timeframe/last_tradingview_exchange` 是
    # `routes_settings._CURSOR_FIELDS` 标注的**只读冻结值**：每次请求由
    # `_apply_session_cursor()` 从会话游标派生后**只回给前端**，而
    # `POST /api/subscribe`（routes_data.py:223-248）早已改为只写会话游标、
    # **不再更新这三个字段**。读它们拿到的是上一次某人订阅时的残留值 ——
    # 本会话明明在看 NASDAQ/NVDA，却按冻结的 GATEIO 去查上一轮记录：
    # 该 404 的不 404，或捞到别的标的当增量锚点（跨标的串味）。
    # 取法与同文件 :647/:603 的 SSE 端点保持逐字一致。
    from web.api.session_ctx import resolve_view, session_id_of

    symbol, timeframe, exchange = resolve_view(ctx, session_id_of(request))
    if not symbol or not timeframe:
        raise HTTPException(status_code=400, detail="缺少品种/周期")
    previous_record = find_latest_successful_record(
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        user_id=_request_user_id(request),
    )
    if previous_record is None:
        raise HTTPException(
            status_code=404,
            detail="无可用历史记录，请使用完整分析",
        )
    return {
        "status": "ok",
        "message": "Incremental analysis pre-check passed; use GET /api/analyze/incremental/stream for streaming.",
        "symbol": symbol,
        "timeframe": timeframe,
        "previous_record_id": getattr(previous_record.meta, "timestamp_local_iso", ""),
    }
