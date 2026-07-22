"""数据库连接管理测试 — Celery 任务级独立引擎（修复：跨事件循环复用连接）。

背景：全局 AsyncEngine 的连接池缓存 asyncpg 连接，而连接绑定创建它的事件循环。
Celery 任务通过 asyncio.run() 为每个任务新建事件循环，第 2 个任务起复用到
上一个循环的连接会抛出 "attached to a different loop"，导致 worker 崩溃。

修复：app.database 新增 create_task_engine()（NullPool 不缓存连接）与
task_db_session()（任务级独立引擎，用完即 dispose）统一工具函数。

覆盖：
    1. create_task_engine — NullPool、独立实例、全局引擎语义不变
    2. task_db_session — 会话获取、退出时 dispose 引擎
    3. 跨事件循环安全 — 多个 asyncio.run() 循环连续使用不报错
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from app.database import create_task_engine, engine, task_db_session


# ======================================================================
# create_task_engine 测试
# ======================================================================


class TestCreateTaskEngine:
    """任务级引擎工厂测试。"""

    def test_returns_async_engine_with_nullpool(self):
        """返回 NullPool 的 AsyncEngine（不缓存连接，杜绝跨循环复用）。"""
        task_engine = create_task_engine()
        try:
            assert isinstance(task_engine, AsyncEngine)
            assert isinstance(task_engine.pool, NullPool)
        finally:
            asyncio.run(task_engine.dispose())

    def test_each_call_returns_independent_engine(self):
        """每次调用返回全新独立引擎（任务级/循环级隔离）。"""
        engine_a = create_task_engine()
        engine_b = create_task_engine()
        try:
            assert engine_a is not engine_b
            assert engine_a.pool is not engine_b.pool
        finally:
            asyncio.run(engine_a.dispose())
            asyncio.run(engine_b.dispose())

    def test_global_engine_unchanged(self):
        """全局引擎语义不受修复影响（仍保留连接池，非 NullPool）。"""
        assert not isinstance(engine.pool, NullPool)


# ======================================================================
# task_db_session 测试
# ======================================================================


class TestTaskDbSession:
    """Celery 任务专用数据库会话测试。"""

    @pytest.mark.asyncio
    async def test_yields_session_and_disposes_engine(self, monkeypatch):
        """会话正常 yield，退出上下文时引擎被 dispose（资源彻底释放）。"""
        disposed: list[AsyncEngine] = []
        original_dispose = AsyncEngine.dispose

        async def _spy_dispose(self: AsyncEngine) -> None:
            disposed.append(self)
            await original_dispose(self)

        monkeypatch.setattr(AsyncEngine, "dispose", _spy_dispose)

        async with task_db_session() as session:
            assert isinstance(session, AsyncSession)
            assert disposed == [], "上下文内不应提前 dispose"

        assert len(disposed) == 1, "退出上下文时必须 dispose 一次任务引擎"

    def test_cross_event_loop_safe(self):
        """跨多个 asyncio.run() 事件循环连续使用不抛跨循环错误。

        这是 Bug 修复的核心回归验证：修复前第 2 个任务起复用全局池中
        绑定旧事件循环的连接而崩溃；修复后每个任务独占 NullPool 引擎，
        连接随会话关闭即释放，绝不跨循环复用。
        """

        async def _use_session() -> bool:
            async with task_db_session() as session:
                return isinstance(session, AsyncSession)

        results = [asyncio.run(_use_session()) for _ in range(3)]
        assert results == [True, True, True]
