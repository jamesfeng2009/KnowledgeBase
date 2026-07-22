"""P1-A 优雅关闭测试 — ResourceManager + SSE 心跳 + Celery 信号。

测试覆盖：
1. ResourceManager 注册/清理/优先级
2. SSE 心跳和 CancelledError 处理
3. Celery worker_shutdown 信号注册
4. Docker/Dockerfile 配置
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ==================================================================
# 1. ResourceManager 测试
# ==================================================================


class TestResourceManager:
    """测试 ResourceManager 注册中心和清理逻辑。"""

    def test_singleton_exists(self):
        """全局 resource_manager 单例应存在。"""
        from app.utils.resource_manager import resource_manager

        assert resource_manager is not None

    def test_register_and_entries(self):
        """注册后应能在 entries 中找到。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        rm.register("test_resource", lambda: None, priority=50)
        assert len(rm.entries) == 1
        assert rm.entries[0].name == "test_resource"
        assert rm.entries[0].priority == 50
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_sync(self):
        """cleanup 应调用 sync close 方法。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        called = []

        def close_fn():
            called.append("closed")

        rm.register("sync_resource", close_fn, priority=50)
        await rm.cleanup(timeout=5)
        assert called == ["closed"]
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_async(self):
        """cleanup 应 await async close 方法。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        called = []

        async def close_fn():
            called.append("closed")

        rm.register("async_resource", close_fn, priority=50)
        await rm.cleanup(timeout=5)
        assert called == ["closed"]
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_priority_order(self):
        """cleanup 应按优先级降序清理。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        order = []

        rm.register("low", lambda: order.append("low"), priority=10)
        rm.register("high", lambda: order.append("high"), priority=100)
        rm.register("mid", lambda: order.append("mid"), priority=50)
        await rm.cleanup(timeout=5)
        assert order == ["high", "mid", "low"]
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_failure_isolated(self):
        """单个资源清理失败不影响其他资源。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        called = []

        def fail_fn():
            raise RuntimeError("boom")

        rm.register("fail", fail_fn, priority=100)
        rm.register("ok", lambda: called.append("ok"), priority=50)
        await rm.cleanup(timeout=5)
        assert called == ["ok"]
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self):
        """重复调用 cleanup 不会重复清理。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()
        count = [0]

        def close_fn():
            count[0] += 1

        rm.register("resource", close_fn)
        await rm.cleanup(timeout=5)
        await rm.cleanup(timeout=5)
        assert count[0] == 1
        rm.clear()

    @pytest.mark.asyncio
    async def test_cleanup_timeout(self):
        """超时的资源应被跳过。"""
        from app.utils.resource_manager import ResourceManager

        rm = ResourceManager()

        async def slow_fn():
            await asyncio.sleep(10)

        rm.register("slow", slow_fn)
        await rm.cleanup(timeout=0.1)
        # 不应卡住，超时后跳过
        rm.clear()


# ==================================================================
# 2. main.py lifespan shutdown 测试
# ==================================================================


class TestLifespanShutdown:
    """验证 main.py lifespan shutdown 调用 ResourceManager.cleanup()。"""

    def test_lifespan_calls_cleanup(self):
        """lifespan 函数源码应包含 resource_manager.cleanup 调用。"""
        import inspect

        from app.main import lifespan

        source = inspect.getsource(lifespan)
        assert "resource_manager" in source
        assert "cleanup" in source
        assert "SHUTDOWN_TIMEOUT" in source

    def test_lifespan_registers_pg_engine(self):
        """lifespan 应注册 pg_engine 资源。"""
        import inspect

        from app.main import lifespan

        source = inspect.getsource(lifespan)
        assert "pg_engine" in source
        assert "engine.dispose" in source

    def test_lifespan_has_shutdown_timeout(self):
        """lifespan 应有总超时保护。"""
        import inspect

        from app.main import lifespan

        source = inspect.getsource(lifespan)
        assert "wait_for" in source
        assert "TimeoutError" in source


# ==================================================================
# 3. SSE 心跳测试
# ==================================================================


class TestSSEHeartbeat:
    """验证 SSE 流支持心跳和 CancelledError 处理。"""

    def test_sse_stream_has_heartbeat(self):
        """_to_sse_stream 应包含心跳逻辑。"""
        import inspect

        from app.utils.sse import _to_sse_stream

        source = inspect.getsource(_to_sse_stream)
        assert "heartbeat" in source
        assert "30" in source  # 30 秒间隔

    def test_sse_stream_handles_cancelled(self):
        """_to_sse_stream 应处理 CancelledError。"""
        import inspect

        from app.utils.sse import _to_sse_stream

        source = inspect.getsource(_to_sse_stream)
        assert "CancelledError" in source

    def test_sse_imports_asyncio(self):
        """sse.py 应导入 asyncio。"""
        from app.utils import sse

        assert hasattr(sse, "asyncio") or "asyncio" in dir(sse)

    @pytest.mark.asyncio
    async def test_sse_heartbeat_on_timeout(self):
        """生成器长时间不产出时应发送心跳。"""
        from app.utils.sse import _to_sse_stream

        async def slow_generator():
            yield "first"
            await asyncio.sleep(35)  # 超过 30 秒心跳间隔
            yield "second"

        chunks = []
        async for chunk in _to_sse_stream(slow_generator()):
            chunks.append(chunk)
            if len(chunks) >= 3:  # first + heartbeat + second
                break

        assert any("heartbeat" in c for c in chunks), "应包含心跳"


# ==================================================================
# 4. Celery worker_shutdown 信号测试
# ==================================================================


class TestCeleryWorkerShutdown:
    """验证 Celery worker_shutdown 信号处理器已注册。"""

    def test_worker_shutdown_imported(self):
        """celery_app.py 应导入 worker_shutdown 信号。"""
        path = Path(__file__).resolve().parent.parent / "celery_app.py"
        content = path.read_text()
        assert "worker_shutdown" in content
        assert "from celery.signals import worker_shutdown" in content

    def test_worker_shutdown_handler_exists(self):
        """应存在 _on_worker_shutdown 处理函数。"""
        path = Path(__file__).resolve().parent.parent / "celery_app.py"
        content = path.read_text()
        assert "_on_worker_shutdown" in content
        assert "engine.dispose" in content


# ==================================================================
# 5. Docker 配置测试
# ==================================================================


class TestDockerGracefulShutdown:
    """验证 Docker 和 Dockerfile 配置了优雅关闭。"""

    def test_docker_compose_core_engine_stop_grace(self):
        """docker-compose.yml 中 core-engine 应配置 stop_grace_period。"""
        path = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
        content = path.read_text()
        assert "stop_grace_period" in content
        assert "30s" in content

    def test_docker_compose_celery_stop_grace(self):
        """docker-compose.yml 中 celery-worker 应配置 stop_grace_period。"""
        path = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
        content = path.read_text()
        assert "60s" in content

    def test_dockerfile_has_timeout_graceful_shutdown(self):
        """Dockerfile 应包含 --timeout-graceful-shutdown 参数。"""
        path = Path(__file__).resolve().parent.parent / "Dockerfile"
        content = path.read_text()
        assert "--timeout-graceful-shutdown" in content
        assert "30" in content
