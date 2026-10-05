"""模式迁移的端到端测试 —— 断言**面板内容**是否属于当前模式。

## 为什么要有这个文件

2026-10-05 之前的 UI 走查断言的全是**状态位**（面板是否可见、body 的
dataset 值、classList、消息条数），没有一个断言「面板当前显示的内容是否
属于当前模式」。于是「实时 → 回看 → 实时」这条状态迁移从未被端到端走过 ——
`replayRecord()` 与 `btn-live` 在旧脚本里是两个独立步骤，没有串成一次迁移。

后果：预测 / 决策树 / 决策三个面板在切回实时后仍显示回看记录的内容，
用户实机报告后才发现。**机制都触发正确，结果是错的。**

本文件按「内容」断言，并且把三个模式串成一条完整的迁移链。

运行前置：服务已在 http://127.0.0.1:8005 运行。
"""
from __future__ import annotations

import json
import urllib.request

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright 未安装（pip install playwright && playwright install chromium）",
)
sync_playwright = playwright_api.sync_playwright

BASE_URL = "http://127.0.0.1:8005"
TIMEOUT_MS = 90_000

# 侧边栏分析产出类面板：内容必须随模式切换而重置
ANALYSIS_PANELS = {
    "future": "#future-content",
    "tree": "#tree-content",
    "decision": "#decision-content",
}

# 空态文案。三个面板用同一个短语，便于统一断言
EMPTY_TEXT = "尚未进行交易分析"


def _records() -> list[dict]:
    url = f"{BASE_URL}/api/records?exchange=GATEIO&symbol=BTCUSDT&timeframe=1h"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def _read_panels(page) -> dict[str, str]:
    return page.evaluate(
        """(sels) => {
          const out = {};
          for (const [k, sel] of Object.entries(sels)) {
            const el = document.querySelector(sel);
            out[k] = el ? (el.innerText || '').replace(/\\s+/g, ' ').trim() : '';
          }
          out.__mode = document.body.dataset.dataMode || '';
          return out;
        }""",
        ANALYSIS_PANELS,
    )


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        pg = ctx.new_page()
        pg.set_default_timeout(TIMEOUT_MS)
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)[:160]))
        pg.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=TIMEOUT_MS)
        pg.evaluate("() => localStorage.clear()")
        pg.reload(wait_until="networkidle", timeout=TIMEOUT_MS)
        pg.wait_for_timeout(10_000)
        pg._collected_errors = errors  # 供断言读取
        yield pg
        browser.close()


# ── 模式迁移链 ────────────────────────────────────────────────────────────

def test_live_to_replay_to_live_resets_all_panels(page):
    """核心回归：实时 → 回看 → 实时，各分析面板必须回到空态。

    修复前，预测 / 决策树 / 决策三个面板在返回实时后仍显示回看记录的内容。
    """
    recs = _records()
    if not recs:
        pytest.skip("没有可用于回看的历史记录")

    before = _read_panels(page)
    assert before["__mode"] == "live"
    for name in ANALYSIS_PANELS:
        assert EMPTY_TEXT in before[name], f"初始状态 {name} 应为空态，实际：{before[name][:60]!r}"

    page.evaluate("(id) => replayRecord(id)", recs[0]["record_id"])
    page.wait_for_timeout(13_000)

    during = _read_panels(page)
    assert during["__mode"] == "replay", "回看后模式标记未切到 replay"
    # 回看确实应当填充内容 —— 否则「返回实时后为空」就没有对比意义
    filled = [n for n in ANALYSIS_PANELS if EMPTY_TEXT not in during[n]]
    assert filled, f"回看后所有面板都是空态，无法验证重置逻辑：{during}"

    page.evaluate("() => document.querySelector('#btn-live').click()")
    page.wait_for_timeout(11_000)

    after = _read_panels(page)
    assert after["__mode"] == "live", "返回实时后模式标记未切回 live"
    for name in ANALYSIS_PANELS:
        assert EMPTY_TEXT in after[name], (
            f"返回实时后 #{name} 仍残留回看内容\n"
            f"  回看时：{during[name][:70]!r}\n"
            f"  返回后：{after[name][:70]!r}"
        )
        assert after[name] != during[name], f"#{name} 返回实时前后完全相同，说明未真正重置"


def test_chat_context_resets_on_back_to_live(page):
    """追问锚点也必须重置，否则会对着不存在的分析提问。"""
    assert _read_panels(page)["__mode"] == "live"
    ctx = page.evaluate(
        "() => (document.querySelector('#chat-context')?.innerText || '').trim()"
    )
    assert "尚未进行交易分析" in ctx or ctx == "", f"追问锚点残留：{ctx!r}"


def test_readonly_state_released_after_back_to_live(page):
    """回看期间是只读态；回到实时后必须解除，否则分析按钮永久消失。"""
    ro = page.evaluate(
        """() => ({
            readonly: document.body.hasAttribute('data-readonly'),
            analyzeHidden: document.querySelector('#btn-analyze-toggle')?.classList.contains('hidden'),
            hintHidden: document.querySelector('#readonly-hint')?.classList.contains('hidden'),
        })"""
    )
    assert ro["readonly"] is False, "回到实时后仍处于只读态"
    assert ro["analyzeHidden"] is False, "回到实时后分析按钮仍隐藏"
    assert ro["hintHidden"] is True, "回到实时后只读提示条仍显示"


def test_demo_to_live_resets_analysis_panels(page):
    """Demo → 实时 走的是另一条分支（无条件重载 K 线），同样不能留残留。"""
    page.evaluate("() => document.querySelector('#btn-demo').click()")
    page.wait_for_timeout(7_000)
    assert page.evaluate("() => document.body.dataset.dataMode") == "demo"

    page.evaluate("() => document.querySelector('#btn-live').click()")
    page.wait_for_timeout(11_000)

    assert page.evaluate("() => document.body.dataset.dataMode") == "live"
    after = _read_panels(page)
    for name in ANALYSIS_PANELS:
        assert EMPTY_TEXT in after[name], f"Demo→实时后 #{name} 残留：{after[name][:70]!r}"


def test_no_js_errors_during_mode_transitions(page):
    """模式切换过程中不得有未捕获异常。

    历史 bug：`resetAnalysisPanels()` 里写成 `updateFlowBarIdle?.()`，
    而该函数根本不存在 —— 可选链对**未声明标识符**仍会抛 ReferenceError，
    与「已声明为 undefined」不同。
    """
    errs = list(getattr(page, "_collected_errors", []))
    assert not errs, "模式切换过程中出现 JS 错误：" + "; ".join(errs[:5])