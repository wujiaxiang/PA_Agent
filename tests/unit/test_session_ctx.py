"""会话身份与视图解析单测。

守护两条铁律（docs/SESSION_STORAGE_DESIGN.md）：

1. **会话身份不能用 Cookie** —— 同源共享会让所有标签页拿到同一个 id，
   「一个标签页服务自己的 K 线图」直接失效。因此走 ``X-Session-Id`` 请求头，
   且必须做字符集校验（该值会进日志与 SQL）。
2. **无会话时必须回落全局 settings** —— 否则单标签页旧行为被破坏，
   且前端尚未发请求头时会全盘失效。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.storage import sessions as sess_repo
from pa_agent.storage.ephemeral import Cursor, reset_registry_for_tests
from web.api.session_ctx import (
    SESSION_HEADER,
    bind_session,
    resolve_view,
    session_id_of,
)


@pytest.fixture()
def db(tmp_path):
    from pa_agent.storage.db import reset_hub_for_tests

    hub = reset_hub_for_tests(tmp_path / "s.db")
    reset_registry_for_tests()
    yield hub
    hub.close_all()


@pytest.fixture()
def ctx():
    """模拟 AppContext：只有 settings 里的全局游标。"""
    general = SimpleNamespace(
        last_symbol="BTCUSDT",
        last_timeframe="1h",
        last_tradingview_exchange="GATEIO",
    )
    return SimpleNamespace(settings=SimpleNamespace(general=general))


def _req(headers: dict):
    return SimpleNamespace(headers=headers)


# ── 请求头解析 ────────────────────────────────────────────────────────────────


def test_session_id_read_from_header():
    assert session_id_of(_req({SESSION_HEADER: "abc-123"})) == "abc-123"


def test_session_id_trims_whitespace():
    assert session_id_of(_req({SESSION_HEADER: "  abc123  "})) == "abc123"


@pytest.mark.parametrize("bad", ["", "   ", "a" * 100, "abc def", "abc;drop", "abc<script>"])
def test_invalid_session_ids_rejected(bad):
    """非空/长度/字符集校验：值会进日志与 SQL，不能放任。"""
    assert session_id_of(_req({SESSION_HEADER: bad})) == ""


def test_missing_header_is_empty_not_error():
    assert session_id_of(_req({})) == ""


def test_request_without_headers_attr_is_safe():
    """任何异常都视作「无会话」，不得冒泡。"""
    class Broken:
        @property
        def headers(self):
            raise RuntimeError("boom")

    assert session_id_of(Broken()) == ""


# ── 视图解析：回落路径 ───────────────────────────────────────────────────────


def test_resolve_view_falls_back_to_global_settings(ctx):
    """无会话头 → 行为与改造前完全一致（向后兼容的关键）。"""
    assert resolve_view(ctx, "") == ("BTCUSDT", "1h", "GATEIO")


def test_resolve_view_uses_session_cursor(ctx, db):
    """有会话游标 → 用会话的，不读全局。"""
    from pa_agent.storage.ephemeral import get_registry

    get_registry().get_or_create("tab-1").cursor = Cursor("NVDA", "15m", "NASDAQ")
    assert resolve_view(ctx, "tab-1") == ("NVDA", "15m", "NASDAQ")


def test_resolve_view_reads_snapshot_when_registry_miss(db, ctx):
    """热层未命中 → 从 SQLite 快照恢复游标（重启后可复原）。"""
    sess_repo.ensure_session("tab-cold")
    sess_repo.set_cursor("tab-cold", symbol="ETHUSDT", timeframe="4h", exchange="GATE")
    assert resolve_view(ctx, "tab-cold") == ("ETHUSDT", "4h", "GATE")


def test_resolve_view_empty_cursor_falls_back(ctx, db):
    """会话存在但游标为空 → 回落全局，不得返回空游标。"""
    sess_repo.ensure_session("tab-new")
    assert resolve_view(ctx, "tab-new") == ("BTCUSDT", "1h", "GATEIO")


def test_resolve_view_expired_snapshot_falls_back(ctx, db):
    sess_repo.ensure_session("tab-dead", ttl_s=-1)
    assert resolve_view(ctx, "tab-dead") == ("BTCUSDT", "1h", "GATEIO")


def test_resolve_view_inherits_exchange_from_global(ctx, db):
    """会话只设了品种时，交易所回落全局 —— 避免把 auto 交易所清空。"""
    from pa_agent.storage.ephemeral import get_registry

    get_registry().get_or_create("t").cursor = Cursor("NVDA", "1h", "")
    sym, tf, ex = resolve_view(ctx, "t")
    assert (sym, tf) == ("NVDA", "1h")
    assert ex == "GATEIO"


def test_resolve_view_survives_broken_ctx(db):
    """ctx 没有 settings 时不得抛异常。"""
    assert resolve_view(SimpleNamespace(), "tab-x") == ("", "", "")


# ── 会话绑定 ──────────────────────────────────────────────────────────────────


def test_bind_session_creates_snapshot(db):
    assert bind_session(_req({SESSION_HEADER: "tab-9"})) == "tab-9"
    assert sess_repo.get_session("tab-9") is not None


def test_bind_session_without_header_is_noop(db):
    assert bind_session(_req({})) == ""
    assert sess_repo.purge_expired() == 0