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

## 只解析 short test summary 段（不要全文扫）

2026-10-05 实测踩坑：原先对整个日志跑 `^(FAILED|ERROR)\\s+(\\S+)`，结果
**Captured log 段里的应用日志行也会命中**，例如

```
ERROR    web.api.routes_data:routes_data.py:451 experience browse: store unreadable
```

被当成名为 `web.api.routes_data:routes_data.py:451` 的测试 → 报成「新增回归」。
应用日志里出现 ERROR 是**正常运行的一部分**，与测试是否失败无关。

因此改为**只解析 `short test summary info` 段**，并且要求条目形如
`<路径>::<用例>`（或纯路径）—— 两者都能排除日志行。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# pytest 的 "FAILED path::test - reason" / "ERROR path::test"
_RESULT_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")
# 汇总段起止标记。-q 下可能是 "=== short test summary info ==="（含前后 ===）
_SUMMARY_START = re.compile(r"^=+.*short test summary info.*=+$")
# 真实用例条目一定带路径分隔符或 ::；应用日志行（Captured log 段）不会
_NODE_SHAPE = re.compile(r"^[\w./\\-]+\.py::|^[\w./\\-]+\.py$|^[\w./\\-]+\.py\s")


def parse_results(log_path: Path) -> set[str]:
    """从 pytest 输出里抽出失败项的 `文件::用例` 全名。

    **只解析 `short test summary info` 段**。整份日志里还有 Captured log 段，
    应用的 ERROR 日志行会被旧正则误当成失败项（见模块 docstring）。
    """
    if not log_path.is_file():
        print(f"[baseline] 找不到 pytest 输出：{log_path}", file=sys.stderr)
        return set()
    found: set[str] = set()
    in_summary = False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _SUMMARY_START.match(line.strip()):
            in_summary = True
            continue
        # 汇总段结束：遇到新的 === 标题（且不是 summary 本身）
        if in_summary and re.match(r"^=+ .* =+$", line.strip()):
            in_summary = False
            continue
        if not in_summary:
            continue
        m = _RESULT_RE.match(line.strip())
        if m and _NODE_SHAPE.match(m.group(2)):
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