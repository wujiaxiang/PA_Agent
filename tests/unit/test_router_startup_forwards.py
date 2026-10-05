"""router 级 startup 事件**确实会被转发** —— 锁定事实，防止再被误判。

## 为什么有这条测试

2026-10-05 有过一次反向结论：声称「FastAPI 0.142 不再转发 router 级
startup 事件」，据此认定 \`routes_chat\` 的清理 task 从未启动，并据此写下约 60 行
兜底代码。那条结论是**错的**，且已写进提交信息与 SESSION_CHANGES。

本测试把**真实行为**钉住：\`include_router\` 会转发 \`on_startup\`，router 级
startup 处理器在 TestClient 下会真的执行。将来 FastAPI 真改了行为，这条会红，
届时可以名正言顺地改代码 —— 而不是凭一次误判就加兜底。

⚠️ 写反方向断言（\`assert not ran\`）是错的：那会把一个已被证伪的前提固化成
回归测试，正是本项目反复踩的「用测试锁死错误结论」。
"""
from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient


def test_include_router_forwards_on_startup():
    """include_router 转发 router 的 on_startup。"""
    src_ran: list[int] = []
    sub = APIRouter()

    @sub.on_event("startup")
    async def _mark() -> None:
        src_ran.append(1)

    app = FastAPI()

    @app.on_event("startup")
    async def _also_mark() -> None:
        src_ran.append(2)

    app.include_router(sub, prefix="/sub")
    with TestClient(app):
        pass

    assert 1 in src_ran, "router 级 startup 未被转发"
    assert 2 in src_ran


def test_app_lifespan_does_not_mask_router_startup():
    """自定义 lifespan 不会屏蔽 router 级 startup。"""
    ran: list[int] = []
    sub = APIRouter()

    @sub.on_event("startup")
    async def _mark() -> None:
        ran.append(1)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(sub)
    with TestClient(app):
        pass
    assert ran == [1]
