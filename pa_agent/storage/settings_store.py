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


def merge_overrides(existing: dict, patch: dict) -> dict:
    """Merge *patch* onto an existing override layer (sparse stays sparse).

    与 :func:`save_overrides` 的**整体替换**语义并存且不冲突：

    - ``PATCH /api/settings`` 的语义是「这份 body 就是我想要的覆盖层」→ 替换，
      合并会让用户删掉的字段复活（见 :func:`save_overrides`）
    - 而内部写入方（connector 同步、provider fallback、设置页保存）只知道
      **自己改了哪几个键**，必须在其余部分保留既有覆盖 → 合并

    合并结果仍会由调用方按基准重新稀疏化，避免「改回默认值」的键永久挡住
    系统兜底的后续更新。
    """
    return deep_merge(existing or {}, patch or {})


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

    **读失败绝不等于「首次运行」**：若 DB 已初始化却读不出来（损坏 / 权限 / 只读），
    此时若继续播种就会把一份可能已损坏的文件**升格成系统兜底**，污染所有用户且
    永不重播种（seed_from_file 只在兜底为空时动作）。故此时明确返回 None，
    交给调用方走纯文件路径 —— 文件本身不会被写坏。
    """
    hub = get_hub()
    if hub.read_failed:
        logger.error(
            "settings baseline 读取失败（%s）；本次不使用 DB，也不播种。"
            "请修复数据库或检查挂载权限。",
            hub.read_error,
        )
        return None

    baseline = load_baseline()
    if baseline is None and file_fallback is not None:
        # 首次运行：把既有文件升格为系统兜底，之后用户继承它
        baseline = file_fallback
        save_baseline(baseline)
    if baseline is None:
        return None
    return deep_merge(baseline, _apply_llm_source(baseline, load_overrides(user_id)))


#: 用户关闭「自带模型」时，其 provider 覆盖里这些键一律作废。
#: ``use_custom`` **不在其中** —— 它是开关本身，必须由用户决定。
_LLM_CONTROL_KEY = "use_custom"


def _apply_llm_source(baseline: dict, overrides: dict) -> dict:
    """按「系统默认 / 自带模型」开关裁剪 provider 覆盖。

    ``provider.use_custom`` 为假（默认，含出厂兜底自己设的假）时，用户对
    provider 的覆盖**整段作废** —— 他们的 LLM 配置完全来自系统出厂默认。

    为什么不能靠「稀疏覆盖」自然表达：用户覆盖过某个字段后，系统对该字段的
    后续更新就再也到不了他这里（遮蔽），而界面上完全看不出自己遮住了什么。
    显式开关让「跟随系统」成为一个可见、可一键撤销的状态。

    其它 section（general / prompt / validation / feishu …）不受影响 —— 它们是
    偏好而非凭证，稀疏覆盖的语义本来就正确。
    """
    if not overrides:
        return overrides

    provider_ov = overrides.get("provider")
    if not isinstance(provider_ov, dict):
        return overrides

    use_custom = provider_ov.get(_LLM_CONTROL_KEY)
    if use_custom is None:
        # 用户从未表达过偏好 → 跟随系统。注意不能把「baseline 设了 false」
        # 当作用户的显式选择：出厂配置不是用户的意愿。
        return {k: v for k, v in overrides.items() if k != "provider"}

    if use_custom:
        return overrides

    # 关：**只**保留开关本身，其余 provider 覆盖全部作废 ——
    # 留着它们就会盖住 baseline，让「跟随系统」名存实亡。
    rest = {k: v for k, v in overrides.items() if k != "provider"}
    rest["provider"] = {_LLM_CONTROL_KEY: False}
    return rest


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
    # **读失败必须拒绝写入**（现存 bug，评审 B4-32）：`load_baseline() or {}`
    # 在读失败时会拿到 {}，于是 compute_diff({}, 全量配置) 会把整份 8 个 section
    # 全部写进 user_prefs。此后系统兜底的任何更新都再也传不到该用户，且用户会
    # 看到「配置被莫名重置」。必须与 resolve() 同样先看 hub.read_failed。
    hub = get_hub()
    if hub.read_failed:
        logger.error(
            "拒绝写入用户覆盖：DB 读取失败（%s）。本次改动只落文件，重启后可能丢失。",
            hub.read_error,
        )
        return False

    baseline = load_baseline() or {}
    return save_overrides(compute_diff(baseline, new_settings), user_id)


def reset_to_system_defaults(user_id: str) -> bool:
    """丢弃该用户的全部覆盖，回到纯系统配置（「恢复默认」）。"""
    return clear_overrides(user_id)

def promote_to_baseline(current: dict[str, Any]) -> bool:
    """把 *current* 提升为系统出厂默认，并同步播种源 ``settings.json``。

    与 :func:`apply_user_change` 的区别是**层级**：后者写用户覆盖区（只影响本人），
    本函数写 ``baseline``（所有用户继承的默认值）。

    两步都要做，缺一不可：
    - 只写 baseline → 空库重播种时拿不到，仍会是旧的出厂配置
    - 只写 settings.json → 当前进程读的是 DB，用户看到的仍是旧基线

    同时**清掉本用户覆盖区**：已成出厂默认的字段留在覆盖区里会永久遮蔽后续的
    系统更新（用户再也不会收到默认值变更）。函数接受任意 current，但调用方
    约定传「已合并覆盖后的生效配置」。
    """
    hub = get_hub()
    if hub.read_failed:
        logger.error("拒绝提升出厂默认：DB 读取失败（%s）", hub.read_error)
        return False

    if not save_baseline(current):
        logger.error("promote_to_baseline: 写 baseline 失败")
        return False

    # 播种源：让全新空库能长出同一份配置。失败不阻断 —— baseline 已落库，
    # 当前进程可用；只是「清库后拿不回这份配置」，属可接受降级。
    try:
        from pathlib import Path

        from pa_agent.config.paths import SETTINGS_JSON_PATH

        Path(SETTINGS_JSON_PATH).write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "promote_to_baseline: 播种源写入失败（baseline 已落库，清库后无法"
            "自动恢复这份配置）：%s", exc,
        )

    from pa_agent.storage.users import default_user_id

    clear_overrides(default_user_id())
    return True
