"""程序化复盘：结算那一刻就能算出来的部分，**不调 LLM**。

## 为什么要有这一层

LLM 复盘只能由用户主动触发：要花钱、要等、模型不可用时就没有，而且**输出
不可复现**——同一条记录复盘两次结论可能不同，没法写断言。

结算时系统已经算完了全部事实：TP/SL 触及时机、实际盈亏、期间最大浮盈与
回撤、走了多少根 K 线。这些**不需要推理**，只需要算。把它们预先算好有三个
好处：

- **确定性**：同输入同输出，可以写断言（LLM 输出没法断言）
- **零成本、不依赖模型可用性**：模型 ``degraded`` 时经验库照常工作
- **没有提示注入面**：不生成任何新文本，只把已算出的数值填进闭词表字段

## 它算不出来的部分

「当时能否预见」「下次同类 setup 的判据」「推理错在哪一步」——这些要语义
抽象，交给 :mod:`pa_agent.records.review_spec` 解析的 LLM 复盘。两层互补：
**本层是默认且总是存在，LLM 层是可选的加深。**

## 结论词表是封闭的

``verdict`` 只取下面五个值之一。这不是措辞讲究 —— 这个字段会被渲染进
决策提示词，值必须封闭，否则等于给模型开了「复盘文本可以夹带指令」的口子。
"""
from __future__ import annotations

from typing import Any

#: 结论的**受控词表**（闭集，进提示词）。
VERDICT_PENDING = "尚未判定"
VERDICT_CONFIRMED = "方向与形态被结果证实"
VERDICT_LUCKY = "判断成立但运气不佳"
VERDICT_WRONG = "判断与走势相悖"
VERDICT_UNTOUCHED = "窗口内未触及价位"
VERDICTS: tuple[str, ...] = (
    VERDICT_PENDING, VERDICT_CONFIRMED, VERDICT_LUCKY,
    VERDICT_WRONG, VERDICT_UNTOUCHED,
)

#: 判定「曾经浮盈过」的**实质门槛**：最大浮盈须达到风险距离的这个比例。
#: 门槛若只写 ``mfe > 0``，则进场即逆向、全程最大浮盈 0.01% 而回撤 11% 的
#: 单子也会被判成「判断成立」—— MAE 都算出来了却不参与判定。而这类单子
#: 恰恰最不该被记成运气不佳：检索端会据此以为「这个形态其实是对的」。
#: 半程（0.5R）是个朴素的实质门槛：曾走到离止损同样远的地方才算「一度成立」。
LUCKY_MFE_RATIO = 0.5

#: 计划价位自洽性检查的容差（相对入场价）。用于识别 TP/SL 倒挂。
PLAN_TOLERANCE_PCT = 1e-6

#: ``verdict`` 文本上限。数值本身已是结论，句子不该长。
MAX_VERDICT_CHARS = 40
#: 机械观察的字符上限。
MAX_CRITERIA_CHARS = 300


def _pct(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _clip(text: str, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _verdict(status: str, mfe_pct: float, risk_pct: float) -> str:
    """status + 最大浮盈 → 闭集结论。

    ``loss`` 且**曾实质浮盈**才判「运气不佳」。门槛是 ``mfe >= 0.5 * 风险距离``
    —— 见 :data:`LUCKY_MFE_RATIO` 的说明：``mfe > 0`` 这种任意正数门槛会把
    一进场就逆向的单子也算进去，而 MAE 明明已经算出来却不参与判定。

    只陈述事实、不下因果判断：这类单子事后最容易被写成「判断正确只是运气
    差」，而真实原因常常是 TP/SL 间距设得不合理。
    """
    if status == "pending":
        return VERDICT_PENDING
    if status == "unresolved":
        return VERDICT_UNTOUCHED
    if status == "win":
        return VERDICT_CONFIRMED
    threshold = LUCKY_MFE_RATIO * risk_pct if risk_pct > 0 else 0.0
    return VERDICT_LUCKY if mfe_pct >= threshold > 0 else VERDICT_WRONG


def plan_is_sane(content: dict[str, Any]) -> bool:
    """入场价是否夹在 SL 与 TP 之间，且方向与 ``is_long`` 一致。

    **写入侧没有校验这条**：``save_pending_if_resolvable`` 只查 ``tp != entry``
    与 ``sl != entry``。于是「做多但 TP=80 < entry=100 < SL=110」这种倒挂
    计划会在第一根正常 bar 上就触发 hit_tp → 判 win。若复盘层不拦，就会给
    一个结构非法的计划盖上闭词表里最强的那句肯定结论。

    返回 False 时 verdict 必须降级为「尚未判定」—— **不给非法计划盖章**。
    """
    entry = float(content.get("entry_price") or 0.0)
    tp = float(content.get("take_profit_price") or 0.0)
    sl = float(content.get("stop_loss_price") or 0.0)
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    tol = entry * PLAN_TOLERANCE_PCT
    if bool(content.get("is_long", True)):
        return sl < entry - tol and tp > entry + tol
    return tp < entry - tol and sl > entry + tol


def build_program_review(
    *,
    status: str,
    content: dict[str, Any],
    exit_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """把一次结算的事实压成结构化摘要。**纯函数，无 IO、可直接断言。**

    ``exit_info`` 来自 :func:`pa_agent.records.experience_writer.resolve_exit`；
    传 ``None`` 表示数据缺失，此时只回落到最小可用的结论。
    """
    info = exit_info or {}
    touched = bool(info.get("touched"))
    mfe = float(info.get("mfe_pct") or 0.0)
    mae = float(info.get("mae_pct") or 0.0)
    bars_to_exit = info.get("bars_to_exit")

    entry = float(content.get("entry_price") or 0.0)
    tp = float(content.get("take_profit_price") or 0.0)
    sl = float(content.get("stop_loss_price") or 0.0)
    sane = plan_is_sane(content)
    risk_pct = abs(entry - sl) / entry * 100.0 if entry > 0 and sl else 0.0
    reward_pct = abs(tp - entry) / entry * 100.0 if entry > 0 and tp else 0.0
    verdict = _verdict(status, mfe, risk_pct)

    pnl = info.get("pnl_pct") if touched else None
    # 盈亏比按**实际达成**算，不是按设定的 TP/SL —— 计划得漂亮而实际没做到，
    # 与计划本身就漂亮是两回事。
    realised_rr = (float(pnl) / risk_pct) if (pnl is not None and risk_pct > 0) else None

    observations: list[str] = []
    if not sane:
        # 计划本身非法时，结算结果不可解释为对判断的验证 —— 不盖章。
        observations.append("⚠ 计划价位不自洽（入场价未夹在止损与止盈之间），结论不予采信")
    elif status == "unresolved":
        observations.append(
            f"{bars_to_exit} 根内既未触止盈也未触止损 —— 价位区间相对本段波动过窄或过宽"
        )
    elif touched:
        kind = "止盈" if info.get("result") == "win" else "止损"
        observations.append(f"第 {bars_to_exit} 根触及{kind}，全程 {bars_to_exit} 根内判定完毕")
    else:
        observations.append(f"已观察 {bars_to_exit} 根，尚未判定")
    if mfe > 0 or mae > 0:
        observations.append(f"期间最大浮盈 {_pct(mfe)}%、最大回撤 {_pct(mae)}%")
    if risk_pct:
        observations.append(
            f"风险距离 {_pct(risk_pct)}%、目标空间 {_pct(reward_pct)}%"
            + (f"，实际盈亏比 {_pct(realised_rr)}" if realised_rr is not None else "")
        )
    if not sane and verdict != VERDICT_PENDING:
        verdict = VERDICT_PENDING
    if verdict == VERDICT_LUCKY:
        observations.append("入场后曾有浮盈才被打回，可复核 TP/SL 间距是否过窄")
    if verdict == VERDICT_UNTOUCHED:
        observations.append("该条不计入成败统计，也不作为失败经验检索")

    return {
        "source": "program",
        "parsed": True,
        "status": status,
        "verdict": _clip(verdict, MAX_VERDICT_CHARS),
        "reusable_criteria": _clip("；".join(observations), MAX_CRITERIA_CHARS),
        "observations": observations,
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "bars_to_exit": bars_to_exit,
        "risk_pct": round(risk_pct, 4),
        "reward_pct": round(reward_pct, 4),
        "realised_rr": round(realised_rr, 4) if realised_rr is not None else None,
        "plan_sane": sane,
    }