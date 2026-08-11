"""
Span Sampler — LangFuse span 采样策略（P2-9）。

设计目标：
    高流量场景下 LangFuse SaaS 配额易耗尽，且海量正常 span 淹没故障信号。
    采样策略保证"故障可追溯"与"成本可控"两个目标同时成立：

        优先级（高 → 低）：
            1. 根 Span（task.run）     → 强制上报（Trace 锚点，不可丢）
            2. error Span（metadata.error 非空）→ 强制上报（故障证据）
            3. 正常 Span              → 按采样率随机

    仅作用于 LangFuse 双写分支（"看"：实时观测），
    本地 SpanRecord（"算"：评测消费）不采样 — 评测需要完整轨迹数据。

接入点（langfuse_tracer.py）：
    - TraceContext.end_span()   — 节点 Span 闭合时
    - TraceContext.span()       — 一次性记录已闭合 Span 时

决策语义：
    - 采样关闭（LANGFUSE_SAMPLING_ENABLED=False）→ 所有 span 上报（向后兼容）
    - 采样开启 + 正常 span → random() < rate 才上报
    - 采样开启 + error span + FORCE_ERROR=True → 强制上报
    - 采样开启 + root span → 强制上报

线程安全：random 模块线程安全，SpanSampler 无可变状态（配置在 __init__ 冻结）。

遵循单一职责：只做采样决策，不构造 span 数据。
遵循依赖倒置：所有阈值从 app.config.get_settings() 获取。
"""

from __future__ import annotations

import random
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


class SpanSampler:
    """LangFuse span 采样器 — 决定 span 是否上报到 LangFuse。

    Attributes:
        sampling_enabled: 采样总开关。False 时所有 span 都上报（向后兼容）。
        sampling_rate: 正常 span 采样率（0.0-1.0）。仅在 sampling_enabled=True 时生效。
        force_error: error span 强制不采样开关。True 时 error span 始终上报，
                     即使 sampling_enabled=False（独立于采样总开关）。
                     注意：sampling_enabled=False 时本来所有 span 都上报，
                     force_error 主要在采样开启场景下兜底 error span。
    """

    def __init__(
        self,
        sampling_enabled: bool = False,
        sampling_rate: float = 0.1,
        force_error: bool = True,
    ) -> None:
        """初始化采样器。

        Args:
            sampling_enabled: 采样总开关。
            sampling_rate: 正常 span 采样率 [0.0, 1.0]。
            force_error: error span 强制上报开关。
        """
        self._enabled = sampling_enabled
        # 钳制到 [0.0, 1.0]，防御配置错误（config 层已有 validator，此处兜底）
        self._rate = max(0.0, min(1.0, sampling_rate))
        self._force_error = force_error
        # 采样计数 — 供监控/日志消费（非线程安全，仅用于观测，容忍误差）
        self._sampled_count: int = 0
        self._dropped_count: int = 0
        self._forced_error_count: int = 0
        self._forced_root_count: int = 0

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """采样是否启用。"""
        return self._enabled

    @property
    def rate(self) -> float:
        """正常 span 采样率。"""
        return self._rate

    @property
    def force_error(self) -> bool:
        """error span 是否强制上报。"""
        return self._force_error

    def should_sample(
        self,
        metadata: dict[str, Any] | None = None,
        is_root: bool = False,
        is_error: bool | None = None,
    ) -> bool:
        """决定 span 是否上报到 LangFuse。

        决策优先级（高 → 低）：
            1. 根 Span（is_root=True）→ 强制 True（Trace 锚点）
            2. error Span（is_error=True 或 metadata.error 非空）→ force_error 开启时 True
            3. 采样关闭 → True（向后兼容，所有 span 上报）
            4. 正常 Span + 采样开启 → random() < rate

        Args:
            metadata: span 的 metadata（用于检测 error 字段）。
            is_root: 是否为根 Span（task.run）。根 Span 强制上报。
            is_error: 显式标记是否为 error span。None 时从 metadata.error 推断。

        Returns:
            True=上报 LangFuse，False=丢弃（仅不写 LangFuse，不影响本地 SpanRecord）。
        """
        # 1. 根 Span 强制上报 — Trace 锚点不可丢
        if is_root:
            self._forced_root_count += 1
            return True

        # 推断 error 状态 — 显式标记优先，否则查 metadata.error
        if is_error is None:
            is_error = bool(metadata and metadata.get("error"))

        # 2. error span 强制上报 — 故障证据不可丢
        if is_error and self._force_error:
            self._forced_error_count += 1
            return True

        # 3. 采样关闭 → 所有 span 上报（向后兼容）
        if not self._enabled:
            self._sampled_count += 1
            return True

        # 4. 正常 span + 采样开启 → 按采样率随机
        if random.random() < self._rate:
            self._sampled_count += 1
            return True

        self._dropped_count += 1
        return False

    # ------------------------------------------------------------------
    # 统计与诊断
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """返回采样统计信息（用于监控和日志）。

        Returns:
            含 enabled / rate / force_error / 各类计数器的字典。
        """
        return {
            "enabled": self._enabled,
            "rate": self._rate,
            "force_error": self._force_error,
            "sampled_count": self._sampled_count,
            "dropped_count": self._dropped_count,
            "forced_error_count": self._forced_error_count,
            "forced_root_count": self._forced_root_count,
        }

    def reset(self) -> None:
        """重置计数器（新一轮 Trace 或测试时调用）。"""
        self._sampled_count = 0
        self._dropped_count = 0
        self._forced_error_count = 0
        self._forced_root_count = 0


# ======================================================================
# 模块级单例 — 从 config 读取配置，避免每个 TraceContext 重复初始化
# ======================================================================

_default_sampler: SpanSampler | None = None


def get_default_sampler() -> SpanSampler:
    """获取全局默认 SpanSampler 单例。

    首次调用时从 app.config 读取 LangFuse 采样配置，决定采样策略。
    后续调用复用单例。

    config 读取失败时默认禁用采样（向后兼容，所有 span 上报）。
    """
    global _default_sampler
    if _default_sampler is not None:
        return _default_sampler

    enabled = False  # 保守默认：读取失败时禁用采样（全上报）
    rate = 0.1
    force_error = True
    try:
        from app.config import get_settings

        settings = get_settings()
        enabled = bool(getattr(settings, "LANGFUSE_SAMPLING_ENABLED", False))
        rate = float(getattr(settings, "LANGFUSE_SAMPLING_RATE", 0.1))
        force_error = bool(getattr(settings, "LANGFUSE_SAMPLING_FORCE_ERROR", True))
    except Exception as exc:
        log.warning("span_sampler.config_read_error", error=str(exc), default="disabled")

    _default_sampler = SpanSampler(
        sampling_enabled=enabled,
        sampling_rate=rate,
        force_error=force_error,
    )
    log.info(
        "span_sampler.initialized",
        enabled=enabled,
        rate=rate,
        force_error=force_error,
    )
    return _default_sampler


def reset_default_sampler() -> None:
    """重置全局单例（测试用，生产代码不应调用）。

    下次 get_default_sampler() 会重新从 config 读取。
    """
    global _default_sampler
    _default_sampler = None
