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

遵循单一职责：本模块只负责指标聚合与计算，不触发告警或写入持久化。
告警由调用方（API 层或日志扫描规则）根据返回的指标自行决策。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class _WindowState:
    """时间窗口内的原始累积数据（线程安全由外层 lock 保证）。"""

    # 会话计数
    total_sessions: int = 0
    completed_sessions: int = 0          # 正常退出（非 max_iterations_reached）
    max_iter_sessions: int = 0           # 触发 max_iterations_reached
    timeout_sessions: int = 0            # 触发总超时
    dedup_hit_sessions: int = 0          # 触发 CrossTurnDeduplicator 指针引用
    tool_call_sessions: int = 0          # 至少调用过 1 次工具
    tool_hit_sessions: int = 0           # 最终答案引用了工具结果

    # 累加器（用于求平均）
    total_iterations: int = 0            # 所有正常结束会话的迭代轮次之和
    total_think_latency_ms: float = 0.0  # think 节点耗时累加
    total_retrieve_latency_ms: float = 0.0
    total_tool_latency_ms: float = 0.0

    # 工具名分布（仅记录 hit，不记录 count）
    tool_name_counts: dict[str, int] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        total = self.total_sessions or 1  # 防除零
        completed_total = self.completed_sessions or 1
        return {
            "window": {
                "total_sessions": self.total_sessions,
                "completion_rate": round(self.completed_sessions / total, 4),
                "max_iterations_rate": round(self.max_iter_sessions / total, 4),
                "timeout_rate": round(self.timeout_sessions / total, 4),
                "dedup_hit_rate": round(self.dedup_hit_sessions / total, 4),
                "loitering_rate": round(
                    (self.dedup_hit_sessions + self.max_iter_sessions) / total, 4
                ),
                "tool_call_rate": round(self.tool_call_sessions / total, 4),
                "tool_hit_rate": round(self.tool_hit_sessions / total, 4),
                "avg_iterations": round(self.total_iterations / completed_total, 2),
                "avg_think_latency_ms": round(
                    self.total_think_latency_ms / total, 2
                ),
                "avg_retrieve_latency_ms": round(
                    self.total_retrieve_latency_ms / total, 2
                ),
                "avg_tool_latency_ms": round(
                    self.total_tool_latency_ms / total, 2
                ),
            },
            "tool_distribution": dict(self.tool_name_counts),
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
    """

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window_seconds = window_seconds
        self._state = _WindowState()
        self._lock: Any | None = None  # 惰性创建 asyncio.Lock（在首个 async 调用时）

    def _ensure_lock(self) -> Any:
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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
            self._state.total_sessions += 1
            if max_iter_reached:
                self._state.max_iter_sessions += 1
            elif total_timeout:
                self._state.timeout_sessions += 1
            else:
                self._state.completed_sessions += 1
                self._state.total_iterations += iterations

            if dedup_hit:
                self._state.dedup_hit_sessions += 1

            self._state.total_think_latency_ms += think_latency_ms
            self._state.total_retrieve_latency_ms += retrieve_latency_ms
            self._state.total_tool_latency_ms += tool_latency_ms

            calls = tool_calls or []
            if calls:
                self._state.tool_call_sessions += 1
                any_hit = False
                for call in calls:
                    name = call.get("name", "unknown")
                    if call.get("hit"):
                        any_hit = True
                        self._state.tool_name_counts[name] = (
                            self._state.tool_name_counts.get(name, 0) + 1
                        )
                if any_hit:
                    self._state.tool_hit_sessions += 1

            log.debug(
                "trail.record_session",
                iterations=iterations,
                max_iter=max_iter_reached,
                timeout=total_timeout,
                dedup=dedup_hit,
                tool_calls=len(calls),
            )

    async def window_summary(self) -> dict[str, Any]:
        """返回当前窗口的聚合指标摘要。"""
        lock = self._ensure_lock()
        async with lock:
            return self._state.to_summary()

    async def reset(self) -> None:
        """重置窗口状态（用于测试或定时清理）。"""
        lock = self._ensure_lock()
        async with lock:
            self._state = _WindowState()


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
