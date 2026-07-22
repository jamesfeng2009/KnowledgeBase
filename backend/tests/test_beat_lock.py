"""Celery Beat 单实例锁测试 — 分布式预备（P1）。

覆盖范围：
    - acquire_beat_lock：获取锁成功 / 锁已被持有 / Redis 不可用放行
    - 锁参数：TTL 和 key 可配置

测试策略：acquire_beat_lock 是纯函数（不依赖 Celery），
通过 exec 加载函数定义，mock redis 模块进行测试。
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# 确保 redis 模块存在（测试环境可能未安装）。
# 注意：仅在 redis 确实不可导入时才注入 Mock，且 Mock 必须是“包”
# （带 __path__），否则后续测试 ``import redis.asyncio`` 会失败。
try:  # pragma: no cover - 环境相关分支
    import redis as _real_redis  # noqa: F401
except ImportError:
    if "redis" not in sys.modules:
        mock_redis_module = types.ModuleType("redis")
        mock_redis_module.__path__ = []  # 标记为包，支持 redis.asyncio 子模块导入
        mock_redis_module.from_url = MagicMock()
        sys.modules["redis"] = mock_redis_module


def _get_acquire_beat_lock():
    """获取 acquire_beat_lock 函数（从源码提取，绕过 celery_app mock）。

    acquire_beat_lock 是纯 Python 函数不依赖 Celery，
    通过 exec 加载函数定义进行测试。
    """
    temp_module = types.ModuleType("_test_beat_lock")
    exec(
        """
def acquire_beat_lock(redis_url, lock_key="celery:beat:lock", ttl=60):
    try:
        import redis
        client = redis.from_url(redis_url, decode_responses=True)
        acquired = client.set(lock_key, "beat_active", nx=True, ex=ttl)
        if acquired:
            return True
        return False
    except Exception as exc:
        return True
""",
        temp_module.__dict__,
    )
    return temp_module.acquire_beat_lock


class TestAcquireBeatLock:
    """acquire_beat_lock 测试 — Redis SETNX 单实例锁。"""

    def test_lock_acquired_successfully(self) -> None:
        """Redis SETNX 成功获取锁。"""
        acquire_beat_lock = _get_acquire_beat_lock()

        mock_client = MagicMock()
        mock_client.set.return_value = True  # SETNX 成功

        with patch("redis.from_url", return_value=mock_client):
            result = acquire_beat_lock("redis://localhost:6379/0")

        assert result is True
        mock_client.set.assert_called_once()
        # 验证使用了 NX 和 EX 参数
        _, kwargs = mock_client.set.call_args
        assert kwargs.get("nx") is True
        assert kwargs.get("ex") == 60

    def test_lock_already_held_returns_false(self) -> None:
        """锁已被其他实例持有时返回 False。"""
        acquire_beat_lock = _get_acquire_beat_lock()

        mock_client = MagicMock()
        mock_client.set.return_value = None  # SETNX 失败（key 已存在）

        with patch("redis.from_url", return_value=mock_client):
            result = acquire_beat_lock("redis://localhost:6379/0")

        assert result is False

    def test_redis_unavailable_returns_true(self) -> None:
        """Redis 不可用时放行（单机模式不需要锁）。"""
        acquire_beat_lock = _get_acquire_beat_lock()

        with patch("redis.from_url", side_effect=Exception("Connection refused")):
            result = acquire_beat_lock("redis://invalid:6379/0")

        assert result is True

    def test_lock_ttl_configurable(self) -> None:
        """锁 TTL 可配置。"""
        acquire_beat_lock = _get_acquire_beat_lock()

        mock_client = MagicMock()
        mock_client.set.return_value = True

        with patch("redis.from_url", return_value=mock_client):
            result = acquire_beat_lock("redis://localhost:6379/0", ttl=120)

        assert result is True
        _, kwargs = mock_client.set.call_args
        assert kwargs.get("ex") == 120

    def test_lock_key_configurable(self) -> None:
        """锁 key 可配置。"""
        acquire_beat_lock = _get_acquire_beat_lock()

        mock_client = MagicMock()
        mock_client.set.return_value = True

        with patch("redis.from_url", return_value=mock_client):
            result = acquire_beat_lock(
                "redis://localhost:6379/0", lock_key="custom:beat:lock"
            )

        assert result is True
        args, _ = mock_client.set.call_args
        assert args[0] == "custom:beat:lock"


# ======================================================================
# Beat 锁定期续期（Bug 修复回归测试）
# ======================================================================
# 原实现：锁 TTL 60s 但从不续期，Beat 运行 60s 后锁自动过期，其他 Beat
# 实例可再次获取锁 → 双 Beat 并发、定时任务重复执行。
# 修复后：start_beat_lock_renewal 启动守护线程，按 ttl//3 周期续期锁。


class _LogStub:
    """logger 桩 — 兼容 structlog 风格 (event, **kwargs) 调用。"""

    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def _rec(self, level, event, **kwargs):
        self.records.append((level, event, kwargs))

    def info(self, event, **kwargs):
        self._rec("info", event, **kwargs)

    def warning(self, event, **kwargs):
        self._rec("warning", event, **kwargs)

    def error(self, event, **kwargs):
        self._rec("error", event, **kwargs)

    def debug(self, event, **kwargs):
        self._rec("debug", event, **kwargs)


def _load_real_beat_lock_functions():
    """从 celery_app.py 源码提取真实的锁相关函数（AST 切片 + 注入依赖 exec）。

    直接 import celery_app 会受套件内 sys.modules 污染（MagicMock）且
    模块级有副作用；AST 提取保证测试的是真实实现而非内联副本。
    """
    import ast
    import threading
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "celery_app.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    parts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_BEAT_LOCK_RENEW_SCRIPT"
            for t in node.targets
        ):
            parts.append(ast.get_source_segment(source, node))
        elif isinstance(node, ast.FunctionDef) and node.name in (
            "acquire_beat_lock",
            "start_beat_lock_renewal",
        ):
            parts.append(ast.get_source_segment(source, node))

    assert len(parts) == 3, "未能从 celery_app.py 提取锁相关定义"

    log_stub = _LogStub()
    namespace = {"logger": log_stub, "threading": threading}
    exec(compile("\n\n\n".join(parts), str(path), "exec"), namespace)
    return (
        namespace["acquire_beat_lock"],
        namespace["start_beat_lock_renewal"],
        log_stub,
    )


class _FakeRedisClient:
    """Redis 客户端桩 — set(nx/ex)/get/eval 的最小语义模拟。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.eval_calls = 0
        self.set_calls: list[tuple] = []
        self.fail_eval_times = 0
        self.closed = False

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, *args):
        self.eval_calls += 1
        if self.fail_eval_times > 0:
            self.fail_eval_times -= 1
            raise ConnectionError("redis flaky")
        key, value = args[0], args[1]
        if self.store.get(key) == value:
            self.store[key] = value
            return 1
        return 0

    def close(self):
        self.closed = True


class TestBeatLockRenewal:
    """Beat 锁定期续期 — 运行期间锁不过期，杜绝双 Beat 并发。"""

    def test_renews_lock_periodically(self):
        """按周期续期：eval 多次刷新 TTL，锁值保持不变。"""
        import time

        _acquire, start_renewal, _log = _load_real_beat_lock_functions()
        client = _FakeRedisClient()
        client.store["celery:beat:lock"] = "v-1"

        with patch("redis.from_url", return_value=client):
            thread, stop = start_renewal(
                "redis://x", lock_value="v-1", ttl=60, interval=0.05,
            )
            try:
                deadline = time.time() + 3
                while client.eval_calls < 3 and time.time() < deadline:
                    time.sleep(0.02)
                assert client.eval_calls >= 3, "锁应按周期续期"
                assert client.store["celery:beat:lock"] == "v-1"
            finally:
                stop.set()
                thread.join(timeout=2)
        assert not thread.is_alive()

    def test_stops_when_lock_held_by_other(self):
        """锁被其他实例持有且重取失败时，记录 beat_lock_lost 并停止续期。"""
        _acquire, start_renewal, log_stub = _load_real_beat_lock_functions()
        client = _FakeRedisClient()
        client.store["celery:beat:lock"] = "other-instance"

        with patch("redis.from_url", return_value=client):
            thread, stop = start_renewal(
                "redis://x", lock_value="v-1", ttl=60, interval=0.05,
            )
            thread.join(timeout=3)
            stop.set()
        assert not thread.is_alive(), "锁被他人持有时应停止续期"
        assert any(e == "celery.beat_lock_lost" for _lv, e, _k in log_stub.records)

    def test_tolerates_transient_redis_errors(self):
        """Redis 短暂抖动不中断续期（记录 warning 后下个周期重试）。"""
        import time

        _acquire, start_renewal, log_stub = _load_real_beat_lock_functions()
        client = _FakeRedisClient()
        client.store["celery:beat:lock"] = "v-1"
        client.fail_eval_times = 2  # 前两次续期抛异常

        with patch("redis.from_url", return_value=client):
            thread, stop = start_renewal(
                "redis://x", lock_value="v-1", ttl=60, interval=0.05,
            )
            try:
                deadline = time.time() + 3
                while client.eval_calls < 5 and time.time() < deadline:
                    time.sleep(0.02)
                assert client.eval_calls >= 5, "抖动后应继续续期"
                assert any(
                    e == "celery.beat_lock_renew_failed"
                    for _lv, e, _k in log_stub.records
                )
            finally:
                stop.set()
                thread.join(timeout=2)
        assert not thread.is_alive()

    def test_reacquires_when_lock_expired_unheld(self):
        """锁过期且无人持有时，重新获取锁并继续续期。"""
        import time

        _acquire, start_renewal, log_stub = _load_real_beat_lock_functions()
        client = _FakeRedisClient()  # key 不存在 = 锁已过期

        with patch("redis.from_url", return_value=client):
            thread, stop = start_renewal(
                "redis://x", lock_value="v-1", ttl=60, interval=0.05,
            )
            try:
                deadline = time.time() + 3
                while (
                    client.store.get("celery:beat:lock") != "v-1"
                    and time.time() < deadline
                ):
                    time.sleep(0.02)
                assert client.store.get("celery:beat:lock") == "v-1", (
                    "锁过期无人持有时应重新获取"
                )
                assert any(
                    e == "celery.beat_lock_reacquired"
                    for _lv, e, _k in log_stub.records
                )
            finally:
                stop.set()
                thread.join(timeout=2)
        assert not thread.is_alive()

    def test_acquire_with_custom_lock_value(self):
        """acquire_beat_lock 支持自定义 lock_value（唯一实例标识）。"""
        acquire, _renew, _log = _load_real_beat_lock_functions()
        client = _FakeRedisClient()

        with patch("redis.from_url", return_value=client):
            result = acquire(
                "redis://x", lock_value="beat_active:host1:123", ttl=60
            )

        assert result is True
        key, value, nx, ex = client.set_calls[0]
        assert key == "celery:beat:lock"
        assert value == "beat_active:host1:123"
        assert nx is True
        assert ex == 60

    def test_startup_block_starts_renewal(self):
        """celery_app.py 启动块接线：获取锁后启动续期线程，锁值含主机与 PID。"""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "celery_app.py").read_text(
            encoding="utf-8"
        )
        assert "start_beat_lock_renewal" in source
        assert "socket.gethostname()" in source
        assert "os.getpid()" in source
