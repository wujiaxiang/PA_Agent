"""文件 → SQLite 幂等导入器。

对应 ``docs/SESSION_STORAGE_DESIGN.md`` §7 阶段 A。设计要点：

- **幂等**：用 ``INSERT ... ON CONFLICT DO UPDATE``，重复跑不会产生重复行，
  也不会覆盖更新的数据（``updated_at`` 单调）。
- **不删数据**：导入器只增改，绝不 DROP。回退只需把 ``hub`` 关掉。
- **容错**：单条坏文件（截断 JSON、legacy 格式不符）跳过并计数，不中断整批。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub
from pa_agent.storage.repositories import upsert_record

logger = logging.getLogger("pa_agent.storage.import")


def _load_raw(path: Path) -> dict | None:
    """读磁盘 JSON。坏文件返回 None 而非抛出 —— 一条坏记录不该阻断整批导入。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("skip unreadable record %s: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def import_analysis_records(
    records_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int | None = None,
) -> dict[str, int]:
    """Import every ``*.json`` under *records_dir* into SQLite.

    Reuses ``list_record_paths`` so the partitioned and legacy flat layouts are
    both covered — 一份导入器，不要重新实现目录扫描。

    Returns ``{"scanned": n, "imported": n, "skipped": n}``.
    """
    from pa_agent.records.analysis_history import list_record_paths, load_record

    paths = list_record_paths(records_dir)
    if limit is not None:
        paths = paths[:limit]

    scanned = len(paths)
    imported = 0
    skipped = 0

    for path in paths:
        raw = _load_raw(path)
        if raw is None:
            skipped += 1
            continue
        record = load_record(path)
        if record is None:
            # 能解析 JSON 但不符合 AnalysisRecord schema —— 多半是早期损坏或
            # 非记录文件。不中断，记一笔。
            skipped += 1
            continue
        if upsert_record(record, raw=raw, file_path=path, user_id=user_id):
            imported += 1
        else:
            skipped += 1

    stats = {"scanned": scanned, "imported": imported, "skipped": skipped}
    logger.info("import_analysis_records: %s", stats)
    return stats


def iter_real_experience_files(experience_dir: Path):
    """Yield experience entry JSON paths, **skipping dot-prefixed directories**.

    必须跳过点号目录，这是 AGENTS.md 的硬要求：
    - ``.seed_demo_*/`` —— 合成数据（pnl_pct 成等差数列、mtime 集中在同一分钟），
      导入后会污染检索结果并被当成本人��验喂回提示词
    - ``.omc/`` —— 工具状态，与经验库无关

    只认 ``*_cases/`` 目录下的 json —— 那是 ``ExperienceWriter`` 的落盘约定
    （``success_cases`` / ``failure_cases`` / ``unresolved_cases`` / ``pending_cases``）。
    """
    if not experience_dir.is_dir():
        return
    for cycle_dir in sorted(experience_dir.iterdir()):
        if not cycle_dir.is_dir() or cycle_dir.name.startswith("."):
            continue
        for sub in sorted(cycle_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if not sub.name.endswith("_cases"):
                continue
            for p in sorted(sub.glob("*.json")):
                if p.is_file():
                    yield p


def _status_from_subdir(subdir_name: str) -> str:
    """``success_cases`` → ``success``。无法识别时归 pending（最保守）。"""
    return subdir_name[: -len("_cases")] if subdir_name.endswith("_cases") else "pending"


def import_experience_entries(
    experience_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int | None = None,
) -> dict[str, int]:
    """Import real experience entries into SQLite.

    ``exchange`` 不在文件名里，只能从 ``content`` 取；缺失时留空字符串 ——
    经验库筛选只用 symbol/timeframe，不用交易所。
    """
    from pa_agent.storage.experience_repo import upsert_entry

    scanned = imported = skipped = 0
    for path in iter_real_experience_files(experience_dir):
        scanned += 1
        if limit is not None and imported >= limit:
            break
        raw = _load_raw(path)
        if raw is None:
            skipped += 1
            continue
        # cycle/<status_cases>/file.json → cycle_position=<cycle>, status=<status>
        parts = path.relative_to(experience_dir).parts
        cycle_position = parts[0] if len(parts) >= 3 else ""
        status = _status_from_subdir(parts[1]) if len(parts) >= 3 else "pending"
        symbol = str(raw.get("symbol") or "")
        timeframe = str(raw.get("timeframe") or "")
        if upsert_entry(
            raw,
            cycle_position=cycle_position,
            status=status,
            symbol=symbol,
            timeframe=timeframe,
            file_path=path,
            user_id=user_id,
        ):
            imported += 1
        else:
            skipped += 1

    stats = {"scanned": scanned, "imported": imported, "skipped": skipped}
    logger.info("import_experience_entries: %s", stats)
    return stats


def import_all(
    *,
    records_dir: Path | None = None,
    experience_dir: Path | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    """Import all file-backed domains. Used by lifespan startup / CLI."""
    from pa_agent.config.paths import EXPERIENCE_DIR, RECORDS_PENDING_DIR

    out: dict[str, Any] = {
        "records": import_analysis_records(records_dir or RECORDS_PENDING_DIR, user_id=user_id),
        "experience": import_experience_entries(
            experience_dir or EXPERIENCE_DIR, user_id=user_id
        ),
    }
    return out
