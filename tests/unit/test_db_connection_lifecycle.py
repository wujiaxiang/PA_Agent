"""SQLite 连接必须随线程结束而回收。

**回归守卫**：连接曾只 append 不回收，而本系统每次分析起一个 followup
线程取一次数据源 —— 每次新建一条连接，线程结束并不会关它。经验库的写入
路径就在这条链上，于是分析次数越多、泄漏越多，且**没有任何报错**：
连接照常工作，只是进程句柄单调增长。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pa_agent.storage.db import _ConnectionHub


@pytest.fixture()
def hub(tmp_path):
    h = _ConnectionHub(Path(tmp_path) / "leak.db")
    h.migrate()
    yield h
    h._disable("test")
    h.close_all()


def _touch_once(hub):
    hub.connect()


def test_short_lived_threads_do_not_leak_connections(hub):
    """跑 30 个短命线程，连接数必须回到 1（当前线程自己那条）。"""
    for _ in range(30):
        t = threading.Thread(target=_touch_once, args=(hub,))
        t.start()
        t.join()

    assert hub.connection_count <= 2, (
        f"泄漏：30 个已结束线程留下了 {hub.connection_count} 条连接"
    )


def test_reaping_does_not_close_a_live_threads_connection(hub):
    """**回归守卫**：回收只针对**已结束**的线程。

    若改用「超时即关」（比 enumerate 简单得多），每轮分析跑几十秒的线程会
    在中途被关掉连接 —— 症状是随机的 `cannot operate on a closed database`，
    极难定位。所以判据必须是线程存活，不是时间。
    """
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def long_running():
        try:
            started.set()
            release.wait(5)
            hub.connect()          # 仍在跑，必须拿到可用连接
        except BaseException as exc:   # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=long_running)
    t.start()
    started.wait(2)

    # 主线程新建连接 → 触发回收
    hub.connect()
    for _ in range(5):
        hub.connect()

    release.set()
    t.join(5)
    assert not errors, f"存活线程的连接被误关：{errors}"


def test_close_all_clears_the_registry(hub):
    hub.connect()
    assert hub.connection_count >= 1
    hub.close_all()
    assert hub.connection_count == 0
