"""资源管理器 — 统一管理应用生命周期中的资源清理。

设计理念：
- 各组件（DB engine、Redis、Milvus、OpenSearch、httpx 等）初始化时
  调用 register() 注册自己的 close 方法
- lifespan shutdown 阶段调用 cleanup()，按优先级逆序清理
- 支持 async 和 sync close 方法
- 超时保护：单个资源清理超过 timeout 秒则跳过

优先级约定（数字越大越先清理）：
- 100: 外部 API 客户端（LLM/Embedding/Reranker）
-  80: 搜索引擎客户端（OpenSearch）
-  70: 向量数据库客户端（Milvus）
-  60: 消息队列/缓存（Redis）
-  40: 数据库连接池（PostgreSQL engine）
-  20: 日志/遥测（structlog/LangFuse flush）
-  10: 最低优先级兜底
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Union

from app.utils.logger import get_logger

log = get_logger(__name__)

# close 方法可以是 sync 或 async
CloseFn = Union[Callable[[], None], Callable[[], Awaitable[None]]]


@dataclass
class ResourceEntry:
    """已注册的资源条目。"""

    name: str
    close_fn: CloseFn
    priority: int = 50
    is_async: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_async = inspect.iscoroutinefunction(self.close_fn)


class ResourceManager:
    """资源注册中心 — 单例模式，管理所有需要清理的资源。

    使用方式：
        # 组件初始化时注册
        from app.utils.resource_manager import resource_manager
        resource_manager.register("redis", redis_client.close, priority=60)

        # lifespan shutdown 时统一清理
        await resource_manager.cleanup(timeout=settings.SHUTDOWN_TIMEOUT)
    """

    def __init__(self) -> None:
        self._entries: list[ResourceEntry] = []
        self._cleaned: bool = False

    def register(
        self,
        name: str,
        close_fn: CloseFn,
        priority: int = 50,
    ) -> None:
        """注册一个需要清理的资源。

        Args:
            name: 资源名称（用于日志标识）。
            close_fn: 清理函数，可以是 sync 或 async。
            priority: 清理优先级（数字越大越先清理）。
        """
        entry = ResourceEntry(name=name, close_fn=close_fn, priority=priority)
        self._entries.append(entry)
        self._cleaned = False
        log.debug("resource_manager.registered", name=name, priority=priority)

    def unregister(self, name: str) -> None:
        """取消注册某个资源（测试用）。"""
        self._entries = [e for e in self._entries if e.name != name]

    async def cleanup(self, timeout: float = 30.0) -> None:
        """按优先级逆序清理所有已注册的资源。

        每个资源有独立超时保护，单个资源清理失败不影响其他资源。

        Args:
            timeout: 总清理超时（秒），超时后强制返回。
        """
        if self._cleaned:
            return
        self._cleaned = True

        # 按优先级降序排列（高优先级先清理）
        sorted_entries = sorted(self._entries, key=lambda e: e.priority, reverse=True)

        log.info(
            "resource_manager.cleanup_start",
            resource_count=len(sorted_entries),
            timeout=timeout,
        )

        for entry in sorted_entries:
            try:
                if entry.is_async:
                    await asyncio.wait_for(
                        entry.close_fn(),  # type: ignore[arg-type]
                        timeout=timeout / max(len(sorted_entries), 1),
                    )
                else:
                    entry.close_fn()  # type: ignore[call-arg]
                log.info("resource_manager.cleaned", name=entry.name)
            except asyncio.TimeoutError:
                log.warning("resource_manager.cleanup_timeout", name=entry.name)
            except Exception as exc:
                log.warning(
                    "resource_manager.cleanup_failed",
                    name=entry.name,
                    error=str(exc)[:200],
                )

        log.info("resource_manager.cleanup_done")

    def clear(self) -> None:
        """清空所有注册项（测试用）。"""
        self._entries.clear()
        self._cleaned = False

    @property
    def entries(self) -> list[ResourceEntry]:
        """已注册的资源列表（测试用）。"""
        return list(self._entries)


# 全局单例
resource_manager = ResourceManager()
