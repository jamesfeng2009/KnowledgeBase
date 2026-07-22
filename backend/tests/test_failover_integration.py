"""P2-A 集成测试 — 故障转移全链路 + 幂等性验证。

测试覆盖：
    1. 端到端故障转移：熔断器开启 → ProviderPool 切换 → 后续请求路由到备用
    2. 健康检查 + API 联动：Provider 故障 → HealthCheckService 标记 → API 返回异常
    3. Celery 任务幂等性：Redis SETNX 锁防止并行执行
    4. API 端点幂等性：连续 GET 返回一致结果
    5. 日志输出验证：关键操作产生结构化日志
    6. 配置驱动的故障转移链
    7. 全部熔断降级场景
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.provider_pool import ProviderPool, clear_pool_cache
from app.services.health_check import HealthCheckService, ProviderHealth
from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    _breakers,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


# ======================================================================
# 辅助函数
# ======================================================================


def _make_failing_provider(name: str, fail_count: int = 5):
    """创建一个会连续失败 N 次后成功的 Mock Provider。"""
    provider = MagicMock()
    provider._name = name
    call_count = {"embed": 0, "chat": 0}

    async def _embed(texts):
        call_count["embed"] += 1
        if call_count["embed"] <= fail_count:
            raise ConnectionError(f"{name} connection error")
        return [[0.1, 0.2]]

    async def _chat(messages, tools=None, stream=False, **kwargs):
        call_count["chat"] += 1
        if call_count["chat"] <= fail_count:
            raise ConnectionError(f"{name} chat error")
        yield f"success-from-{name}"

    provider.embed = _embed
    provider.chat = _chat
    provider.rerank = AsyncMock(return_value=[{"index": 0, "score": 0.9}])
    provider.search = AsyncMock(return_value=[{"id": "1", "score": 0.8}])
    provider.health_check = AsyncMock(return_value=True)
    provider.default_model = f"model-{name}"
    return provider


def _make_healthy_provider(name: str):
    """创建一个始终健康的 Mock Provider。"""
    provider = MagicMock()
    provider._name = name

    async def _embed(texts):
        return [[0.9, 0.8]]

    async def _chat(messages, tools=None, stream=False, **kwargs):
        yield f"chunk-from-{name}-1"
        yield f"chunk-from-{name}-2"

    provider.embed = _embed
    provider.chat = _chat
    provider.rerank = AsyncMock(return_value=[{"index": 0, "score": 0.95}])
    provider.search = AsyncMock(return_value=[{"id": "2", "score": 0.99}])
    provider.health_check = AsyncMock(return_value=True)
    provider.default_model = f"model-{name}"
    return provider


# ======================================================================
# 端到端故障转移集成测试
# ======================================================================


class TestEndToEndFailoverIntegration:
    """端到端故障转移：熔断器 → ProviderPool → 路由切换。"""

    def setup_method(self):
        reset_all_circuit_breakers()
        clear_pool_cache()

    async def test_embed_failover_full_flow(self):
        """Embed 端到端故障转移：主 Provider 熔断 → 自动切换到备用。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        # 第一次调用 — p1 熔断，自动切换到 p2
        result1 = await pool.embed(["test"])
        assert result1 == [[0.9, 0.8]]
        assert pool.current_provider_name == "b2"

        # 第二次调用 — 仍然使用 p2（p1 仍熔断）
        result2 = await pool.embed(["test2"])
        assert result2 == [[0.9, 0.8]]

    async def test_chat_failover_full_flow(self):
        """LLM chat 端到端故障转移：主 Provider 熔断 → 流式切换到备用。"""
        p1 = MagicMock()

        async def _fail_chat(messages, tools=None, stream=False, **kwargs):
            raise CircuitBreakerOpenError("b1", CircuitState.OPEN)
            yield  # noqa

        p1.chat = _fail_chat
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        chunks = []
        async for chunk in pool.chat([], stream=True):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert "p2" in chunks[0]
        assert pool.current_provider_name == "b2"

    async def test_three_provider_chain_full_failover(self):
        """三 Provider 链式故障转移：p1 熔断 → p2 熔断 → p3 成功。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = MagicMock()
        p2.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b2", CircuitState.OPEN)
        )
        p3 = _make_healthy_provider("p3")
        pool = ProviderPool([p1, p2, p3], ["b1", "b2", "b3"], "embedder")

        result = await pool.embed(["test"])
        assert result == [[0.9, 0.8]]
        assert pool.current_provider_name == "b3"

    async def test_all_providers_circuit_open_raises_error(self):
        """所有 Provider 熔断 → 抛出 CircuitBreakerOpenError。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = MagicMock()
        p2.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b2", CircuitState.OPEN)
        )
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with pytest.raises(CircuitBreakerOpenError):
            await pool.embed(["test"])

    async def test_failover_then_recovery(self):
        """故障转移后主 Provider 恢复 — 下次调用回到主 Provider。"""
        # 注意：这个测试验证的是当熔断器恢复后，
        # ProviderPool 的行为取决于熔断器状态，不是自动回切。
        # ProviderPool 总是从第一个开始尝试，如果其熔断器关闭就用它。
        p1 = _make_healthy_provider("p1")
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        # 正常情况 — 使用 p1
        await pool.embed(["test1"])
        assert pool.current_provider_name == "b1"

        # 手动模拟 p1 熔断
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )

        # p1 熔断 — 切换到 p2
        await pool.embed(["test2"])
        assert pool.current_provider_name == "b2"

        # p1 恢复 — 但 ProviderPool 从第一个开始尝试
        p1.embed = AsyncMock(return_value=[[0.5, 0.6]])

        # 下次调用 — p1 不再熔断，回到 p1
        result = await pool.embed(["test3"])
        assert result == [[0.5, 0.6]]
        assert pool.current_provider_name == "b1"


# ======================================================================
# 健康检查 + API 联动测试
# ======================================================================


class TestHealthCheckAPIIntegration:
    """HealthCheckService + /health/providers API 联动测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_api_returns_health_check_results(self):
        """API 端点返回 HealthCheckService 的检查结果。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        cached = {
            "embedder_openai": ProviderHealth(
                name="embedder_openai",
                type="embedder",
                healthy=True,
                latency_ms=45.0,
                circuit_state="closed",
                last_check="2026-01-01T00:00:00Z",
            ),
            "vllm": ProviderHealth(
                name="vllm",
                type="llm",
                healthy=False,
                error="connection refused",
                circuit_state="open",
                last_check="2026-01-01T00:00:00Z",
            ),
        }

        with patch(
            "app.services.health_check.HealthCheckService.load_from_redis",
            new_callable=AsyncMock,
            return_value=cached,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/providers")
                assert resp.status_code == 200

                data = resp.json()["data"]
                assert data["total"] == 2
                assert data["healthy_count"] == 1
                assert data["source"] == "cache"

                providers = data["providers"]
                assert providers["embedder_openai"]["healthy"] is True
                assert providers["vllm"]["healthy"] is False

    async def test_api_fresh_check_on_cache_miss(self):
        """缓存未命中时 API 执行同步检查。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        fresh = {
            "anthropic": ProviderHealth(
                name="anthropic",
                type="llm",
                healthy=True,
                latency_ms=120.0,
                circuit_state="closed",
                last_check="2026-01-01T00:00:00Z",
            )
        }

        with patch(
            "app.services.health_check.HealthCheckService.load_from_redis",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
            return_value=fresh,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/providers")
                data = resp.json()["data"]
                assert data["source"] == "fresh"
                assert data["total"] == 1

    async def test_health_check_service_with_pool_providers(self):
        """HealthCheckService 能检查 ProviderPool 中的 Provider。"""
        service = HealthCheckService()

        # Mock registry 返回一个 mock provider entry
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2]])

        mock_entry = MagicMock()
        mock_entry.name = "test_embedder"
        mock_entry.type = "embedder"
        mock_entry.breaker_name = "embedder_openai"
        mock_entry.factory = MagicMock(return_value=mock_provider)

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[mock_entry],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ):
            results = await service.check_all()
            assert "embedder_openai" in results
            assert results["embedder_openai"].healthy is True


# ======================================================================
# 幂等性验证测试
# ======================================================================


class TestIdempotencyVerification:
    """所有接口的幂等性验证。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_health_providers_api_idempotent(self):
        """/health/providers 连续 GET 返回一致结果。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        cached = {
            "vllm": ProviderHealth(
                name="vllm",
                type="llm",
                healthy=True,
                circuit_state="closed",
                last_check="2026-01-01T00:00:00Z",
            )
        }

        with patch(
            "app.services.health_check.HealthCheckService.load_from_redis",
            new_callable=AsyncMock,
            return_value=cached,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp1 = await client.get("/health/providers")
                resp2 = await client.get("/health/providers")
                resp3 = await client.get("/health/providers")

                assert resp1.json() == resp2.json() == resp3.json()

    async def test_circuit_breakers_api_idempotent(self):
        """/health/circuit-breakers 连续 GET 返回一致结果。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp1 = await client.get("/health/circuit-breakers")
            resp2 = await client.get("/health/circuit-breakers")
            assert resp1.json() == resp2.json()

    async def test_health_api_idempotent(self):
        """/health 连续 GET 返回一致结果。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp1 = await client.get("/health")
            resp2 = await client.get("/health")
            assert resp1.json() == resp2.json()

    async def test_celery_task_idempotent_with_lock(self):
        """Celery 健康检查任务锁防止并行执行。"""
        from app.utils.task_lock import TaskLock

        # 第一次 — 锁获取成功
        lock1 = TaskLock(key="test", value="abc", acquired=True, ttl=60)

        with patch(
            "app.utils.task_lock.acquire_task_lock",
            new_callable=AsyncMock,
            return_value=lock1,
        ), patch(
            "app.utils.task_lock.release_task_lock",
            new_callable=AsyncMock,
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
            return_value={},
        ):
            from tasks.health_tasks import _run_health_check

            result1 = await _run_health_check()
            assert result1["status"] == "completed"

        # 第二次 — 锁被持有（另一个实例正在运行）
        lock2 = TaskLock(key="test", value="xyz", acquired=False, ttl=60)

        with patch(
            "app.utils.task_lock.acquire_task_lock",
            new_callable=AsyncMock,
            return_value=lock2,
        ), patch(
            "app.utils.task_lock.release_task_lock",
            new_callable=AsyncMock,
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
        ) as mock_check:
            result2 = await _run_health_check()
            assert result2["status"] == "skipped"
            mock_check.assert_not_called()

    async def test_provider_pool_embed_idempotent_on_success(self):
        """ProviderPool embed 成功后重复调用结果一致。"""
        p1 = _make_healthy_provider("p1")
        pool = ProviderPool([p1], ["b1"], "embedder")

        r1 = await pool.embed(["test"])
        r2 = await pool.embed(["test"])
        assert r1 == r2 == [[0.9, 0.8]]


# ======================================================================
# 日志输出验证测试
# ======================================================================


class TestLoggingVerification:
    """关键操作的结构化日志验证。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_health_check_logs_start_and_complete(self):
        """HealthCheckService.check_all 记录 start 和 complete 日志。"""
        service = HealthCheckService()

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ), patch(
            "app.services.health_check.log"
        ) as mock_log:
            await service.check_all()

            # 验证 start 和 complete 日志
            log_calls = [str(c) for c in mock_log.info.call_args_list]
            assert any("health_check.start" in c for c in log_calls)
            assert any("health_check.complete" in c for c in log_calls)

    async def test_provider_pool_logs_failover(self):
        """ProviderPool 故障转移时记录 failover 日志。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with patch("app.llm.provider_pool.log") as mock_log:
            await pool.embed(["test"])

            log_calls = [str(c) for c in mock_log.info.call_args_list]
            assert any("provider_pool.failover" in c for c in log_calls)

    async def test_provider_pool_logs_circuit_open(self):
        """ProviderPool 遇到熔断器开启时记录 circuit_open 日志。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with patch("app.llm.provider_pool.log") as mock_log:
            await pool.embed(["test"])

            log_calls = [str(c) for c in mock_log.warning.call_args_list]
            assert any("provider_pool.circuit_open" in c for c in log_calls)

    async def test_provider_pool_logs_all_circuits_open(self):
        """所有 Provider 熔断时记录 all_circuits_open 错误日志。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = MagicMock()
        p2.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b2", CircuitState.OPEN)
        )
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with patch("app.llm.provider_pool.log") as mock_log:
            with pytest.raises(CircuitBreakerOpenError):
                await pool.embed(["test"])

            log_calls = [str(c) for c in mock_log.error.call_args_list]
            assert any("provider_pool.all_circuits_open" in c for c in log_calls)

    async def test_health_check_logs_provider_result(self):
        """HealthCheckService 对每个 Provider 记录 result 日志。"""
        service = HealthCheckService()

        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1]])

        mock_entry = MagicMock()
        mock_entry.name = "test"
        mock_entry.type = "embedder"
        mock_entry.breaker_name = "embedder_openai"
        mock_entry.factory = MagicMock(return_value=mock_provider)

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[mock_entry],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ), patch(
            "app.services.health_check.log"
        ) as mock_log:
            await service.check_all()

            log_calls = [str(c) for c in mock_log.info.call_args_list]
            assert any("health_check.provider_result" in c for c in log_calls)


# ======================================================================
# 配置驱动测试
# ======================================================================


class TestConfigDrivenFailover:
    """配置驱动的故障转移链测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()
        clear_pool_cache()

    def test_empty_failover_chain_creates_single_provider_pool(self):
        """空故障转移链 → 单 Provider 池。"""
        from app.llm.provider_pool import _build_pool

        default_provider = _make_healthy_provider("default")
        pool = _build_pool(
            pool_type="embedder",
            failover_chain="",
            default_factory=lambda: default_provider,
            default_breaker_name="default_breaker",
        )
        assert pool.provider_count == 1

    def test_failover_chain_parsed_correctly(self):
        """故障转移链正确解析为 Provider 列表。"""
        from app.llm.provider_pool import _build_pool

        # Mock registry entries
        mock_entries = []
        for name, ptype, breaker in [
            ("openai", "embedder", "embedder_openai"),
            ("tei", "embedder", "embedder_tei"),
        ]:
            entry = MagicMock()
            entry.name = name
            entry.type = ptype
            entry.breaker_name = breaker
            entry.factory = MagicMock(return_value=_make_healthy_provider(name))
            mock_entries.append(entry)

        with patch(
            "app.llm.provider_pool.get_all_provider_entries",
            return_value=mock_entries,
        ), patch(
            "app.llm.provider_pool._get_or_create_provider",
            side_effect=lambda name, ptype: _make_healthy_provider(name),
        ):
            pool = _build_pool(
                pool_type="embedder",
                failover_chain="openai,tei",
                default_factory=lambda: _make_healthy_provider("default"),
                default_breaker_name="default",
            )

        assert pool.provider_count == 2

    def test_config_has_all_failover_chain_settings(self):
        """Settings 包含所有故障转移链配置项。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "LLM_FAILOVER_CHAIN")
        assert hasattr(s, "EMBEDDER_FAILOVER_CHAIN")
        assert hasattr(s, "RERANKER_FAILOVER_CHAIN")
        assert hasattr(s, "VECTOR_STORE_FAILOVER_CHAIN")
        # 默认为空字符串（不启用故障转移）
        assert isinstance(s.LLM_FAILOVER_CHAIN, str)
        assert isinstance(s.EMBEDDER_FAILOVER_CHAIN, str)

    def test_config_has_health_check_settings(self):
        """Settings 包含健康检查配置项。"""
        from app.config import get_settings

        s = get_settings()
        assert s.HEALTH_CHECK_INTERVAL == 30
        assert s.HEALTH_CHECK_CACHE_TTL == 60


# ======================================================================
# 降级场景测试
# ======================================================================


class TestDegradationScenarios:
    """各种降级场景测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_redis_unavailable_health_check_degrades(self):
        """Redis 不可用时 HealthCheckService 降级（不抛异常）。"""
        service = HealthCheckService()

        with patch(
            "app.services.health_check.aioredis.from_url",
            side_effect=ConnectionError("Redis unavailable"),
        ):
            # _save_to_redis 不抛异常
            await service._save_to_redis({})
            # load_from_redis 返回 None
            result = await service.load_from_redis()
            assert result is None

    async def test_redis_unavailable_api_falls_back_to_fresh(self):
        """Redis 不可用时 API 降级为同步检查。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        fresh = {
            "vllm": ProviderHealth(
                name="vllm",
                type="llm",
                healthy=True,
                circuit_state="closed",
                last_check="2026-01-01T00:00:00Z",
            )
        }

        with patch(
            "app.services.health_check.HealthCheckService.load_from_redis",
            new_callable=AsyncMock,
            return_value=None,  # Redis 不可用 → None
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
            return_value=fresh,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/providers")
                data = resp.json()["data"]
                assert data["source"] == "fresh"

    async def test_task_lock_redis_unavailable_degrades_gracefully(self):
        """Redis 不可用时任务锁降级为放行（不阻塞执行）。"""
        from app.utils.task_lock import TaskLockContext

        with patch(
            "app.utils.task_lock._get_redis",
            side_effect=ConnectionError("Redis unavailable"),
        ):
            async with TaskLockContext("health_check", "all") as ctx:
                assert ctx.acquired is True  # 降级模式放行

    async def test_provider_pool_with_single_provider_no_failover(self):
        """单 Provider 池不执行故障转移。"""
        p1 = _make_healthy_provider("p1")
        pool = ProviderPool([p1], ["b1"], "embedder")

        result = await pool.embed(["test"])
        assert result == [[0.9, 0.8]]
        assert pool.current_provider_name == "b1"

    async def test_provider_pool_non_circuit_error_does_not_failover(self):
        """非熔断异常不触发故障转移。"""
        p1 = MagicMock()
        p1.embed = AsyncMock(side_effect=RuntimeError("unexpected error"))
        p2 = _make_healthy_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with pytest.raises(RuntimeError, match="unexpected error"):
            await pool.embed(["test"])

        assert pool.current_provider_name == "b1"  # 未切换
