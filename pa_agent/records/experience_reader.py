"""ExperienceReader — read-only access to the experience library.

Scans ``success_cases/`` and ``failure_cases/`` subdirectories under
``EXPERIENCE_DIR / cycle_position /``, sorts files by the timestamp
embedded in their filenames (descending, newest first), and returns
the top 5 entries across both directories combined.

File naming convention (timestamp portion):
    YYYY-MM-DD_HH-mm-ss   (minutes use '-', not ':')

Example filename:
    2026-05-18_14-30-45_XAUUSD_1h.json

This module is strictly read-only — it never writes or deletes files.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from pa_agent.records.experience_writer import RETRIEVABLE_STATUSES, STATUS_WIN
from pa_agent.records.schema import ExperienceEntry

# Regex to extract the timestamp portion from a filename.
_TS_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"


def _default_logger() -> logging.Logger:
    return logging.getLogger(__name__)


#: 方向别名 → 规范值。三个来源各说各话：阶段一诊断输出
#: ``bullish/bearish/neutral``（prompt_engineering/市场诊断框架.txt），
#: 阶段二决策输出 ``做多/做空``（json_validator 限定的枚举），种子数据里又是
#: ``up/down``。检索打分要跨这三者比对，必须先归一。
_DIRECTION_ALIASES: dict[str, str] = {
    "up": "up", "long": "up", "buy": "up", "bull": "up", "bullish": "up",
    "做多": "up", "看涨": "up", "涨": "up",
    "down": "down", "short": "down", "sell": "down", "bear": "down",
    "bearish": "down", "做空": "down", "看跌": "down", "跌": "down",
    "neutral": "neutral", "none": "neutral", "中性": "neutral", "": "neutral",
}


def _normalize_direction(raw: object) -> str:
    """把任意来源的方向值归一成 ``up`` / ``down`` / ``neutral``。"""
    return _DIRECTION_ALIASES.get(str(raw or "").strip().lower(), "")


def _parse_timestamp_ms(filename: str) -> Optional[int]:
    """Extract and parse the timestamp from a filename.

    Returns the timestamp in milliseconds, or ``None`` if the filename
    does not contain a parseable timestamp.
    """
    match = _TS_PATTERN.search(filename)
    if not match:
        return None
    ts_str = match.group(1)
    try:
        dt = datetime.strptime(ts_str, _TS_FORMAT)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _loads(raw: object) -> dict | None:
    """解析一行里的 ``content_json``。坏行跳过，不让单条脏数据毁掉整批检索。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ExperienceReader:
    """Read experience entries from the experience library (read-only).

    Parameters
    ----------
    experience_dir:
        **已废弃**。库是唯一真源，不再从文件系统读任何东西；该参数仅为兼容
        既有构造调用而保留。
    logger:
        Optional logger instance.  A module-level logger is used when
        ``None``.
    """

    def __init__(
        self,
        experience_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if experience_dir is None:
            from pa_agent.config.paths import EXPERIENCE_DIR
            experience_dir = EXPERIENCE_DIR

        self._experience_dir = experience_dir
        self._logger = logger or _default_logger()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_top5(self, cycle_position: str, *, user_id: str = "") -> list[ExperienceEntry]:
        """Return the most-recent verified experience entries for *cycle_position*.

        **只查库**（2026-10-05 起文件布局已废弃）。DB 是唯一真源，不再有
        「扫不到就回落文件」这一步 —— 那条回落路本身就是旧设计的产物：它意味着
        同一条经验有两个真源，而两者静默分叉时没人知道该信哪个。

        只取 **已验证** 的 win/loss：pending / unresolved 不是已验证经验，
        不得当成失败经验喂回提示词（AGENTS.md）。

        为什么把 DB 逻辑放进 ``read_top5`` 而不是 ``read_for_stage2``：
        ``read_top5`` 是唯一的检索漏斗（``read_for_stage2`` 与浏览 API 都走它），
        在这里改能一次覆盖所有调用方，14 处 ``mock.read_top5`` 测试点继续有效。

        本模块**严格只读**：只查，不写库也不写文件（AGENTS.md「写入方唯一入口」）。

        Parameters
        ----------
        cycle_position:
            The cycle position label (e.g. ``"micro_channel"``).
        user_id:
            Whose library to read.  留空则用存储层默认用户 —— 多用户隔离
            的唯一依据（文件系统里没有用户概念）。

        Returns
        -------
        list[ExperienceEntry]
            Newest first.  Empty list when nothing is retrievable — including
            when the store itself is unreadable, which callers cannot
            distinguish. That is intentional: 经验库不可用时最该做的是
            让分析照常跑完，而不是带一个假「库里为空」去影响决策。
        """
        return self._read_top5_from_db(cycle_position, user_id) or []

    def _read_top5_from_db(
        self, cycle_position: str, user_id: str
    ) -> list[ExperienceEntry] | None:
        """DB 读取。``None`` 表示「存储层不可用」，``[]`` 表示「确实没有」。

        两者仍要区分 —— 前者该记 warning，后者不该吵。靠 ``QueryResult.failed``，
        不靠事后去读 hub 上的标志位。
        """
        try:
            from pa_agent.storage.experience_repo import list_entries
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("experience storage unavailable: %s", exc)
            return None

        kwargs: dict = {
            # 只取已验证的 win/loss：pending/unresolved 不是已验证经验，
            # 不得当失败经验喂回提示词（AGENTS.md「两阶段状态机」）
            "statuses": RETRIEVABLE_STATUSES,
            "cycle_position": cycle_position,
            "limit": 5,
        }
        if user_id:
            kwargs["user_id"] = user_id
        try:
            rows = list_entries(**kwargs)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("experience DB read failed: %s", exc)
            return None
        if getattr(rows, "failed", False):
            self._logger.warning(
                "experience store unreadable (%s); no experience this run",
                getattr(rows, "error", "") or "unknown",
            )
            return None

        out: list[ExperienceEntry] = []
        for row in rows:
            content = _loads(row.get("content_json"))
            if content is None:
                continue
            out.append(ExperienceEntry(
                filename=str(row.get("entry_id") or ""),
                case_type="success" if row.get("status") == STATUS_WIN else "failure",
                cycle_position=str(row.get("cycle_position") or cycle_position),
                timestamp_ms=int(row.get("timestamp_ms") or 0),
                content=content,
            ))
        return out

    def read_for_stage2(
        self,
        cycle_position: str,
        *,
        direction: str = "",
        patterns: list[str] | None = None,
        max_entries: int = 3,
        user_id: str = "",
    ) -> list[ExperienceEntry]:
        """Return recent experience entries filtered for Stage 2 relevance.

        打分逻辑刻意留在 Python 而不是下推到 SQL：只有「方向 +2」与「形态
        交集 +N」两项，数据量百级；SQL 化收益低、回归面大。
        """
        entries = self.read_top5(cycle_position, user_id=user_id)
        if not entries:
            return []

        dir_norm = _normalize_direction(direction)
        pattern_set = {
            str(p).strip().lower() for p in (patterns or []) if str(p).strip()
        }

        def _score(entry: ExperienceEntry) -> int:
            content = entry.content if isinstance(entry.content, dict) else {}
            score = 0
            # 两边都归一化再比：条目的 direction 来自阶段二的 order_direction
            # （校验限定 ["做多","做空"]），而传入的是阶段一的 direction
            # （bullish/bearish/neutral）。直接比字符串则**永远不等**，
            # +2 分恒为 0，只剩形态交集在起作用 —— 一个不会报错、只会让
            # 检索悄悄退化的缺陷。
            ent_dir = _normalize_direction(content.get("direction", ""))
            if dir_norm and ent_dir == dir_norm:
                score += 2
            ent_patterns = content.get("detected_patterns") or []
            if pattern_set and isinstance(ent_patterns, list):
                overlap = pattern_set.intersection(
                    {str(p).strip().lower() for p in ent_patterns}
                )
                score += len(overlap)
            return score

        ranked = sorted(entries, key=lambda e: (_score(e), e.timestamp_ms), reverse=True)
        cap = max(0, min(max_entries, 10))
        return ranked[:cap]

