#!/usr/bin/env python3
"""比对 pytest 结果与已知失败基线 —— **只对新增失败红**。

## 为什么需要它

本仓库存量 `FAILED=78 ERROR=30`，主体是经验库 / 存储层正在重构中的代码。
直接 `pytest tests/unit` 接进 CI 会让 CI **永久红** —— 那等于把 CI 关掉，
还不如现在这个「假绿灯」至少不误导。

所以策略是：
- 存量失败（基线内）→ **警告**，不失败
- **新增失败**（基线外）→ **失败**，这才是真正要拦的回归

## 语义

退出码：
- `0` 无新增失败（可能有存量失败，会打印摘要）
- `1` 有新增失败

## 用法

```bash
pytest tests/unit > /tmp/out.log 2>&1 || true      # pytest 失败也要继续比对
python tools/ci_diff_baseline.py \
    --log /tmp/out.log \
    --baseline tests/ci/baseline_failures.txt
```

## 基线维护

存量失败被修好后，**必须同时更新基线**，否则：
- 修好了但仍在基线里 → 漏报（真回归也可能是「基线里的某个名字」）
- 更糟：基线里没删掉的项，一旦同名用例再次失败会被当存量放过

所以本脚本会把「基线里已不存在于本次结果」的项单独报出来，提醒清理。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# pytest 输出里 "FAILED path::test - reason" / "ERROR path::test"
_RESULT_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def parse_results(log_path: Path) -> set[str]:
    """从 pytest 输出里抽出失败项的 `文件::用例` 全名。"""
    if not log_path.is_file():
        print(f"[baseline] 找不到 pytest 输出：{log_path}", file=sys.stderr)
        return set()
    found: set[str] = set()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RESULT_RE.match(line)
        if m:
            found.add(m.group(2))
    return found


def read_baseline(baseline_path: Path) -> set[str]:
    if not baseline_path.is_file():
        return set()
    return {
        line.strip()
        for line in baseline_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pytest 失败基线比对")
    ap.add_argument("--log", required=True, type=Path, help="pytest 输出文件")
    ap.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/ci/baseline_failures.txt"),
        help="已知失败基线",
    )
    args = ap.parse_args(argv)

    actual = parse_results(args.log)
    baseline = read_baseline(args.baseline)

    new_failures = sorted(actual - baseline)
    fixed = sorted(baseline - actual)

    print(f"[baseline] 本次失败 {len(actual)} 项 / 基线 {len(baseline)} 项")
    if baseline:
        print(f"[baseline] 存量失败（不阻断）：{len(actual & baseline)} 项")
        print(f"[baseline] 其中已修复、建议从基线移除：{len(fixed)} 项")

    if fixed:
        print("\n[baseline] ↓ 以下基线项本次未复现，请从基线文件移除：")
        for name in fixed:
            print(f"  - {name}")

    if new_failures:
        print(f"\n✗ 新增失败 {len(new_failures)} 项（基线外，判定为回归）：")
        for name in new_failures:
            print(f"  ✗ {name}")
        print(
            "\n若确认是**预期内**的新失败（如正在重构中），把它加进 "
            f"{args.baseline} 并提交。"
        )
        return 1

    print("\n✅ 无新增失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())