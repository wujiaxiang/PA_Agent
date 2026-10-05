"""配置级联：系统兜底 → 用户覆盖 → 环境变量。

    shell env  >  ★用户覆盖(user_prefs, 稀疏)  >  ★系统兜底(global_config)  >  settings.json(遗留/灾备)

**为什么用户层必须稀疏**：若把整份 Settings 复制进 user_prefs，用户改一个字段就得
搬走全量配置，此后系统兜底配置的任何更新都再也传不到他身上 —— 覆盖关系会悄悄
断掉。正确做法是**只存差异**（patch），加载时深合并。

**播种顺序**（首次运行）：

1. ``settings.json``（既有文件）→ 写入 **global_config** 作为系统兜底
2. ``user_prefs`` 留空 —— 用户尚未改过任何东西，理应完全继承系统配置

这样「用户默认使用系统配置」是自然结果，而不是需要特判的分支。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pa_agent.storage.db import get_hub, now

logger = logging.getLogger("pa_agent.storage.settings")

#: 整份配置在 global_config / user_prefs 里的键名。
_BASELINE_KEY = "settings.baseline"
_OVERRIDE_KEY = "settings.overrides"


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge *patch* onto *base*, returning a new dict.

    只对 dict 递归，其余类型直接覆盖（列表整体替换而非合并 —— 配置里的列表语义
    都是「整份设定」，逐项合并会产生用户没预期的结果）。

    ``None`` 值被当作「未设置」跳过：前端可能显式传 null，而那不代表要清空值。
    """
    out = dict(base)
    for k, v in (patch or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def compute_diff(base: dict, current: dict) -> dict:
    """Sparse patch turning *base* into *current* (only differing leaves).

    存差异而非全量，见模块 docstring。
    """
    diff: dict[str, Any] = {}
    for key, cur_val in current.items():
        base_val = base.get(key)
        if isinstance(cur_val, dict) and isinstance(base_val, dict):
            sub = compute_diff(base_val, cur_val)
            if sub:
                diff[key] = sub
        elif cur_val != base_val:
            diff[key] = cur_val
    return diff


# ── 系统兜底（global_config） ────────────────────────────────────────────────


def load_baseline() -> dict[str, Any] | None:
    if get_hub().disabled:
        return None
    row = get_hub().query_one(
        "SELECT value_json FROM global_config WHERE key = ?", (_BASELINE_KEY,)
    )
    return _parse(row)


def save_baseline(data: dict[str, Any]) -> bool:
    if get_hub().disabled:
        return False
    return get_hub().execute(
        """
        INSERT INTO global_config (key, value_json, updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json, updated_at = excluded.updated_at
        """,
        (_BASELINE_KEY, json.dumps(data, ensure_ascii=False), now()),
    )


# ── 用户覆盖（user_prefs，稀疏） ────────────────────────────────────────────


def load_overrides(user_id: str) -> dict[str, Any]:
    """Always returns a dict — 空覆盖与「无覆盖」等价，都是继承系统配置。"""
    if get_hub().disabled:
        return {}
    row = get_hub().query_one(
        "SELECT value_json FROM user_prefs WHERE user_id = ? AND key = ?",
        (user_id, _OVERRIDE_KEY),
    )
    return _parse(row) or {}


def save_overrides(patch: dict[str, Any], user_id: str) -> bool:
    """Replace the user's override patch wholesale.

    整体替换而非合并：PATCH /api/settings 的语义是「这份 body 就是我想要的
    覆盖层」，合并会让已删除的字段复活。
    """
    if get_hub().disabled:
        return False
    ok = get_hub().execute(
        """
        INSERT INTO user_prefs (user_id, key, value_json, updated_at) VALUES (?,?,?,?)
        ON CONFLICT(user_id, key) DO UPDATE SET
            value_json = excluded.value_json, updated_at = excluded.updated_at
        """,
        (user_id, _OVERRIDE_KEY, json.dumps(patch or {}, ensure_ascii=False), now()),
    )
    if not ok:
        logger.warning("save settings overrides failed; file copy still holds the value")
    return ok


def clear_overrides(user_id: str) -> bool:
    """Reset to pure system defaults. 「恢复默认」按钮的落点。"""
    return get_hub().execute(
        "DELETE FROM user_prefs WHERE user_id = ? AND key = ?", (user_id, _OVERRIDE_KEY)
    )


def _parse(row) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        data = json.loads(row["value_json"])
    except json.JSONDecodeError:
        logger.warning("settings row has corrupt JSON; ignoring it")
        return None
    return data if isinstance(data, dict) else None


def resolve(user_id: str, file_fallback: dict[str, Any] | None = None) -> dict | None:
    """Full effective config: baseline ← overrides, with file as last resort.

    返回 None 表示「DB 与文件都没有配置」，调用方应落默认值。
    """
    baseline = load_baseline()
    if baseline is None and file_fallback is not None:
        # 首次运行：把既有文件升格为系统兜底，之后用户继承它
        baseline = file_fallback
        save_baseline(baseline)
    if baseline is None:
        return None
    return deep_merge(baseline, load_overrides(user_id))


def seed_from_file(file_data: dict[str, Any]) -> bool:
    """把既有 settings.json 升格为系统兜底（仅当尚无兜底时）。"""
    if load_baseline() is not None:
        return False
    ok = save_baseline(file_data)
    if ok:
        logger.info("seeded system baseline from settings.json")
    return ok


def apply_user_change(new_settings: dict[str, Any], user_id: str) -> bool:
    """用户在 UI 上改配置 → 只把**差异**存进他自己的配置区。

    系统兜底区不被触碰 —— 它是所有用户的只读默认值。用户改动即覆盖，
    语义与「用户只能用系统配置，改了就存自己这里」一致。

    存差异而非全量的理由见模块 docstring：全量复制会让系统兜底后续的更新
    再也传不到该用户。
    """
    baseline = load_baseline() or {}
    return save_overrides(compute_diff(baseline, new_settings), user_id)


def reset_to_system_defaults(user_id: str) -> bool:
    """丢弃该用户的全部覆盖，回到纯系统配置（「恢复默认」）。"""
    return clear_overrides(user_id)