# -*- coding: utf-8 -*-
"""用户记录查询与写入 —— 写入→查询往返、用户隔离、已知行为的契约固化。

覆盖范围（AGENTS.md「分析记录：读端只查库」一节要求固化的东西）：

* ``analysis_records`` 的写入 / 列表 / 详情 / 删除
* ``user_prefs`` / ``sessions`` / ``chat_turns`` 的按用户隔离

## 为什么这些用例必须造真数据

被测的正是「数据归属对不对」。一旦 mock 掉 ``repositories.list_records`` /
``get_record_detail`` / ``PendingWriter``，剩下的就只是「断言自己刚写进去的
东西等于自己刚写进去的东西」—— 那正是本模块出问题时最难被发现的那一类
（写入成功、按用户过滤永远为空，却一路绿灯）。本文件**不 mock 任何被测函数**，
只 mock 边界（:func:`Path.unlink`、``repositories.delete_record``）来制造
故障，且每次都断言故障的可观测后果。

## 隔离

``tests/conftest.py`` 已在导入期把 ``PA_AGENT_DB_PATH`` 指向临时目录；本文件
再按用例把 hub 换到 ``tmp_path``，并把 ``routes_records.RECORDS_DIR`` 指向
``tmp_path``。**全程不碰 ``records/pa_agent.db``**（用 ``stat`` 前后比对验证，
见交付说明）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import routes_records
from web.api.routes_records import router as records_router

# ── 造数据：一条最小但**合法**的 AnalysisRecord ───────────────────────────────
# 刻意与生产形状一致（stage2_decision 里有嵌套的 decision），因为
# `_list_records` 读的是 `s2["decision"]["order_type"]`；写成扁平结构的话
# order_type 恒为 None，而断言只看「返回了几条」就看不出字段对不上。

_T0_MS = 1_770_000_000_000   # 固定基准，避免依赖真实时钟


def make_record_dict(
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    exchange: str = "GATEIO",
    index: int = 0,
    exception: dict | None = None,
    marker: str = "",
    user_id: str = "",
) -> dict:
    ts_ms = _T0_MS + index * 60_000
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    return {
        "meta": {
            "timestamp_local_iso": iso,
            "timestamp_local_ms": ts_ms,
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_count": 200,
            "ai_provider": {"model": "unit-test"},
            "decision_stance": "balanced",
            "incremental": False,
            "continuous": False,
            "last_close_bar_iso": iso,
            # 与生产同形：``two_stage._build_empty_record`` 会把 submit() 拿到的
            # user_id 盖进 meta，所以「记录自带的归属」是真实存在的通道。
            "user_id": user_id,
        },
        "kline_data": [
            {
                "ts_open": ts_ms - 3_600_000, "open": 1.0, "high": 2.0,
                "low": 0.5, "close": 1.5, "closed": True,
            },
        ],
        "htf_text": "",
        "stage1_messages": [],
        "stage1_response": {},
        "stage1_diagnosis": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "detected_patterns": [],
            "diagnosis_confidence": 60,
            "bar_by_bar_summary": [f"probe-{marker or symbol}"],
        },
        "stage2_messages": [],
        "stage2_response": {},
        "stage2_decision": {
            "decision": {
                "order_type": "限价单",
                "order_direction": "做多",
                "entry_price": 1.0,
                "take_profit_price": 2.0,
                "take_profit_price_2": 3.0,
                "stop_loss_price": 0.5,
                "trade_confidence": 70,
                "reasoning": f"marker={marker}",
            },
        },
        "strategy_files_used": [],
        "experience_loaded": [],
        "exception": exception,
        "usage_total": {
            "prompt_tokens": 10, "cached_prompt_tokens": 0,
            "completion_tokens": 10, "total_tokens": 20,
        },
    }


class Seeder:
    """把记录**按生产链路**写盘 + 双写进库。

    刻意走 ``PendingWriter``（而不是手 ``write_text``）：读端自 2026-10-05 起
    只查库，只写文件会让所有用例拿到空列表；同时手写文件会绕过
    ``_mirror_to_sqlite``，于是「user_id 有没有真的落库」这件事根本测不到。

    ``user_id`` **两条通道都走**（与生产一致）：

    1. 盖进 ``meta.user_id`` —— ``two_stage._build_empty_record`` 就是这么做的；
    2. 作为 ``save_full`` / ``save_partial`` 的关键字参数显式传。

    两条都传是有意的冗余：任一条单独坏掉时下面的归属断言都会红，而**只有**
    其中一条坏掉正是这次修复的真实故障面（原先两条都不存在）。
    """

    def __init__(self, root: Path, monkeypatch):
        from pa_agent.records.pending_writer import PendingWriter

        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(routes_records, "RECORDS_DIR", root)
        self._writer = PendingWriter(pending_dir=root)
        self._monkeypatch = monkeypatch
        self.paths: list[Path] = []

    def write(
        self,
        *,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        exchange: str = "GATEIO",
        index: int = 0,
        partial: bool = False,
        exception: dict | None = None,
        marker: str = "",
        user_id: str | None = None,
    ) -> Path:
        from pa_agent.records.schema import AnalysisRecord

        if partial and exception is None:
            exception = {"stage": "stage2", "category": "network_error", "message": "boom"}
        owner = user_id or ""
        data = make_record_dict(
            symbol=symbol, timeframe=timeframe, exchange=exchange,
            index=index, exception=exception, marker=marker, user_id=owner,
        )
        record = AnalysisRecord.model_validate(data)
        path = (
            self._writer.save_partial(record, "network_error", user_id=owner)
            if partial
            else self._writer.save_full(record, user_id=owner)
        )
        self.paths.append(path)
        return path


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """令牌密钥固定 —— 测试要签发**真令牌**走真鉴权，而不是 mock 身份函数。"""
    monkeypatch.setenv("PA_AGENT_TOKEN_SECRET", "unit-test-secret-do-not-use-in-prod")


@pytest.fixture()
def token():
    """签发真令牌（形状与 pa_agent/storage/auth.py::issue_token 一致）。"""
    from pa_agent.storage.auth import issue_token

    from web.api import auth_ctx

    auth_ctx.reset_revocation_state_for_tests()
    return lambda user_id: issue_token(user_id)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path_factory):
    """每个用例一份干净的库（读端只查库，不隔离就会读到别的用例写的行）。

    **收尾绝对不能 `close_all()`**：``get_hub()`` 是**进程内唯一**的 hub，
    关掉之后同进程后续测试拿到的是已关闭连接 ⇒ 一批与本文件无关的用例
    随机变红（顺序依赖，单独跑本文件又全绿 —— 最难查的那种）。
    正确做法是照 ``conftest.db_path_isolated`` 那一套：把 hub **指回**
    session 级临时库，由它自己的连接去接管。
    """
    import os

    from pa_agent.storage.db import reset_hub_for_tests

    d = tmp_path_factory.mktemp("db")
    hub = reset_hub_for_tests(d / "iso.db")
    yield hub
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(records_router, prefix="/api")
    ctx = MagicMock()
    ctx.settings.provider.api_key = ""
    app.state.ctx = ctx
    return app


def _auth(token, user_id: str) -> dict:
    return {"Authorization": f"Bearer {token(user_id)}"}


# ═══════════════════════════════════════════════════════════════════════════
# 一、写入 → 查询 往返
# ═══════════════════════════════════════════════════════════════════════════


def test_write_n_then_list_returns_n_newest_first(tmp_path, monkeypatch, token):
    """写 N 条 → 列表返回 N 条，且按 ``ts_local_ms`` 倒序。"""
    seed = Seeder(tmp_path, monkeypatch)
    n = 6
    for i in range(n):
        seed.write(index=i, marker=f"m{i}")

    with TestClient(_make_app()) as c:
        resp = c.get("/api/records", headers=_auth(token, "admin"))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == n, f"写了 {n} 条，列表返回 {len(body)} 条"
    stamps = [item["timestamp"] for item in body]
    assert stamps == sorted(stamps, reverse=True), f"不是倒序：{stamps}"
    # 最新写入的那条排第一（按 ts_local_ms，不是按 created_at）
    newest = _rows_from_db()[0]
    assert body[0]["record_id"] == routes_records._record_id_from_row(newest)


def test_list_limit_truncates_and_there_is_no_offset(tmp_path, monkeypatch, token):
    """``limit`` 生效；**并且没有 offset/游标** —— 这是实测出来的接口边界。

    ``GET /api/records`` 只有 ``limit``，没有 ``offset``/``before``。所以：

    * ``limit=2`` 永远返回**最新**的 2 条，连翻 4 次也是同样这 2 条；
    * 想拿第 3 条以后的数据，当前只能把 ``limit`` 调大（上限 500）。

    把它写成断言而不是留给下一个人去发现：「limit 生效」测得到，
    「没有翻页能力」测不到 —— 而后者才是前端不实现分页的原因。
    """
    seed = Seeder(tmp_path, monkeypatch)
    total = 7
    for i in range(total):
        seed.write(index=i, marker=f"m{i}")

    with TestClient(_make_app()) as c:
        first = c.get("/api/records", params={"limit": 2}, headers=_auth(token, "admin")).json()
        again = c.get("/api/records", params={"limit": 2}, headers=_auth(token, "admin")).json()
        # offset 参数不被接受（FastAPI 忽略未声明的 query 参数 ⇒ 结果不变）
        offset = c.get("/api/records", params={"limit": 2, "offset": 4},
                       headers=_auth(token, "admin")).json()
        full = c.get("/api/records", params={"limit": 500}, headers=_auth(token, "admin")).json()

    assert len(first) == 2
    assert [r["record_id"] for r in again] == [r["record_id"] for r in first], "结果应稳定"
    assert [r["record_id"] for r in offset] == [r["record_id"] for r in first], \
        "offset 被静默忽略 ⇒ 没有翻页能力"
    assert len(full) == total, "调大 limit 就能取全"
    assert {r["record_id"] for r in full} >= {r["record_id"] for r in first}


def test_detail_returns_payload_identical_to_write(tmp_path, monkeypatch, token):
    """详情按 record_id 精确取回，正文（payload_json）与写入一致。"""
    seed = Seeder(tmp_path, monkeypatch)
    path = seed.write(index=3, marker="payload-check")
    record_id = routes_records._record_id_from_row(_rows_from_db()[0])
    assert record_id == "GATEIO/BTCUSDT/1h/" + path.stem

    with TestClient(_make_app()) as c:
        resp = c.get(f"/api/records/{record_id}", headers=_auth(token, "admin"))

    assert resp.status_code == 200
    body = resp.json()
    # 正文与磁盘 JSON 逐字一致（读端只查库，payload_json 就是写进去那份）
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert body["stage1_diagnosis"] == on_disk["stage1_diagnosis"]
    assert body["stage2_decision"]["decision"]["reasoning"] == "marker=payload-check"
    assert body["meta"]["symbol"] == on_disk["meta"]["symbol"]
    # 路由派生字段也在（前端 replayRecord 依赖）
    assert body["anchor_bar_ts_ms"] == _T0_MS + 3 * 60_000 - 3_600_000


# ═══════════════════════════════════════════════════════════════════════════
# 二、用户隔离（核心）
# ═══════════════════════════════════════════════════════════════════════════


def test_user_b_list_cannot_see_user_a_records(tmp_path, monkeypatch, token):
    """用户 A 写入的记录，用户 B 的列表里一条都看不到。"""
    seed = Seeder(tmp_path, monkeypatch)
    for i in range(4):
        seed.write(index=i, user_id="carol")

    with TestClient(_make_app()) as c:
        carol = c.get("/api/records", headers=_auth(token, "carol")).json()
        admin = c.get("/api/records", headers=_auth(token, "admin")).json()

    assert len(carol) == 4, "carol 应看到自己写的 4 条"
    assert admin == [], f"admin 不该看到 carol 的记录，实际拿到 {len(admin)} 条"


def test_cross_user_detail_is_404_not_200(tmp_path, monkeypatch, token):
    """A 直接用 URL 访问 B 的 record_id 详情 → 必须 404/403，绝不是 200。

    记录详情含完整 stage1/stage2 推理，是本系统最敏感的数据之一。历史上
    这个端点是直接 ``open()`` 读文件且不过滤用户 —— 任何人拿到 URL 就能读
    到别人的完整推理。这条用例是那个洞的守墓人。
    """
    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=1, user_id="carol")
    with TestClient(_make_app()) as c:
        listing = c.get("/api/records", headers=_auth(token, "carol")).json()
        record_id = listing[0]["record_id"]

        resp = c.get(f"/api/records/{record_id}", headers=_auth(token, "admin"))

    assert resp.status_code in (403, 404), \
        f"admin 读 carol 的详情应被拒，实际 {resp.status_code}"
    assert "marker" not in resp.text, "响应体里不该带出别人的正文"


def test_request_without_token_falls_back_to_default_user(tmp_path, monkeypatch, token):
    """**回落口径**：无 Authorization 头 ⇒ 落到 ``DEFAULT_USER_ID``（"admin"）。

    固化这条是因为它同时是两件事的判据：
    1. ``ALLOW_ANONYMOUS_ADMIN=False`` 时中间件会先 401，请求根本到不了路由；
    2. 单挂路由（无中间件）或后台线程场景下，落到的就是 ``admin``。
    这条断言让「无身份 = admin」这个口径写死在测试里，而不是靠猜。
    """
    from pa_agent.storage.db import DEFAULT_USER_ID

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, user_id=DEFAULT_USER_ID)
    seed.write(index=1, user_id="carol")

    with TestClient(_make_app()) as c:
        anon = c.get("/api/records").json()

    assert len(anon) == 1, f"无身份应只看到 {DEFAULT_USER_ID} 的 1 条，实际 {len(anon)} 条"
    assert DEFAULT_USER_ID == "admin", "回落口径变了：默认用户不再是 admin"


# ═══════════════════════════════════════════════════════════════════════════
# 三、已知行为 —— 测出来当契约固化
# ═══════════════════════════════════════════════════════════════════════════


def test_include_partial_false_filters_partial_rows(tmp_path, monkeypatch, token):
    """``include_partial=false``（默认）真的过滤掉 ``status='partial'``。

    生产实测：库里 34 条 = ok 25 + partial 9，``?limit=50`` 返回 25。
    这里按同样的口径（12 ok + 5 partial + 3 error）固化。

    **两个容易被猜错的点，本用例把实测结果钉死**：

    1. 默认过滤有**两道**：SQL 层 ``status != 'partial'``，Python 层再加一道
       ``record.exception is not None``。所以 ``status='error'``（异常非空但
       没有 ``_partial_reason``）也被排除 —— 默认视图只剩 12 条 ok。
    2. ``include_partial=true`` 会把**两道都关掉**，于是 ``status='error'``
       的 3 条也一起回来 ⇒ 20 条，而不是「12 ok + 5 partial = 17」。
       开关的名字只提 partial，实际是「不要过滤任何异常记录」。
    """
    from pa_agent.storage.db import get_hub

    seed = Seeder(tmp_path, monkeypatch)
    for i in range(12):
        seed.write(index=i)
    for i in range(12, 17):
        seed.write(index=i, partial=True)
    for i in range(17, 20):
        seed.write(
            index=i,
            exception={"stage": "stage2", "category": "network_error", "message": "boom"},
        )

    statuses = [r["status"] for r in get_hub().query("SELECT status FROM analysis_records")]
    assert statuses.count("ok") == 12
    assert statuses.count("partial") == 5
    assert statuses.count("error") == 3, "异常记录应被标成 error 而不是 ok"

    with TestClient(_make_app()) as c:
        default = c.get("/api/records", headers=_auth(token, "admin")).json()
        withp = c.get("/api/records", params={"include_partial": "true"},
                      headers=_auth(token, "admin")).json()

    assert len(default) == 12, f"默认应只剩 12 条 ok，实际 {len(default)}"
    assert all(not i["has_exception"] for i in default), "默认视图不该含 exception 非空的行"
    assert include_partial_true_is_a_blunt_switch(len(withp)), (
        f"include_partial=true 的口径变了：实测 {len(withp)} 条"
    )
    assert sum(1 for i in withp if i["partial_reason"]) == 5


def include_partial_true_is_a_blunt_switch(n: int) -> bool:
    """include_partial=true 的实测口径：12 ok + 5 partial + 3 error = 20 条。"""
    return n == 20


def test_browse_all_returns_every_record_of_this_user(tmp_path, monkeypatch, token):
    """「全部品种」模式（不传过滤）返回该用户的全部记录，跨交易所/品种。"""
    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, exchange="GATEIO", symbol="BTCUSDT", timeframe="1h")
    seed.write(index=1, exchange="NASDAQ", symbol="NVDA", timeframe="5m")
    seed.write(index=2, exchange="SSE", symbol="600519", timeframe="1d")
    seed.write(index=3, exchange="GATEIO", symbol="BTCUSDT", timeframe="1h", user_id="carol")

    with TestClient(_make_app()) as c:
        body = c.get("/api/records", params={"limit": 50}, headers=_auth(token, "admin")).json()

    assert len(body) == 3, "「全部品种」= 自己的全部记录，不含他人"
    assert {i["symbol"] for i in body} == {"BTCUSDT", "NVDA", "600519"}
    # 跨品种浏览必须回传归属字段，否则前端无法给条目打标
    for item in body:
        assert item["symbol"] and item["exchange"] and item["timeframe"]


def test_partial_filter_is_anded_not_rejected(tmp_path, monkeypatch, token):
    """**部分过滤的真实行为：既不拒绝，也不退化成全扫描 —— 就是 AND。**

    ``repositories.list_records`` 把每个非空入参直接 AND 进 WHERE。所以：

    * ``?symbol=NVDA``            → 只按 symbol 收窄，exchange/timeframe 不限
    * ``?symbol=NVDA&timeframe=5m`` → 两个条件同时收窄
    * ``?exchange=&symbol=NVDA``  → 空串视同「不过滤」（`if exchange:` 短路）

    这是从 SQL 读出来的**事实**而不是设计意图的复述：早期的
    ``_file_candidates`` 自愈回退确实是「只给 symbol 就扫全盘」，那条路径
    已被整体删除（它同时是用户隔离漏洞）。本用例把它钉死，免得有人照着
    旧注释以为还在全扫描。
    """
    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, exchange="NASDAQ", symbol="NVDA", timeframe="5m")
    seed.write(index=1, exchange="NASDAQ", symbol="NVDA", timeframe="1d")
    seed.write(index=2, exchange="GATEIO", symbol="BTCUSDT", timeframe="1h")
    seed.write(index=3, exchange="NASDAQ", symbol="AMD", timeframe="5m")

    with TestClient(_make_app()) as c:
        only_symbol = c.get("/api/records", params={"symbol": "NVDA"},
                            headers=_auth(token, "admin")).json()
        symbol_tf = c.get("/api/records", params={"symbol": "NVDA", "timeframe": "5m"},
                          headers=_auth(token, "admin")).json()
        empty_exchange = c.get("/api/records", params={"exchange": "", "symbol": "NVDA"},
                               headers=_auth(token, "admin")).json()
        all_rows = c.get("/api/records", params={"limit": 500},
                         headers=_auth(token, "admin")).json()

    # 只给 symbol：按 symbol 收窄，但**跨 timeframe 命中两条**（不是全扫描的 4 条）
    assert len(only_symbol) == 2
    assert {i["timeframe"] for i in only_symbol} == {"5m", "1d"}
    # 给了两个就 AND 两个
    assert len(symbol_tf) == 1
    # 空串视同不过滤
    assert len(empty_exchange) == len(only_symbol)
    # 关键：部分过滤的返回集 ⊆ 全量，且比全量小（= 没有退化成全扫描）
    assert len(only_symbol) < len(all_rows) == 4


def test_partial_filter_query_plan_degrades_to_per_user_index_scan(tmp_path, monkeypatch):
    """部分过滤的**执行层**事实：结果正确，但索引前缀对不上 ⇒ 退化成「该用户的全扫描」。

    ``ix_rec_lookup`` 的前导列是 ``(user_id, exchange, symbol, timeframe,
    ts_local_ms DESC)``。只给 ``symbol`` 而**跳过 exchange** 时，SQLite 无法把
    这条查询的前缀对上索引，只能改用 ``ix_rec_recent (user_id)``，
    再在索引页上过滤 symbol —— 即：**该用户的每一行都会被读到**。

    实测 EXPLAIN 输出：

    * 三者齐全 → ``SEARCH analysis_records USING INDEX ix_rec_lookup
      (user_id=? AND exchange=? AND symbol=? AND timeframe=?)``
    * 只给 symbol → ``SEARCH analysis_records USING INDEX ix_rec_recent (user_id=?)``

    两者都**不是** ``SCAN analysis_records``（不是无索引裸扫），但代价差一个
    数量级。前端因此坚持「过滤条件三者齐全或三者皆空」（app.js 的
    ``loadHistoryList``）—— 这条用例是那个约束的**依据**，不是它的复述。
    """
    from pa_agent.storage.db import get_hub

    seed = Seeder(tmp_path, monkeypatch)
    for i in range(5):
        seed.write(index=i)

    def plan(sql: str, params: tuple) -> str:
        rows = get_hub().query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(dict(r)["detail"]) for r in rows)

    full = plan(
        "SELECT * FROM analysis_records WHERE user_id = ? AND exchange = ? "
        "AND symbol = ? AND timeframe = ? AND status != 'partial' "
        "ORDER BY ts_local_ms DESC LIMIT ?",
        ("admin", "GATEIO", "BTCUSDT", "1h", 50),
    )
    partial = plan(
        "SELECT * FROM analysis_records WHERE user_id = ? AND symbol = ? "
        "AND status != 'partial' ORDER BY ts_local_ms DESC LIMIT ?",
        ("admin", "BTCUSDT", 50),
    )
    browse_all = plan(
        "SELECT * FROM analysis_records WHERE user_id = ? AND status != 'partial' "
        "ORDER BY ts_local_ms DESC LIMIT ?",
        ("admin", 50),
    )

    assert "ix_rec_lookup" in full, f"三者齐全应走 ix_rec_lookup，实测：{full}"
    # 部分过滤：不是裸全表扫，但确实丢了索引前缀 → 该用户的每一行都会被读到
    assert "SCAN analysis_records" not in partial, f"不该退化成裸全表扫：{partial}"
    assert "ix_rec_recent" in partial, f"部分过滤的实测口径变了：{partial}"
    assert "ix_rec_lookup" not in partial
    # 「全部品种」与部分过滤走同一条索引 —— 代价相同，这也是前端把它默认
    # limit=50 的原因（取不到更多就不必扫更多）。
    assert browse_all == partial, f"「全部品种」应与部分过滤同代价：{browse_all} vs {partial}"


# ═══════════════════════════════════════════════════════════════════════════
# 四、失败与边界
# ═══════════════════════════════════════════════════════════════════════════


def test_delete_removes_file_then_db_row(tmp_path, monkeypatch, token):
    """删除顺序：**先删文件，再删库行**；两者都成功后行也消失。"""
    seed = Seeder(tmp_path, monkeypatch)
    path = seed.write(index=0)
    with TestClient(_make_app()) as c:
        record_id = c.get("/api/records", headers=_auth(token, "admin")).json()[0]["record_id"]
        resp = c.delete(f"/api/records/{record_id}", headers=_auth(token, "admin"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["db_deleted"] is True
    assert not path.exists(), "文件必须被删"
    assert _rows_from_db() == [], "库行必须被删"


def _rows_from_db() -> list[dict]:
    """直接读库（不经 HTTP）。排序固定为 ``ts_local_ms`` 倒序，与列表接口一致。"""
    from pa_agent.storage.db import get_hub

    return [
        dict(r)
        for r in get_hub().query("SELECT * FROM analysis_records ORDER BY ts_local_ms DESC")
    ]


def test_delete_returns_200_when_db_row_delete_fails(tmp_path, monkeypatch, token):
    """文件删成功、库行删失败 ⇒ 仍返回 **200 + db_deleted:false**（不 500）。

    这条是有意的设计（见 ``delete_record`` docstring）：用户「删掉这条」的
    意图已经达成，回 500 会诱导重试，而重试只会拿到 404。代价是留下一行
    指向已消失文件的死索引行 —— 列表接口会把它显示成一条点不开的条目。
    """
    from pa_agent.storage import repositories

    seed = Seeder(tmp_path, monkeypatch)
    path = seed.write(index=0)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repositories, "delete_record", boom)

    with TestClient(_make_app()) as c:
        record_id = c.get("/api/records", headers=_auth(token, "admin")).json()[0]["record_id"]
        resp = c.delete(f"/api/records/{record_id}", headers=_auth(token, "admin"))

    assert resp.status_code == 200, "库行删失败不得回 500"
    body = resp.json()
    assert body["ok"] is True
    assert body["db_deleted"] is False, "必须明确告诉调用方索引行还在"
    assert not path.exists(), "文件已删"
    assert len(_rows_from_db()) == 1, "库行应仍在（死索引行）"


def test_delete_returns_500_and_keeps_db_row_when_file_unlink_fails(tmp_path, monkeypatch, token):
    """文件删不掉 ⇒ 500，且**索引行一定还在**（绝不能出现「库里有、盘上没有」）。"""
    import pathlib

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0)

    real_unlink = pathlib.Path.unlink

    def failing_unlink(self, *a, **k):
        if self.suffix == ".json":
            raise PermissionError("read-only file system")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", failing_unlink)

    with TestClient(_make_app()) as c:
        record_id = c.get("/api/records", headers=_auth(token, "admin")).json()[0]["record_id"]
        resp = c.delete(f"/api/records/{record_id}", headers=_auth(token, "admin"))

    assert resp.status_code == 500
    assert len(_rows_from_db()) == 1, "文件删失败时索引行必须原样保留"


def test_read_failure_is_indistinguishable_from_empty_over_http(tmp_path, monkeypatch, token):
    """**读失败 vs 确实为空：从 HTTP 上分不出来，只有 ``hub.read_failed`` 能分。**

    这是 ``hub.query()`` 的两义性：失败也返回 ``[]``。``_list_records`` 把
    异常吞掉后返回空列表，路由照样 ``200 + []``。前端因此只能把两者渲染成
    同一个空态 —— 这正是 ``renderHistoryError`` 要单独拆一个文案的原因。

    更要紧的是第二条断言：``read_failed`` **线程局部**，而
    ``GET /api/records`` 的查询跑在 ``asyncio.to_thread`` 的工作线程里，
    **请求线程读到的 ``read_failed`` 是上一次成功读留下的 ``False``**。
    所以「查完再去主线程读标志位」这条路在本端点上是走不通的。

    制造真读失败的方式：把表真删掉（走 ``hub.query`` 自己的 except 分支），
    而不是 mock ``query`` —— mock 掉之后 ``_local.read_error`` 根本不会被置位，
    本用例就变成了在断言「mock 有没有被调用」。
    """
    from pa_agent.storage.db import get_hub

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0)
    hub = get_hub()
    assert hub.execute("DROP TABLE analysis_records") is True

    with TestClient(_make_app()) as c:
        resp = c.get("/api/records", headers=_auth(token, "admin"))

    assert resp.status_code == 200
    assert resp.json() == [], "读失败时 HTTP 层的表现与「确实为空」完全一样"

    # 线程局部：让**本线程**经历一次读失败，再让**另一个线程**成功读一次 ——
    # 后者不得抹掉前者的失败标记（这正是 `read_failed` 做成 threading.local
    # 而不是实例属性的原因：早期版本会被别的线程的一次成功读抹掉）。
    hub = get_hub()
    hub.query("SELECT * FROM analysis_records")          # 本线程：失败
    assert hub.read_failed is True, "本线程应看到 read_failed=True"

    other: dict = {}

    def _other_thread():
        get_hub().query("SELECT key FROM schema_meta")     # 另一线程：成功
        other["read_failed"] = get_hub().read_failed

    t = threading.Thread(target=_other_thread)
    t.start()
    t.join()
    assert other["read_failed"] is False, "另一个线程成功读 → 它自己的标记是干净的"
    assert hub.read_failed is True, \
        "但本线程的失败标记必须还在 —— 它曾被别的线程的任意一次成功读抹掉过"


def test_route_query_runs_off_thread_so_flag_is_unreachable_from_caller(
    tmp_path, monkeypatch, token
):
    """``GET /api/records`` 的查询跑在 ``asyncio.to_thread`` ⇒ 调用方线程看不到 read_failed。

    路由里是 ``await asyncio.to_thread(_list_records, ...)``（为了不阻塞事件循环），
    于是 ``hub._local.read_error`` 被置在**工作线程**上。请求处理线程随后去读
    ``hub.read_failed`` 拿到的是「上一次成功」留下的 False。

    所以本端点不存在「查完再读标志位」的降级判据 —— 前端只能把读失败与
    「确实为空」渲染成同一个空态（``renderHistoryError`` 是本次新增的区分）。
    """
    from pa_agent.storage.db import get_hub

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0)
    hub = get_hub()
    hub.query("SELECT * FROM analysis_records")
    assert hub.read_failed is False, "先在主线程留一个干净状态"

    hub.execute("DROP TABLE analysis_records")
    with TestClient(_make_app()) as c:
        resp = c.get("/api/records", headers=_auth(token, "admin"))

    assert resp.json() == []
    assert hub.read_failed is False, \
        "主线程看不到工作线程里的失败标记（实测如此）—— 降级判据在本端点上不可达"


def test_read_failure_after_good_rows_is_still_an_empty_list(tmp_path, monkeypatch, token):
    """**表里本来有行、读失败后仍然返回空列表** —— 与「真没有」完全同形。"""
    from pa_agent.storage.db import get_hub

    seed = Seeder(tmp_path, monkeypatch)
    for i in range(3):
        seed.write(index=i)

    with TestClient(_make_app()) as c:
        good = c.get("/api/records", headers=_auth(token, "admin")).json()
        assert len(good) == 3
        get_hub().execute("DROP TABLE analysis_records")
        broken = c.get("/api/records", headers=_auth(token, "admin")).json()

    assert broken == [], "3 行变成 []，调用方无从区分"


def test_pending_writer_stamps_owner_from_caller(tmp_path, monkeypatch, token):
    """**提醒灯（修复前是红的）**：守护 ``PendingWriter`` 把归属跟着调用方走。

    ## 它守护的是什么

    修复前，``orchestrator.submit(user_id=...)`` 把 user_id 一路传给了经验库，
    却**没有**传给 ``PendingWriter``；``_mirror_to_sqlite`` 于是调
    ``upsert_record(record, raw=data, file_path=path)`` —— 落库 ``user_id``
    **恒为** ``DEFAULT_USER_ID``。

    症状（非 admin 用户）：分析成功、文件落盘、接口 200，而
    ``GET /api/records`` 按 ``current_user_id`` 过滤 ⇒ **列表永远为空**。
    实测 carol 的令牌查 37 条记录 → ``list 0``。全程**零报错**。

    本用例修复前断言的正是这个坏行为（落 admin、carol 列表为空），
    因此**它是红的**。现在转正为断言修复后的正确行为：

    * carol 写 ⇒ 落库 ``user_id == 'carol'``，carol 列表**非空**；
    * 同样的写入对 admin **不可见**；
    * ``save_full`` / ``save_partial`` 签名里**确实有** ``user_id``。

    之所以断言「签名里有 user_id」而不只是「结果对」：结果对也可能是因为
    别的路径碰巧补上了归属，而这正是最初漏掉的那一处。
    """
    from pa_agent.records.pending_writer import PendingWriter

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, user_id="carol")

    owner = _rows_from_db()[0]["user_id"]
    assert owner == "carol", (
        "落库归属没有跟着调用方走 —— 非 admin 用户会再次遇到"
        "「分析成功、接口 200、列表永远为空」"
        f"（当前落在 {owner!r}）"
    )

    import inspect

    for fn in (PendingWriter.save_full, PendingWriter.save_partial):
        params = inspect.signature(fn).parameters
        assert "user_id" in params, (
            f"{fn.__name__} 又不接 user_id 了 —— 请更新本用例与 PendingWriter 调用方"
        )
        assert params["user_id"].kind is inspect.Parameter.KEYWORD_ONLY, \
            f"{fn.__name__} 的 user_id 必须是关键字参数（positional 会在 GUI 侧错位）"

    with TestClient(_make_app()) as c:
        carol = c.get("/api/records", headers=_auth(token, "carol")).json()
        admin = c.get("/api/records", headers=_auth(token, "admin")).json()

    assert len(carol) == 1, f"carol 应看到自己写的那 1 条，实际 {len(carol)} 条"
    assert admin == [], f"admin 不该看到 carol 的记录，实际 {len(admin)} 条"


def test_pending_writer_takes_owner_from_record_when_no_arg(
    tmp_path, monkeypatch, token
):
    """**提醒灯（修复前是红的）**：守护「归属随记录本身走」这条主通道。

    ## 为什么这条比上一条更重要

    上一条测的是显式 ``user_id=`` 入参；而**生产的真正主通道**是
    「记录自带的归属」：``two_stage._build_empty_record`` 在构造记录时就把
    ``submit()`` 的 ``user_id`` 盖进了 ``record.meta.user_id``。

    依赖它而不是「要求 12 处 ``save_partial`` 调用点各自记得传参」的理由：

    * 漏一个调用点 = 一条静默落到 admin 名下、用户永远看不到的记录，
      **且不报错**；
    * ``ctx.pending_writer`` 是**进程级单例**，若把归属挂在实例字段上，
      并发分析下 A 的归属会被 B 覆盖 —— 参数化 + 读记录自带字段没有这个竞态。

    修复前 ``PendingWriter`` 既不接 ``user_id`` 入参、也完全不读
    ``meta.user_id``，所以本用例修复前是红的。现在断言：只靠
    ``record.meta.user_id``（不传任何入参）就能落对归属。
    """
    from pa_agent.records.schema import AnalysisRecord

    seed = Seeder(tmp_path, monkeypatch)
    # 记录自带 dave 的归属，但 save_* 一个参数都不传
    record = AnalysisRecord.model_validate(
        make_record_dict(index=0, user_id="dave", marker="carried-owner")
    )
    path = seed._writer.save_full(record)

    rows = _rows_from_db()
    assert len(rows) == 1
    assert rows[0]["user_id"] == "dave", (
        "记录自带的 meta.user_id 没被采纳 —— 落库归属恒为 admin"
        f"（当前 {rows[0]['user_id']!r}）"
    )
    with TestClient(_make_app()) as c:
        dave = c.get("/api/records", headers=_auth(token, "dave")).json()
        admin = c.get("/api/records", headers=_auth(token, "admin")).json()
    assert len(dave) == 1, f"dave 应看到自己写的那 1 条，实际 {len(dave)} 条"
    assert admin == []
    assert path.exists()


def test_pending_writer_falls_back_to_default_user_without_any_owner(
    tmp_path, monkeypatch, token
):
    """无归属可用（GUI 桌面路径不传 user_id）⇒ 回落 admin，**不得抛异常**。

    这条固化的是**修复后的兼容边界**：桌面 GUI 与 ``free_chat`` 至今不传
    user_id，它们的记录仍应落在默认用户名下并正常可查。把回落默认值写成
    断言，是为了防止有人把「认不出归属」改成抛异常或写成空串 —— 空串会让
    ``WHERE user_id = 'admin'`` 一条都匹配不上，比抛异常更难排查。
    """
    from pa_agent.storage.db import DEFAULT_USER_ID
    from pa_agent.records.schema import AnalysisRecord

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0)  # 既不传 user_id，meta.user_id 也是空

    assert _rows_from_db()[0]["user_id"] == DEFAULT_USER_ID

    with TestClient(_make_app()) as c:
        body = c.get("/api/records", headers=_auth(token, DEFAULT_USER_ID)).json()
    assert len(body) == 1, f"默认用户的记录应可查，实际 {len(body)} 条"


def test_upsert_record_conflict_updates_ownership(tmp_path, monkeypatch):
    """**提醒灯（修复前是红的）**：同 ``record_id`` 复用时归属必须跟着更新。

    ## 它守护的是什么

    ``analysis_records`` 的主键是文件 stem，重复写入走
    ``ON CONFLICT(record_id) DO UPDATE SET``。而该分支的列清单**曾经不含**
    ``user_id`` —— 归属只在 INSERT 时生效。

    后果：一条先以 admin 身份落库、随后被真正的所有者复写的记录，归属会
    **永远卡在 admin**，用户看到的仍是一份空列表，且**没有任何报错**。
    生产文件名带 ``uuid8`` 后缀使同名几乎不可能，但「几乎」不等于「不会」
    ——测试播种、旧数据迁移、目录被复制都会撞上。

    修复前本用例是红的（第二次 upsert 后 ``user_id`` 仍是 admin）。
    """
    from pa_agent.records.schema import AnalysisRecord
    from pa_agent.storage import repositories

    seed = Seeder(tmp_path, monkeypatch)
    path = seed._writer.save_full(
        AnalysisRecord.model_validate(make_record_dict(index=0))
    )
    assert _rows_from_db()[0]["user_id"] == "admin", "前提：首次以 admin 落库"

    # 同一个 record_id（= 文件 stem），这次归属是 carol
    record = AnalysisRecord.model_validate(
        make_record_dict(index=0, user_id="carol", marker="re-owned")
    )
    raw = record.model_dump()
    assert repositories.upsert_record(record, raw=raw, file_path=path,
                                      user_id="carol") is True

    rows = _rows_from_db()
    assert len(rows) == 1, f"应当仍是一行（同名复用），实际 {len(rows)} 行"
    assert rows[0]["user_id"] == "carol", (
        "ON CONFLICT 的列清单里没有 user_id ⇒ 归属永远卡在首次写入时的 admin"
        f"（当前 {rows[0]['user_id']!r}）"
    )


def test_repo_delete_reports_false_when_nothing_matched(tmp_path, monkeypatch, token):
    """**提醒灯（修复前是红的）**：``db_deleted`` 必须依据**实际命中行数**。

    ## 它守护的是什么

    ``DatabaseHub.execute()`` 返回的是「**SQL 执行成功**」而不是「删掉了多少
    行」——``DELETE ... WHERE`` 匹配 0 行同样返回 ``True``。原来的
    ``delete_record`` 直接把它当「删干净了」上报，于是**接口撒谎**：
    ``db_deleted: true`` 而索引行原封不动。调用方拿它当成功信号就再也不会
    去核对，而接口撒谎比删不掉更难排查。

    修复前本用例是红的（跨用户删除时 ``db_deleted`` 报 ``true``）。
    现在直接钉住仓储口径：**0 行匹配 ⇒ False**，且行确实还在。
    """
    from pa_agent.storage import repositories

    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, user_id="carol")
    assert repositories.delete_record("no-such-stem", user_id="carol") is False, \
        "匹配 0 行却上报成功 —— 接口会在删不掉时谎报 db_deleted=true"
    assert repositories.delete_record("no-such-stem", user_id="admin") is False
    assert len(_rows_from_db()) == 1, "0 行匹配不该动任何行"


def test_delete_endpoint_scopes_to_caller(tmp_path, monkeypatch, token):
    """**提醒灯（修复前是红的）**：DELETE 必须有用户作用域。

    ## 它守护的是什么

    修复前 ``delete_record(record_id)`` 签名里**没有** ``request``，
    拿不到调用者身份，于是三重后果（实测，任何登录用户拿到别人的 record_id）：

    1. **受害者的文件被删掉** —— ``target.unlink()`` 只看路径、不看归属；
    2. **索引行删不掉** —— ``repo_delete(target.stem)`` 用仓储默认
       ``user_id='admin'``，与行上的 ``user_id`` 不匹配；
    3. **却回报 ``db_deleted: true``** —— ``hub.execute()`` 返回的是执行成功。

    净效果：别人的记录**从磁盘消失了**，索引行留下，接口还宣称删干净了。

    修复前本用例是红的（它当时断言的正是上面这三条坏行为）。现在断言：

    * 端点签名里**确实有** ``request``；
    * admin 删 carol 的记录 → **404**，且**文件与索引行都还在**；
    * carol 删自己的记录 → **200 + db_deleted:true**，两边都真的消失；
    * 404 的 detail 与「记录不存在」**完全同形** —— 区分开等于向探测者
      确认某个 id 确实存在，那本身是信息泄露。
    """
    import inspect

    from web.api.routes_records import delete_record as endpoint

    assert "request" in inspect.signature(endpoint).parameters, (
        "delete_record 又拿不到 request 了 —— 跨用户删除会重新变成裸奔"
    )

    seed = Seeder(tmp_path, monkeypatch)
    path = seed.write(index=0, user_id="carol")

    with TestClient(_make_app()) as c:
        record_id = c.get("/api/records", headers=_auth(token, "carol")).json()[0]["record_id"]

        # ── 越权删除：carol 的记录，admin 去删 ──────────────────────────────
        stolen = c.delete(f"/api/records/{record_id}", headers=_auth(token, "admin"))
        assert stolen.status_code == 404, \
            f"跨用户删除未被拒绝（实际 {stolen.status_code}）"
        assert path.exists(), "**受害者的记录文件被删掉了**"
        assert len(_rows_from_db()) == 1, "索引行不该被动"

        # ── 本人删除：必须真的删干净，且不谎报 ──────────────────────────────
        mine = c.delete(f"/api/records/{record_id}", headers=_auth(token, "carol"))
        assert mine.status_code == 200, f"本人删自己的记录失败：{mine.json()}"
        assert mine.json() == {"ok": True, "record_id": record_id, "db_deleted": True}, \
            "db_deleted 必须依据实际命中行数，而不是「SQL 执行成功」"
        assert not path.exists(), "文件必须被删"
        assert _rows_from_db() == [], "索引行必须被删"


def test_delete_404_does_not_leak_whether_the_id_exists(tmp_path, monkeypatch, token):
    """**提醒灯（修复前是红的）**：越权与不存在必须**同形**。

    ## 它守护的是什么

    加了归属判据之后，如果不刻意处理，很容易顺手做成「403 / record belongs to
    another user」——那等于向探测者**确认这个 record_id 确实存在**，本身就是
    一条信息泄露（record_id 是路径的一部分，逐段猜就能枚举）。

    修复前这个端点压根不判归属，所以「越权」返回 200 而「不存在」返回 404，
    本用例是红的（状态码与响应体都不同形）。现在两者必须**逐字一致**。
    """
    seed = Seeder(tmp_path, monkeypatch)
    seed.write(index=0, user_id="carol")

    with TestClient(_make_app()) as c:
        record_id = c.get("/api/records", headers=_auth(token, "carol")).json()[0]["record_id"]
        not_mine = c.delete(f"/api/records/{record_id}", headers=_auth(token, "admin"))
        never_existed = c.delete("/api/records/GATEIO/BTCUSDT/1h/2099-01-01_00-00-00",
                                 headers=_auth(token, "admin"))

    assert not_mine.status_code == never_existed.status_code == 404
    assert not_mine.json() == never_existed.json(), (
        f"「不属于你」与「不存在」的响应不同形 —— 等于确认该 id 存在："
        f"{not_mine.json()} vs {never_existed.json()}"
    )
    assert len(_rows_from_db()) == 1, "404 路径不得动任何行"


# ═══════════════════════════════════════════════════════════════════════════
# 五、user_prefs / sessions / chat_turns 的按用户隔离
# ═══════════════════════════════════════════════════════════════════════════


def test_user_prefs_isolated_per_user(tmp_path, monkeypatch, token):
    """``user_prefs`` 主键是 (user_id, key) ⇒ A 的覆盖不会漏给 B。"""
    from pa_agent.storage.settings_store import load_overrides, save_overrides

    assert save_overrides({"decision_stance": "aggressive"}, "carol") is True
    assert save_overrides({"decision_stance": "conservative"}, "admin") is True

    assert load_overrides("carol") == {"decision_stance": "aggressive"}
    assert load_overrides("admin") == {"decision_stance": "conservative"}
    assert load_overrides("nobody") == {}, "无覆盖的用户继承系统配置"


def test_session_row_carries_and_keeps_its_user(tmp_path, monkeypatch, token):
    """``sessions`` 的 user_id 落库后不因后续 get 而改变。"""
    from pa_agent.storage.sessions import ensure_session, get_session

    ensure_session("tab-a", user_id="carol")
    ensure_session("tab-b", user_id="admin")
    assert get_session("tab-a")["user_id"] == "carol"
    assert get_session("tab-b")["user_id"] == "admin"
    # 主键是 session_id ⇒ 同一 session_id 再次 ensure 不会把归属改掉
    ensure_session("tab-a", user_id="admin")
    assert get_session("tab-a")["user_id"] == "carol"


def test_chat_turns_isolated_per_user(tmp_path, monkeypatch, token):
    """``chat_turns`` 按 (user_id, thread_key) 分桶，A 读不到 B 的追问。"""
    from pa_agent.storage.chat_repo import append_turn, list_turns

    assert append_turn(
        thread_key="same-key", turn=1,
        user="carol 的问题", assistant="carol 的回答",
        user_id="carol",
    ) is True

    mine = list_turns("same-key", user_id="carol")
    assert [t["role"] for t in mine] == ["user", "assistant"]
    assert [t["content"] for t in mine] == ["carol 的问题", "carol 的回答"]

    theirs = list_turns("same-key", user_id="admin")
    assert theirs == [], f"admin 不该看到 carol 的追问线程，实际 {len(theirs)} 条"


def test_chat_turns_clear_thread_scoped_to_user(tmp_path, monkeypatch, token):
    """``clear_thread`` 也不得清掉别人的同一 thread_key。"""
    from pa_agent.storage.chat_repo import append_turn, clear_thread, list_turns

    append_turn(thread_key="same-key", turn=1, user="carol 的", user_id="carol")
    append_turn(thread_key="same-key", turn=1, user="admin 的", user_id="admin")

    assert clear_thread("same-key", user_id="admin") is True
    assert list_turns("same-key", user_id="admin") == []
    # append_turn 一轮写两行（user + assistant），故 carol 仍剩 2 行
    assert len(list_turns("same-key", user_id="carol")) == 2, \
        "admin 清线程把 carol 的也清了 —— 跨用户删除"