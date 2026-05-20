"""Normalize gate_trace / decision_trace items before JSON schema validation."""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_BAR_RANGE_RE = re.compile(r"^K(\d+)-K(\d+)$", re.IGNORECASE)
_SINGLE_BAR_RE = re.compile(r"^K(\d+)$", re.IGNORECASE)

# node_id -> {raw answer -> (canonical answer, branch)}
_NODE_ANSWER_BY_ID: dict[str, dict[str, tuple[str, str]]] = {
    "2.3": {
        "多头": ("是", "bullish"),
        "空头": ("是", "bearish"),
        "做多": ("是", "bullish"),
        "做空": ("是", "bearish"),
        "bullish": ("是", "bullish"),
        "bearish": ("是", "bearish"),
        "bull": ("是", "bullish"),
        "bear": ("是", "bearish"),
        "中性": ("中性", "neutral"),
        "neutral": ("中性", "neutral"),
    },
    "4.2": {
        "上涨": ("是", "bullish"),
        "下跌": ("是", "bearish"),
        "多头": ("是", "bullish"),
        "空头": ("是", "bearish"),
        "bullish": ("是", "bullish"),
        "bearish": ("是", "bearish"),
    },
    "6.2": {
        "普通交易区间": ("是", "trading_range"),
        "普通区间": ("是", "trading_range"),
        "普通": ("是", "trading_range"),
        "趋势型交易区间": ("是", "trending_tr"),
        "趋势型区间": ("是", "trending_tr"),
        "趋势型": ("是", "trending_tr"),
        "trading_range": ("是", "trading_range"),
        "trending_tr": ("是", "trending_tr"),
    },
    "6.3": {
        "下边界": ("是", "lower"),
        "上边界": ("是", "upper"),
        "在下边界": ("是", "lower"),
        "在上边界": ("是", "upper"),
        "区间下边界": ("是", "lower"),
        "区间上边界": ("是", "upper"),
        "下边界附近": ("是", "lower"),
        "上边界附近": ("是", "upper"),
        "中间": ("否", "middle"),
        "中间1/3": ("否", "middle"),
        "在中间": ("否", "middle"),
        "中间区域": ("否", "middle"),
        "不在边界": ("否", "middle"),
        "lower": ("是", "lower"),
        "upper": ("是", "upper"),
        "middle": ("否", "middle"),
    },
    "8.2": {
        "楔形回撤": ("是", "pullback"),
        "楔形反转": ("是", "reversal"),
        "回撤": ("是", "pullback"),
        "反转": ("是", "reversal"),
        "pullback": ("是", "pullback"),
        "reversal": ("是", "reversal"),
    },
    "3.5": {
        "路径A": ("是", "path_a"),
        "路径B": ("是", "path_b"),
        "路径C": ("是", "path_c"),
        "A": ("是", "path_a"),
        "B": ("是", "path_b"),
        "C": ("是", "path_c"),
    },
}

_GENERIC_ANSWER: dict[str, str] = {
    "通过": "是",
    "未通过": "否",
    "不通过": "否",
    "违反": "否",
    "触犯": "否",
    "无交易计划，不存在触犯": "否",
    "未触犯": "否",
    "不存在触犯": "否",
    "pass": "是",
    "fail": "否",
    "yes": "是",
    "no": "否",
}

_BAR_RANGE_ALIASES = frozenset({"全局", "全图", "整体", "全部", "all"})
_NULLISH_STRINGS = frozenset({"", "null", "none", "nil", "n/a"})


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _NULLISH_STRINGS


def _ensure_trace_string_fields(item: dict[str, Any]) -> None:
    """Coerce missing / null JSON values to strings before schema validation."""
    for key in ("node_id", "question", "answer", "reason"):
        if key not in item or _is_nullish(item.get(key)):
            if key == "answer" and item.get("skipped"):
                item[key] = "不适用"
            elif key in ("question", "reason"):
                item[key] = "—"
            elif key == "node_id":
                item[key] = ""
            elif key == "answer":
                item[key] = "不适用" if item.get("skipped") else "否"
            else:
                item[key] = ""

_COMPOSITE_ANSWER_RE = re.compile(
    r"^(是|否|中性|等待|不适用)\s*[,，:：\-—]\s*(.+)$"
)
_COMPOSITE_ANSWER_PAREN_RE = re.compile(
    r"^(是|否|中性|等待|不适用)\s*[（(](.+?)[）)]\s*$"
)


def infer_max_bar_seq_from_trace(trace: list[Any]) -> int | None:
    """Infer largest K index mentioned anywhere in the trace."""
    max_seq = 0
    for item in trace:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("bar_range", "") or "")
        for m in re.finditer(r"K(\d+)", raw, re.IGNORECASE):
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq or None


def fix_bar_range_string(text: str, *, default_max_seq: int | None = None) -> str:
    """Canonicalize bar_range: order, aliases, spacing."""
    raw = str(text).strip()
    if not raw:
        return ""

    if raw in _BAR_RANGE_ALIASES or raw.lower() in {"global", "all"}:
        if default_max_seq and default_max_seq > 1:
            return f"K{default_max_seq}-K1"
        return "不适用"

    compact = raw.upper().replace(" ", "")
    m = _BAR_RANGE_RE.match(compact)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            return f"K{a}"
        if a < b:
            a, b = b, a
        return f"K{a}-K{b}"

    single = _SINGLE_BAR_RE.match(compact)
    if single:
        return f"K{single.group(1)}"

    return raw


def _branch_from_tail(per_node: dict[str, tuple[str, str]], tail: str) -> str | None:
    """Match classification tail text to a branch id."""
    tail = tail.strip()
    if not tail or not per_node:
        return None
    if tail in per_node:
        return per_node[tail][1]
    tail_l = tail.lower()
    if tail_l in per_node:
        return per_node[tail_l][1]
    for key in sorted(per_node.keys(), key=len, reverse=True):
        if key in tail or key.lower() in tail_l:
            return per_node[key][1]
    return None


def _resolve_trace_answer(
    node_id: str,
    answer: str,
) -> tuple[str, str | None] | None:
    """Map raw AI answer to (canonical answer, optional branch). None = unchanged."""
    ans = answer.strip()
    if not ans:
        return None

    per_node = _NODE_ANSWER_BY_ID.get(node_id, {})

    mapped = per_node.get(ans) or per_node.get(ans.lower())
    if mapped:
        return mapped

    for pat in (_COMPOSITE_ANSWER_RE, _COMPOSITE_ANSWER_PAREN_RE):
        m = pat.match(ans)
        if m:
            base, tail = m.group(1), m.group(2).strip()
            branch = _branch_from_tail(per_node, tail)
            if branch:
                return base, branch
            if per_node:
                return base, None
            return base, None

    for key in sorted(per_node.keys(), key=len, reverse=True):
        if key in ans or key.lower() in ans.lower():
            return per_node[key]

    if ans in _GENERIC_ANSWER:
        return _GENERIC_ANSWER[ans], None
    if ans.lower() in _GENERIC_ANSWER:
        return _GENERIC_ANSWER[ans.lower()], None

    return None


def _coerce_bar_range(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
    prior_bar_range: str | None = None,
) -> None:
    """Ensure bar_range is a non-null string (schema + validator)."""
    nid = str(item.get("node_id", ""))
    skipped = bool(item.get("skipped"))
    ans = str(item.get("answer", "")).strip()

    if skipped and not ans:
        item["answer"] = "不适用"
        ans = "不适用"

    br = item.get("bar_range")
    if _is_nullish(br):
        if skipped or ans == "不适用":
            item["bar_range"] = "不适用"
            return
        if prior_bar_range and prior_bar_range not in ("不适用", "—"):
            item["bar_range"] = prior_bar_range
            logger.debug(
                "bar_range null -> copied %s (node %s)",
                prior_bar_range,
                nid,
            )
            return
        if default_max_seq and default_max_seq > 1:
            item["bar_range"] = f"K{default_max_seq}-K1"
            logger.debug(
                "bar_range null -> K%s-K1 (node %s)",
                default_max_seq,
                nid,
            )
            return
        item["bar_range"] = "不适用"
        return

    fixed = fix_bar_range_string(str(br), default_max_seq=default_max_seq)
    if fixed != str(br).strip():
        logger.debug("bar_range %s -> %s (node %s)", br, fixed, nid)
    item["bar_range"] = fixed


def normalize_trace_item(
    item: dict[str, Any],
    *,
    default_max_seq: int | None = None,
    prior_bar_range: str | None = None,
) -> None:
    """Mutate one trace item: answer + bar_range."""
    _ensure_trace_string_fields(item)
    nid = str(item.get("node_id", "")).strip()
    if nid == "14":
        item["node_id"] = "14.1"

    nid = str(item.get("node_id", ""))

    ans = str(item.get("answer", "")).strip()
    if ans:
        resolved = _resolve_trace_answer(nid, ans)
        if resolved is not None:
            new_ans, branch = resolved
            if new_ans != ans:
                logger.debug(
                    "trace answer %r -> %r (node %s branch=%s)",
                    ans,
                    new_ans,
                    nid,
                    branch,
                )
            item["answer"] = new_ans
            if branch:
                item.setdefault("branch", branch)

    bar_from = item.get("bar_from")
    bar_to = item.get("bar_to")
    if bar_from is not None and bar_to is not None and _is_nullish(item.get("bar_range")):
        bf, bt = int(bar_from), int(bar_to)
        item["bar_range"] = f"K{max(bf, bt)}-K{min(bf, bt)}" if bf != bt else f"K{bf}"

    _coerce_bar_range(
        item,
        default_max_seq=default_max_seq,
        prior_bar_range=prior_bar_range,
    )


def normalize_trace_list(
    trace: list[Any] | None,
    *,
    default_max_seq: int | None = None,
) -> list[Any] | None:
    if not isinstance(trace, list):
        return trace
    max_seq = default_max_seq or infer_max_bar_seq_from_trace(trace)
    last_br: str | None = None
    for item in trace:
        if isinstance(item, dict):
            normalize_trace_item(
                item,
                default_max_seq=max_seq,
                prior_bar_range=last_br,
            )
            br = str(item.get("bar_range", "") or "")
            if br and br not in ("不适用", "—", "-"):
                last_br = br
    return trace


def normalize_stage1_traces(obj: dict[str, Any]) -> None:
    normalize_trace_list(obj.get("gate_trace"))


def normalize_stage2_traces(obj: dict[str, Any]) -> None:
    trace = obj.get("decision_trace")
    if isinstance(trace, list):
        normalize_trace_list(trace)
