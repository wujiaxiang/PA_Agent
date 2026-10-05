"""Background settlement of pending experience records.

Without this, a record stays ``pending`` forever unless the user happens to
open the library and press 验证. The two-stage design is only useful if stage 2
actually runs on its own.

Design constraints
------------------
* **Never on the analysis path.** A daemon thread with its own overlap guard;
  the analysis flow must not block on it (AGENTS.md: never let experience
  bookkeeping surface into the main flow).
* **Only the currently-viewed instrument, via the shared data source.**
  Settling records for *other* instruments needs a dedicated
  :class:`~web.api.experience_verifier._DedicatedSource` per record — a fresh
  TradingView connection each. Doing that on a timer would hammer the upstream
  and is not worth it; those records settle when the user switches back (or
  presses the button). This keeps the periodic pass to a single cheap snapshot
  read.
* **Idempotent and self-limiting** — the N-bar rule in ``settle_record``
  decides everything; a pass that settles nothing is a no-op.
* **Read ``ctx.settings.general`` each pass**, not a snapshot: the user may
  change symbol/timeframe at any time and the scope must follow the chart.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("pa_agent.web.experience_scheduler")

#: Default pass interval. Short enough that a settled result shows up while the
#: user is still looking at the chart, long enough not to hammer the upstream.
DEFAULT_INTERVAL_S = 180.0

#: Never start the timer faster than this, whatever settings say.
_MIN_INTERVAL_S = 30.0


class _Guard:
    """Single-flight guard — a pass must never overlap itself."""

    def __init__(self) -> None:
        self._flag = threading.Lock()
        self._busy = False
        #: 每轮允许的「专用数据源」预算（保护上游，见 run_once 里的说明）
        self._dedicated_budget = 1

    def try_acquire(self) -> bool:
        with self._flag:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._flag:
            self._busy = False


_guard = _Guard()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def mode_of(ctx: Any) -> str:
    """``"auto"`` (timer settles) or ``"manual"`` (button only)."""
    cfg = getattr(getattr(ctx, "settings", None), "prompt", None)
    value = str(getattr(cfg, "experience_verify_mode", "auto") or "auto").strip().lower()
    return value if value in ("auto", "manual") else "auto"


def run_once(ctx: Any, force: bool = False) -> dict[str, Any] | None:
    """Run a single settlement pass. Returns the verifier summary, or None.

    Parameters
    ----------
    force:
        Bypass the ``experience_verify_mode`` check. The UI's 验证 button
        passes ``force=True`` — choosing "manual" must not disable the button,
        only the *timer*.

    Safe to call from anywhere: never raises, never overlaps itself.
    """
    if not force and mode_of(ctx) == "manual":
        logger.debug("experience scheduler: mode=manual, timer pass skipped")
        return None
    if not _guard.try_acquire():
        logger.debug("experience scheduler: previous pass still running, skipping")
        return None
    try:
        from web.api.experience_verifier import verify_pending

        # **不按品种过滤**。`settings.general.last_*` 是「每次请求从会话游标派生、
        # 只回给前端」的只读字段，`/api/subscribe` 早已不更新它 —— 从这里读到的
        # 是冻结的旧值，实测切到 NVDA/5m 后 `checked=0`（快照里还是 BTCUSDT），
        # 等于所有非该品种的记录**永久结算不了**。
        #
        # 「别打爆上游」的正确形态是**限制工作量**而不是限制正确性：
        # 共享源三轴匹配时复用（零成本），不匹配才建专用源，且每轮最多 `budget` 条。
        budget = max(1, int(getattr(_guard, "_dedicated_budget", 1)))
        try:
            from pa_agent.data.factory import create_data_source

            factory = lambda: create_data_source("tradingview")  # noqa: E731
        except Exception:  # noqa: BLE001
            factory = None

        summary = verify_pending(
            shared_source=getattr(ctx, "data_source", None),
            source_factory=factory,
            settings=getattr(ctx, "settings", None),
            scope=None,
            max_dedicated=budget,
        )
        settled = (summary.get("win", 0) + summary.get("loss", 0)
                   + summary.get("unresolved", 0))
        if settled:
            logger.info(
                "experience scheduler settled %s record(s): %s", settled, summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience scheduler pass failed: %s", exc)
        return None
    finally:
        _guard.release()


def _loop(ctx: Any, interval_s: float) -> None:
    logger.info("experience scheduler started (every %.0fs)", interval_s)
    while not _stop.is_set():
        # First pass shortly after start so a restart can settle what the
        # previous process left pending.
        if _stop.wait(min(20.0, interval_s)):
            break
        run_once(ctx)              # mode=manual 时这里是空操作
        if _stop.wait(interval_s):
            break
    logger.info("experience scheduler stopped")


def start(ctx: Any, interval_s: float | None = None) -> Optional[threading.Thread]:
    """Start the scheduler once. Returns the thread, or None if already up."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    cfg = getattr(getattr(ctx, "settings", None), "prompt", None)
    raw = interval_s if interval_s is not None else getattr(
        cfg, "experience_verify_interval_s", DEFAULT_INTERVAL_S)
    try:
        interval = max(_MIN_INTERVAL_S, float(raw))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S

    _stop.clear()
    t = threading.Thread(target=_loop, args=(ctx, interval),
                         name="experience-scheduler", daemon=True)
    t.start()
    _thread = t
    return t


def stop(timeout: float = 5.0) -> None:
    global _thread
    _stop.set()
    t, _thread = _thread, None
    if t is not None and t.is_alive():
        t.join(timeout=timeout)