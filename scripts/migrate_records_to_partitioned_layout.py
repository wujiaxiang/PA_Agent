#!/usr/bin/env python
"""一次性迁移脚本：把 records/pending/ 下的旧平铺记录移动到新的分目录结构。

旧布局: records/pending/{timestamp}_{symbol}_{timeframe}.json
新布局: records/pending/{exchange}/{symbol}/{timeframe}/{timestamp}.json

用法:
    python scripts/migrate_records_to_partitioned_layout.py [--dry-run] [--force] [--records-dir PATH]
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Add project root to path so we can import pa_agent modules
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pa_agent.records.schema import AnalysisRecord
from pa_agent.records.pending_writer import _safe_path_segment


# 旧平铺文件名格式: {timestamp}_{symbol}_{timeframe}.json
# timestamp 含下划线的话会复杂，但通常 ISO 格式用 - 替换 :
# 例如: 2026-07-18T14-00-13_BTCUSDT_1d.json
FLAT_FILENAME_RE = re.compile(r"^(?P<timestamp>.+)_(?P<symbol>[^_]+)_(?P<timeframe>[^_]+)\.json$")


def parse_flat_filename(filename: str):
    """从旧平铺文件名解析 symbol/timeframe。返回 (symbol, timeframe) 或 None。"""
    m = FLAT_FILENAME_RE.match(filename)
    if not m:
        return None
    return m.group("symbol"), m.group("timeframe")


def load_record_exchange(filepath: Path, fallback_exchange: str) -> str:
    """从记录文件加载 exchange。若记录中 exchange 为空，用 fallback。"""
    try:
        with filepath.open("r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("meta", {})
        exchange = meta.get("exchange", "") or ""
        if not exchange:
            return fallback_exchange
        return exchange
    except Exception as e:
        print(f"  ⚠ 加载失败 {filepath}: {e}", file=sys.stderr)
        return fallback_exchange


def get_fallback_exchange() -> str:
    """从 config/settings.json 读取 general.last_tradingview_exchange 作为兜底。"""
    settings_path = ROOT / "config" / "settings.json"
    if not settings_path.exists():
        return ""
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            s = json.load(f)
        return s.get("general", {}).get("last_tradingview_exchange", "") or ""
    except Exception:
        return ""


def is_already_partitioned(filepath: Path, pending_dir: Path) -> bool:
    """检查文件是否已经在分目录结构下（即父目录不是 pending_dir 本身）。"""
    try:
        filepath.relative_to(pending_dir)
        return filepath.parent != pending_dir
    except ValueError:
        return False


def build_target_path(pending_dir: Path, exchange: str, symbol: str, timeframe: str, timestamp_filename: str) -> Path:
    """构造新的目标路径: pending_dir/{exchange}/{symbol}/{timeframe}/{timestamp}.json"""
    return (
        pending_dir
        / _safe_path_segment(exchange)
        / _safe_path_segment(symbol)
        / _safe_path_segment(timeframe)
        / timestamp_filename
    )


def main():
    parser = argparse.ArgumentParser(description="迁移 records/pending 旧平铺记录到分目录结构")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际移动文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    parser.add_argument("--records-dir", default=str(ROOT / "records" / "pending"), help="记录目录路径")
    args = parser.parse_args()

    pending_dir = Path(args.records_dir)
    if not pending_dir.exists():
        print(f"✗ 记录目录不存在: {pending_dir}")
        return 1

    fallback_exchange = get_fallback_exchange()
    print(f"配置兜底 exchange: {fallback_exchange or '(空)'}")
    print(f"模式: {'DRY-RUN' if args.dry_run else 'ACTUAL MOVE'}")
    print()

    # 收集所有顶层 .json 文件（旧平铺布局）
    flat_files = [f for f in pending_dir.glob("*.json") if f.is_file()]
    # 排除 .followups.jsonl（不是记录文件）
    flat_files = [f for f in flat_files if not f.name.endswith(".followups.jsonl")]

    success = 0
    skipped = 0
    failed = 0

    for filepath in flat_files:
        # 尝试从文件名解析 symbol/timeframe
        parsed = parse_flat_filename(filepath.name)
        if not parsed:
            print(f"⊘ 跳过（文件名不匹配旧格式）: {filepath.name}")
            skipped += 1
            continue

        symbol, timeframe = parsed
        exchange = load_record_exchange(filepath, fallback_exchange)

        if not exchange:
            print(f"⚠ 跳过（无 exchange 信息）: {filepath.name}")
            skipped += 1
            continue

        target = build_target_path(pending_dir, exchange, symbol, timeframe, filepath.name)

        if target.exists() and not args.force:
            print(f"⊘ 跳过（目标已存在，用 --force 覆盖）: {target}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"[DRY-RUN] {filepath.name} → {target.relative_to(pending_dir)}")
            success += 1
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(filepath), str(target))
                print(f"✓ {filepath.name} → {target.relative_to(pending_dir)}")
                success += 1
            except Exception as e:
                print(f"✗ 失败 {filepath.name}: {e}", file=sys.stderr)
                failed += 1

    print()
    print(f"统计: 成功={success}, 跳过={skipped}, 失败={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
