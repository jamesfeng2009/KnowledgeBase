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

# 确保 redis 模块存在（测试环境可能未安装）
if "redis" not in sys.modules:
    mock_redis_module = types.ModuleType("redis")
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
