"""测试全局夹具：把 SQLite 隔离到临时目录，绝不碰真实数据。

**为什么必需**：写双份之后，``PendingWriter`` / ``ExperienceWriter`` 在落盘时会
顺带调用 ``get_hub()``。而 ``get_hub()`` 会在**默认路径** ``records/pa_agent.db``
上懒创建数据库 —— 于是任何没显式重置 hub 的既有测试，都会把数据写进开发者的
真实数据目录。实测后果：DB 里 83 条分析记录 / 10 条经验条目，而磁盘只有 27 个
记录文件、0 个经验文件，纯粹是测试脏数据。

隔离必须在此处（模块导入期）完成：pytest 先导入 conftest.py，再收集测试模块，
晚一步就来不及了 —— 收集期只要有人 import 到 storage 模块就会建库。

本文件位于 ``tests/`` 根，对 ``tests/unit`` ``tests/property`` ``tests/integration``
``tests/e2e`` 一律生效。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 必须在任何 pa_agent.storage import 之前设置 —— get_hub() 读该环境变量。
_SESSION_DB_DIR = Path(tempfile.mkdtemp(prefix="pa_agent_test_db_"))
os.environ.setdefault(
    "PA_AGENT_DB_PATH", str(_SESSION_DB_DIR / "pa_agent_test.db")
)

# 部分测试会连真实 .env，模型凭证可能指向付费端点。测试里一律不发真请求。
os.environ.setdefault("PA_AGENT_TESTING", "1")