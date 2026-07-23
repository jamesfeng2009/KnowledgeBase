"""P2-A Task 2: HealthCheckService + Celery Beat + API 端点测试。

测试覆盖：
    1. ProviderHealth 数据模型
    2. HealthCheckService.check_all() — 全量健康检查
    3. _do_lightweight_check() — 各类型 Provider 轻量级检查
    4. _check_provider() — 实例化失败 / 检查失败场景
    5. Redis 缓存读写（_save_to_redis / load_from_redis）
    6. Celery 任务幂等性（TaskLockContext）
    7. API 端点行为（缓存命中 / 缓存未命中）
    8. Provider 注册表
    9. 配置参数验证
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（仅当 celery 未安装时才 mock celery_app）
try:
    import celery  # noqa: F401
except ImportError:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app
else:
    # celery 已安装 — 清除其他测试文件可能注入的 mock，使用真实 celery_app
    sys.modules.pop("celery_app", None)

from app.llm.registry import ProviderMeta, get_all_provider_entries
from app.services.health_check import (
    REDIS_KEY,
    HealthCheckService,
    ProviderHealth,
)
from app.utils.circuit_breaker import CircuitState, _breakers, reset_all_circuit_breakers


# ======================================================================
# ProviderHealth 模型测试
# ======================================================================


class TestProviderHealthModel:
    """ProviderHealth Pydantic 模型测试。"""

    def test_create_healthy_provider(self):
        """健康 Provider 正常创建。"""
        health = ProviderHealth(
            name="embedder_openai",
            type="embedder",
            healthy=True,
            latency_ms=42.5,
            circuit_state="closed",
            last_check="2026-01-01T00:00:00Z",
        )
        assert health.name == "embedder_openai"
        assert health.healthy is True
        assert health.latency_ms == 42.5
        assert health.error is None

    def test_create_unhealthy_provider(self):
        """不健康 Provider 正常创建。"""
        health = ProviderHealth(
            name="reranker_cohere",
            type="reranker",
            healthy=False,
            error="connection refused",
            circuit_state="open",
            last_check="2026-01-01T00:00:00Z",
        )
        assert health.healthy is False
        assert health.error == "connection refused"

    def test_model_dump_roundtrip(self):
        """model_dump 序列化/反序列化往返一致。"""
        original = ProviderHealth(
            name="vllm",
            type="llm",
            healthy=True,
            latency_ms=100.0,
            circuit_state="closed",
            last_check="2026-01-01T00:00:00Z",
        )
        dumped = original.model_dump()
        restored = ProviderHealth(**dumped)
        assert restored.name == original.name
        assert restored.healthy == original.healthy
        assert restored.latency_ms == original.latency_ms

    def test_optional_fields_default_none(self):
        """可选字段默认 None。"""
        health = ProviderHealth(
            name="test",
            type="embedder",
            healthy=True,
            circuit_state="closed",
            last_check="2026-01-01T00:00:00Z",
        )
        assert health.latency_ms is None
        assert health.error is None


# ======================================================================
# Provider 注册表测试
# ======================================================================


class TestProviderRegistry:
    """Provider 注册表测试。"""

    def test_get_all_provider_entries_returns_list(self):
        """get_all_provider_entries 返回列表。"""
        entries = get_all_provider_entries()
        assert isinstance(entries, list)

    def test_get_all_provider_entries_has_meta(self):
        """每个 entry 都是 ProviderMeta 实例。"""
        entries = get_all_provider_entries()
        for entry in entries:
            assert isinstance(entry, ProviderMeta)
            assert entry.name
            assert entry.type in ("embedder", "reranker", "vectorstore", "llm")
            assert entry.breaker_name
            assert callable(entry.factory)

    def test_registry_includes_openai_embedder(self):
        """注册表包含 OpenAI Embedder（测试环境已设 dummy key）。"""
        entries = get_all_provider_entries()
        embedder_entries = [e for e in entries if e.type == "embedder"]
        names = [e.name for e in embedder_entries]
        assert "openai" in names

    def test_registry_includes_anthropic_llm(self):
        """注册表包含 Anthropic LLM（测试环境已设 dummy key）。"""
        entries = get_all_provider_entries()
        llm_entries = [e for e in entries if e.type == "llm"]
        names = [e.name for e in llm_entries]
        assert "anthropic" in names


# ======================================================================
# HealthCheckService 单元测试
# ======================================================================


class TestHealthCheckService:
    """HealthCheckService 核心逻辑测试。"""

    def setup_method(self):
        """每个测试前重置熔断器。"""
        reset_all_circuit_breakers()

    async def test_check_all_returns_dict(self):
        """check_all 返回 ProviderHealth 字典。"""
        service = HealthCheckService()

        # Mock get_all_provider_entries 返回空列表 — 结果为空字典
        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ):
            results = await service.check_all()
            assert isinstance(results, dict)

    async def test_check_all_with_mocked_healthy_provider(self):
        """check_all 对健康 Provider 返回 healthy=True。"""
        service = HealthCheckService()

        mock_factory = MagicMock(return_value=MagicMock())
        mock_entry = ProviderMeta(
            name="mock_openai",
            type="embedder",
            breaker_name="embedder_openai",
            factory=mock_factory,
        )

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[mock_entry],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ), patch.object(
            service,
            "_do_lightweight_check",
            new_callable=AsyncMock,
            return_value=None,
        ):
            results = await service.check_all()
            assert "embedder_openai" in results
            assert results["embedder_openai"].healthy is True
            assert results["embedder_openai"].latency_ms is not None
            assert results["embedder_openai"].circuit_state == "closed"

    async def test_check_all_with_failing_provider(self):
        """check_all 对失败 Provider 返回 healthy=False。"""
        service = HealthCheckService()

        mock_factory = MagicMock(return_value=MagicMock())
        mock_entry = ProviderMeta(
            name="mock_openai",
            type="embedder",
            breaker_name="embedder_openai",
            factory=mock_factory,
        )

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[mock_entry],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ), patch.object(
            service,
            "_do_lightweight_check",
            new_callable=AsyncMock,
            side_effect=ConnectionError("API unreachable"),
        ):
            results = await service.check_all()
            assert results["embedder_openai"].healthy is False
            assert "API unreachable" in results["embedder_openai"].error

    async def test_check_all_calls_save_to_redis(self):
        """check_all 结束后调用 _save_to_redis。"""
        service = HealthCheckService()

        with patch(
            "app.services.health_check.get_all_provider_entries",
            return_value=[],
        ), patch.object(
            service, "_save_to_redis", new_callable=AsyncMock
        ) as mock_save:
            await service.check_all()
            mock_save.assert_called_once()

    async def test_check_provider_instantiate_failure(self):
        """Provider 实例化失败时返回 unhealthy。"""
        service = HealthCheckService()
        now = datetime.now(timezone.utc).isoformat()

        mock_entry = ProviderMeta(
            name="fail",
            type="embedder",
            breaker_name="embedder_fail",
            factory=MagicMock(side_effect=ImportError("missing dependency")),
        )

        health = await service._check_provider(mock_entry, now)
        assert health.healthy is False
        assert "instantiate_failed" in health.error

    async def test_check_provider_check_failure(self):
        """Provider 检查调用失败时返回 unhealthy 并记录延迟。"""
        service = HealthCheckService()
        now = datetime.now(timezone.utc).isoformat()

        mock_provider = MagicMock()
        mock_entry = ProviderMeta(
            name="fail",
            type="embedder",
            breaker_name="embedder_openai",
            factory=MagicMock(return_value=mock_provider),
        )

        with patch.object(
            service,
            "_do_lightweight_check",
            new_callable=AsyncMock,
            side_effect=TimeoutError("request timeout"),
        ):
            health = await service._check_provider(mock_entry, now)
            assert health.healthy is False
            assert "request timeout" in health.error
            assert health.latency_ms is not None


# ======================================================================
# _do_lightweight_check 各类型测试
# ======================================================================


class TestLightweightCheck:
    """_do_lightweight_check 按类型执行正确检查。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_embedder_check_calls_embed(self):
        """Embedder 检查调用 embed()。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        entry = ProviderMeta("openai", "embedder", "embedder_openai", lambda: mock_provider)
        await service._do_lightweight_check(entry, mock_provider)
        mock_provider.embed.assert_called_once_with(["health_check"])

    async def test_embedder_check_empty_result_raises(self):
        """Embedder 返回空嵌入时抛异常。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[])

        entry = ProviderMeta("openai", "embedder", "embedder_openai", lambda: mock_provider)
        with pytest.raises(ValueError, match="empty embedding"):
            await service._do_lightweight_check(entry, mock_provider)

    async def test_embedder_check_empty_vector_raises(self):
        """Embedder 返回零维向量时抛异常。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(return_value=[[]])

        entry = ProviderMeta("openai", "embedder", "embedder_openai", lambda: mock_provider)
        with pytest.raises(ValueError, match="empty embedding"):
            await service._do_lightweight_check(entry, mock_provider)

    async def test_reranker_check_calls_rerank(self):
        """Reranker 检查调用 rerank()。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.rerank = AsyncMock(return_value=[{"index": 0, "score": 0.9}])

        entry = ProviderMeta("cohere", "reranker", "reranker_cohere", lambda: mock_provider)
        await service._do_lightweight_check(entry, mock_provider)
        mock_provider.rerank.assert_called_once()

    async def test_vectorstore_check_calls_health_check(self):
        """VectorStore 检查调用 health_check()。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=True)

        entry = ProviderMeta("opensearch", "vectorstore", "vectorstore_opensearch", lambda: mock_provider)
        await service._do_lightweight_check(entry, mock_provider)
        mock_provider.health_check.assert_called_once()

    async def test_vectorstore_check_false_raises(self):
        """VectorStore health_check 返回 False 时抛异常。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=False)

        entry = ProviderMeta("opensearch", "vectorstore", "vectorstore_opensearch", lambda: mock_provider)
        with pytest.raises(ValueError, match="health_check returned False"):
            await service._do_lightweight_check(entry, mock_provider)

    async def test_llm_check_calls_models_list(self):
        """LLM 检查调用 client.models.list()。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.client = MagicMock()
        mock_provider.client.models = MagicMock()
        mock_provider.client.models.list = AsyncMock()

        entry = ProviderMeta("vllm", "llm", "vllm", lambda: mock_provider)
        await service._do_lightweight_check(entry, mock_provider)
        mock_provider.client.models.list.assert_called_once()

    async def test_llm_check_no_client_checks_circuit_breaker(self):
        """LLM 无 client.models 时检查熔断器状态。"""
        service = HealthCheckService()
        mock_provider = MagicMock()
        mock_provider.client = MagicMock(spec=[])  # 无 models 属性

        entry = ProviderMeta("custom", "llm", "vllm", lambda: mock_provider)
        # 熔断器 closed — 不抛异常
        await service._do_lightweight_check(entry, mock_provider)


# ======================================================================
# Redis 缓存测试
# ======================================================================


class TestRedisCaching:
    """Redis 缓存读写测试。"""

    async def test_save_to_redis_calls_setex(self):
        """_save_to_redis 调用 Redis SETEX。"""
        service = HealthCheckService()

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        with patch(
            "app.services.health_check.aioredis.from_url",
            return_value=mock_redis,
        ):
            results = {
                "embedder_openai": ProviderHealth(
                    name="embedder_openai",
                    type="embedder",
                    healthy=True,
                    latency_ms=50.0,
                    circuit_state="closed",
                    last_check="2026-01-01T00:00:00Z",
                )
            }
            await service._save_to_redis(results)
            mock_redis.setex.assert_called_once()
            args = mock_redis.setex.call_args
            assert args[0][0] == REDIS_KEY  # key
            assert args[0][1] == 60  # TTL
            # value 是 JSON 字符串
            payload = json.loads(args[0][2])
            assert "embedder_openai" in payload

    async def test_save_to_redis_handles_failure(self):
        """Redis 写入失败时不抛异常。"""
        service = HealthCheckService()

        with patch(
            "app.services.health_check.aioredis.from_url",
            side_effect=ConnectionError("Redis unavailable"),
        ):
            # 不抛异常
            await service._save_to_redis({})

    async def test_load_from_redis_returns_cached(self):
        """load_from_redis 返回缓存数据。"""
        service = HealthCheckService()

        cached_data = {
            "embedder_openai": {
                "name": "embedder_openai",
                "type": "embedder",
                "healthy": True,
                "latency_ms": 30.0,
                "error": None,
                "circuit_state": "closed",
                "last_check": "2026-01-01T00:00:00Z",
            }
        }

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(cached_data))

        with patch(
            "app.services.health_check.aioredis.from_url",
            return_value=mock_redis,
        ):
            result = await service.load_from_redis()
            assert result is not None
            assert "embedder_openai" in result
            assert result["embedder_openai"].healthy is True

    async def test_load_from_redis_returns_none_on_miss(self):
        """缓存不存在时返回 None。"""
        service = HealthCheckService()

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        with patch(
            "app.services.health_check.aioredis.from_url",
            return_value=mock_redis,
        ):
            result = await service.load_from_redis()
            assert result is None

    async def test_load_from_redis_handles_failure(self):
        """Redis 读取出错时返回 None。"""
        service = HealthCheckService()

        with patch(
            "app.services.health_check.aioredis.from_url",
            side_effect=ConnectionError("Redis unavailable"),
        ):
            result = await service.load_from_redis()
            assert result is None

    async def test_save_then_load_roundtrip(self):
        """写入后读取数据一致。"""
        service = HealthCheckService()

        # 模拟 Redis 内存存储
        redis_store: dict[str, str] = {}

        mock_redis = AsyncMock()

        async def fake_setex(key, ttl, value):
            redis_store[key] = value

        async def fake_get(key):
            return redis_store.get(key)

        mock_redis.setex = fake_setex
        mock_redis.get = fake_get

        with patch(
            "app.services.health_check.aioredis.from_url",
            return_value=mock_redis,
        ):
            original = {
                "vllm": ProviderHealth(
                    name="vllm",
                    type="llm",
                    healthy=True,
                    latency_ms=120.5,
                    circuit_state="closed",
                    last_check="2026-07-21T10:00:00Z",
                )
            }
            await service._save_to_redis(original)
            loaded = await service.load_from_redis()

            assert loaded is not None
            assert "vllm" in loaded
            assert loaded["vllm"].healthy is True
            assert loaded["vllm"].latency_ms == 120.5


# ======================================================================
# Celery 任务幂等性测试
# ======================================================================


class TestHealthCheckTaskIdempotency:
    """Celery 健康检查任务幂等性测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_task_runs_when_lock_acquired(self):
        """锁获取成功时正常执行健康检查。"""
        from app.utils.task_lock import TaskLock, TaskLockContext

        lock = TaskLock(key="test", value="abc", acquired=True, ttl=60)

        with patch(
            "app.utils.task_lock.acquire_task_lock",
            new_callable=AsyncMock,
            return_value=lock,
        ), patch(
            "app.utils.task_lock.release_task_lock",
            new_callable=AsyncMock,
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
            return_value={},
        ):
            from tasks.health_tasks import _run_health_check

            result = await _run_health_check()
            assert result["status"] == "completed"

    async def test_task_skipped_when_lock_held(self):
        """锁被持有时跳过执行。"""
        from app.utils.task_lock import TaskLock

        lock = TaskLock(key="test", value="abc", acquired=False, ttl=60)

        with patch(
            "app.utils.task_lock.acquire_task_lock",
            new_callable=AsyncMock,
            return_value=lock,
        ), patch(
            "app.utils.task_lock.release_task_lock",
            new_callable=AsyncMock,
        ), patch(
            "app.services.health_check.HealthCheckService.check_all",
            new_callable=AsyncMock,
        ) as mock_check:
            from tasks.health_tasks import _run_health_check

            result = await _run_health_check()
            assert result["status"] == "skipped"
            assert result["reason"] == "already_running"
            mock_check.assert_not_called()

    def test_celery_entry_point_returns_dict(self):
        """Celery 入口函数返回字典。"""
        from tasks.health_tasks import health_check_all_providers

        # Mock asyncio.run 和内部逻辑
        with patch("tasks.health_tasks.asyncio.run") as mock_run:
            mock_run.return_value = {"status": "completed", "total": 0, "healthy": 0, "unhealthy": 0}
            result = health_check_all_providers()
            assert isinstance(result, dict)
            assert "status" in result

    def test_celery_entry_point_handles_error(self):
        """Celery 入口函数捕获异常返回 error 状态。"""
        from tasks.health_tasks import health_check_all_providers

        with patch("tasks.health_tasks.asyncio.run", side_effect=RuntimeError("unexpected")):
            result = health_check_all_providers()
            assert result["status"] == "error"
            assert "unexpected" in result["error"]


# ======================================================================
# API 端点测试
# ======================================================================


class TestHealthProvidersAPI:
    """/health/providers API 端点测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_api_returns_cache_hit(self):
        """缓存命中时返回 source=cache。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        cached = {
            "embedder_openai": ProviderHealth(
                name="embedder_openai",
                type="embedder",
                healthy=True,
                latency_ms=50.0,
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
                resp = await client.get("/health/providers")
                assert resp.status_code == 200
                data = resp.json()
                assert data["code"] == 0
                assert data["data"]["source"] == "cache"
                assert data["data"]["healthy_count"] == 1
                assert data["data"]["total"] == 1

    async def test_api_returns_fresh_on_cache_miss(self):
        """缓存未命中时返回 source=fresh。"""
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        fresh = {
            "vllm": ProviderHealth(
                name="vllm",
                type="llm",
                healthy=True,
                latency_ms=80.0,
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
                assert resp.status_code == 200
                data = resp.json()
                assert data["data"]["source"] == "fresh"
                assert data["data"]["healthy_count"] == 1

    async def test_api_is_idempotent(self):
        """连续两次调用返回一致结果（幂等性）。"""
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
                assert resp1.json() == resp2.json()


# ======================================================================
# 配置参数测试
# ======================================================================


class TestHealthCheckConfig:
    """P2-A 健康检查配置参数测试。"""

    def test_config_has_health_check_interval(self):
        """Settings 包含 HEALTH_CHECK_INTERVAL。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "HEALTH_CHECK_INTERVAL")
        assert settings.HEALTH_CHECK_INTERVAL > 0

    def test_config_has_health_check_cache_ttl(self):
        """Settings 包含 HEALTH_CHECK_CACHE_TTL。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "HEALTH_CHECK_CACHE_TTL")
        assert settings.HEALTH_CHECK_CACHE_TTL > 0

    def test_config_has_failover_chains(self):
        """Settings 包含故障转移链配置。"""
        from app.config import get_settings

        settings = get_settings()
        assert hasattr(settings, "LLM_FAILOVER_CHAIN")
        assert hasattr(settings, "EMBEDDER_FAILOVER_CHAIN")
        assert hasattr(settings, "RERANKER_FAILOVER_CHAIN")
        assert hasattr(settings, "VECTOR_STORE_FAILOVER_CHAIN")

    def test_default_interval_30_seconds(self):
        """默认检查间隔 30 秒。"""
        from app.config import get_settings

        settings = get_settings()
        assert settings.HEALTH_CHECK_INTERVAL == 30

    def test_default_cache_ttl_60_seconds(self):
        """默认缓存 TTL 60 秒。"""
        from app.config import get_settings

        settings = get_settings()
        assert settings.HEALTH_CHECK_CACHE_TTL == 60


# ======================================================================
# Celery Beat 调度配置测试
# ======================================================================


class TestBeatSchedule:
    """Celery Beat 调度配置测试。"""

    def setup_method(self):
        """每个测试前恢复真实 celery 和 celery_app，清除其他测试注入的 mock。"""
        import importlib

        sys.modules.pop("celery_app", None)
        # 检查 celery 是否被 mock 替换（真实模块的 __file__ 是 str）
        celery_mod = sys.modules.get("celery")
        if celery_mod is not None and not isinstance(
            getattr(celery_mod, "__file__", None), str
        ):
            # 清除所有 celery.* 子模块，强制重新导入真实包
            for key in list(sys.modules.keys()):
                if key == "celery" or key.startswith("celery."):
                    del sys.modules[key]
            importlib.import_module("celery")
        importlib.import_module("celery.schedules")

    def test_beat_schedule_has_health_check(self):
        """Beat 调度包含健康检查任务。"""
        from celery_app import celery_app

        assert "health-check-providers-30s" in celery_app.conf.beat_schedule

    def test_beat_schedule_task_name_correct(self):
        """Beat 任务名正确。"""
        from celery_app import celery_app

        entry = celery_app.conf.beat_schedule["health-check-providers-30s"]
        assert entry["task"] == "tasks.health_tasks.health_check_all_providers"

    def test_beat_schedule_interval_30s(self):
        """Beat 调度间隔 30 秒。"""
        from celery_app import celery_app

        entry = celery_app.conf.beat_schedule["health-check-providers-30s"]
        assert entry["schedule"] == 30.0

    def test_health_tasks_in_celery_include(self):
        """health_tasks 在 Celery include 列表中。"""
        from celery_app import celery_app

        assert "tasks.health_tasks" in celery_app.conf.include
