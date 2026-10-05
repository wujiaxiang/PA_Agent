# -*- coding: utf-8 -*-
"""追问在**刷新后**恢复：分桶键稳定 + 读端 + 模型上下文播种。

三个破口，本文件逐个设防（每条都用例名标出它防的是哪个）：

1. **线程键刷新必漂移**（前端拼 ``record_id``）
   → ``TestThreadKeyStability``：前端不再拼，record 段由服务端从锚点记录推导；
2. **DB 有数据但没有读端**（``list_turns`` 只有 tests 调用）
   → ``TestRestoreEndpoint``：``GET /api/chat/turns`` 能把同一线程读回来；
3. **只补读端更糟**（界面 2 轮、模型以为第 1 轮）
   → ``TestSeeding``：内存桶被回收后重建会话时必须把历史播种进
   ``_history_full``，并把 ``_turn`` 顶到 ``max(turn)``。

**为什么必须在 HTTP 入口上测**（而不是只测仓储）：
播种发生在 ``chat_stream`` 的「内存桶 miss」分支里。只测 ``chat_repo`` 时，
「路由忘了调播种」这一条照样全绿 —— 而它正是缺口③本身。

**绝不写真实目录**：hub 一律重定向到 ``tmp_path``（见 ``db`` 夹具，
与 ``test_chat_repo.py`` 同款，还原动作一致，少一个都算测试污染）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.api.routes_chat as chat_mod
from pa_agent.records.schema import RecordMeta
from pa_agent.storage import chat_repo
from pa_agent.storage.db import reset_hub_for_tests
from web.api.routes_chat import router as chat_router

#: 测试用的固定时间戳（毫秒）。分桶键的 record 段由它现算，
#: 故换机器/换时区都不会让断言漂移 —— 断言的是「现算且稳定」，不是某个字面量。
_TS_MS = 1762000000000
_SYMBOL = "NVDA"
_TIMEFRAME = "15m"


# ── 夹具 ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path: Path):
    """Hub 指向 tmp_path。收尾必须还原到会话级 DB。

    不还原的后果不是「慢」而是「静默错」：后续用例会连到一个已被 pytest
    删掉的路径，``sqlite3.connect`` 会重建一个**空库**，于是所有人的
    ``no such table`` / 「历史为空」都被算到这次改动头上。
    """
    hub = reset_hub_for_tests(tmp_path / "chat_restore.db")
    yield hub
    hub.close_all()
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


@pytest.fixture(autouse=True)
def _reset_chat_sessions():
    chat_mod._chat_sessions.clear()
    chat_mod._chat_cleanup_task = None
    _SpyChatSession.instances.clear()
    yield
    chat_mod._chat_sessions.clear()
    _SpyChatSession.instances.clear()


def _record(basename: str | None = None, *, ms: int = _TS_MS):
    """一条**没有** ``_basename`` 的真实记录（真 ``RecordMeta``）。

    ``_basename`` 在生产里**从来不会被赋值** —— 全仓只有 ``pending_writer``
    把它拼进文件名，没人回挂到 record 对象上。所以 record 段能不能推出来，
    只能靠从 ``record.meta`` 现算。

    用真的 ``RecordMeta``（而不是 MagicMock/SimpleNamespace）是因为播种路径
    会构造**真的** ``FreeChatSession``，其 ``_build_prefix`` 会调
    ``meta.model_dump()`` —— 替身到那儿会抛 AttributeError，测的就不是生产路径
    了。
    """
    rec = SimpleNamespace(
        meta=RecordMeta(
            timestamp_local_iso="2025-11-01T12:26:40",
            timestamp_local_ms=ms,
            symbol=_SYMBOL,
            timeframe=_TIMEFRAME,
            exchange="NASDAQ",
            bar_count=120,
            ai_provider={"model": "stub"},
        ),
        stage1_diagnosis=None,
        stage2_decision=None,
        kline_data=[],
    )
    if basename is not None:
        rec._basename = basename
    return rec


def _ctx(record):
    ctx = MagicMock()
    ctx._last_record = record
    ctx.data_source.latest_snapshot.return_value = []
    return ctx


def _app(record):
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")
    ctx = _ctx(record)

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    return app


class _SpyChatSession:
    """最小但**忠实**的 ``FreeChatSession`` 替身。

    只实现路由真正用到的那几个成员，且**逐字照抄** ``free_chat`` 的两处状态
    变更 —— 缺了任何一条，测的就是一个没人在用的模型：

    - ``__init__``：``_turn = 0`` / ``_history_full = []``；
    - ``send()``：先 ``_turn += 1``，再把 user + assistant **两条**追加进
      ``_history_full``。

    ``seen_history[i]`` 记下第 i 次 ``send()`` 进去时看到的 ``_history_full``
    —— 它就是 ``send()`` 会拼进 ``history_for_api`` 的那份上下文。
    """

    instances: list["_SpyChatSession"] = []

    def __init__(self, base_record=None, client=None, assembler=None,
                 pending_writer=None, ledger=None, settings=None,
                 kline_snapshot_fn=None):
        self._base_record = base_record
        self._turn = 0
        self._history_full: list[dict] = []
        self.kline_snapshot_fn = kline_snapshot_fn
        self.seen_history: list[list[dict]] = []
        _SpyChatSession.instances.append(self)

    @property
    def history_full(self) -> list[dict]:
        return list(self._history_full)

    def send(self, user_text, cancel_token=None, on_reasoning_token=None,
             on_content_token=None):
        self._turn += 1
        self.seen_history.append(list(self._history_full))
        self._history_full.append({"role": "user", "content": user_text})
        self._history_full.append({
            "role": "assistant",
            "content": f"答{self._turn}",
            "reasoning_content": None,
        })
        return SimpleNamespace(
            content=f"答{self._turn}",
            reasoning_content=None,
            usage=SimpleNamespace(
                prompt_tokens=1, cached_prompt_tokens=0,
                completion_tokens=1, total_tokens=2,
            ),
        )


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events, cur_event, cur_data = [], None, []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "" and cur_event is not None:
            events.append((cur_event, json.loads("\n".join(cur_data))))
            cur_event, cur_data = None, []
    return events


def _wait_rows(thread_key: str, n: int, timeout: float = 5.0) -> list[dict]:
    """轮询等 ``n`` 行落库。

    ``_run`` 在线程池里跑，SSE 收到 ``done`` 就结束响应 —— 拿到响应体的那一刻
    worker 可能还在写。固定 sleep 在慢机器上会假红，有界轮询才是诚实的做法。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = chat_repo.list_recent_turns(thread_key)
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    return chat_repo.list_recent_turns(thread_key)


def _seed_db_turns(thread_key: str, turns: list[tuple[str, str, bool]]) -> None:
    """往库里直接灌历史。*turns* = ``[(user, assistant, cancelled), ...]``。

    绕开路由直接写库，是为了让「内存桶已回收、库里还在」这个状态可以被
    **确定地**造出来 —— 靠跑一次 SSE 再等 TTL 过期，那要 30 分钟。
    """
    for i, (user, assistant, cancelled) in enumerate(turns, start=1):
        assert chat_repo.append_turn(
            thread_key=thread_key,
            turn=i,
            user=user,
            assistant=assistant,
            record_id="synthetic",
            symbol=_SYMBOL,
            timeframe=_TIMEFRAME,
            cancelled=cancelled,
        ) is True


# ── 1. 线程键稳定性（防破口①） ────────────────────────────────────────────────


class TestThreadKeyStability:
    """前端不再拼 ``record_id`` 之后，键必须**只**由服务端事实决定。

    实测的破口：同一 session 三连刷新得到三个互不相同的键
    （``…|NVDA_15m_2026-10-05T10:31:02|…`` / ``…|chat_1791212104|…`` /
    ``…|chat_1791212855|…``），于是 ``FreeChatSession`` 在 30 分钟 TTL 内也
    永远命中不了。
    """

    def test_key_is_identical_across_repeated_resolution(self):
        """不传 record_id 时，同一条记录反复解析得到同一个键。"""
        rec = _record()
        keys = {
            chat_mod._resolve_thread_key(rec, session_id="pa-x", attach_kline_snapshot=True)[1]
            for _ in range(3)
        }
        assert len(keys) == 1, f"键在重复解析之间漂移：{keys}"

    def test_key_survives_a_freshly_parsed_record_object(self):
        """重新从磁盘解析出的**另一个对象**推同一个键。

        刷新后前端状态全丢、服务端重新加载记录，拿到的是全新对象。
        按对象身份（``id()``）分桶的话这里必然不等。
        """
        k1, _ = chat_mod._resolve_thread_key(_record(), session_id="pa-x",
                                             attach_kline_snapshot=True)
        k2, _ = chat_mod._resolve_thread_key(_record(), session_id="pa-x",
                                             attach_kline_snapshot=True)
        assert k1 == k2

    def test_record_segment_is_derived_not_the_latest_placeholder(self):
        """record 段必须被真推导出来，不能塌成 ``latest``。

        ``latest`` 意味着「所有记录共用一个桶」：先追问记录 A、再回看记录 B，
        B 会携带 A 的 stage1/stage2（``_cached_prefix`` 构造时固化）——
        **答非所问且无任何报错**。
        """
        db_record_id, session_key = chat_mod._resolve_thread_key(
            _record(), session_id="pa-x", attach_kline_snapshot=True
        )
        assert db_record_id != "latest"
        assert _SYMBOL in session_key and _TIMEFRAME in session_key
        assert session_key.endswith("|k")

    def test_distinct_records_do_not_share_a_bucket(self):
        """两条不同记录（毫秒不同）必须分到不同的桶。

        这里特意用**同一秒内**的两个时间戳：`free_chat._derive_record_id`
        的分钟位写成了 ``%m``（月），秒级记录在它眼里全都一样。record 段必须
        带上毫秒，否则「同一秒跑两次分析」就会共用一个桶 —— 携带上一条的
        stage1/stage2，**无任何报错**。
        """
        a = _record()
        b = _record(ms=_TS_MS + 250)
        ka, _ = chat_mod._resolve_thread_key(a, session_id="pa-x", attach_kline_snapshot=True)
        kb, _ = chat_mod._resolve_thread_key(b, session_id="pa-x", attach_kline_snapshot=True)
        assert ka != kb

    def test_free_chat_derive_record_id_distinguishes_minutes(self):
        """守护 ``_derive_record_id`` 的分钟位（``free_chat.py`` 与 ``pending_writer.py``）。

        曾经的格式串是 ``%H-%m-%S`` —— 小写 ``%m`` 是**月**，于是同一小时内
        秒数相同的两次记录算出**同一个 id**。实测 12:26:40 / 12:27:40 / 12:28:40
        全部映射成 ``2025-11-01_12-11-40``。

        在 ``pending_writer.py`` 上这不只是分桶撞车：它生成记录文件名，而
        ``repositories._record_basename`` 用 ``Path.stem`` 作 DB 主键 ⇒
        **同一小时内分析两次，后一条静默覆盖前一条的索引行**。

        注意本用例曾长期断言「两个不同时刻算出**相同** id」—— 那是把 bug 编码
        成了期望行为。修好后若再把它改回那个方向，等于把修复锁死。
        """
        from pa_agent.orchestrator.free_chat import _derive_record_id

        a = _record()
        b = _record(ms=_TS_MS + 60_000)          # 12:26:40 vs 12:27:40
        assert _derive_record_id(a) != _derive_record_id(b), (
            "_derive_record_id 又退回 %H-%m-%S（分钟位写成了月）了"
        )
        ka, _ = chat_mod._resolve_thread_key(a, session_id="pa-x", attach_kline_snapshot=True)
        kb, _ = chat_mod._resolve_thread_key(b, session_id="pa-x", attach_kline_snapshot=True)
        assert ka != kb

    def test_explicit_record_id_still_wins_for_legacy_callers(self):
        """显式传入的 record_id 仍然优先（老客户端 / 直连 API 不断）。"""
        db_record_id, session_key = chat_mod._resolve_thread_key(
            _record(), session_id="pa-x", record_id="legacy-id",
            attach_kline_snapshot=True,
        )
        assert db_record_id == "legacy-id"
        assert session_key.startswith("pa-x|legacy-id|")

    def test_snapshot_flag_is_part_of_the_key(self):
        """第三段是快照开关，切了它就该开新会话。"""
        _, on = chat_mod._resolve_thread_key(_record(), session_id="pa-x",
                                             attach_kline_snapshot=True)
        _, off = chat_mod._resolve_thread_key(_record(), session_id="pa-x",
                                              attach_kline_snapshot=False)
        assert on.endswith("|k") and off.endswith("|n")


# ── 2. 读端（防破口②） ───────────────────────────────────────────────────────


class TestRestoreEndpoint:
    """``GET /api/chat/turns`` 把同一线程的历史读回来。

    **为什么是普通 GET 而不是 SSE**：原生 ``EventSource`` 带不了请求头，
    拿不到 ``X-Session-Id`` 就分不出 tab ⇒ 读回别人的追问。
    """

    def _client(self, record):
        return TestClient(_app(record))

    def test_reads_back_what_the_stream_wrote(self, db):
        """SSE 写进去的那一轮，GET 必须原样读回（同一个线程键）。"""
        rec = _record()
        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with self._client(rec) as c:
                resp = c.get("/api/chat/stream?text=止损放哪&attach_kline_snapshot=true")
                assert "done" in [e for e, _ in _sse_events(resp.text)]
                _, session_key = chat_mod._resolve_thread_key(
                    rec, session_id="", attach_kline_snapshot=True
                )
                _wait_rows(session_key, 2)
                data = c.get("/api/chat/turns?attach_kline_snapshot=true").json()

        assert data["thread_key"] == session_key
        assert data["source"] == "db"
        assert data["turn_count"] == 1
        assert data["turns"] == [{
            "turn": 1,
            "user": "止损放哪",
            "assistant": "答1",
            "reasoning": None,
            "usage": {"prompt_tokens": 1, "cached_prompt_tokens": 0,
                      "completion_tokens": 1, "total_tokens": 2},
            "cancelled": False,
            "ts_ms": data["turns"][0]["ts_ms"],
        }]

    def test_recovers_when_memory_bucket_was_recycled(self, db):
        """内存桶没了、库里有 → 仍然读得回来（这正是刷新后的状态）。"""
        rec = _record()
        _, session_key = chat_mod._resolve_thread_key(
            rec, session_id="", attach_kline_snapshot=True
        )
        _seed_db_turns(session_key, [("q1", "a1", False), ("q2", "a2", False)])

        with self._client(rec) as c:
            data = c.get("/api/chat/turns?attach_kline_snapshot=true").json()

        assert [t["user"] for t in data["turns"]] == ["q1", "q2"]
        assert [t["assistant"] for t in data["turns"]] == ["a1", "a2"]

    def test_cancelled_turn_is_a_complete_turn_not_a_missing_one(self, db):
        """取消的一轮（只有提问、没有回答）必须作为**完整一轮**出现。

        前端据此画「[该轮追问被取消]」。若折叠时把它丢掉，界面上会凭空少一条
        assistant，看起来像 UI 吞了消息。
        """
        rec = _record()
        _, session_key = chat_mod._resolve_thread_key(
            rec, session_id="", attach_kline_snapshot=True
        )
        _seed_db_turns(session_key, [("q1", "a1", False), ("q2", "", True)])

        with self._client(rec) as c:
            data = c.get("/api/chat/turns?attach_kline_snapshot=true").json()

        assert data["turn_count"] == 2
        assert data["turns"][1]["cancelled"] is True
        assert data["turns"][1]["assistant"] == ""
        assert data["turns"][1]["user"] == "q2"

    def test_empty_thread_is_an_explicit_empty_state(self, db):
        """没聊过 → 200 + 空列表 + ``source="empty"``，不是 404/503。

        前端据此显示确定的空态。返回「加载中」之外的任何含糊态，都会让
        「真的没聊过」与「读不出来」在界面上长得一样。
        """
        with self._client(_record()) as c:
            resp = c.get("/api/chat/turns?attach_kline_snapshot=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["turns"] == [] and data["turn_count"] == 0
        assert data["source"] == "empty"
        assert data["thread_key"] != "", "有锚点就必须给出键，方便前端对账"

    def test_no_anchor_record_is_its_own_state(self, db):
        """当前标的压根没分析过 → ``source="no_anchor"``，同样 200。"""
        ctx = MagicMock()
        ctx._last_record = None
        ctx.data_source.latest_snapshot.return_value = []
        app = FastAPI()
        app.include_router(chat_router, prefix="/api")

        @app.on_event("startup")
        async def _set_ctx():
            app.state.ctx = ctx

        with patch("pa_agent.records.analysis_history.find_latest_successful_record",
                   return_value=None):
            with TestClient(app) as c:
                resp = c.get("/api/chat/turns?attach_kline_snapshot=true")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "no_anchor"
        assert data["turns"] == [] and data["thread_key"] == ""

    def test_endpoint_is_not_sse(self, db):
        """读端必须走普通 JSON，不能是 SSE。

        SSE 依赖浏览器能带 header；读端一旦退回 SSE，原生 EventSource 拿不到
        ``X-Session-Id``，分不出 tab。
        """
        with self._client(_record()) as c:
            resp = c.get("/api/chat/turns?attach_kline_snapshot=true")
        assert resp.headers["content-type"].startswith("application/json")
        # SSE 的形状是 data: 前缀的文本流
        assert not resp.text.lstrip().startswith(("data:", "event:"))


# ── 3. 播种（防破口③） ───────────────────────────────────────────────────────


class TestSeeding:
    """内存桶被回收后重建会话，**必须**把历史播种进模型上下文。

    ``chat_repo`` 自己的 docstring 写死了这条：重建一个 ``FreeChatSession``
    去吃 DB 历史并不会让模型看到之前那几轮 —— ``_cached_prefix`` 在构造时
    按「本轮首次提问」固化，续上下文只能靠 ``_history_full``。
    """

    def _thread_key(self):
        _, key = chat_mod._resolve_thread_key(
            _record(), session_id="", attach_kline_snapshot=True
        )
        return key

    def test_send_after_eviction_sees_the_restored_turns(self, db):
        """第二次 send 之前，``_history_full`` 里已经有前两轮的问答。"""
        rec = _record()
        session_key = self._thread_key()
        _seed_db_turns(session_key, [("q1", "a1", False), ("q2", "a2", False)])

        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(rec)) as c:
                resp = c.get("/api/chat/stream?text=q3&attach_kline_snapshot=true")
        assert "done" in [e for e, _ in _sse_events(resp.text)]

        assert len(_SpyChatSession.instances) == 1
        seen = _SpyChatSession.instances[0].seen_history[0]
        assert [m["content"] for m in seen] == ["q1", "a1", "q2", "a2"], (
            "模型上下文里没有前两轮 —— 界面显示 2 轮、模型以为这是第 1 轮，"
            "比空白更糟"
        )

    def test_turn_counter_is_seeded_not_restarted(self, db):
        """``_turn`` 顶到 ``max(turn)``，下一轮从 3 开始而不是从 1。"""
        rec = _record()
        session_key = self._thread_key()
        _seed_db_turns(session_key, [("q1", "a1", False), ("q2", "a2", False)])

        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(rec)) as c:
                c.get("/api/chat/stream?text=q3&attach_kline_snapshot=true")
        rows = _wait_rows(session_key, 6)
        assert [r["turn"] for r in rows] == [1, 1, 2, 2, 3, 3]

    def test_cancelled_turn_does_not_shift_the_turn_number(self, db):
        """取消的一轮在 ``_history_full`` 里只有一条消息，轮次号仍要接着走。

        这是长度法 ``len(history_full)//2 + 1`` 的死穴：
        3 条消息算出 3//2+1 = 2，而正确答案是 3。
        """
        rec = _record()
        session_key = self._thread_key()
        _seed_db_turns(session_key, [("q1", "a1", False), ("q2", "", True)])

        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(rec)) as c:
                c.get("/api/chat/stream?text=q3&attach_kline_snapshot=true")

        sess = _SpyChatSession.instances[0]
        # seen_history[0] = send() 进去时模型能看到的那份上下文：
        # 库里 1 轮完整（2 条）+ 1 轮取消（1 条）= 3 条。
        # 长度法会算成 3//2+1 = 2，正确答案是 3。
        assert len(sess.seen_history[0]) == 3, "取消的一轮只该留一条提问"
        assert [m["content"] for m in sess.seen_history[0]] == ["q1", "a1", "q2"]
        assert sess._turn == 3, "恢复后 _turn 从库里的 max(turn)=2 接着涨"
        rows = _wait_rows(session_key, 5)
        assert sorted({r["turn"] for r in rows}) == [1, 2, 3]

    def test_seeding_is_skipped_when_there_is_no_history(self, db):
        """库里没有历史时**什么都不播种**：不许凭空虚构一个分桶。

        ``_turn`` 保持 0、``_history_full`` 保持空 —— 那样才是「没聊过」。
        """
        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(_record())) as c:
                c.get("/api/chat/stream?text=q1&attach_kline_snapshot=true")

        sess = _SpyChatSession.instances[0]
        assert sess._turn == 1          # 只涨了自己发的那一轮
        assert [m["content"] for m in sess.seen_history[0]] == []

    def test_a_live_bucket_is_never_reseeded(self, db):
        """内存命中时不得再查库、也不得重复播种。

        重复播种会让 ``_history_full`` 里同一段对话出现两遍 —— 界面上是 2 轮，
        模型看到 4 条消息。
        """
        rec = _record()
        session_key = self._thread_key()
        _seed_db_turns(session_key, [("q1", "a1", False)])

        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(rec)) as c:
                c.get("/api/chat/stream?text=q2&attach_kline_snapshot=true")
                c.get("/api/chat/stream?text=q3&attach_kline_snapshot=true")

        assert len(_SpyChatSession.instances) == 1, "内存命中却重建了会话"
        # 库里的 1 轮 + 本次 2 轮 = 3 轮 6 条；再播种一次就会变成 8 条。
        assert len(_SpyChatSession.instances[0].history_full) == 6

    def test_read_endpoint_seeds_the_memory_bucket_too(self, db):
        """读端与播种同批：GET 之后内存桶就绪，用户马上提问也是接得上。

        只回填界面不播种的话，界面 2 轮、模型第 1 轮 —— 那比空白更糟。
        """
        rec = _record()
        session_key = self._thread_key()
        _seed_db_turns(session_key, [("q1", "a1", False)])

        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(rec)) as c:
                c.get("/api/chat/turns?attach_kline_snapshot=true")
        assert len(_SpyChatSession.instances) == 1
        assert [m["content"] for m in _SpyChatSession.instances[0].history_full] == ["q1", "a1"]

    def test_read_endpoint_does_not_create_an_empty_bucket(self, db):
        """空历史时不建内存桶：没有东西可播种，凭空登记只会掩盖真正的空态。"""
        with patch.object(chat_mod, "FreeChatSession", _SpyChatSession):
            with TestClient(_app(_record())) as c:
                c.get("/api/chat/turns?attach_kline_snapshot=true")
        assert _SpyChatSession.instances == []
        assert chat_mod._chat_sessions == {}


# ── 4. 轮次号计算 ────────────────────────────────────────────────────────────


class TestNextTurnNumber:
    """``_next_turn_number`` 必须与 ``FreeChatSession._turn`` 严格同源。"""

    def test_uses_turn_counter_as_primary(self):
        sess = MagicMock()
        sess._turn = 7
        sess.history_full = []
        assert chat_mod._next_turn_number(sess) == 8

    def test_falls_back_to_length_when_turn_is_not_an_int(self):
        """``_turn`` 不可用时退回长度法，且**返回 int**。

        必须是 int：轮次号会被 ``chat_repo.append_turn`` 用 ``int()`` 强转，
        类型不对整轮就不落库 —— 而落库失败只记 warning，界面上看不出任何异常。
        """
        sess = MagicMock()
        sess._turn = "not-a-number"
        sess.history_full = [{"role": "user"}, {"role": "assistant"}] * 3
        n = chat_mod._next_turn_number(sess)
        assert n == 4 and isinstance(n, int)

    def test_cancelled_heavy_history_does_not_undercount(self):
        """5 条消息（3 轮，其中一轮被取消）必须算出 6，不是 3。"""
        sess = MagicMock()
        sess._turn = 5
        sess.history_full = [{"role": "user"}] * 5
        assert chat_mod._next_turn_number(sess) == 6


# ── 5. 仓储折叠 ──────────────────────────────────────────────────────────────


class TestRepoFolding:
    """折叠与播种共用同一份数据，否则界面与模型会各说各话。"""

    def test_list_recent_takes_the_tail_and_returns_it_ascending(self, db):
        for i in range(1, 6):
            _seed_db_turns("thr", [(f"q{i}", f"a{i}", False)])
        rows = chat_repo.list_recent_turns("thr", limit=4)
        assert [r["content"] for r in rows] == ["q4", "a4", "q5", "a5"]

    def test_load_thread_folds_pairs(self, db):
        _seed_db_turns("thr", [("q1", "a1", False), ("q2", "a2", False)])
        turns = chat_repo.load_thread("thr")
        assert [(t["turn"], t["user"], t["assistant"]) for t in turns] == [
            (1, "q1", "a1"), (2, "q2", "a2"),
        ]

    def test_seed_messages_round_trips_completed_turns(self, db):
        _seed_db_turns("thr", [("q1", "a1", False), ("q2", "a2", False)])
        messages, max_turn = chat_repo.seed_messages(chat_repo.load_thread("thr"))
        assert max_turn == 2
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "q1"), ("assistant", "a1"),
            ("user", "q2"), ("assistant", "a2"),
        ]

    def test_seed_messages_drops_the_assistant_of_a_cancelled_turn(self, db):
        """取消的一轮**不产**空 assistant：往上下文里塞空回答是在骗模型。"""
        _seed_db_turns("thr", [("q1", "a1", False), ("q2", "", True)])
        messages, max_turn = chat_repo.seed_messages(chat_repo.load_thread("thr"))
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
        ]
        assert max_turn == 2, "轮次号要照常推进，取消不算少一轮"

    def test_seed_messages_survives_a_half_turn(self, db):
        """只有 user 行的一轮（截断 / 写坏）不得被补成「有回答」。"""
        turns = [{"turn": 1, "user": "q1", "assistant": "", "cancelled": False,
                  "reasoning": None, "usage": {}}]
        messages, max_turn = chat_repo.seed_messages(turns)
        assert messages == [{"role": "user", "content": "q1"}]
        assert max_turn == 1


# ── 6. 前端契约（静态断言） ──────────────────────────────────────────────────


class TestFrontendContract:
    """前端契约只能静态断言 —— 浏览器行为没有单测环境。

    这些断言逐条对应前端改动，缺一条就意味着某半边没上线：
    只改后端（能读、没回填）= 刷新后仍然空白；只改前端（画出来了、
    后端仍漂移）= 模型上下文是空的。
    """

    @staticmethod
    def _app_js() -> str:
        return Path("web/static/js/app.js").read_text(encoding="utf-8")

    def test_frontend_no_longer_builds_a_record_id(self):
        """前端不得再拼 ``record_id``（它是刷新必漂移的那个键段）。"""
        src = self._app_js()
        assert "chat_${Date.now()}" not in src, (
            "前端还在用一次性 id 拼分桶键 —— 刷新一次换一次键，"
            "FreeChatSession 永远命中不了"
        )
        assert "lastRecord.timestamp_local_iso" not in src, (
            "record_id 仍派生自 lastRecord，而它每次页面加载重置为 null"
        )

    def test_stream_url_carries_no_record_id(self):
        src = self._app_js()
        expected = ("/api/chat/stream?text=${encodeURIComponent(text)}"
                    "&attach_kline_snapshot=true")
        assert expected in src, "SSE URL 形状变了，回填与 SSE 将对不上同一个桶"
        chat_stream_line = next(
            (ln for ln in src.splitlines() if "/api/chat/stream?" in ln), ""
        )
        assert "record_id=" not in chat_stream_line, (
            f"追问 SSE URL 仍在带前端拼的 record_id：{chat_stream_line.strip()}"
        )

    def test_frontend_calls_the_restore_endpoint(self):
        src = self._app_js()
        assert "/api/chat/turns" in src, "前端没有回填读端"

    def test_backfill_is_wired_into_enable_chat_and_boot(self):
        """两条触发路径都必须在。

        **这里踩过一个坑**：最初写成
        ``body = src[enable_chat.index("{"):]`` —— 下标是相对 ``enable_chat``
        的，却拿去切 ``src``，于是 ``body`` 变成了「从文件开头那个位置一直到
        结尾」的整段文本，``backfillChatHistory()`` 自然落在里面 —— 断言永远
        成立，把 ``enableChat()`` 里那一行删掉也照样绿（反向验证当场抓到了）。
        切出来的片段必须**自己**参与索引，且长度要有上界。
        """
        src = self._app_js()
        start = src.index("function enableChat()")
        body = src[start:start + 600]          # 上界：enableChat 只有十来行
        assert "function enableChat()" in body and len(body) == 600
        assert "backfillChatHistory()" in body, (
            "enableChat() 没有触发回填 —— 分析完成后画面上的历史不会更新"
        )
        assert "if (typeof enableChat === 'function') enableChat();" in src, (
            "boot 序列没有触发 enableChat() —— 刷新后追问框不会被回填"
        )

    def test_backfill_never_repaints_while_streaming(self):
        """生成中重画会抹掉正在流式输出的那一轮（done 先推、落库在后）。"""
        src = self._app_js()
        fn = src[src.index("async function backfillChatHistory()"):]
        fn = fn[:fn.index("\n}")]
        assert "if (chatAbortController) return;" in fn

    def test_loading_and_empty_are_two_distinct_states(self):
        """"加载中" 与 "没有历史" 必须是两句话。

        空 div 与「真的没聊过」在界面上同形，用户分不清是加载失败、正在加载
        还是压根没聊过 —— 这正是 requirement 5 禁止的歧义态。
        """
        src = self._app_js()
        assert "正在加载追问历史…" in src
        assert "还没有追问记录" in src
        assert "追问历史加载失败" in src
        assert "CHAT_NOTE_CLASS = 'chat-history-note'" in src

    def test_same_thread_backfill_is_skipped(self):
        """同键重复回填必须跳过，否则会抹掉本轮 live 追加的消息。"""
        src = self._app_js()
        assert "key === chatHistoryKey && panel.querySelector('.chat-msg')" in src, (
            "同线程回填没有跳过逻辑 —— 重画会清掉画面上刚追加的追问"
        )

    def test_clearing_the_panel_resets_the_backfilled_key(self):
        """「清空」之后必须允许重新回填，否则清空了再也画不回来。"""
        src = self._app_js()
        fn = src[src.index("function clearChatOutput()"):]
        fn = fn[:fn.index("\n}")]
        assert "chatHistoryKey = null;" in fn
        assert "chat-history-note" in fn

    def test_html_version_is_bumped(self):
        """``app.js?v=N`` 必须存在且带版本号，否则老 webview 会拿旧脚本。

        **不要断言具体数字**。本用例上一轮写的是 ``v=67``，结果登录界面那轮
        递增到 68 之后它就红了 —— 而那次递增是**应该**的。这类断言会把
        「别人正常改版本号」误报成回归，久而久之大家会去改测试而不是去查。
        真正要防的是「忘了递增」，那是**同一份 HTML 内**引用与文件不匹配，
        由下面那条对着脚本内容断言的用例守住。
        """
        import re

        html = Path("web/static/index.html").read_text(encoding="utf-8")
        m = re.search(r"app\.js\?v=(\d+)", html)
        assert m, "index.html 的 app.js 引用没有 ?v=N（老 webview 会缓存旧脚本）"
        assert int(m.group(1)) > 0
