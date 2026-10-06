# -*- coding: utf-8 -*-
"""增量锚点的用户隔离 —— 核心断言是**提示词内容**，不是返回值。

## 这个文件在防什么

``analysis_history.find_latest_successful_record`` 曾是
``(symbol, timeframe, exchange, directory)``，**没有 user_id**。它是三处生产
写路径的取数入口：

* ``routes_analyze`` 增量分析锚点 / 增量预检
* ``routes_chat`` 追问锚点回落

而 ``prompt_assembler.build_incremental_stage1`` 会把上一条记录的推理**原文**
注入提示词。于是同目录里只要有别人更新的记录，carol 点「增量」就会拿到 admin
的完整 stage1 推理，新记录还继承 admin 的诊断结论 —— **全程零报错**。

## 为什么核心断言必须是提示词内容

断言「``find_latest_successful_record(user_id='carol')`` 返回的
``meta.user_id == 'carol'`` 只能证明取数层过滤了，证明不了**注入被挡住**。
真正的后果发生在 ``build_incremental_stage1`` 产出的 messages 上，所以本文件
断言的是 messages 全文里**不含** admin 的哨兵串。

## 哨兵放在哪几处（这一点必须说清楚）

修复前 `build_incremental_stage1` 有**四条**能带出上一轮内容的通道，
本文件把哨兵**同时**埋进全部四条，断言只要有一条没堵住就红：

1. ``stage1_diagnosis`` → ``[2] assistant`` = ``json.dumps(诊断)``（**主通道**）
2. ``stage1_diagnosis`` / ``stage2_decision`` / ``meta`` → ``[3] user`` 的
   ``previous_summary``（``json.dumps``）
3. ``stage1_response["content"]`` → 仅当 ``stage1_diagnosis`` 为空时**原文**
   回落进 ``[2]``（``_normalize_prev_stage1_assistant_for_incremental`` 末行）
4. ``stage1_response["reasoning_content"]`` → mimo provider 下进 ``[2]`` 的
   ``reasoning_content`` 字段

注：任务描述里说的「``stage1_response["content"]`` **原文**注入 ``[2]``」在
**当前**代码里只是**兜底分支**（诊断非空时优先用诊断重建）。主通道是 1/2，
严重程度完全相同 —— admin 的逐棒结论照样整段进 carol 的提示词。

## 不 mock 任何被测函数

``build_incremental_stage1`` **不许** mock：注入发生在它内部，mock 掉等于把
被测逻辑拿走了，测试必绿而漏洞仍在。``PromptAssembler`` 用**真类**、
``PromptAssembler.build_incremental_stage1`` 走**真实现**、记录走**真
``PendingWriter.save_full``**（含双写进库）。

## 反向验证

``test_sentinel_does_travel_when_wrong_record_is_used`` 是**对照用例**：它
故意拿 admin 那条去拼提示词，并断言哨兵**确实出现在** messages 里。
没有这一条，主用例的「不含哨兵」就可能是因为哨兵根本走不到提示词而恒绿。

## 隔离

``tests/conftest.py`` 已在导入期把 ``PA_AGENT_DB_PATH`` 指到临时目录；本文件
再把 hub 换到 ``tmp_path``，并把 ``analysis_history.RECORDS_PENDING_DIR``
指到 ``tmp_path``（盘扫回落用）。**全程不碰 ``records/pa_agent.db``** ——
交付时用 ``stat -c "%Y:%s" records/pa_agent.db`` 前后比对确认。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.records import analysis_history
from pa_agent.records.pending_writer import PendingWriter

#: admin 的 stage1 里独有的可识别串。只要它出现在 carol 的提示词里就是泄漏。
ADMIN_SENTINEL = "ADMIN-PRIVATE-STAGE1-9f3a2b7c-do-not-leak"
#: carol 自己的串。**必须**出现在提示词里 —— 否则「不含哨兵」可能只是因为
#: 增量压根没跑起来（锚点为 None / 抛异常），断言就成了空断言。
CAROL_SENTINEL = "CAROL-OWN-STAGE1-4d81fe02"

EXCHANGE = "GATEIO"
SYMBOL = "BTCUSDT"
TIMEFRAME = "1h"

_T0_MS = 1_770_000_000_000  # 固定基准，避免依赖真实时钟
_HOUR_MS = 3_600_000


# ── 造数据：真实 AnalysisRecord ──────────────────────────────────────────────


def _bars(newest_ts: float, count: int) -> list[dict]:
    """``count`` 根已收盘 K 线，**最新在前**（与生产一致），含量价五元组。"""
    return [
        {
            "ts_open": newest_ts - i * _HOUR_MS,
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000.0,
            "closed": True,
        }
        for i in range(count)
    ]


def _record_dict(
    *,
    anchor_ts: float,
    user_id: str | None,
    sentinel: str,
    ts_ms: int,
) -> dict:
    """构造一条**合法且「成功」**的记录（否则取数层一律跳过它）。

    ``user_id=None`` 刻意表示「``meta`` 里**没有**这个键」—— 用来固化存量行
    缺 ``user_id`` 时的判定。
    """
    iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    meta = {
        "timestamp_local_iso": iso,
        "timestamp_local_ms": ts_ms,
        "exchange": EXCHANGE,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bar_count": 5,
        "ai_provider": {"model": "unit-test", "base_url": "", "api_key": "****"},
        "decision_stance": "balanced",
        "incremental": False,
        "continuous": False,
        "last_close_bar_iso": iso,
    }
    if user_id is not None:
        meta["user_id"] = user_id
    return {
        "meta": meta,
        "kline_data": _bars(anchor_ts, 5),
        "htf_text": "",
        "stage1_messages": [],
        # 通道 3：raw 原文。诊断非空时不走这里，但埋上以防回落分支复活。
        "stage1_response": {
            "content": f'{{"note":"{sentinel}"}}',
            # 通道 4：mimo provider 下的 reasoning。
            "reasoning_content": f"内部推理 {sentinel}",
        },
        # 通道 1：诊断（非空 ⇒ `[2]` assistant 由它重建）。
        "stage1_diagnosis": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "detected_patterns": [],
            "diagnosis_confidence": 60,
            "bar_by_bar_summary": [f"逐棒结论 {sentinel}"],
        },
        "stage2_messages": [],
        "stage2_response": {},
        # 通道 2：`[3]` user 的 previous_summary 也带 stage2_decision。
        "stage2_decision": {
            "decision": {
                "order_type": "限价单",
                "order_direction": "做多",
                "entry_price": 100.0,
                "take_profit_price": 110.0,
                "take_profit_price_2": 120.0,
                "stop_loss_price": 95.0,
                "trade_confidence": 70,
                "reasoning": f"下单理由 {sentinel}",
            },
        },
        "strategy_files_used": [],
        "experience_loaded": [],
        "exception": None,
        "usage_total": {
            "prompt_tokens": 10, "cached_prompt_tokens": 0,
            "completion_tokens": 10, "total_tokens": 20,
        },
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """hub 与扫盘根双双隔离到 tmp。"""
    from pa_agent.storage.db import reset_hub_for_tests

    pending = tmp_path / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(analysis_history, "RECORDS_PENDING_DIR", pending)

    db_file = tmp_path / "iso.db"
    reset_hub_for_tests(db_file)
    analysis_history.invalidate_latest_record_cache()
    try:
        yield SimpleNamespace_(pending=pending, db_file=db_file, tmp_path=tmp_path)
    finally:
        analysis_history.invalidate_latest_record_cache()
        reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


class SimpleNamespace_:
    """避免 ``types.SimpleNamespace`` 与本模块内同名变量混淆。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _seed(env, *, user_id: str | None, sentinel: str, ts_ms: int) -> Path:
    """经**真实** ``PendingWriter.save_full`` 落一条记录（文件 + 双写进库）。

    刻意走生产写入链路而不是 ``write_text``：读端已改为**优先查库**，只写文件
    的记录在库侧查不到，用例就会因为「构造方式不对」而测不到本模块声称的东西。
    """
    from pa_agent.records.schema import AnalysisRecord

    writer = PendingWriter(pending_dir=env.pending)
    record = AnalysisRecord.model_validate(
        _record_dict(
            anchor_ts=1_700_000_000.0,
            user_id=user_id,
            sentinel=sentinel,
            ts_ms=ts_ms,
        )
    )
    path = writer.save_full(record, user_id=user_id or "")
    # **显式把 mtime 对齐到分析时刻**。盘扫按文件 mtime 倒序，而本容器文件系统
    # 的 mtime 粒度粗到两条连续写入的记录可能落在同一时间戳上 ⇒ 排序退化成
    # rglob 的任意目录序 ⇒ 「哪条更新」随运行而变。真实生产里 admin 那条就是
    # 更晚写进去的，这里把它写实。
    os.utime(path, (ts_ms / 1000.0, ts_ms / 1000.0))
    analysis_history.invalidate_latest_record_cache()
    return path


def _make_frame(newest_ts: float, n: int = 7):
    """造一帧 K 线。``ts_open`` 必须覆盖锚点，否则增量拿不到 delta。"""
    from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

    bars = tuple(
        KlineBar(
            seq=i + 1,
            ts_open=float(newest_ts - i * _HOUR_MS),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1000.0,
            closed=True,
        )
        for i in range(n)
    )
    return KlineFrame(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        bars=bars,
        indicators=IndicatorBundle(
            ema20=tuple(100.0 + i for i in range(n)),
            atr14=tuple(1.0 for _ in range(n)),
        ),
        snapshot_ts_local_ms=int(newest_ts * 1000),
    )


def _make_assembler(tmp_path: Path):
    """**真** ``PromptAssembler``。有真实提示词目录就用真的，否则用空目录。

    刻意不 mock：``build_incremental_stage1`` 就是被测逻辑本身。
    """
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.config.paths import PROMPT_DIR

    return PromptAssembler(prompt_dir=PROMPT_DIR if PROMPT_DIR.is_dir() else tmp_path)


def _flatten(messages: list[dict]) -> str:
    """把 messages 压成一段文本 —— 哨兵可能在 content，也可能在 reasoning_content。"""
    chunks: list[str] = []
    for m in messages:
        chunks.append(str(m.get("content") or ""))
        extra = m.get("reasoning_content")
        if extra:
            chunks.append(str(extra))
        for key, val in (m.items() if isinstance(m, dict) else ()):
            if key not in ("role", "content", "reasoning_content"):
                chunks.append(json.dumps(val, ensure_ascii=False, default=str))
    return "\n".join(chunks)


def _snippet(blob: str, needle: str, width: int = 260) -> str:
    """截出哨兵在提示词里的**上下文原文**。

    只断言「哨兵在不在」看不出泄漏的形态；把命中位置前后各截一段印进报错，
    反向验证时才能亲眼看到 admin 的推理是以什么样子进了 carol 的提示词。
    """
    idx = blob.find(needle)
    if idx < 0:
        return "(哨兵未出现在文本中)"
    lo = max(0, idx - width)
    hi = min(len(blob), idx + len(needle) + width)
    return (
        f"命中位置 offset={idx} / 全文 {len(blob)} 字符；"
        f"[{lo}:{hi}] 片段：\n---\n{blob[lo:hi]}\n---"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 一、核心：carol 的增量提示词里不含 admin 的哨兵
# ═══════════════════════════════════════════════════════════════════════════


def test_carol_incremental_prompt_excludes_admin_sentinel(env):
    """**本文件的核心断言。**

    同 (exchange, symbol, timeframe) 下有 carol 与 admin 两条记录，admin 的那条
    **更新**。carol 走真实的增量链路产出提示词，断言全文不含 admin 的哨兵串。

    链路全是真的：``PendingWriter.save_full`` → ``find_latest_successful_record``
    → ``count_new_bars_since_record`` → ``build_incremental_stage1``。
    """
    # carol 先、admin 后 —— admin 那条在「最新」的位置上，正是修复前会被选中的。
    carol_path = _seed(
        env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS
    )
    admin_path = _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    # 锚点必须带上 user_id —— 这正是生产路由必须传的那个参数。
    previous = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
        directory=env.pending, user_id="carol",
    )
    assert previous is not None, "carol 必须能取到**自己**的锚点"

    # 真·增量：当前窗口比锚点多 2 根已收盘 K 线。
    # 两条记录的 kline_data 相同 ⇒ 即使取错了记录，这一步照样成立 ——
    # **泄漏发生在拼提示词那一步，不是在算 delta 那一步**，所以下面的断言
    # 必须真的走到 `build_incremental_stage1` 才有意义。
    frame = _make_frame(1_700_000_000.0 + 2 * _HOUR_MS, n=7)
    new_bars = analysis_history.count_new_bars_since_record(frame, previous)
    assert new_bars == 2, f"锚点对不上（{new_bars}）会让增量退化成空转"

    assembler = _make_assembler(env.tmp_path)
    messages = assembler.build_incremental_stage1(frame, previous, new_bars)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]

    # ── **本文件的第一断言，且刻意排在归属断言之前** ──────────────────────
    # 修复回退时必须红在这一条、且报错里**直接印出漏进来的哨兵原文**；
    # 若把归属断言排在前面，回退时只会在「拿到的不是 carol」那里红，
    # 看不到「泄漏真的发生了」，反向验证就只剩一个间接信号。
    blob = _flatten(messages)
    assert ADMIN_SENTINEL not in blob, (
        "**跨用户数据泄漏**：admin 的 stage1 内容出现在 carol 的增量提示词里 —— "
        "取数层没有按 user_id 过滤。泄漏原文（哨兵上下文）：\n"
        + _snippet(blob, ADMIN_SENTINEL)
    )
    # 反向：carol 自己的内容**必须**在 —— 证明增量真的跑了，不是空转绿。
    assert CAROL_SENTINEL in blob, (
        "carol 自己的上一轮内容也不在提示词里 ⇒ 用例没测到东西"
    )

    # 锚点归属：carol 拿到的是**自己**那条
    assert analysis_history.resolve_record_owner(previous) == "carol"
    assert CAROL_SENTINEL in json.dumps(previous.stage1_diagnosis, ensure_ascii=False)

    # admin 的那条**不该**被删掉或改写 —— 隔离是「看不见」不是「动不得」。
    assert admin_path.is_file(), "admin 的记录文件被删了"
    assert carol_path.is_file(), "carol 的记录文件被删了"
    admin_json = json.loads(admin_path.read_text(encoding="utf-8"))
    assert admin_json["meta"]["user_id"] == "admin"
    assert ADMIN_SENTINEL in json.dumps(admin_json, ensure_ascii=False), (
        "admin 的记录被改写了 —— 本次修复只做读端过滤，不得写任何记录"
    )


def test_sentinel_does_travel_when_wrong_record_is_used(env):
    """**对照用例 / 反向验证**：哨兵确实能穿过 ``build_incremental_stage1``。

    没有这一条，上面那条「不含哨兵」就有可能是**因为哨兵根本走不到提示词**
    而恒绿（构造方式不对、哨兵埋在用不上的字段、断言看错了 message）。
    这里刻意**故意**用 admin 那条去拼提示词，并断言哨兵**出现**。

    修复回退（调用点不传 user_id）时，生产入口选中的正是 admin 这条 ⇒
    carol 的提示词里就会带着这个串。
    """
    _seed(env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS)
    _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    # 模拟「未修复」：不传 user_id ⇒ 取数层无归属概念 ⇒ 选中最新那条（admin）
    wrong = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE, directory=env.pending,
    )
    assert analysis_history.resolve_record_owner(wrong) == "admin", (
        "本用例的前提失效：不过滤时应当选中 admin。取数层若已改成默认 admin，"
        "这里的哨兵证明力要重新评估。"
    )

    frame = _make_frame(1_700_000_000.0 + 2 * _HOUR_MS, n=7)
    assembler = _make_assembler(env.tmp_path)
    messages = assembler.build_incremental_stage1(frame, wrong, 2)
    blob = _flatten(messages)

    assert ADMIN_SENTINEL in blob, (
        "哨兵竟然没进提示词 —— 说明 `build_incremental_stage1` 的注入通道变了，"
        "主用例的「不含哨兵」就成了空断言，必须重写"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 二、存量行缺 user_id 的判定
# ═══════════════════════════════════════════════════════════════════════════


def test_legacy_record_without_user_id_belongs_to_default_user(env):
    """存量记录 ``meta`` 里**没有** ``user_id`` 键 ⇒ 归默认用户（admin）。

    理由（与 ``pending_writer._resolve_owner`` 写入端同一条链）：该字段是带
    默认值后加的，旧记录读出来是 ``""``；镜像进 ``analysis_records`` 时写入端
    也是回落到 admin。若读端判成「不属于任何人」，同一条记录会在列表端可见
    （按 ``user_id='admin'`` 过滤）却在增量锚点里不可见 —— 两个读端两个答案。
    """
    from pa_agent.storage.db import DEFAULT_USER_ID

    # meta 里**不写** user_id 键，模拟存量行
    legacy_path = _seed(env, user_id="", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS)

    record = analysis_history.load_record(legacy_path)
    assert record is not None
    assert record.meta.user_id == "", "存量行的 meta 读出来应是空串"
    assert analysis_history.resolve_record_owner(record) == DEFAULT_USER_ID == "admin"

    # 按 admin 查得到
    as_admin = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
        directory=env.pending, user_id="admin",
    )
    assert as_admin is not None
    assert analysis_history.resolve_record_owner(as_admin) == "admin"

    # 按 carol 查不到（存量行不该凭空变成别人的）
    as_carol = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
        directory=env.pending, user_id="carol",
    )
    assert as_carol is None

    # 空串入参 == 回落默认用户（与经验库三档语义一致：None=不过滤 / ""=默认）
    as_blank = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
        directory=env.pending, user_id="",
    )
    assert as_blank is not None
    assert analysis_history.resolve_record_owner(as_blank) == "admin"


def test_user_id_none_means_no_filtering(env):
    """``user_id=None`` ⇒ 不过滤（桌面 GUI 的唯一取值）。

    这一档是 GUI 零行为变化的保证：GUI 四个调用点只传 ``symbol``/``timeframe``，
    取到的仍是「目录里最新的一条」，与改造前逐字相同。
    """
    _seed(env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS)
    _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    got = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE, directory=env.pending,
    )
    assert got is not None
    assert analysis_history.resolve_record_owner(got) == "admin"


def test_gui_call_sites_still_pass_no_user_id():
    """GUI 四个调用点**逐字未改**，且不传 user_id ⇒ GUI 行为零变化。

    回归护栏：将来有人「顺手」给 GUI 也传个 user_id，或把 ``user_id`` 改成
    必填参数，本用例会红。

    用 **AST** 判定而不是字符串包含 —— 断言写成 ``"user_id=" in getsource(...)``
    时，文件里别处的 ``user_id=`` 会造成假阳性，而删掉出问题的那一处仍可能
    绿（AGENTS.md「反向验证要看撤掉后哪条变红」）。这里直接遍历
    ``find_latest_successful_record`` 的**每一个**调用，断言它们的
    ``keyword`` 里没有 ``user_id``。
    """
    import ast
    import inspect

    from pa_agent.gui import analysis_prep_worker, main_window

    targets = ("find_latest_successful_record",)
    checked = 0
    for mod in (analysis_prep_worker, main_window):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in targets:
                continue
            checked += 1
            kw_names = {kw.arg for kw in node.keywords}
            assert "user_id" not in kw_names, (
                f"GUI 的调用点（{mod.__name__}:{node.lineno}）被传了 user_id="
                f"{{...}} —— 桌面端没有用户概念，这会改变它的取数范围"
            )
    assert checked >= 4, f"只找到 {checked} 个 GUI 调用点，AST 判定可能失效了"

    # GUI 用的正是「不传 user_id」这一档
    assert analysis_history._resolve_owner_scope(None) is None
    assert analysis_history._resolve_owner_scope("carol") == "carol"
    assert analysis_history._resolve_owner_scope("  carol  ") == "carol"
    assert analysis_history._resolve_owner_scope("") == "admin"


def test_cache_key_is_scoped_per_user(env):
    """进程级缓存必须**按用户分桶**。

    否则 carol 查一次把 admin 的结果（按 mtime 更新的那条）缓存下来，carol
    下次拿到仍是 admin 的记录 —— 过滤做了，但缓存把它又漏回去了。
    """
    _seed(env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS)
    _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    def ask(uid):
        got = analysis_history.find_latest_successful_record(
            symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
            directory=env.pending, user_id=uid,
        )
        return None if got is None else analysis_history.resolve_record_owner(got)

    # 交替查询，且不重新扫盘（缓存命中路径）
    assert ask("admin") == "admin"
    assert ask("carol") == "carol"
    assert ask("admin") == "admin"
    assert ask("carol") == "carol"
    assert ask("nobody") is None
    assert ask("carol") == "carol"


def test_disk_fallback_still_filters_by_owner(env, monkeypatch):
    """库查不到时回落扫盘 —— 回落**不得**放宽归属判断。

    回落存在的理由是库抖动时不能让所有人凭空多付一次全量分析；但它若不过滤，
    等于在降级路径上把泄漏重新打开。两种路径都必须过滤。
    """
    carol_path = _seed(
        env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS
    )
    admin_path = _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    # 让库查不到：把 hub 指向一个空库（表结构齐全但无行）
    from pa_agent.storage.db import reset_hub_for_tests

    empty_db = env.tmp_path / "empty.db"
    reset_hub_for_tests(empty_db)
    analysis_history.invalidate_latest_record_cache()

    got = analysis_history.find_latest_successful_record(
        symbol=SYMBOL, timeframe=TIMEFRAME, exchange=EXCHANGE,
        directory=env.pending, user_id="carol",
    )
    assert got is not None, "库为空时应回落扫盘，而不是报「无历史记录」"
    assert analysis_history.resolve_record_owner(got) == "carol", (
        "回落路径没过滤归属 —— 降级即泄漏"
    )
    assert CAROL_SENTINEL in json.dumps(got.stage1_diagnosis, ensure_ascii=False)

    assert admin_path.is_file() and carol_path.is_file()


# ═══════════════════════════════════════════════════════════════════════════
# 三、生产入口：路由必须把请求身份传下去
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def token_env(monkeypatch):
    monkeypatch.setenv("PA_AGENT_TOKEN_SECRET", "unit-test-secret-do-not-use-in-prod")
    from web.api import auth_ctx

    auth_ctx.reset_revocation_state_for_tests()


def _auth(token, user_id: str) -> dict:
    return {"Authorization": f"Bearer {token(user_id)}"}


def _make_app(env):
    """真实 app + 真实 ctx（与 ``test_record_exchange_binding`` 同形）。"""
    from pa_agent.config.settings import Settings
    from web.api.routes_analyze import router as analyze_router
    from web.api.routes_data import router as data_router

    app = FastAPI()
    app.include_router(analyze_router, prefix="/api")
    app.include_router(data_router, prefix="/api")

    settings = Settings()
    settings.general.last_data_source = "tradingview"

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.settings = settings
    ctx.data_source = MagicMock()
    ctx.data_source.latest_snapshot.return_value = []
    ctx.pending_writer = PendingWriter(pending_dir=env.pending)
    ctx.client = None
    ctx.assembler = None
    ctx.router = None
    ctx.validator = None
    ctx.exp_reader = None

    app.state.ctx = ctx
    return app, ctx


def test_incremental_precheck_endpoint_is_scoped_to_request_user(env, token_env):
    """``POST /api/analyze/incremental``：carol 通过、无记录的用户 404。

    走**真令牌** + 真身份解析（不 mock ``current_user_id``）：本条证明的是
    「路由确实把请求身份传到了取数层」，而取数层过滤另有上面几条守着。
    """
    from pa_agent.storage.auth import issue_token

    _seed(env, user_id="carol", sentinel=CAROL_SENTINEL, ts_ms=_T0_MS)
    _seed(
        env, user_id="admin", sentinel=ADMIN_SENTINEL, ts_ms=_T0_MS + 10 * _HOUR_MS
    )

    app, _ = _make_app(env)
    token = issue_token
    session_headers = {"X-Session-Id": "scope-test-session"}

    with TestClient(app) as c:
        r = c.post(
            "/api/subscribe",
            json={"kind": "tradingview", "symbol": SYMBOL,
                  "timeframe": TIMEFRAME, "exchange": EXCHANGE},
            headers=session_headers,
        )
        assert r.status_code == 200, r.text

        # carol 有自己那条 ⇒ 预检通过
        r_carol = c.post(
            "/api/analyze/incremental",
            headers={**session_headers, **_auth(token, "carol")},
        )
        assert r_carol.status_code == 200, (
            f"carol 有自己的记录却预检失败：{r_carol.status_code} {r_carol.text}"
        )

        # dave 一条都没有 ⇒ 404，**不得**回落到 admin 的那条
        r_dave = c.post(
            "/api/analyze/incremental",
            headers={**session_headers, **_auth(token, "dave")},
        )
        assert r_dave.status_code == 404, (
            f"dave 没有记录却预检通过（{r_dave.status_code}）—— "
            f"路由没有把请求身份传到取数层，或取数层没过滤"
        )
        assert "无可用历史记录" in r_dave.json()["detail"]

        # admin 自己那条仍然可用（不能因为加了过滤把管理员也挡了）
        r_admin = c.post(
            "/api/analyze/incremental",
            headers={**session_headers, **_auth(token, "admin")},
        )
        assert r_admin.status_code == 200, r_admin.text
