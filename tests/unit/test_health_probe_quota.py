"""健康探针不得继承客户端的退避重试 —— 探针放大成 6 次请求就是配额杀手。

生产实测：`/api/health/check` 的 model_api 延迟 166 s（5+10+20+40+80 = 155 s 退避），
而后台心跳每 300 s 跑一次。免费模型（space-bunny-free）的配额就是这么烧完的 ——
2026-10-10 实测一天 390 条限流日志，最早一条在 2026-08，比本轮任何改动都早。

测试纪律（本轮确立）：
- 必须执行**真实**实现，不能在测试里重写逻辑
- 必须断言**实际发生的调用次数**，不是标识符出现
"""
from __future__ import annotations

import inspect
from pathlib import Path

from pa_agent.ai.rate_limit import call_with_rate_limit_backoff
from pa_agent.util.startup_health_check import check_model_api

REPO = Path(__file__).resolve().parents[2]


class _RateLimited(Exception):
    """看起来像 429 的异常 —— is_rate_limit_error 靠 status_code 识别。"""

    def __init__(self) -> None:
        super().__init__("Error code: 429 - Rate limit exceeded")
        self.status_code = 429


class _ProbeClient:
    """假客户端，但**走真实的** call_with_rate_limit_backoff。

    计数器必须数**真正打到上游的那一次**（退避重试的是 chat() 内部的 lambda，
    不是 chat() 本身）。第一版数的是 chat() 调用次数，于是撤掉 max_retries=0
    后测试照样绿 —— 它测的不是它声称的东西。
    """

    def __init__(self) -> None:
        self.attempts = 0

    def chat(self, messages, *, max_retries=None, **_kw):
        def upstream():
            self.attempts += 1
            raise _RateLimited()

        return call_with_rate_limit_backoff(
            upstream,
            max_retries=5 if max_retries is None else int(max_retries),
        )


def test_probe_makes_exactly_one_attempt_not_six():
    """撤掉 max_retries=0 后这条必须变红 —— 它测的是真实退避路径。"""
    client = _ProbeClient()
    report = check_model_api(type("Ctx", (), {"client": client}))
    assert report.status == "error", f"探针应报告失败，实际 {report.status}"
    assert client.attempts == 1, (
        f"探针只应打 1 次，实际 {client.attempts} 次 —— "
        "健康探针继承了客户端的 5 次退避重试，一次探针变六次上游请求"
    )


def test_probe_latency_is_bounded():
    """探针必须快。166 s 的「健康检查」不是健康检查。"""
    client = _ProbeClient()
    report = check_model_api(type("Ctx", (), {"client": client}))
    assert report.latency_ms < 5000, (
        f"探针延迟 {report.latency_ms:.0f} ms —— 退避发生在 chat() 内部，"
        "调用方的 timeout_s 管不住它"
    )


def test_probe_reports_what_it_actually_probed():
    """断言要看内容：detail 必须说明失败原因，不能只给状态位。"""
    client = _ProbeClient()
    report = check_model_api(type("Ctx", (), {"client": client}))
    assert "429" in report.detail or "RateLimit" in report.detail, (
        f"detail 没有反映真实失败原因：{report.detail!r}"
    )


def test_single_attempt_guarantee_is_documented():
    """TypeError 回退分支无法保证单次尝试 —— 这一点必须写在代码里。"""
    src = inspect.getsource(check_model_api)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "max_retries=0" in code, "探针没有显式禁用重试"
    assert "cannot guarantee a single attempt" in src, (
        "回退分支会走默认 5 次重试，这个风险必须写明"
    )


def test_frontend_polls_the_cached_endpoint_not_the_probing_one():
    """前端轮询 `/api/health`（返回缓存报告），不是 `/api/health/check`。

    这条存在的意义是防止有人把前端改成轮询**会真打模型**的那个端点 ——
    那会把一个开着的标签页变成 1440 次/天的配额消耗器。
    """
    src = (REPO / "web/static/js/app.js").read_text(encoding="utf-8")
    assert "API.get('/api/health')" in src, "前端不再轮询缓存端点"
    assert "API.get('/api/health/check')" not in src, (
        "前端轮询了会真打模型的端点 —— 每次轮询都是一次真实 LLM 请求"
    )
