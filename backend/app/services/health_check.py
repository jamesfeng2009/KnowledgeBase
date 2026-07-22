"""AI 服务健康检查服务 — 定时检测所有 Provider 可用性。

P2-A Task 2: 为 Celery Beat 和 /health/providers API 提供后端支撑。

工作流：
1. 遍历 Provider 注册表，逐一实例化并执行轻量级健康检查
2. 结果写入 Redis（key=health:providers, TTL=60s）
3. API 端点从 Redis 读取缓存结果，缓存不存在时降级为同步检查

幂等保障：
- Celery 任务使用 Redis SETNX 锁防止并行执行
- API 端点为 GET，天然幂等
- 健康检查不修改任何状态，仅读取 Provider 状态
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel

from app.config import get_settings
from app.llm.registry import ProviderMeta, get_all_provider_entries
from app.utils.circuit_breaker import CircuitState, _breakers
from app.utils.logger import get_logger

log = get_logger(__name__)


class ProviderHealth(BaseModel):
    """单个 Provider 的健康状态。"""

    name: str  # 熔断器名称 (e.g., "embedder_openai")
    type: str  # 类型: "embedder" | "reranker" | "vectorstore" | "llm"
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None
    circuit_state: str  # "closed" | "open" | "half_open"
    last_check: str  # ISO 8601 时间戳


def sanitize_providers(
    providers: dict[str, ProviderHealth],
    include_details: bool,
) -> dict[str, dict]:
    """生成 provider 健康状态视图（信息泄漏防护）。

    非 admin（``include_details=False``）剥离 ``error`` 细节字段 —
    error 可能包含 API key 片段、内部 URL 等敏感信息，
    仅 admin 可见；普通用户/未认证用户仅返回健康状态摘要。

    Args:
        providers: name → ProviderHealth 映射。
        include_details: 是否保留 error 细节（仅 admin 传 True）。

    Returns:
        name → dict 视图映射。
    """
    views = {name: h.model_dump() for name, h in providers.items()}
    if not include_details:
        for view in views.values():
            view.pop("error", None)
    return views


async def is_request_admin(request) -> bool:
    """解析可选 Bearer 认证 — 仅 active admin 返回 True。

    未携带凭证、凭证无效或查询异常一律按非 admin 处理（安全默认），
    不影响健康检查接口本身对普通用户的可用性。

    Args:
        request: FastAPI Request 对象。

    Returns:
        True 表示请求者为 active admin。
    """
    authorization = request.headers.get("authorization", "")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    try:
        from app.database import async_session_factory
        from app.services.auth_service import AuthService

        async with async_session_factory() as session:
            user = await AuthService(session).get_current_user(parts[1])
    except Exception:
        return False
    return bool(
        getattr(user, "role", None) == "admin"
        and getattr(user, "is_active", False)
    )


REDIS_KEY = "health:providers"


class HealthCheckService:
    """AI 服务健康检查调度服务。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        # Redis 客户端复用：避免每次读写都新建连接（异常时置 None 下次重建）
        self._redis: aioredis.Redis | None = None

    def _get_redis(self) -> aioredis.Redis:
        """获取复用的 Redis 客户端（懒加载单例）。"""
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._settings.REDIS_URL, decode_responses=True
            )
        return self._redis

    def _reset_redis(self) -> None:
        """连接异常后重置，下次调用时重建。"""
        self._redis = None

    async def check_all(self) -> dict[str, ProviderHealth]:
        """检查所有 Provider 健康状态，结果存 Redis。

        并发执行所有检查，单个 Provider 超时不影响整体返回。

        Returns:
            以熔断器名称为 key 的健康状态字典。
        """
        entries = get_all_provider_entries()
        now = datetime.now(timezone.utc).isoformat()
        timeout = float(
            getattr(self._settings, "HEALTH_CHECK_TIMEOUT", 10)
        )

        log.info(
            "health_check.start",
            provider_count=len(entries),
            types=list({e.type for e in entries}),
        )

        async def _guarded_check(entry: ProviderMeta) -> ProviderHealth:
            """单个 Provider 检查 + 超时保护，保证 check_all 不被拖死。"""
            try:
                return await asyncio.wait_for(
                    self._check_provider(entry, now), timeout=timeout
                )
            except asyncio.TimeoutError:
                log.warning(
                    "health_check.timeout",
                    name=entry.breaker_name,
                    timeout_s=timeout,
                )
                cb = _breakers.get(entry.breaker_name)
                return ProviderHealth(
                    name=entry.breaker_name,
                    type=entry.type,
                    healthy=False,
                    error=f"health check timeout after {timeout}s",
                    circuit_state=cb.state.value if cb else "closed",
                    last_check=now,
                )

        healths = await asyncio.gather(*(_guarded_check(e) for e in entries))
        results: dict[str, ProviderHealth] = {h.name: h for h in healths}

        for health in healths:
            log.info(
                "health_check.provider_result",
                name=health.name,
                healthy=health.healthy,
                latency_ms=health.latency_ms,
                circuit_state=health.circuit_state,
                error=health.error,
            )

        # 写入 Redis
        await self._save_to_redis(results)

        healthy_count = sum(1 for h in results.values() if h.healthy)
        log.info(
            "health_check.complete",
            total=len(results),
            healthy=healthy_count,
            unhealthy=len(results) - healthy_count,
        )

        return results

    async def _check_provider(
        self, entry: ProviderMeta, now: str
    ) -> ProviderHealth:
        """检查单个 Provider — 实例化 + 轻量级调用。"""
        cb = _breakers.get(entry.breaker_name)
        circuit_state = cb.state.value if cb else "closed"

        try:
            provider = entry.factory()
        except Exception as exc:
            log.warning(
                "health_check.instantiate_failed",
                name=entry.breaker_name,
                error=str(exc),
            )
            return ProviderHealth(
                name=entry.breaker_name,
                type=entry.type,
                healthy=False,
                error=f"instantiate_failed: {exc}",
                circuit_state=circuit_state,
                last_check=now,
            )

        # 执行轻量级健康检查
        t0 = time.monotonic()
        try:
            await self._do_lightweight_check(entry, provider)
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
            return ProviderHealth(
                name=entry.breaker_name,
                type=entry.type,
                healthy=True,
                latency_ms=latency_ms,
                circuit_state=circuit_state,
                last_check=now,
            )
        except Exception as exc:
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
            log.warning(
                "health_check.check_failed",
                name=entry.breaker_name,
                error=str(exc),
                latency_ms=latency_ms,
            )
            return ProviderHealth(
                name=entry.breaker_name,
                type=entry.type,
                healthy=False,
                latency_ms=latency_ms,
                error=str(exc),
                circuit_state=circuit_state,
                last_check=now,
            )

    async def _do_lightweight_check(
        self, entry: ProviderMeta, provider: Any
    ) -> None:
        """根据 Provider 类型执行轻量级健康检查。"""
        if entry.type == "embedder":
            # embed("health_check") — 验证 API 可达
            result = await provider.embed(["health_check"])
            if not result or len(result[0]) == 0:
                raise ValueError("empty embedding returned")

        elif entry.type == "reranker":
            # rerank("test", ["doc"], top_k=1) — 验证 API 可达
            await provider.rerank("health_check", ["test doc"], top_k=1)

        elif entry.type == "vectorstore":
            # 调用已有 health_check() 方法
            healthy = await provider.health_check()
            if not healthy:
                raise ValueError("health_check returned False")

        elif entry.type == "llm":
            # LLM — 调用 client.models.list() 验证 API 可达
            if hasattr(provider, "client") and hasattr(
                provider.client, "models"
            ):
                await provider.client.models.list()
            else:
                # 无 list models API — 仅检查熔断器状态
                if cb := _breakers.get(entry.breaker_name):
                    if cb.state == CircuitState.OPEN:
                        raise ValueError("circuit breaker is OPEN")

    async def _save_to_redis(
        self, results: dict[str, ProviderHealth]
    ) -> None:
        """将健康检查结果写入 Redis（复用连接，异常时重置待下次重建）。"""
        try:
            redis = self._get_redis()
            payload = json.dumps(
                {k: v.model_dump() for k, v in results.items()}
            )
            ttl = getattr(self._settings, "HEALTH_CHECK_CACHE_TTL", 60)
            await redis.setex(REDIS_KEY, ttl, payload)
            log.debug("health_check.redis_saved", key=REDIS_KEY, ttl=ttl)
        except Exception as exc:
            self._reset_redis()
            log.warning("health_check.redis_save_failed", error=str(exc))

    async def load_from_redis(self) -> dict[str, ProviderHealth] | None:
        """从 Redis 读取缓存的健康检查结果。

        Returns:
            缓存的健康状态字典，缓存不存在时返回 None。
        """
        try:
            redis = self._get_redis()
            data = await redis.get(REDIS_KEY)
            if data:
                parsed = json.loads(data)
                return {
                    k: ProviderHealth(**v) for k, v in parsed.items()
                }
            return None
        except Exception as exc:
            self._reset_redis()
            log.warning("health_check.redis_load_failed", error=str(exc))
            return None
