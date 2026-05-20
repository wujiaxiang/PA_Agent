"""Prompt assembler for Stage 1 (diagnosis) and Stage 2 (decision)."""
from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path
from typing import Any

from pa_agent.data.base import KlineFrame

logger = logging.getLogger(__name__)

# ── Hardcoded output format reminders ─────────────────────────────────────────

_STAGE1_OUTPUT_REMINDER = """
请严格按照以下 JSON 格式输出诊断结果,不要输出任何其他内容:

```json
{
  "cycle_position": "spike|micro_channel|tight_channel|normal_channel|broad_channel|trending_tr|trading_range|extreme_tr|unknown",
  "alternative_cycle_position": null,
  "direction": "bullish|bearish|neutral",
  "diagnosis_confidence": 75,
  "spike_stage": null,
  "market_phase": "stable|transitioning",
  "transition_risk": null,
  "detected_patterns": [],
  "key_signals": [],
  "htf_context": "",
  "entry_setup": "",
  "strategy_files_needed": ["下跌通道分析识别.txt", "下跌通道交易策略.txt"],
  "risk_warning": "",
  "gate_trace": [
    {
      "node_id": "0.1",
      "question": "是否看得懂当前市场？",
      "answer": "是",
      "action": "继续",
      "reason": "结构清晰",
      "branch": "yes",
      "section": "总原则",
      "bar_range": "由你填写，如 K42-K1"
    }
  ],
  "gate_result": "proceed"
}
```

## 阶段一闸门（二元决策树 §0–§2，必须执行）

在输出诊断 JSON 前，按《二元决策.txt》**依次**评估以下节点，并写入 gate_trace（仅记录你实际评估的节点，通常 6–10 条）：
§0：0.1 看得懂市场 → 0.2 是否具备继续分析的条件（定性，**不是**交易者方程）
§1：1.1 数据足够 → 1.2 识别周期 → 1.3 极端混乱
§2：2.1 惯性方向 → 2.2 大时间框架 → 2.3 多/空/中性（**answer 只能用 是/否/中性**；方向写在 branch：bullish/bearish/neutral，勿写「多头」「空头」作 answer）

**禁止在阶段一评估：**
- **0.3**（交易者方程仅为原则；数值检验在阶段二 **10.3**）
- **§9–§11**（入场、风险、下单均属阶段二）

规则：
- answer 只能是：是 / 否 / 中性 / 等待 / 不适用
- 任一闸门导致「等待/unknown」时，gate_result 设为 wait 或 unknown，并在最后一条 trace 写明 reason
- gate_result=proceed 表示可通过闸门进入阶段二；wait/unknown 表示不应进入策略与下单评估
- gate_trace 与 cycle_position、direction 不得矛盾

**每条 gate_trace / decision_trace 必须包含 bar_range（K线依据，由你自行判断）：**
- **程序不会替你填写**；你必须根据「本节点实际引用了哪些 K 线」写出序号范围
- 格式：`K{较老序号}-K{较新序号}` 或单根 `K1`（**序号1=最新已收盘**，序号越大越早）
- **每个节点的 bar_range 应不同**（除非该节点确实与上一节点使用完全相同窗口）；禁止所有节点照抄同一个范围
- 区间格式必须为 **K{较老}-K{较新}**（如 K4-K1），**禁止** K1-K4；单根写 K1；全图分析可写「全局」（程序会展开）
- 方向/分类类节点（如 4.2 上涨还是下跌）：**answer 只用 是/否/中性**，方向写在 **branch**（bullish/bearish），勿写「上涨」「下跌」作 answer
- **6.2**（区间类型）：answer=是/否，branch=trending_tr 或 trading_range；勿把「趋势型交易区间」写在 answer
- **6.3**（是否在边界）：answer=是/否，branch=lower/upper/middle；勿写「是，在下边界」——应写 answer=是、branch=lower
- 扫描类节点（如禁止行为）：answer 用 **是**（通过）或 **否**（触犯），勿写「通过」
- **禁止照抄**本提示 JSON 示例里的占位文字或说明中的举例数字；必须对应当前 K 线表与你在 reason 中的分析
- 跳过节点（skipped:true）：answer=不适用，bar_range 填字符串 `不适用`（**禁止填 null**）
- question 只写问题本身，不要把 bar_range 写进 question

diagnosis_confidence 必须为 0-100 的整数(满分100),表示对 cycle_position 等诊断结论的综合置信评分。
禁止使用 high、medium、low 等字符串;分数越高表示对当前市场状态判断越有把握。

diagnosis_confidence 分档说明:
- 90-100:周期位置非常典型,K线特征完全匹配频谱定义,多时间框架方向一致,信号充分无矛盾
- 70-89:周期位置较明确,主要特征吻合频谱定义,可能有个别模糊信号但不影响核心判断
- 50-69:周期位置存在歧义(如 trending_tr vs normal_channel),信号部分矛盾,需更多K线确认;市场可能处于过渡阶段
- 30-49:信号严重矛盾,周期位置难以判定,K线特征与多种状态都有部分重叠
- 0-29:数据不足以支撑任何诊断,或市场状态极度混乱(如极端交易区间)
""".strip()

_STAGE2_OUTPUT_CONTRACT = """
请严格按照以下 JSON 格式输出决策结果，不要输出任何其他内容。
重要规则：当 order_type 为“不下单”时，entry_price、take_profit_price、stop_loss_price、order_direction 必须全部为 null。

```json
{
  "decision": {
    "order_direction": "做多|做空|null",
    "order_type": "限价单|突破单|市价单|不下单",
    "entry_price": null,
    "take_profit_price": null,
    "stop_loss_price": null,
    "reasoning": "",
    "diagnosis_confidence": 75,
    "diagnosis_confidence_reasoning": "",
    "trade_confidence": 70,
    "trade_confidence_reasoning": "",
    "key_factors": [],
    "watch_points": [],
    "risk_assessment": "",
    "invalidation_condition": ""
  },
  "diagnosis_summary": {
    "cycle_position": "",
    "direction": "",
    "key_signals": []
  },
  "decision_trace": [
    {
      "node_id": "4.1",
      "section": "通道",
      "question": "是否出现有序波段结构？",
      "answer": "是",
      "reason": "HH+HL",
      "skipped": false,
      "bar_range": "由你填写"
    }
  ],
  "terminal": {
    "node_id": "11.2",
    "outcome": "trade",
    "label": "..."
  }
}
```

说明：decision_trace 需输出完整决策路径（通常多条）；每条 trace 的 **bar_range 必须由你根据该节点实际使用的 K 线填写**，不得照抄示例。

## 阶段二决策路径（二元决策树 §3–§11、§14）

阶段一 gate_result=proceed 时，decision_trace 必须遵守**执行顺序**（可跳过不适用分支，但不可乱序）：

1. **§3–§8** 按 cycle_position 走对应结构分支（尖峰/通道/区间/反转/楔形等）
2. **§9** 入场信号二元检查（9.1→9.5，须先确认信号 K 线收盘）
3. **§10** 风险收益（必须按序）：**10.1 止损明确 → 10.2 止损不过大 → 10.3 交易者方程 → 10.4 单笔风险**
4. **§11** 下单方式（仅当 10.3 为「是」且拟下单时评估 11.1–11.4）
5. **§14** 禁止行为清单：下单前快速扫描，触犯任一条 → order_type=不下单

**交易者方程（10.3）规则：**
- 必须使用已拟定的 entry / stop / target 估算胜率、回报、风险后再判
- **10.3 通过之前**不得输出具体下单类型；**10.3 之后**才写 §11
- 因方程不通过而放弃：terminal.node_id 应为 **10.3**，outcome=reject 或 wait

**跳过规则：**
- 无持仓：跳过 §12、§13（不写 trace）
- 不适用分支：skipped:true，answer=不适用

terminal 必须与 order_type 一致：
- 有下单 → outcome=trade，terminal.node_id 建议为最后一个 §11 节点
- 不下单 → outcome=wait 或 reject

阶段一 gate_result 为 wait/unknown 时：系统会短路，不应调用本阶段。

置信度分为两部分，各自独立打分（均为 0–100 整数，必须填写）：

一、diagnosis_confidence —— 对市场趋势与市场周期判断的把握
分档说明：
- 90-100：周期位置非常典型，趋势方向明确，多时间框架一致，K线特征完全匹配频谱定义
- 70-89：周期位置较明确，趋势方向可判定，主要特征吻合，可能有个别模糊信号
- 50-69：周期位置存在歧义（如 trending_tr vs normal_channel），趋势方向不够清晰，信号部分矛盾
- 30-49：信号严重矛盾，周期位置难以判定，趋势方向不确定
- 0-29：市场极度混乱或数据不足，无法做出有效诊断
diagnosis_confidence_reasoning：必须简要说明打分依据（如“trending_tr 与 normal_channel 特征重叠，HTF 方向与小框架不一致”）

二、trade_confidence —— 对交易决策本身的把握
分档说明：
- 90-100：极高把握，入场方案结构清晰、理由充分，风险回报比优异
- 70-89：较高把握，主要逻辑明确，入场方案可行
- 50-69：中等把握，存在不确定性但仍可执行当前决策（含观望）
- 30-49：较低把握，建议继续等待更清晰信号
- 0-29：极低把握；若同时判断不应交易，可配合 order_type="不下单"
trade_confidence_reasoning：必须简要说明打分依据（如“入场信号明确但止损空间偏大，risk:reward 仅 1.5:1”）
""".strip()

# txt files merged into each stage system prompt (order preserved)
STAGE1_PROMPT_TXT_FILES: tuple[str, ...] = (
    "提示词大纲_人设与思维方式.txt",
    "市场诊断框架.txt",
    "二元决策.txt",
    "文件16-K线信号识别.txt",
)

STAGE2_BASE_PROMPT_TXT_FILES: tuple[str, ...] = (
    "提示词大纲_人设与思维方式.txt",
    "二元决策.txt",
    "文件17-止损和止盈与仓位管理.txt",
)


def stage1_prompt_txt_files() -> list[str]:
    """Return ordered .txt filenames injected in Stage 1 system prompt."""
    return list(STAGE1_PROMPT_TXT_FILES)


def stage2_prompt_txt_files(strategy_files: list[str] | None = None) -> list[str]:
    """Return ordered .txt filenames injected in Stage 2 system prompt."""
    routed = [f for f in (strategy_files or []) if f]
    return [STAGE2_BASE_PROMPT_TXT_FILES[0], *routed, STAGE2_BASE_PROMPT_TXT_FILES[1]]


# ── PromptAssembler ────────────────────────────────────────────────────────────

class PromptAssembler:
    """Builds message lists for Stage 1 and Stage 2 API calls."""

    def __init__(
        self,
        prompt_dir: Path,
        experience_reader: Any = None,
    ) -> None:
        self._prompt_dir = prompt_dir
        self._experience_reader = experience_reader

    # ── File loading ──────────────────────────────────────────────────────────

    def _load(self, filename: str) -> str:
        """Load a prompt file by name. Returns empty string on error."""
        path = self._prompt_dir / filename
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to load prompt file %s: %s", filename, exc)
            return f"[ERROR: could not load {filename}]"

    # ── K-line table rendering ────────────────────────────────────────────────

    @staticmethod
    def _render_kline_table(frame: KlineFrame) -> str:
        """Render the K-line data as a text table (newest bar first)."""
        lines = [
            "序号 | 时间                | 开盘价    | 最高价    | 最低价    | 收盘价    | 成交量    | EMA20     | ATR14",
            "-----+--------------------+----------+----------+----------+----------+----------+-----------+----------",
        ]
        for i, bar in enumerate(frame.bars):
            ema = frame.indicators.ema20[i]
            atr = frame.indicators.atr14[i]
            ema_str = f"{ema:.4f}" if not math.isnan(ema) else "N/A"
            atr_str = f"{atr:.4f}" if not math.isnan(atr) else "N/A"
            # ts_open is in milliseconds (MT5 source); convert to seconds for fromtimestamp()
            dt = datetime.datetime.fromtimestamp(bar.ts_open / 1000).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{bar.seq:<4} | {dt:<19} | {bar.open:<9.4f} | {bar.high:<9.4f} | "
                f"{bar.low:<9.4f} | {bar.close:<9.4f} | {bar.volume:<9.0f} | "
                f"{ema_str:<10} | {atr_str}"
            )
        return "\n".join(lines)

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def build_stage1(self, frame: KlineFrame) -> list[dict]:
        """Build the message list for Stage 1 (market diagnosis)."""
        system_parts = [
            *(self._load(name) for name in STAGE1_PROMPT_TXT_FILES),
            _STAGE1_OUTPUT_REMINDER,
        ]
        system_content = "\n\n" + "\n\n---\n\n".join(p for p in system_parts if p)

        kline_table = self._render_kline_table(frame)
        n_bars = len(frame.bars)
        user_content = (
            f"## 当前分析目标\n\n"
            f"品种:{frame.symbol} 周期:{frame.timeframe} K线数量:{n_bars}\n"
            f"（K线序号：1=最新已收盘，最大 K{n_bars}；"
            f"每个决策节点的 bar_range 由你自行选择子区间，勿超出 K{n_bars}-K1）\n\n"
            f"## K线数据(序号1=最新已收盘K线,序号越大越早;不含当前未收盘K线)\n\n"
            f"{kline_table}\n\n"
            f"请根据以上数据,按照系统提示中的格式输出 JSON 诊断结果。"
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def build_stage2(
        self,
        frame: KlineFrame,
        stage1_json: dict,
        strategy_files: list[str],
        experience_entries: list[Any],
    ) -> list[dict]:
        """Build the message list for Stage 2 (trading decision)."""
        # System prompt: 人设 → 策略文件 → 风控 → 经验 → 输出契约
        system_parts = [self._load(name) for name in stage2_prompt_txt_files(strategy_files)]

        if experience_entries:
            exp_text = self._render_experience(experience_entries)
            system_parts.append(exp_text)

        system_parts.append(_STAGE2_OUTPUT_CONTRACT)

        system_content = "\n\n" + "\n\n---\n\n".join(p for p in system_parts if p)

        # User prompt
        kline_table = self._render_kline_table(frame)
        gate_result = stage1_json.get("gate_result", "proceed")
        gate_trace = stage1_json.get("gate_trace") or []
        gate_block = ""
        if gate_trace:
            gate_block = (
                f"## 阶段一闸门路径 (gate_result={gate_result})\n\n"
                f"```json\n{json.dumps(gate_trace, ensure_ascii=False, indent=2)}\n```\n\n"
            )

        n_bars = len(frame.bars)
        user_content = (
            f"## 阶段一诊断结果\n\n```json\n{json.dumps(stage1_json, ensure_ascii=False, indent=2)}\n```\n\n"
            f"{gate_block}"
            f"## K线数据(与阶段一相同, 共{n_bars}根；各节点 bar_range 由你据实填写)\n\n{kline_table}\n\n"
            f"请根据以上诊断、闸门路径和K线数据,按《二元决策.txt》§3–§15 输出 JSON 决策结果"
            f"(含 decision_trace 与 terminal)。\n"
            f"注意:如果判断不下单,entry_price、take_profit_price、stop_loss_price、order_direction 必须全部为 null。"
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    def stage2_system_prompt_only(
        self,
        strategy_files: list[str],
        experience_entries: list[Any],
    ) -> str:
        """Return only the Stage 2 system prompt string (for FreeChatSession reuse)."""
        system_parts = [self._load(name) for name in stage2_prompt_txt_files(strategy_files)]
        if experience_entries:
            system_parts.append(self._render_experience(experience_entries))
        system_parts.append(_STAGE2_OUTPUT_CONTRACT)
        return "\n\n" + "\n\n---\n\n".join(p for p in system_parts if p)

    @staticmethod
    def _render_experience(entries: list[Any]) -> str:
        """Render experience library entries as a text block."""
        lines = ["## 经验库(最近案例,供参考)"]
        for i, entry in enumerate(entries, 1):
            if isinstance(entry, dict):
                lines.append(
                    f"\n### 案例 {i}\n```json\n{json.dumps(entry, ensure_ascii=False, indent=2)}\n```"
                )
            else:
                lines.append(f"\n### 案例 {i}\n{entry}")
        return "\n".join(lines)
