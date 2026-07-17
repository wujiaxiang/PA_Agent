# -*- coding: utf-8 -*-
"""E2E smoke test for the Web backend — 完整 Web 分析流程 + 无 PyQt6 启动验证。

覆盖（Task 15.1 / 15.2 / 15.3）：
- GET /api/health 健康检查
- GET /api/datasources 数据源列表
- GET /api/bars K 线数据（含 closed 字段）
- GET /api/analyze/stream SSE 完整分析流程（mock orchestrator → done 事件含 decision_tree/overlay）
- GET /api/settings 设置脱敏
- 验证 web.server 不导入 pa_agent.gui 模块
- 验证 web.server lifespan 不依赖 PyQt6
"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.data.base import KlineBar
from pa_agent.util.threading import OrchestratorEvent
from web.api.routes_analyze import router as analyze_router
from web.api.routes_chat import router as chat_router
from web.api.routes_data import router as data_router
from web.api.routes_settings import router as settings_router


def _parse_sse(text: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "" and cur_event is not None:
            data_str = "\n".join(cur_data)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                data = data_str
            events.append((cur_event, data))
            cur_event = None
            cur_data = []
    return events


def _make_bars(n: int = 20) -> list[KlineBar]:
    """构造 n 根 K 线，最后一根 forming。"""
    return [
        KlineBar(
            seq=n - i,
            ts_open=1_700_000_000_000 + i * 60_000,
            open=2000.0 + i,
            high=2010.0 + i,
            low=1995.0 + i,
            close=2005.0 + i,
            volume=100.0,
            closed=i < n - 1,
        )
        for i in range(n)
    ]


def _make_mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.general.last_symbol = "XAUUSD"
    ctx.settings.general.last_timeframe = "1h"
    ctx.settings.general.last_data_source = "mock"
    ctx.settings.provider.api_key = "sk-1234567890abcdef"
    ctx.settings.provider.context_window = 2_000_000
    ctx.settings.general.context_warning_threshold_pct = 80.0
    ctx.logger = MagicMock()

    ds = MagicMock()
    ds.supported_timeframes.return_value = ["1m", "5m", "15m", "1h", "4h", "1d"]
    ds.latest_snapshot.return_value = _make_bars(20)
    ctx.data_source = ds
    return ctx


def _make_fake_record() -> MagicMock:
    """构造含完整 decision_tree + decision_overlay 的 mock record。"""
    record = MagicMock()
    record.meta.symbol = "XAUUSD"
    record.meta.timeframe = "1h"
    record.stage1_diagnosis = {
        "gate_trace": [{"node_id": "G1", "answer": "是", "reason": "结构清晰"}],
        "gate_result": "proceed",
        "direction": "bullish",
        "cycle_position": "normal_channel",
    }
    record.stage2_response = {
        "decision_trace": [{"node_id": "10.3", "answer": "是", "reason": "RR 通过"}],
        "terminal": {"node_id": "11.2", "outcome": "trade", "label": "突破做多"},
        "gate_shortcircuited": False,
    }
    record.stage2_decision = {
        "order_type": "突破单",
        "order_direction": "做多",
        "chart_overlay_active": True,
        "entry_price": 2010.5,
        "stop_loss_price": 1995.0,
        "take_profit_price": 2050.0,
        "take_profit_price_2": 2090.0,
        "estimated_win_rate": 55,
        "trade_confidence": 70,
        "diagnosis_confidence": 75,
    }
    record.strategy_files_used = ["上涨通道分析识别.txt"]
    record.usage_total = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    record.exception = None
    return record


@pytest.fixture
def web_client():
    """构建挂载全部 4 个 router 的 FastAPI 测试 app（模拟 web.server.app）。"""
    app = FastAPI()
    app.include_router(settings_router, prefix="/api")
    app.include_router(data_router, prefix="/api")
    app.include_router(analyze_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = _make_mock_ctx()

    with TestClient(app) as c:
        yield c


# ── 15.1 完整 Web 分析流程 ─────────────────────────────────────────────────────


class TestWebSmokeFlow:
    """端到端冒烟测试：验证 Web 后端全链路可用。"""

    def test_health_check(self, web_client):
        """GET /api/health 返回 ok。"""
        resp = web_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_list_datasources(self, web_client):
        resp = web_client.get("/api/datasources")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert all("id" in d and "label" in d for d in data)

    def test_get_bars(self, web_client):
        """GET /api/bars 返回 K 线含 closed 字段。"""
        resp = web_client.get("/api/bars?count=20")
        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "XAUUSD"
        bars = body["bars"]
        assert len(bars) == 20
        for b in bars:
            assert "closed" in b
        forming = [b for b in bars if b["closed"] is False]
        assert len(forming) == 1

    def test_get_settings(self, web_client):
        """GET /api/settings 返回脱敏 api_key。"""
        with patch("web.api.routes_settings.load_settings") as mock_load:
            from pa_agent.config.settings import Settings, AIProviderSettings
            mock_load.return_value = Settings(
                provider=AIProviderSettings(api_key="sk-1234567890abcdef")
            )
            resp = web_client.get("/api/settings")
        assert resp.status_code == 200
        assert "****" in resp.json()["provider"]["api_key"]

    def test_analyze_full_sse_flow(self, web_client):
        """GET /api/analyze/stream 完整 SSE 流：事件序列 + done 含决策载荷。"""
        record = _make_fake_record()

        def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
            on_event(OrchestratorEvent.Stage1Started)
            on_event(OrchestratorEvent.Stage1Done)
            on_event(OrchestratorEvent.Stage2Started)
            on_event(OrchestratorEvent.Stage2Done)
            on_event(OrchestratorEvent.RecordSaved)
            return record

        fake_orch = MagicMock()
        fake_orch.submit.side_effect = fake_submit

        with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
             patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
            resp = web_client.get("/api/analyze/stream?bar_count=20")

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        types = [e for e, _ in events]

        # 事件序列验证
        assert "orchestrator_event" in types
        assert "done" in types

        orch_names = [d["event"] for e, d in events if e == "orchestrator_event"]
        assert "Stage1Started" in orch_names
        assert "Stage2Done" in orch_names
        assert "RecordSaved" in orch_names

        # done 载荷验证
        done_data = next(d for e, d in events if e == "done")
        rec = done_data["record"]
        assert rec["symbol"] == "XAUUSD"

        dt = rec["decision_tree"]
        assert dt["gate_trace"] is not None
        assert dt["decision_trace"] is not None
        assert dt["terminal"]["outcome"] == "trade"
        assert dt["gate_result"] == "proceed"

        ov = rec["decision_overlay"]
        assert ov["order_type"] == "突破单"
        assert ov["entry_price"] == 2010.5
        assert ov["stop_loss_price"] == 1995.0
        assert ov["chart_overlay_active"] is True

    def test_analyze_error_path(self, web_client):
        """orchestrator 异常时 SSE 返回 error 事件。"""
        fake_orch = MagicMock()
        fake_orch.submit.side_effect = RuntimeError("AI 超时")

        with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
             patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
            resp = web_client.get("/api/analyze/stream?bar_count=10")

        events = _parse_sse(resp.text)
        types = [e for e, _ in events]
        assert "error" in types
        err = next(d for e, d in events if e == "error")
        assert "AI 超时" in err["message"]


# ── 15.2 / 15.3 无 PyQt6 启动验证 ─────────────────────────────────────────────


class TestNoPyQt6Dependency:
    """验证 Web 后端不依赖 PyQt6 / pa_agent.gui。"""

    def test_web_module_source_has_no_gui_imports(self):
        """web/ 目录下所有 .py 文件不应 import pa_agent.gui。"""
        import os
        import re

        web_dir = os.path.join(os.path.dirname(__file__), "..", "..", "web")
        gui_pattern = re.compile(r"(?:from\s+pa_agent\.gui|import\s+pa_agent\.gui)")
        pyqt_pattern = re.compile(r"(?:from\s+PyQt|import\s+PyQt|from\s+pyqtgraph|import\s+pyqtgraph)")

        violations = []
        for root, _dirs, files in os.walk(web_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
                if gui_pattern.search(content):
                    violations.append(f"GUI import in {fpath}")
                if pyqt_pattern.search(content):
                    violations.append(f"PyQt import in {fpath}")

        assert not violations, "Web 模块不应导入 pa_agent.gui 或 PyQt6:\n" + "\n".join(violations)

    def test_app_context_bootstrap_has_no_gui_imports(self):
        """AppContext.bootstrap() 不应导入 pa_agent.gui。"""
        import os
        import re

        ctx_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "pa_agent", "app_context.py"
        )
        with open(ctx_path, encoding="utf-8") as f:
            content = f.read()
        gui_pattern = re.compile(r"(?:from\s+pa_agent\.gui|import\s+pa_agent\.gui)")
        pyqt_pattern = re.compile(r"(?:from\s+PyQt|import\s+PyQt)")
        assert not gui_pattern.search(content), "app_context.py 不应导入 pa_agent.gui"
        assert not pyqt_pattern.search(content), "app_context.py 不应导入 PyQt6"

    def test_web_server_importable_without_gui(self):
        """导入 web.server 后 sys.modules 中不应出现 pa_agent.gui.* 模块。

        如果 web.server 已被其他测试导入，则验证当前 sys.modules 中
        pa_agent.gui 的存在不是因为 web.server 导致的。
        """
        # 记录导入前的 gui 模块
        gui_before = {k for k in sys.modules if k.startswith("pa_agent.gui")}

        # 尝试导入 web.server（如果尚未导入）
        import importlib
        mod = importlib.import_module("web.server")
        assert mod is not None
        assert hasattr(mod, "app")

        # 导入后不应新增 pa_agent.gui 模块
        gui_after = {k for k in sys.modules if k.startswith("pa_agent.gui")}
        new_gui = gui_after - gui_before
        assert not new_gui, f"导入 web.server 不应触发 pa_agent.gui 加载，新增: {new_gui}"

    def test_web_server_has_health_endpoint(self):
        """web.server.app 应注册 /api/health 端点。"""
        from web.server import app
        # FastAPI app 的 routes 应包含 /api/health
        health_routes = [r for r in app.routes if getattr(r, "path", "") == "/api/health"]
        assert len(health_routes) > 0, "web.server.app 应包含 /api/health 端点"

    def test_web_server_has_all_routers(self):
        """web.server.app 应挂载全部 4 个 router。

        使用 OpenAPI schema 收集路径——新版 FastAPI/Starlette 将 include_router
        的路由包装为 _IncludedRouter，直接遍历 app.routes 无法获取子路由路径。
        """
        from web.server import app
        schema = app.openapi()
        paths = set(schema.get("paths", {}).keys())
        assert "/api/settings" in paths, f"missing /api/settings in {paths}"
        assert "/api/bars" in paths, f"missing /api/bars in {paths}"
        assert "/api/analyze/stream" in paths, f"missing /api/analyze/stream in {paths}"
        assert "/api/chat/stream" in paths, f"missing /api/chat/stream in {paths}"
        assert "/api/datasources" in paths, f"missing /api/datasources in {paths}"

    def test_lifespan_does_not_import_gui(self):
        """web.server.lifespan 函数源码不应引用 pa_agent.gui。"""
        import inspect
        from web.server import lifespan

        source = inspect.getsource(lifespan)
        assert "pa_agent.gui" not in source, "lifespan 不应引用 pa_agent.gui"
        assert "PyQt" not in source, "lifespan 不应引用 PyQt"

    def test_real_lifespan_bootstrap_without_pyqt6(self):
        """真实触发 web.server.lifespan，验证 AppContext.bootstrap() 在无 PyQt6
        环境下能完整完成（不抛 ModuleNotFoundError）。

        这是 Task 15.2 的真实运行时验证——前面几个 test_*_does_not_import_gui
        只检查源码字符串，无法挡住传递依赖在运行时导入 PyQt6 的 regression
        （例如 session_ledger.py 顶层 `from PyQt6.QtCore import QObject`）。

        本测试需要 config/settings.json 存在；否则 skip。
        """
        import os
        from pathlib import Path

        settings_path = Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
        if not settings_path.exists():
            pytest.skip("config/settings.json 不存在，跳过真实 lifespan 测试")

        # 记录 bootstrap 前已加载的 PyQt6 模块
        pyqt_before = {k for k in sys.modules if k.startswith("PyQt6")}

        from web.server import app

        # TestClient 作为 context manager 会触发 lifespan startup + shutdown
        with TestClient(app) as client:
            # lifespan 必须已成功完成 startup（否则 with 会抛异常）
            # 顺便验证 health 端点真的能响应
            resp = client.get("/api/health")
            assert resp.status_code == 200
            # 心跳任务在后台异步运行,首次调用可能还在 "starting" 状态,
            # 或已完成(可能 ok / degraded / error,取决于本机配置)。
            # 关键验证:不抛 PyQt6 导入错误 + 端点能响应 + status 字段存在
            data = resp.json()
            assert "status" in data, f"health 响应缺少 status 字段: {data}"
            assert data["status"] in ("starting", "ok", "degraded", "error"), (
                f"health 状态值异常: {data}"
            )

            # 验证 bootstrap 过程没有为了兜底而强行 import PyQt6
            pyqt_after = {k for k in sys.modules if k.startswith("PyQt6")}
            new_pyqt = pyqt_after - pyqt_before
            # 允许 PyQt6 已被其他测试加载过（pyqt_before 非空），但不允许新增
            assert not new_pyqt, (
                f"web.server lifespan 不应在运行时新增 PyQt6 导入，新增: {new_pyqt}"
            )
