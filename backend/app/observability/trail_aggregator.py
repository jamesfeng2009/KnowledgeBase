"""
Agent 执行轨迹聚合指标 — 完成率 / 工具命中率 / 平均收敛步数 / 兜圈率。

对齐需求（P0-3 + P1-7）：
- 完成率 = 正常结束（非 max_iterations_reached）的会话占比。
- 工具命中率 = 最终答案中引用了工具结果的会话占比。
- 平均收敛步数 = 决策循环正常结束时的平均迭代轮次。
- 兜圈率 = 触发 ``CrossTurnDeduplicator`` 指针引用 的会话占比 +
           触发 ``max_iterations_reached`` 的会话占比。
- 卡死告警 = ``max_iterations_reached`` 或总超时（AGENT_TOTAL_TIMEOUT_SECONDS）
           触发的会话数量及占比。

实现：基于 asyncio.Lock 的线程安全内存聚合，按时间窗口滑动累积。
每次 ``answer()`` 结束通过 ``record_session()`` 上报一次快照。
对外暴露 ``window_summary()`` 返回窗口内指标；暴露 Prometheus 风格的
计数器/累加器供 /metrics 或 api/v1 接口消费。

P2-9 滑动窗口：每条会话记录携带 ``time.monotonic()`` 时间戳，
``record_session`` / ``window_summary`` 调用时驱逐早于 ``window_seconds``
的记录，``window_summary()`` 返回的是窗口内而非自进程启动起的全部指标。
此前实现接受 ``window_seconds`` 参数却从不执行驱逐，长时间运行后聚合值
失去时效性；现已修复。

遵循单一职责：本模块只负责指标聚合与计算，不触发告警或写入持久化。
告警由调用方（API 层或日志扫描规则）根据返回的指标自行决策。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class _SessionRecord:
    """单次会话的轨迹快照（P2-9：带时间戳以支持滑动窗口驱逐）。

    每个字段对应 ``record_session`` 的一次上报入参，``window_summary``
    时按窗口内全部记录现算聚合指标，驱逐过期记录无需回退计数器。
    """

    timestamp: float  # time.monotonic()，窗口驱逐基准
    iterations: int
    max_iter_reached: bool = False
    total_timeout: bool = False
    dedup_hit: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    think_latency_ms: float = 0.0
    retrieve_latency_ms: float = 0.0
    tool_latency_ms: float = 0.0


def _compute_summary(records: list[_SessionRecord]) -> dict[str, Any]:
    """从窗口内会话记录列表现算聚合指标（与历史 _WindowState.to_summary 同口径）。

    Args:
        records: 已驱逐过期项后的窗口内会话记录列表。

    Returns:
        ``{"window": {...}, "tool_distribution": {...}}``；空列表返回全 0 摘要。
    """
    total_sessions = len(records)
    total = total_sessions or 1  # 防除零

    completed_sessions = 0
    max_iter_sessions = 0
    timeout_sessions = 0
    dedup_hit_sessions = 0
    tool_call_sessions = 0
    tool_hit_sessions = 0
    total_iterations = 0
    total_think_latency_ms = 0.0
    total_retrieve_latency_ms = 0.0
    total_tool_latency_ms = 0.0
    tool_name_counts: dict[str, int] = {}

    for rec in records:
        if rec.max_iter_reached:
            max_iter_sessions += 1
        elif rec.total_timeout:
            timeout_sessions += 1
        else:
            completed_sessions += 1
            total_iterations += rec.iterations

        if rec.dedup_hit:
            dedup_hit_sessions += 1

        total_think_latency_ms += rec.think_latency_ms
        total_retrieve_latency_ms += rec.retrieve_latency_ms
        total_tool_latency_ms += rec.tool_latency_ms

        calls = rec.tool_calls or []
        if calls:
            tool_call_sessions += 1
            any_hit = False
            for call in calls:
                name = call.get("name", "unknown")
                if call.get("hit"):
                    any_hit = True
                    tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
            if any_hit:
                tool_hit_sessions += 1

    completed_total = completed_sessions or 1  # 防除零
    return {
        "window": {
            "total_sessions": total_sessions,
            "completion_rate": round(completed_sessions / total, 4),
            "max_iterations_rate": round(max_iter_sessions / total, 4),
            "timeout_rate": round(timeout_sessions / total, 4),
            "dedup_hit_rate": round(dedup_hit_sessions / total, 4),
            "loitering_rate": round(
                (dedup_hit_sessions + max_iter_sessions) / total, 4
            ),
            "tool_call_rate": round(tool_call_sessions / total, 4),
            "tool_hit_rate": round(tool_hit_sessions / total, 4),
            "avg_iterations": round(total_iterations / completed_total, 2),
            "avg_think_latency_ms": round(total_think_latency_ms / total, 2),
            "avg_retrieve_latency_ms": round(
                total_retrieve_latency_ms / total, 2
            ),
            "avg_tool_latency_ms": round(total_tool_latency_ms / total, 2),
        },
        "tool_distribution": dict(tool_name_counts),
    }


class TrailAggregator:
    """Agent Loop 轨迹聚合器 — 线程安全的时间窗口内指标统计。

    使用方式：
        aggregator = TrailAggregator(window_seconds=3600)
        # answer() 结束时上报
        aggregator.record_session(
            iterations=3,
            max_iter_reached=False,
            total_timeout=False,
            dedup_hit=False,
            tool_calls=[{"name": "knowledge_search", "hit": True}],
            think_latency_ms=1200,
            retrieve_latency_ms=800,
            tool_latency_ms=500,
        )
        # API 层查询
        metrics = aggregator.window_summary()

    P2-9：``window_seconds`` 现在实际生效 —— 早于窗口的会话记录在
    ``record_session`` / ``window_summary`` 时被驱逐，``window_summary()``
    返回窗口内指标而非自进程启动起的全部累积值。
    """

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window_seconds = window_seconds
        # deque 按入队时间单调递增，popleft 驱逐最早过期记录 O(1)
        self._records: deque[_SessionRecord] = deque()
        self._lock: Any | None = None  # 惰性创建 asyncio.Lock（在首个 async 调用时）

    def _ensure_lock(self) -> Any:
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _evict(self, now: float | None = None) -> None:
        """驱逐超出时间窗口的会话记录（P2-9）。

        Args:
            now: 当前 monotonic 时间戳；缺省取 ``time.monotonic()``。
                传入参数便于测试注入虚拟时间。
        """
        if not self._records:
            return
        cutoff = (now if now is not None else time.monotonic()) - self._window_seconds
        # 记录按 timestamp 单调递增入队，从队首逐个驱逐直到首条仍在窗口内
        while self._records and self._records[0].timestamp < cutoff:
            self._records.popleft()

    async def record_session(
        self,
        *,
        iterations: int,
        max_iter_reached: bool = False,
        total_timeout: bool = False,
        dedup_hit: bool = False,
        tool_calls: list[dict[str, Any]] | None = None,
        think_latency_ms: float = 0.0,
        retrieve_latency_ms: float = 0.0,
        tool_latency_ms: float = 0.0,
    ) -> None:
        """上报单个会话的执行结果。

        Args:
            iterations: 决策循环实际执行的迭代轮次（从 think/retrieve/tool 循环中统计）。
            max_iter_reached: 是否因超过 max_iterations 而强制退出。
            total_timeout: 是否因 Agent Loop 总超时而退出。
            dedup_hit: 是否触发 CrossTurnDeduplicator 指针引用（兜圈信号）。
            tool_calls: 工具调用列表，每项含 ``name`` 和可选 ``hit`` 布尔值。
            think_latency_ms: think 节点耗时（毫秒）。
            retrieve_latency_ms: retrieve 节点耗时（毫秒）。
            tool_latency_ms: tool_call 节点耗时（毫秒）。
        """
        lock = self._ensure_lock()
        async with lock:
            now = time.monotonic()
            self._evict(now)
            self._records.append(
                _SessionRecord(
                    timestamp=now,
                    iterations=iterations,
                    max_iter_reached=max_iter_reached,
                    total_timeout=total_timeout,
                    dedup_hit=dedup_hit,
                    tool_calls=list(tool_calls or []),
                    think_latency_ms=think_latency_ms,
                    retrieve_latency_ms=retrieve_latency_ms,
                    tool_latency_ms=tool_latency_ms,
                )
            )

            log.debug(
                "trail.record_session",
                iterations=iterations,
                max_iter=max_iter_reached,
                timeout=total_timeout,
                dedup=dedup_hit,
                tool_calls=len(tool_calls or []),
            )

    async def window_summary(self) -> dict[str, Any]:
        """返回当前窗口的聚合指标摘要（驱逐过期记录后现算）。"""
        lock = self._ensure_lock()
        async with lock:
            self._evict()
            return _compute_summary(list(self._records))

    async def reset(self) -> None:
        """重置窗口状态（用于测试或定时清理）。"""
        lock = self._ensure_lock()
        async with lock:
            self._records.clear()


# 进程级单例 — 全局轨迹聚合器
_global_aggregator: TrailAggregator | None = None


def get_trail_aggregator() -> TrailAggregator:
    """获取全局轨迹聚合器单例。"""
    global _global_aggregator
    if _global_aggregator is None:
        _global_aggregator = TrailAggregator()
    return _global_aggregator


def reset_trail_aggregator() -> None:
    """重置全局轨迹聚合器（仅测试使用）。"""
    global _global_aggregator
    _global_aggregator = None
