"""
P1-B 增量更新 + 幂等写入测试。

测试覆盖：
    1. TaskLock — Redis SETNX 锁获取/释放
    2. TaskLockContext — 上下文管理器
    3. process_document 幂等锁集成
    4. _build_search_index_async 增量更新逻辑
    5. 配置参数验证
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.task_lock import (
    TaskLock,
    TaskLockContext,
    acquire_task_lock,
    release_task_lock,
)


# ======================================================================
# TaskLock 数据类测试
# ======================================================================


class TestTaskLockDataclass:
    """TaskLock 数据类测试。"""

    def test_task_lock_creation(self):
        """TaskLock 可正常创建。"""
        lock = TaskLock(key="test", value="abc", acquired=True, ttl=30)
        assert lock.key == "test"
        assert lock.value == "abc"
        assert lock.acquired is True
        assert lock.ttl == 30

    def test_task_lock_not_acquired(self):
        """TaskLock acquired=False 表示未获取到锁。"""
        lock = TaskLock(key="test", value="abc", acquired=False, ttl=30)
        assert lock.acquired is False


# ======================================================================
# acquire_task_lock 测试
# ======================================================================


class TestAcquireTaskLock:
    """acquire_task_lock 函数测试。"""

    def test_acquire_task_lock_importable(self):
        """acquire_task_lock 可正常导入。"""
        assert callable(acquire_task_lock)

    @pytest.mark.asyncio
    async def test_acquire_returns_task_lock(self):
        """返回 TaskLock 实例。"""
        # Mock Redis 不可用时降级为 acquired=True
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            lock = await acquire_task_lock("test_task", "doc-123")
            assert isinstance(lock, TaskLock)
            assert lock.acquired is True  # 降级模式放行

    @pytest.mark.asyncio
    async def test_acquire_key_format(self):
        """锁 key 格式正确。"""
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            lock = await acquire_task_lock("process_document", "doc-456")
            assert "process_document" in lock.key
            assert "doc-456" in lock.key


# ======================================================================
# release_task_lock 测试
# ======================================================================


class TestReleaseTaskLock:
    """release_task_lock 函数测试。"""

    def test_release_task_lock_importable(self):
        """release_task_lock 可正常导入。"""
        assert callable(release_task_lock)

    @pytest.mark.asyncio
    async def test_release_not_acquired_returns_false(self):
        """未获取的锁释放返回 False。"""
        lock = TaskLock(key="test", value="abc", acquired=False, ttl=30)
        result = await release_task_lock(lock)
        assert result is False

    @pytest.mark.asyncio
    async def test_release_redis_unavailable(self):
        """Redis 不可用时释放返回 False 但不抛异常。"""
        lock = TaskLock(key="test", value="abc", acquired=True, ttl=30)
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            result = await release_task_lock(lock)
            assert result is False


# ======================================================================
# TaskLockContext 测试
# ======================================================================


class TestTaskLockContext:
    """TaskLockContext 上下文管理器测试。"""

    def test_task_lock_context_importable(self):
        """TaskLockContext 可正常导入。"""
        assert TaskLockContext is not None

    @pytest.mark.asyncio
    async def test_context_manager_enters_and_exits(self):
        """上下文管理器正常进入和退出。"""
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            async with TaskLockContext("test_task", "doc-123") as ctx:
                assert ctx is not None
                # 降级模式下 acquired=True
                assert ctx.acquired is True

    @pytest.mark.asyncio
    async def test_context_manager_acquired_property(self):
        """acquired 属性在进入后可用。"""
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            async with TaskLockContext("test_task", "doc-123") as ctx:
                assert isinstance(ctx.acquired, bool)

    @pytest.mark.asyncio
    async def test_context_manager_key_property(self):
        """key 属性在进入后可用。"""
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")
            async with TaskLockContext("process_document", "doc-456") as ctx:
                assert "process_document" in ctx.key
                assert "doc-456" in ctx.key

    @pytest.mark.asyncio
    async def test_context_manager_exit_releases_lock(self):
        """退出时自动释放锁。"""
        lock = TaskLock(key="test", value="abc", acquired=True, ttl=30)
        with patch("app.utils.task_lock.acquire_task_lock", new_callable=AsyncMock) as mock_acquire, \
             patch("app.utils.task_lock.release_task_lock", new_callable=AsyncMock) as mock_release:
            mock_acquire.return_value = lock
            mock_release.return_value = True
            async with TaskLockContext("test_task", "doc-123"):
                pass
            mock_release.assert_called_once_with(lock)


# ======================================================================
# process_document 幂等锁集成测试
# ======================================================================


class TestProcessDocumentIdempotency:
    """process_document 幂等锁集成验证。"""

    def test_document_tasks_imports_task_lock(self):
        """document_tasks.py 导入了 task_lock。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert "task_lock" in source
        assert "TaskLockContext" in source

    def test_process_document_has_lock_check(self):
        """process_document 包含锁检查逻辑。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert "task_skipped_locked" in source
        assert "lock_check_failed" in source

    def test_process_document_accepts_tenant_id(self):
        """process_document 接受 tenant_id 参数。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert "tenant_id: str | None = None" in source

    def test_process_document_returns_skipped_when_locked(self):
        """锁被持有时返回 skipped 状态。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert '"status": "skipped"' in source
        assert "跳过重复执行" in source


# ======================================================================
# 增量更新逻辑测试
# ======================================================================


class TestIncrementalUpdate:
    """增量更新逻辑验证。"""

    def test_index_tasks_has_hash_check(self):
        """index_tasks.py 包含 content_hash 增量检查。"""
        import tasks.index_tasks as mod

        source = open(mod.__file__).read()
        assert "content_hash" in source
        assert "skipped_unchanged" in source
        assert "content_hash 未变化" in source

    def test_index_tasks_stores_hash_in_opensearch(self):
        """索引写入时存储 content_hash。"""
        import tasks.index_tasks as mod

        source = open(mod.__file__).read()
        assert '"content_hash"' in source

    def test_index_tasks_hash_check_in_build_search(self):
        """_build_search_index_async 包含哈希检查。"""
        import tasks.index_tasks as mod

        source = open(mod.__file__).read()
        # 检查 _build_search_index_async 函数中有增量检查
        assert "current_hash" in source
        assert "indexed_hash" in source


# ======================================================================
# 配置参数验证
# ======================================================================


class TestTaskLockConfig:
    """任务锁配置参数验证。"""

    def test_config_has_task_lock_ttl(self):
        """Settings 包含 TASK_LOCK_TTL。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "TASK_LOCK_TTL")
        assert settings.TASK_LOCK_TTL > 0

    def test_config_has_task_lock_prefix(self):
        """Settings 包含 TASK_LOCK_REDIS_PREFIX。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "TASK_LOCK_REDIS_PREFIX")
        assert isinstance(settings.TASK_LOCK_REDIS_PREFIX, str)
        assert len(settings.TASK_LOCK_REDIS_PREFIX) > 0

    def test_task_lock_ttl_default_1800(self):
        """TASK_LOCK_TTL 默认 1800 秒（30 分钟）。"""
        from app.config import get_settings

        settings = get_settings()
        assert settings.TASK_LOCK_TTL == 1800

    def test_task_lock_prefix_format(self):
        """TASK_LOCK_REDIS_PREFIX 以冒号结尾。"""
        from app.config import get_settings

        settings = get_settings()
        assert settings.TASK_LOCK_REDIS_PREFIX.endswith(":")


# ======================================================================
# Lua 脚本验证
# ======================================================================


class TestReleaseLockScript:
    """释放锁 Lua 脚本验证。"""

    def test_release_script_exists(self):
        """释放锁 Lua 脚本存在。"""
        from app.utils.task_lock import _RELEASE_LOCK_SCRIPT

        assert _RELEASE_LOCK_SCRIPT is not None
        assert "redis.call" in _RELEASE_LOCK_SCRIPT
        assert "del" in _RELEASE_LOCK_SCRIPT

    def test_release_script_checks_value_match(self):
        """Lua 脚本检查 value 匹配后才删除。"""
        from app.utils.task_lock import _RELEASE_LOCK_SCRIPT

        assert "ARGV[1]" in _RELEASE_LOCK_SCRIPT
        assert "KEYS[1]" in _RELEASE_LOCK_SCRIPT

    def test_release_script_returns_count(self):
        """Lua 脚本返回删除计数。"""
        from app.utils.task_lock import _RELEASE_LOCK_SCRIPT

        assert "return" in _RELEASE_LOCK_SCRIPT


# ======================================================================
# 幂等性端到端场景验证
# ======================================================================


class TestIdempotencyScenario:
    """幂等性端到端场景验证。"""

    def test_task_lock_context_redis_unavailable_degrades_gracefully(self):
        """Redis 不可用时降级为无锁模式（放行）。"""
        # 这是核心安全设计 — Redis 故障不阻塞任务执行
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Connection refused")

            async def _test():
                async with TaskLockContext("test", "doc-1") as ctx:
                    return ctx.acquired

            result = asyncio.run(_test())
            assert result is True  # 降级模式放行

    def test_task_lock_key_contains_task_name_and_id(self):
        """锁 key 包含任务名和任务 ID。"""
        with patch("app.utils.task_lock._get_redis") as mock_redis:
            mock_redis.side_effect = Exception("Redis unavailable")

            async def _test():
                async with TaskLockContext("process_document", "doc-abc-123") as ctx:
                    return ctx.key

            key = asyncio.run(_test())
            assert "process_document" in key
            assert "doc-abc-123" in key

    def test_dedup_config_scope_kb_only(self):
        """DEDUP_SCOPE_KB_ONLY 配置存在。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "DEDUP_SCOPE_KB_ONLY")
        assert isinstance(settings.DEDUP_SCOPE_KB_ONLY, bool)


# ======================================================================
# process_document 锁覆盖整个执行期（Bug 修复回归测试）
# ======================================================================
# 原实现：_check_lock() 的 async with 中 return，锁随上下文退出立即释放，
# 30 分钟处理全程无锁、幂等失效。修复后：手动 __aenter__ 获取锁并持有到
# 任务结束，finally 中 __aexit__ 释放，锁覆盖任务整个执行期。


class _CeleryTaskStub:
    """celery_app.task 装饰器桩 — 保留原函数使其可直接调用。"""

    @staticmethod
    def task(*_args, **_kwargs):
        def _decorator(fn):
            return fn

        return _decorator


def _load_document_tasks_module():
    """以受控方式加载真实的 tasks/document_tasks.py（celery_app 打桩）。

    测试套件中 celery_app 可能被其他用例注入 MagicMock，导致任务函数
    被装饰器吞掉无法调用；此处用桩 celery_app exec 源码加载独立模块
    对象，保证拿到可调用的真实 process_document，且 monkeypatch 作用于
    该独立模块，不影响套件内其他用例。
    """
    import sys as _sys
    import types as _types
    from pathlib import Path

    stub_celery_app = _types.ModuleType("celery_app")
    stub_celery_app.celery_app = _CeleryTaskStub()

    path = Path(__file__).resolve().parent.parent / "tasks" / "document_tasks.py"
    source = path.read_text(encoding="utf-8")
    module = _types.ModuleType("ekb_test_document_tasks_lock")
    module.__file__ = str(path)

    saved = _sys.modules.get("celery_app")
    _sys.modules["celery_app"] = stub_celery_app
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        if saved is not None:
            _sys.modules["celery_app"] = saved
        else:
            _sys.modules.pop("celery_app", None)
    return module


class _FakeLock:
    """TaskLockContext 桩 — 记录进入/退出次数，模拟获取结果。"""

    instances: list = []
    acquire_result: bool = True
    acquire_error: Exception | None = None

    def __init__(self, task_name, task_id, ttl=None):
        self.task_name = task_name
        self.task_id = task_id
        self.ttl = ttl
        self.entered = 0
        self.exited = 0
        self._acquired = False
        _FakeLock.instances.append(self)

    @property
    def acquired(self):
        return self._acquired

    async def __aenter__(self):
        self.entered += 1
        if _FakeLock.acquire_error is not None:
            raise _FakeLock.acquire_error
        self._acquired = _FakeLock.acquire_result
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.exited += 1


class _FakeTaskSelf:
    """bind=True 任务的 self 桩 — 模拟 request/max_retries/retry。"""

    class _RetrySignal(Exception):
        """模拟 self.retry() 抛出的重试信号。"""

    def __init__(self, retries=0, max_retries=3):
        from types import SimpleNamespace

        self.request = SimpleNamespace(retries=retries, id="test-task-id")
        self.max_retries = max_retries

    def retry(self, exc=None):
        raise _FakeTaskSelf._RetrySignal(exc)


class TestProcessDocumentLockCoverage:
    """process_document 任务锁覆盖整个执行期（Bug 修复回归）。"""

    @pytest.fixture()
    def mod(self, monkeypatch):
        module = _load_document_tasks_module()
        progress_calls = []
        monkeypatch.setattr(
            module,
            "_update_parse_progress",
            lambda *a, **k: progress_calls.append((a, k)),
        )
        monkeypatch.setattr(
            module, "_should_use_multipart_pipeline", lambda doc_id: False
        )
        monkeypatch.setattr(module, "_send_to_dead_letter", lambda **k: None)
        module._test_progress_calls = progress_calls
        return module

    @pytest.fixture()
    def fake_lock(self, monkeypatch):
        _FakeLock.instances = []
        _FakeLock.acquire_result = True
        _FakeLock.acquire_error = None
        # process_document 在函数内 from app.utils.task_lock import TaskLockContext，
        # patch 该模块属性即可在调用时生效
        monkeypatch.setattr("app.utils.task_lock.TaskLockContext", _FakeLock)
        return _FakeLock

    def test_lock_held_skips_idempotently(self, mod, fake_lock):
        """锁被其他 worker 持有时幂等跳过，主体不执行、不误释放锁。"""
        fake_lock.acquire_result = False

        result = mod.process_document(_FakeTaskSelf(), "doc-locked-1")

        assert result["status"] == "skipped"
        assert "跳过重复执行" in result["message"]
        # 主体未执行（无 queued 进度）
        assert mod._test_progress_calls == []
        # 锁以正确的任务名/文档 ID 申请
        lock = fake_lock.instances[0]
        assert lock.task_name == "process_document"
        assert lock.task_id == "doc-locked-1"
        assert lock.entered == 1
        # 未持有锁 → 不调用释放
        assert lock.exited == 0

    def test_lock_covers_full_execution_and_releases(self, mod, fake_lock, monkeypatch):
        """任务执行期间锁保持持有，正常返回后在 finally 中释放。"""
        body_entered = {"v": False}

        async def _fake_parse(doc_id):
            body_entered["v"] = True
            # 关键断言：主体执行期间锁不得提前释放
            assert fake_lock.instances[0].exited == 0, "任务执行期间锁被提前释放"
            return {"status": "failed", "error": "parse boom"}

        monkeypatch.setattr(mod, "_parse_and_chunk_async", _fake_parse)

        result = mod.process_document(_FakeTaskSelf(), "doc-ok-1")

        assert result["status"] == "failed"
        assert body_entered["v"] is True
        # 主体确实执行（queued 进度已写入）
        assert any(
            k.get("stage") == "queued" for _a, k in mod._test_progress_calls
        )
        # 任务结束后锁被释放（finally）
        assert fake_lock.instances[0].exited == 1

    def test_lock_released_on_retryable_exception(self, mod, fake_lock, monkeypatch):
        """主体抛异常走重试路径时，锁同样被释放（不泄漏）。"""
        async def _boom(doc_id):
            raise RuntimeError("parse exploded")

        monkeypatch.setattr(mod, "_parse_and_chunk_async", _boom)

        with pytest.raises(_FakeTaskSelf._RetrySignal):
            mod.process_document(_FakeTaskSelf(), "doc-err-1")

        assert fake_lock.instances[0].exited == 1

    def test_lock_acquire_failure_degrades_to_lockless(self, mod, fake_lock, monkeypatch):
        """Redis 不可用（获取锁异常）时降级为无锁模式，任务继续执行。"""
        fake_lock.acquire_error = ConnectionError("redis down")

        async def _fake_parse(doc_id):
            return {"status": "failed", "error": "x"}

        monkeypatch.setattr(mod, "_parse_and_chunk_async", _fake_parse)

        result = mod.process_document(_FakeTaskSelf(), "doc-deg-1")

        assert result["status"] == "failed"
        # 降级模式主体照常执行
        assert mod._test_progress_calls
        # 未持有锁 → 不释放
        assert fake_lock.instances[0].exited == 0
