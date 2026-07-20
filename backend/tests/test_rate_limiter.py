"""API 限流中间件测试 — TokenBucket + RateLimiter + RedisRateLimiter + FastAPI 集成。

覆盖范围：
    - TokenBucket：令牌消费、补充、耗尽
    - RateLimiter：多客户端隔离、burst 突发
    - RedisRateLimiter：Redis 原子化限流、降级模式、Lua 脚本
    - FastAPI 集成：429 响应、健康检查豁免、限流关闭
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# TokenBucket 测试
# ======================================================================


class TestTokenBucket:
    """令牌桶限流器测试。"""

    def test_initial_burst_available(self) -> None:
        """初始时桶满，可消费 burst 个令牌。"""
        from app.middleware import TokenBucket

        bucket = TokenBucket(capacity=5, refill_per_second=1.0)
        for _ in range(5):
            assert bucket.try_consume()
        # 桶空了
        assert not bucket.try_consume()

    def test_refill_over_time(self) -> None:
        """令牌随时间补充。"""
        import time
        from app.middleware import TokenBucket

        bucket = TokenBucket(capacity=2, refill_per_second=100.0)  # 快速补充
        assert bucket.try_consume()
        assert bucket.try_consume()
        assert not bucket.try_consume()

        # 等待补充
        time.sleep(0.05)  # 50ms → 补充约 5 个令牌
        assert bucket.try_consume()

    def test_capacity_cap(self) -> None:
        """令牌数不超过容量上限。"""
        import time
        from app.middleware import TokenBucket

        bucket = TokenBucket(capacity=3, refill_per_second=1000.0)
        time.sleep(0.1)  # 等待补充
        # 最多消费 3 个（容量上限）
        assert bucket.try_consume()
        assert bucket.try_consume()
        assert bucket.try_consume()
        assert not bucket.try_consume()

    def test_single_consume(self) -> None:
        """默认每次消费 1 个令牌。"""
        from app.middleware import TokenBucket

        bucket = TokenBucket(capacity=1, refill_per_second=0.0)
        assert bucket.try_consume()
        assert not bucket.try_consume()


# ======================================================================
# RateLimiter 测试
# ======================================================================


class TestRateLimiter:
    """按客户端限流管理器测试。"""

    def test_multiple_clients_isolated(self) -> None:
        """不同客户端的桶相互隔离。"""
        from app.middleware import RateLimiter

        limiter = RateLimiter(per_minute=60, burst=2)
        # 客户端 A 消耗 2 个
        assert limiter.allow("client_A")
        assert limiter.allow("client_A")
        assert not limiter.allow("client_A")

        # 客户端 B 仍有配额
        assert limiter.allow("client_B")
        assert limiter.allow("client_B")
        assert not limiter.allow("client_B")

    def test_burst_then_deny(self) -> None:
        """突发 burst 后拒绝后续请求。"""
        from app.middleware import RateLimiter

        limiter = RateLimiter(per_minute=1, burst=3)
        for _ in range(3):
            assert limiter.allow("user_1")
        assert not limiter.allow("user_1")

    def test_clear_resets_all(self) -> None:
        """clear 清空所有桶。"""
        from app.middleware import RateLimiter

        limiter = RateLimiter(per_minute=60, burst=1)
        assert limiter.allow("user_1")
        assert not limiter.allow("user_1")

        limiter.clear()
        assert limiter.allow("user_1")


# ======================================================================
# _get_client_id 测试
# ======================================================================


class TestGetClientId:
    """客户端标识提取测试。"""

    def test_api_key_priority(self) -> None:
        """X-API-Key 优先作为客户端标识。"""
        from app.middleware import _get_client_id

        mock_request = MagicMock()
        mock_request.headers = {"x-api-key": "test-key-12345"}
        mock_request.client = MagicMock()
        mock_request.client.host = "127.0.0.1"

        client_id = _get_client_id(mock_request)
        assert client_id == "key:test-key-12345"

    def test_authorization_header(self) -> None:
        """Authorization 头作为回退。"""
        from app.middleware import _get_client_id

        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer token123"}
        mock_request.client = MagicMock()
        mock_request.client.host = "127.0.0.1"

        client_id = _get_client_id(mock_request)
        assert client_id.startswith("key:Bearer")

    def test_ip_fallback(self) -> None:
        """无 API Key 时回退到 IP。"""
        from app.middleware import _get_client_id

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.client = MagicMock()
        mock_request.client.host = "192.168.1.1"

        client_id = _get_client_id(mock_request)
        assert client_id == "ip:192.168.1.1"

    def test_forwarded_for(self) -> None:
        """X-Forwarded-For 头优先于 client.host。"""
        from app.middleware import _get_client_id

        mock_request = MagicMock()
        mock_request.headers = {"x-forwarded-for": "10.0.0.1, 10.0.0.2"}
        mock_request.client = MagicMock()
        mock_request.client.host = "127.0.0.1"

        client_id = _get_client_id(mock_request)
        assert client_id == "ip:10.0.0.1"

    def test_api_key_truncated(self) -> None:
        """过长的 API Key 被截断。"""
        from app.middleware import _get_client_id

        long_key = "a" * 100
        mock_request = MagicMock()
        mock_request.headers = {"x-api-key": long_key}
        mock_request.client = None

        client_id = _get_client_id(mock_request)
        # 截断到 32 字符 + "key:" 前缀
        assert len(client_id) <= 36


# ======================================================================
# FastAPI 集成测试
# ======================================================================


def _create_test_app(rate_limit_enabled: bool = True, per_minute: int = 60, burst: int = 2):
    """创建测试用 FastAPI 应用。"""
    import app.middleware as mw
    from app.middleware import setup_middleware

    # 重置全局限流器，防止跨测试污染
    mw._rate_limiter = None

    settings = MagicMock()
    settings.CORS_ORIGINS = ["*"]
    settings.RATE_LIMIT_ENABLED = rate_limit_enabled
    settings.RATE_LIMIT_PER_MINUTE = per_minute
    settings.RATE_LIMIT_BURST = burst

    app = FastAPI()
    with patch("app.middleware.get_settings", return_value=settings):
        setup_middleware(app)

    @app.get("/api/test")
    async def test_endpoint():
        return {"code": 0, "message": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


class TestRateLimitIntegration:
    """限流中间件 FastAPI 集成测试。"""

    def test_health_exempt_from_rate_limit(self) -> None:
        """健康检查路径不受限流影响。"""
        app = _create_test_app(burst=1)
        client = TestClient(app)

        # 连续访问 /health 不会被限流
        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_rate_limit_returns_429(self) -> None:
        """超过 burst 后返回 429。"""
        app = _create_test_app(burst=2)
        client = TestClient(app)

        # 前两次请求通过
        resp1 = client.get("/api/test")
        assert resp1.status_code == 200
        resp2 = client.get("/api/test")
        assert resp2.status_code == 200

        # 第三次被限流
        resp3 = client.get("/api/test")
        assert resp3.status_code == 429
        assert resp3.headers.get("Retry-After") == "60"

    def test_rate_limit_disabled(self) -> None:
        """限流关闭时不限制请求。"""
        app = _create_test_app(rate_limit_enabled=False, burst=1)
        client = TestClient(app)

        for _ in range(10):
            resp = client.get("/api/test")
            assert resp.status_code == 200

    def test_different_clients_not_affected(self) -> None:
        """不同 API Key 的客户端互不影响。"""
        app = _create_test_app(burst=1)
        client = TestClient(app)

        # 客户端 A 用完配额
        resp_a1 = client.get("/api/test", headers={"x-api-key": "key_A"})
        assert resp_a1.status_code == 200
        resp_a2 = client.get("/api/test", headers={"x-api-key": "key_A"})
        assert resp_a2.status_code == 429

        # 客户端 B 仍有配额
        resp_b = client.get("/api/test", headers={"x-api-key": "key_B"})
        assert resp_b.status_code == 200


# ======================================================================
# RedisRateLimiter 测试 — 分布式共享计数（P0 预备工作）
# ======================================================================


class TestRedisRateLimiter:
    """Redis-backed 限流器测试 — 多实例共享计数 + 降级模式。"""

    def test_redis_available_uses_lua_script(self) -> None:
        """Redis 可用时通过 Lua 脚本原子化取令牌。"""
        from app.middleware import RedisRateLimiter

        limiter = RedisRateLimiter(
            per_minute=60, burst=2, redis_url="redis://localhost:6379/0"
        )

        # mock Redis 连接
        mock_redis = AsyncMock()
        mock_redis.script_load = AsyncMock(return_value="lua_sha_123")
        mock_redis.evalsha = AsyncMock(return_value=1)  # 允许

        async def mock_ensure():
            limiter._redis = mock_redis
            limiter._lua_sha = "lua_sha_123"
            limiter._degraded = False
            return mock_redis

        limiter._ensure_redis = mock_ensure

        import asyncio

        result = asyncio.run(limiter.allow("client_A"))
        assert result is True
        # evalsha 应被调用
        mock_redis.evalsha.assert_called_once()

    def test_redis_unavailable_degrades_to_memory(self) -> None:
        """Redis 不可用时降级为内存令牌桶，限流功能不丢。"""
        from app.middleware import RedisRateLimiter

        limiter = RedisRateLimiter(
            per_minute=60, burst=2, redis_url="redis://invalid:6379/0"
        )

        # mock _ensure_redis 返回 None（Redis 不可用）
        async def mock_ensure():
            limiter._degraded = True
            return None

        limiter._ensure_redis = mock_ensure

        import asyncio

        # 降级模式下使用内存令牌桶，burst=2
        assert asyncio.run(limiter.allow("client_A")) is True
        assert asyncio.run(limiter.allow("client_A")) is True
        assert asyncio.run(limiter.allow("client_A")) is False  # 桶空

    def test_redis_runtime_error_falls_back_to_memory(self) -> None:
        """Redis 运行时故障时临时降级为内存模式。"""
        from app.middleware import RedisRateLimiter

        limiter = RedisRateLimiter(
            per_minute=60, burst=1, redis_url="redis://localhost:6379/0"
        )

        # mock Redis 初始化成功但 evalsha 抛异常
        mock_redis = AsyncMock()
        mock_redis.script_load = AsyncMock(return_value="lua_sha_123")
        mock_redis.evalsha = AsyncMock(side_effect=Exception("Redis 连接断开"))

        async def mock_ensure():
            limiter._redis = mock_redis
            limiter._lua_sha = "lua_sha_123"
            limiter._degraded = False
            return mock_redis

        limiter._ensure_redis = mock_ensure

        import asyncio

        # 第一次调用 Redis 故障 → 降级到内存，内存 burst=1 允许
        result1 = asyncio.run(limiter.allow("client_A"))
        assert result1 is True
        # 第二次调用已降级，内存桶空了
        result2 = asyncio.run(limiter.allow("client_A"))
        assert result2 is False

    def test_redis_degraded_flag_persists_after_error(self) -> None:
        """Redis 故障后 degraded 标志持续，后续调用直接走内存。"""
        from app.middleware import RedisRateLimiter

        limiter = RedisRateLimiter(
            per_minute=60, burst=5, redis_url="redis://localhost:6379/0"
        )

        mock_redis = AsyncMock()
        mock_redis.script_load = AsyncMock(return_value="sha")
        mock_redis.evalsha = AsyncMock(side_effect=Exception("连接断开"))

        async def mock_ensure():
            limiter._redis = mock_redis
            limiter._lua_sha = "sha"
            return mock_redis

        limiter._ensure_redis = mock_ensure

        import asyncio

        asyncio.run(limiter.allow("client_A"))
        # 故障后应标记降级
        assert limiter._degraded is True

    def test_clear_resets_fallback_buckets(self) -> None:
        """clear 清空内存降级桶。"""
        from app.middleware import RedisRateLimiter

        limiter = RedisRateLimiter(
            per_minute=60, burst=1, redis_url="redis://localhost:6379/0"
        )

        async def mock_ensure():
            limiter._degraded = True
            return None

        limiter._ensure_redis = mock_ensure

        import asyncio

        asyncio.run(limiter.allow("client_A"))  # 消费令牌
        assert not asyncio.run(limiter.allow("client_A"))  # 桶空
        limiter.clear()  # 清空
        assert asyncio.run(limiter.allow("client_A"))  # 重新可用

    def test_lua_script_content_is_valid(self) -> None:
        """Lua 脚本包含必要的令牌桶逻辑。"""
        from app.middleware import _RATE_LIMIT_LUA

        # 验证 Lua 脚本包含关键操作
        assert "HMGET" in _RATE_LIMIT_LUA
        assert "HMSET" in _RATE_LIMIT_LUA
        assert "EXPIRE" in _RATE_LIMIT_LUA
        assert "math.min" in _RATE_LIMIT_LUA
        assert "capacity" in _RATE_LIMIT_LUA
        assert "refill_rate" in _RATE_LIMIT_LUA
