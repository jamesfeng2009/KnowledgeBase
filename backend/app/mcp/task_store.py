"""
TaskStore — 单一职责：MCP 长耗时任务的持久化状态管理。

对齐 MCP 2026-07-28 规范 Tasks 扩展的核心语义：
- 持久化 taskId 句柄，客户端断线重连后可凭 taskId 轮询
- 任务状态机：working → completed / failed / cancelled
- 支持 TTL 自动过期，避免 Redis 无限增长

使用 Redis Hash 存储任务状态，单 key 独立 TTL 管理。
Redis 不可用时降级为进程内 dict（单实例开发场景兜底）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

#: 任务状态枚举 — 对齐 MCP Tasks 扩展规范
TASK_WORKING = "working"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

#: 终态集合 — 到达后状态不再变化
_TERMINAL_STATES = frozenset({TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

#: 默认 TTL — 1 小时后自动过期（对齐 Celery result_expires）
_DEFAULT_TTL_SECONDS: int = 3600

#: 默认轮询间隔建议（毫秒）— 服务器告诉客户端该多久问一次
_DEFAULT_POLL_INTERVAL_MS: int = 2000

#: Redis key 前缀
_REDIS_KEY_PREFIX: str = "mcp:task:"

#: 进程内降级存储 — Redis 不可用时的兜底（单实例开发场景）
# 结构: {task_id: {"status": ..., "result": ..., "error": ..., "created_at": ..., "updated_at": ...}}
_fallback_store: dict[str, dict[str, Any]] = {}


class TaskStore:
    """MCP 任务状态存储 — Redis 优先，进程内降级。

    遵循单一职责：只负责任务状态的 CRUD，不关心任务执行逻辑。
    遵循开闭原则：新增状态字段只需扩展 _serialize / _deserialize，不改调用方。
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        poll_interval_ms: int = _DEFAULT_POLL_INTERVAL_MS,
    ) -> None:
        """初始化 TaskStore。

        Args:
            redis_url: Redis 连接 URL。为 None 时降级为进程内 dict。
            ttl_seconds: 任务状态 TTL（秒），过期后自动清理。
            poll_interval_ms: 建议客户端轮询间隔（毫秒）。
        """
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._poll_interval_ms = poll_interval_ms
        self._redis = None
        self._use_fallback = redis_url is None

    async def _get_redis(self):
        """惰性初始化 Redis 连接 — 首次调用时创建。"""
        if self._redis is not None:
            return self._redis
        if self._redis_url is None:
            self._use_fallback = True
            return None
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
            await self._redis.ping()
            log.info("mcp.task_store.redis_connected", url=self._redis_url)
            return self._redis
        except Exception as exc:
            log.warning(
                "mcp.task_store.redis_unavailable_fallback",
                error=str(exc)[:200],
            )
            self._use_fallback = True
            self._redis = None
            return None

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def create_task(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str | None = None,
    ) -> str:
        """创建任务并返回 taskId。

        在 Redis 中写入初始状态 working，设置 TTL。
        调用方负责随后启动后台执行并调用 complete_task / fail_task。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参（用于审计和重放）。
            tenant_id: 租户 ID（用于隔离）。

        Returns:
            唯一任务 ID（UUID hex）。
        """
        task_id = uuid.uuid4().hex
        now = time.time()
        task_data = {
            "task_id": task_id,
            "tool": tool_name,
            "status": TASK_WORKING,
            "result": None,
            "error": None,
            "tenant_id": tenant_id,
            "arguments": arguments,
            "created_at": now,
            "updated_at": now,
        }

        redis = await self._get_redis()
        if redis is not None:
            key = self._key(task_id)
            await redis.hset(
                key,
                mapping={k: json.dumps(v, ensure_ascii=False, default=str) for k, v in task_data.items()},
            )
            await redis.expire(key, self._ttl_seconds)
        else:
            _fallback_store[task_id] = task_data

        log.info(
            "mcp.task_store.created",
            task_id=task_id,
            tool=tool_name,
            ttl=self._ttl_seconds,
        )
        return task_id

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """获取任务当前状态。

        Args:
            task_id: 任务 ID。

        Returns:
            任务状态字典，不存在返回 None。包含字段：
            task_id / tool / status / result / error / created_at / updated_at
        """
        redis = await self._get_redis()
        if redis is not None:
            key = self._key(task_id)
            raw = await redis.hgetall(key)
            if not raw:
                return None
            return {k: json.loads(v) for k, v in raw.items()}

        return _fallback_store.get(task_id)

    async def complete_task(self, task_id: str, result: Any) -> None:
        """标记任务为已完成，写入最终结果。

        终态写入后刷新 TTL（给客户端窗口来取结果）。
        """
        await self._update_task(task_id, status=TASK_COMPLETED, result=result)
        log.info("mcp.task_store.completed", task_id=task_id)

    async def fail_task(self, task_id: str, error: str) -> None:
        """标记任务为失败，写入错误信息。"""
        await self._update_task(task_id, status=TASK_FAILED, error=error)
        log.warning("mcp.task_store.failed", task_id=task_id, error=error[:200])

    async def cancel_task(self, task_id: str) -> bool:
        """标记任务为已取消。

        取消是协作式的 — 仅设置状态标志，不强制中断执行。
        返回 False 表示任务已是终态无法取消。

        Returns:
            True = 取消成功，False = 任务不存在或已处于终态。
        """
        task = await self.get_task(task_id)
        if task is None:
            return False
        if task.get("status") in _TERMINAL_STATES:
            return False
        await self._update_task(task_id, status=TASK_CANCELLED)
        log.info("mcp.task_store.cancelled", task_id=task_id)
        return True

    @property
    def poll_interval_ms(self) -> int:
        """建议客户端轮询间隔（毫秒）。"""
        return self._poll_interval_ms

    @property
    def ttl_seconds(self) -> int:
        """任务状态 TTL（秒）。"""
        return self._ttl_seconds

    def is_terminal(self, status: str) -> bool:
        """判断状态是否为终态。"""
        return status in _TERMINAL_STATES

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _key(self, task_id: str) -> str:
        return f"{_REDIS_KEY_PREFIX}{task_id}"

    async def _update_task(
        self,
        task_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """更新任务状态 — 仅更新非 None 字段。"""
        now = time.time()
        updates: dict[str, str] = {
            "status": json.dumps(status, ensure_ascii=False),
            "updated_at": json.dumps(now, default=str),
        }
        if result is not None:
            updates["result"] = json.dumps(result, ensure_ascii=False, default=str)
        if error is not None:
            updates["error"] = json.dumps(error, ensure_ascii=False)

        redis = await self._get_redis()
        if redis is not None:
            key = self._key(task_id)
            await redis.hset(key, mapping=updates)
            # 终态刷新 TTL — 给客户端取结果的时间窗口
            ttl = self._ttl_seconds if status in _TERMINAL_STATES else self._ttl_seconds
            await redis.expire(key, ttl)
        else:
            task = _fallback_store.get(task_id)
            if task is not None:
                task["status"] = status
                task["updated_at"] = now
                if result is not None:
                    task["result"] = result
                if error is not None:
                    task["error"] = error


#: 全局单例 — 进程内复用，避免每次调用都创建 Redis 连接
_global_store: TaskStore | None = None


def get_task_store(
    redis_url: str | None = None,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    poll_interval_ms: int = _DEFAULT_POLL_INTERVAL_MS,
) -> TaskStore:
    """获取全局 TaskStore 单例。

    首次调用时惰性初始化，后续复用同一实例。
    Redis URL 从 app.config.get_settings() 获取（如未显式传入）。
    """
    global _global_store
    if _global_store is None:
        if redis_url is None:
            try:
                from app.config import get_settings

                redis_url = get_settings().REDIS_URL
            except Exception:
                redis_url = None
        _global_store = TaskStore(
            redis_url=redis_url,
            ttl_seconds=ttl_seconds,
            poll_interval_ms=poll_interval_ms,
        )
    return _global_store
