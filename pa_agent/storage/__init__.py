"""持久化与会话存储层（内嵌 SQLite + 内存会话注册表）。

分层职责见 ``docs/SESSION_STORAGE_DESIGN.md``：

- **L1 全局级** ``global_config``   —— 凭证 / 模型 / 全局开关，无 user_id
- **L2 用户级** ``analysis_records`` / ``experience_entries`` / ``trade_records``
  / ``user_prefs`` / ``chat_turns`` —— 累积型知识与资产，带 user_id，多会话共享
- **L3 会话级** ``sessions``        —— 缓存级快照，带 session_id，TTL 过期即清
- **热层**   ``ephemeral.SessionRegistry`` —— 内存态（SSE 队列 / 追问对象 / 游标镜像）

本包**禁止依赖 Qt**，且不得被 ``web/`` 反向依赖 —— 见 AGENTS.md 分层约束。
"""
from __future__ import annotations

__all__ = ["db", "schema", "ephemeral", "repos", "ids"]
