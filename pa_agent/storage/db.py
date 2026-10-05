"""SQLite 连接管理：WAL + 线程局部 + 故障降级。

三条硬约束（均来自实测踩坑，不是防御性编程）：

1. **连接不可跨线程**。项目大量使用 ``asyncio.to_thread``（见 ``routes_data.py``
   全篇），而 ``sqlite3`` 连接的 ``check_same_thread`` 默认拒绝跨线程使用。
   → 用 ``threading.local`` 每线程一个连接。
2. **DB 故障绝不能拖垮行情**。K 线是核心功能，数据库只是索引层。
   → 任何异常降级为「本进程不使用 DB」，仅记 warning，由调用方回退文件路径。
3. **写入必须原子**。``settings.py`` 的 ``path.write_text`` 会留半截 JSON；
   SQLite 的事务能免费解决这个问题，故记录/配置写入一律走事务。

DB 文件默认落在 ``records/pa_agent.db``：该目录已在 ``docker-compose.yml`` 中
bind mount，无需新增部署步骤；且 ``rglob("*.json")`` 不会匹配 ``.db``，与既有
文件扫描逻辑无冲突。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pa_agent.storage.schema import SCHEMA_VERSION, all_statements

logger = logging.getLogger("pa_agent.storage")

T = TypeVar("T")

# 默认 DB 位置：records/ 已在 docker-compose bind mount 内（docker-compose.yml:13）
DEFAULT_DB_PATH: Path = Path(__file__).resolve().parent.parent.parent / "records" / "pa_agent.db"

# 单用户默认值。所有 L2/L3 表的 user_id 默认值；接入多用户后由请求头/鉴权填充。
DEFAULT_USER_ID = "default"

# busy_timeout：并发写（后台调度器 + 分析主流程 + 多 tag 轮询）撞锁时等待而非立刻抛
# sqlite3.OperationalError。5s 足够覆盖一次短事务。
_BUSY_TIMEOUT_MS = 5000


def db_path() -> Path:
    """Resolve the DB path, honouring the ``PA_AGENT_DB_PATH`` override.

    测试用临时文件、只读部署想改位置时都走这个环境变量，避免在代码里硬编码。
    """
    import os

    override = os.environ.get("PA_AGENT_DB_PATH", "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


class _ConnectionHub:
    """线程局部连接池 + 全局降级开关。

    ``_disabled`` 一旦置位就不再尝试打开连接 —— 磁盘满 / 文件损坏这类故障下，
    每个请求都重试一次 IO 只会把事件循环拖垮。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._disabled = False
        self._disabled_reason = ""
        self._all_conns: list[sqlite3.Connection] = []

    # ── 连接获取 ──────────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection | None:
        """Return this thread's connection, or ``None`` when DB is unavailable.

        ``None`` 是契约的一部分，不是异常 —— 调用方据此回退文件存储。
        """
        if self._disabled:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._path),
                timeout=_BUSY_TIMEOUT_MS / 1000.0,
                # 每线程独立连接（约束 1）
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            self._disable(f"connect failed: {exc}")
            return None

        conn.row_factory = sqlite3.Row
        try:
            # WAL：读写不互相阻塞。SSE 轮询在读、分析在写，全局模式下二者会互相阻塞。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        except sqlite3.Error as exc:
            # PRAGMA 失败（如文件系统不支持 WAL）不该让整条链路停摆，降级到默认模式。
            logger.warning("SQLite PRAGMA setup failed, continuing with defaults: %s", exc)

        self._local.conn = conn
        with self._lock:
            self._all_conns.append(conn)
        return conn

    def _disable(self, reason: str) -> None:
        """Latch the DB off for this process. 不可逆 —— 需重启才恢复。"""
        if not self._disabled:
            logger.warning(
                "SQLite disabled, falling back to file storage for this process: %s", reason
            )
        self._disabled = True
        self._disabled_reason = reason

    @property
    def disabled(self) -> bool:
        return self._disabled

    # ── 迁移 ──────────────────────────────────────────────────────────────────
    def migrate(self) -> bool:
        """Apply DDL idempotently.  Returns True on success."""
        conn = self.connect()
        if conn is None:
            return False
        try:
            with conn:
                for stmt in all_statements():
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
        except sqlite3.Error as exc:
            self._disable(f"migrate failed: {exc}")
            return False
        return True

    def schema_version(self) -> int:
        conn = self.connect()
        if conn is None:
            return 0
        try:
            row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            return int(row["value"]) if row else 0
        except sqlite3.Error:
            return 0

    # ── 事务辅助 ──────────────────────────────────────────────────────────────
    def execute(self, sql: str, params: tuple = ()) -> bool:
        """单条写。失败仅记 warning 并降级，绝不冒泡进业务流。"""
        conn = self.connect()
        if conn is None:
            return False
        try:
            with conn:
                conn.execute(sql, params)
            return True
        except sqlite3.Error as exc:
            logger.warning("SQLite execute failed (sql=%.60s): %s", sql, exc)
            return False

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """读。失败返回空列表 —— 与「查无结果」同义，调用方据此回退文件。"""
        conn = self.connect()
        if conn is None:
            return []
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning("SQLite query failed (sql=%.60s): %s", sql, exc)
            return []

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def run_in_tx(self, fn: Callable[[sqlite3.Connection], T]) -> T | None:
        """Run *fn* inside a transaction.

        Returns ``None`` on any SQLite error.  Used by the importer, where a
        partial import is worse than none: the caller re-runs it (idempotent).
        """
        conn = self.connect()
        if conn is None:
            return None
        try:
            with conn:  # BEGIN / COMMIT / ROLLBACK
                return fn(conn)
        except sqlite3.Error as exc:
            logger.warning("SQLite transaction failed: %s", exc)
            return None

    # ── 诊断 ──────────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """Snapshot for /api/health —— 让存储层故障可观测，而不是等它静默。"""
        return {
            "enabled": not self._disabled,
            "path": str(self._path),
            "schema_version": self.schema_version(),
            "disabled_reason": self._disabled_reason,
            "connections": len(self._all_conns),
            "db_size_bytes": self._path.stat().st_size if self._path.exists() else 0,
        }

    def close_all(self) -> None:
        """Close every connection handed out. 仅测试与优雅停机使用。"""
        with self._lock:
            conns, self._all_conns = self._all_conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass


# ── 进程级单例 ────────────────────────────────────────────────────────────────
_hub: _ConnectionHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> _ConnectionHub:
    """Process-wide hub.  Deliberately a singleton: SQLite is a file, not a service."""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = _ConnectionHub(db_path())
                _hub.migrate()
    return _hub


def reset_hub_for_tests(path: Path | None = None) -> _ConnectionHub:
    """Rebuild the hub against *path*. 测试专用，不在生产路径调用。"""
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.close_all()
        _hub = _ConnectionHub(path or db_path())
        _hub.migrate()
    return _hub


def now() -> float:
    """Single time source, so tests can monkeypatch one place."""
    return time.time()
