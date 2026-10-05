"""文件 → SQLite 幂等导入器。

对应 ``docs/SESSION_STORAGE_DESIGN.md`` §7 阶段 A。设计要点：

- **幂等**：用 ``INSERT ... ON CONFLICT DO UPDATE``，重复跑不会产生重复行，
  也不会覆盖更新的数据（``updated_at`` 单调）。
- **不删数据**：导入器只增改，绝不 DROP。回退只需把 ``hub`` 关掉。
- **容错**：单条坏文件（截断 JSON、legacy 格式不符、缺列的 CSV）跳过并计数，
  不中断整批。

覆盖三域：``records/pending/*.json``（分析记录）、``experience/**/*_cases/*.json``
（经验库）、``trade_records/*.csv``（交易记录）。**三域的文件都是权威副本**，
DB 只作索引，随时可以从文件重建。
"""
from __future__ import annotations

import csv
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


def import_trade_records(
    trade_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int | None = None,
) -> dict[str, int]:
    """Import every ``trade_records/*.csv`` into SQLite.

    与 ``analysis_records`` / ``experience_entries`` 同契约：

    - **幂等**：``trade_id`` 由 ``(symbol, timeframe, record_time, 行号)`` 决定，
      重复导入只会 UPDATE 同一行，不会多出副本。这也意味着**写双份已经写过的行，
      导入后仍然只有一行** —— 两边的行号定义必须一致，见 ``trade_repo`` 模块文档。
    - **不删数据**：只增改，绝不 DROP。
    - **容错**：空文件 / 缺列 / 二进制坏文件一律跳过并计数，绝不中断整批。

    **CSV 是权威副本，DB 只是索引**：本函数可以从零重建整张表，删库不丢历史。

    统计口径：``scanned`` = 找到的 CSV 文件数（不含点号开头与子目录）；
    ``imported`` = 成功入库的数据行数；``skipped`` = 被跳过的**文件**数 +
    无 symbol 的数据行数。只有表头的文件 scanned+1 但 imported/skipped 均 +0
    （没东西可导，也不算「跳过」）。
    """
    from pa_agent.storage.trade_repo import upsert_trade_row

    scanned = imported = skipped = 0
    if not trade_dir.is_dir():
        logger.info("import_trade_records: %s does not exist, nothing to do", trade_dir)
        return {"scanned": 0, "imported": 0, "skipped": 0}

    paths = sorted(p for p in trade_dir.glob("*.csv") if p.is_file() and not p.name.startswith("."))
    for path in paths:
        scanned += 1
        if limit is not None and imported >= limit:
            break
        # 先整份解析再入库：解析失败 → 该文件一条都不写。部分导入比不导入更难解释，
        # 且 trade_id 幂等，重跑一次即可收敛，故不做逐行容错。
        parsed = _parse_trade_csv(path)
        if parsed is None:
            skipped += 1
            continue
        for row_no, row in parsed:
            if upsert_trade_row(row, csv_path=path, row_no=row_no, user_id=user_id):
                imported += 1
            else:
                skipped += 1

    stats = {"scanned": scanned, "imported": imported, "skipped": skipped}
    logger.info("import_trade_records: %s", stats)
    return stats


def _parse_trade_csv(path: Path) -> list[tuple[int, dict]] | None:
    """``[(行号, 行 dict), ...]``；无法当作交易记录解析时返回 ``None``。

    行号从 1 开始且不含表头 —— 必须与 ``trade_repo.peek_next_row_no`` 的定义
    完全一致，否则导入会给已双写的行再造副本。
    """
    from pa_agent.storage.trade_repo import REQUIRED_CSV_COLUMNS

    try:
        # errors="replace"：文件里混入一两个坏字节时仍能数清行号，
        # 真正的「这不是交易记录」交给下面的缺列判定。
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
            if missing:
                logger.info("skip %s: missing columns %s", path.name, missing)
                return None
            out: list[tuple[int, dict]] = []
            for row_no, row in enumerate(reader, start=1):
                # DictReader 把溢出的列塞进 None 键，这里丢掉
                out.append((row_no, {k: v for k, v in row.items() if k is not None}))
    except (OSError, csv.Error, ValueError) as exc:
        logger.warning("skip unreadable trade CSV %s: %s", path, exc)
        return None
    return out


def import_all(
    *,
    records_dir: Path | None = None,
    experience_dir: Path | None = None,
    trade_dir: Path | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    """Import all file-backed domains. Used by lifespan startup / CLI."""
    from pa_agent.config.paths import (
        EXPERIENCE_DIR,
        RECORDS_PENDING_DIR,
        TRADE_RECORDS_DIR,
    )

    out: dict[str, Any] = {
        "records": import_analysis_records(records_dir or RECORDS_PENDING_DIR, user_id=user_id),
        "experience": import_experience_entries(
            experience_dir or EXPERIENCE_DIR, user_id=user_id
        ),
        "trades": import_trade_records(trade_dir or TRADE_RECORDS_DIR, user_id=user_id),
    }
    return out
