#!/usr/bin/env python
"""多会话写入范围检查（配合 SESSION_CHANGES.md 使用）。

两个模式：

``--hook``（本地 pre-commit）
    比对**暂存区**文件与 SESSION_CHANGES.md「🔴 进行中」区声明的写入范围。
    命中即**阻断** —— 这正是「静默覆盖别人未提交工作」发生的那一刻，
    也是唯一还来得及挽回的时刻。

``--ci``（GitHub Actions）
    两条规则，都不涉及"是谁在改"这种易错的判断：
    1. 改了代码却没动 SESSION_CHANGES.md → 失败（强制登记纪律）
    2. 改动文件命中别人的「进行中」范围 → 警告（不阻断：正在干活的那个
       会话自己落地成果时必然命中自己的范围，阻断只会逼人绕过检查）

设计取舍：
- **按「路径前缀」匹配，不做精确文件名匹配**：条目里写 ``web/api/``
  就该盖住 ``web/api/routes_records.py``。精确匹配等于让登记形同虚设。
- **不做提交者身份判定**：靠 git config / PR 作者去猜"这条是不是我写的"
  在多账号、CI 提交、rebase 场景下都会误判。宁可漏报也不误伤。
- **不依赖任何第三方库**：仓库现有 CI 跑在 windows-latest，
  纯标准库才能直接跑。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGE_DOC = REPO_ROOT / "SESSION_CHANGES.md"

# 「进行中」区的开始标记。以它为界，只有这部分表示"此刻有人在写"。
INPROGRESS_MARKERS = ("## 🔴 进行中", "## 进行中")
DONE_MARKERS = ("## ✅ 已提交", "## 已提交")

# 代码文件：改了这些就必须登记改动记录
CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".json", ".yml", ".yaml", ".toml", ".sh", ".sql", ".vue", ".svelte",
}
CODE_PREFIXES = ("pa_agent/", "web/", "tests/", "tools/", ".github/")
# 纯文档/数据目录：改这些不必登记（它们的变更没有代码冲突语义）
DOC_PREFIXES = ("docs/", "experience/", "records/", "logs/", "trade_records/")

FENCE_RE = re.compile(r"^```", re.M)


def _run_git(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True,
            text=True, timeout=60, check=False,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def staged_files() -> list[str]:
    """Files staged for commit, including renames/deletions."""
    out = _run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def changed_files_in_range(base: str, head: str) -> list[str]:
    out = _run_git(["diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def is_code_file(path: str) -> bool:
    if path.startswith(DOC_PREFIXES):
        return False
    if path.endswith((".md", ".txt")):
        return False
    if path.startswith(CODE_PREFIXES):
        return True
    return Path(path).suffix in CODE_EXTS


def extract_scopes(doc_text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(in_progress_scopes, done_scopes)`` as ``(owner, scope)``.

    Parses the markdown tables inside each section: the first column of a
    table row is the file or directory, and the nearest preceding heading
    provides the owner. Row cells are wrapped in backticks.
    """
    # 去掉围栏代码块，避免把模板里的示例当成真实范围
    body = FENCE_RE.sub("", doc_text)

    def section(start_markers: tuple[str, ...], end_markers: tuple[str, ...]) -> str:
        lo = -1
        for m in start_markers:
            i = body.find(m)
            if i >= 0:
                lo = i if lo < 0 else min(lo, i)
        if lo < 0:
            return ""
        hi = len(body)
        for m in end_markers:
            i = body.find(m, lo + 1)
            if i >= 0:
                hi = min(hi, i)
        return body[lo:hi]

    def parse(chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        owner = "(未标注会话)"
        for line in chunk.splitlines():
            # 只认 ### 级标题作为会话名。条目内部的小节必须写成 ####，
            # 否则「最近的 ###」永远是最后一个小节（如「改动文件（写入范围）」），
            # 报错就完全指不出是谁占用了文件。
            h = re.match(r"^###\s+(.*)", line)
            if h:
                owner = h.group(1).strip()
                continue
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            first = cells[0]
            # 跳过表头与分隔行
            if set(first) <= set("-: ") or first in ("文件", "文件 / 路径"):
                continue
            for m in re.finditer(r"`([^`]+)`", first):
                out.append((owner, m.group(1).strip()))
        return out

    inprog = parse(section(INPROGRESS_MARKERS, DONE_MARKERS))
    done = parse(section(DONE_MARKERS, ()))
    return inprog, done


def scope_matches(scope: str, path: str) -> bool:
    """Directory scopes match by prefix; file scopes match exactly."""
    scope = scope.rstrip("/")
    if scope.endswith("*"):
        return path.startswith(scope[:-1])
    return path == scope or path.startswith(scope + "/")


def _rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def run_hook() -> int:
    if not CHANGE_DOC.exists():
        print(f"⚠️  未找到 {CHANGE_DOC.name}，跳过写入范围检查")
        return 0

    staged = [_rel(f) for f in staged_files()]
    if not staged:
        return 0
    if CHANGE_DOC.name in staged:
        # 本次提交更新了改动记录。**不直接放行**：那等于「顺手碰一下文档就能
        # 绕过检查」，形同虚设。仍然照常检查，只把提示说清楚 —— 合法的用法是
        # 把条目从「进行中」移到「已提交」，那样它本就不再占用范围，检查自然通过。
        print("ℹ️  本次提交包含 SESSION_CHANGES.md（仍会照常做范围检查）")

    inprog, _done = extract_scopes(CHANGE_DOC.read_text(encoding="utf-8"))
    if not inprog:
        return 0

    hits: dict[str, set[str]] = {}
    for path in staged:
        for owner, scope in inprog:
            if scope_matches(scope, path):
                hits.setdefault(path, set()).add(owner)

    if not hits:
        return 0

    print("\n🚫 提交被阻断：本次暂存的文件落在他人声明的「进行中」写入范围内\n")
    for path, owners in sorted(hits.items()):
        print(f"   {path}")
        for o in sorted(owners):
            print(f"       ↳ 已被「{o}」声明占用")
    print(f"\n   详情见 {CHANGE_DOC.name} 的「🔴 进行中」区。")
    print("   处理方式：")
    print("     · 对方已完成 → 先请其把条目移到「已提交」并 push，再重试")
    print("     · 双方都要改 → 在同一条目里加你的名字（如「A / B 共改」）")
    print("     · 范围有误 → 先修正条目，别靠临时摘除条目绕过")
    print("\n   确实要绕过本次检查（确认无冲突）：")
    print("     SKIP_WRITE_SCOPE_CHECK=1 git commit ...\n")
    return 1


def run_ci(base: str, head: str) -> int:
    doc_name = CHANGE_DOC.name
    changed = [_rel(f) for f in changed_files_in_range(base, head)]

    if not changed:
        return 0

    code_changed = [f for f in changed if is_code_file(f)]
    doc_changed = doc_name in changed

    if code_changed and not doc_changed:
        print("::error::改了代码文件却没有更新 "
              f"{doc_name}（AGENTS.md「多会话协作规范」要求完工前必写）")
        for f in code_changed[:25]:
            print(f"  · {f}")
        if len(code_changed) > 25:
            print(f"  · …… 另有 {len(code_changed) - 25} 个")
        return 1

    if not CHANGE_DOC.exists():
        return 0

    inprog, done = extract_scopes(CHANGE_DOC.read_text(encoding="utf-8"))
    if inprog:
        warned: dict[str, set[str]] = {}
        for path in changed:
            for owner, scope in inprog:
                if scope_matches(scope, path):
                    warned.setdefault(path, set()).add(owner)
        if warned:
            print("::warning::以下改动落在他人「进行中」的写入范围内 —— "
                  "合入前请确认对方的改动已一并落地或已协调")
            for path, owners in sorted(warned.items()):
                print(f"  · {path}  ←  「{'、'.join(sorted(owners))}」")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hook", action="store_true",
                      help="本地 pre-commit：暂存区 vs 进行中范围，硬阻断")
    mode.add_argument("--ci", action="store_true",
                      help="CI：强制登记改动记录 + 范围重叠告警")
    ap.add_argument("--base", default="origin/main", help="CI 模式的基线 ref")
    ap.add_argument("--head", default="HEAD", help="CI 模式的比较 ref")
    args = ap.parse_args(argv)

    if args.hook:
        if __import__("os").environ.get("SKIP_WRITE_SCOPE_CHECK"):
            print("⏭  已通过 SKIP_WRITE_SCOPE_CHECK 跳过写入范围检查")
            return 0
        return run_hook()

    sys.exit(run_ci(args.base, args.head))


if __name__ == "__main__":
    # 统一走 main()：此前 __main__ 对 --hook 直接调 run_hook()，
    # 把 main() 里的 SKIP_WRITE_SCOPE_CHECK 判断绕了过去 —— 绕过开关形同虚设。
    raise SystemExit(main())