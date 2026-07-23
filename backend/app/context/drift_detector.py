"""
漂移检测器 — 检测对话中话题是否发生漂移（主题切换）。

三级策略（按成本升序）：
    1. 规则检测：比较新旧焦点的 topic/entity 关键词域，零 Token
    2. Embedding 检测：计算当前查询与焦点主题的 cosine 相似度
    3. 置信度衰减：连续 N 轮焦点置信度低于阈值

设计要点：
    - 规则优先，Embedding 兜底（省 Token）
    - Embedder 不可用时降级为纯规则
    - 所有检测失败时返回 no_drift，沿用 P3 焦点继承

遵循单一职责：本模块只负责漂移检测，不做焦点提取或指代消解。
遵循优雅降级：Embedder/LLM 不可用时回退到规则或跳过，不阻断对话。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.context.focus_tracker import ConversationFocus
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class DriftResult:
    """漂移检测结果。

    Attributes:
        is_drift: 是否发生漂移。
        drift_score: 漂移分数 [0.0, 1.0]，越高越可能漂移。
        previous_focus: 漂移前的焦点（供 SSE 推送）。
        detection_method: 检测方法 "rule" / "embedding" / "confidence" / "none"。
        action: 建议动作 "reset_focus" / "keep_focus" / "expand_retrieval"。
    """

    is_drift: bool
    drift_score: float = 0.0
    previous_focus: ConversationFocus | None = None
    detection_method: str = "none"
    action: str = "keep_focus"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "is_drift": self.is_drift,
            "drift_score": round(self.drift_score, 3),
            "previous_focus": self.previous_focus.to_dict() if self.previous_focus else None,
            "detection_method": self.detection_method,
            "action": self.action,
        }


class DriftDetector:
    """漂移检测器 — 检测对话中话题是否发生漂移。

    使用方式::

        detector = DriftDetector(embedder)
        result = await detector.check("上海天气怎么样？", current_focus, history)
        if result.is_drift:
            # 重置焦点，重新提取
            ...

    策略优先级：规则检测（零 Token）→ Embedding 检测 → 置信度衰减。
    """

    #: cosine 相似度低于此值 = 漂移
    _DRIFT_SIMILARITY_THRESHOLD: float = 0.4
    #: 0.4-0.6 = 可能漂移
    _POSSIBLE_DRIFT_THRESHOLD: float = 0.6
    #: 连续低置信度轮数达到此值 = 漂移
    _CONFIDENCE_DECAY_ROUNDS: int = 3
    #: 低置信度阈值
    _LOW_CONFIDENCE_THRESHOLD: float = 0.4

    def __init__(self, embedder: Any | None = None) -> None:
        """初始化漂移检测器。

        Args:
            embedder: EmbeddingProvider 实例，为 None 时只用规则检测。
        """
        self._embedder = embedder
        self._low_confidence_streak: int = 0

    async def check(
        self,
        query: str,
        current_focus: ConversationFocus | None,
        history: list[dict[str, str]] | None = None,
    ) -> DriftResult:
        """检测当前查询是否发生话题漂移。

        Args:
            query: 当前用户查询。
            current_focus: 当前对话焦点（来自 TopicTracker）。
            history: 对话历史（可选，用于置信度衰减检测）。

        Returns:
            DriftResult: 漂移检测结果。
        """
        # 无焦点 → 无法检测漂移（首轮对话）
        if current_focus is None:
            return DriftResult(
                is_drift=False, drift_score=0.0,
                detection_method="none", action="keep_focus",
            )

        # Level 1: 规则检测 — 话题关键词域比较
        rule_result = self._rule_check(query, current_focus)
        if rule_result is not None:
            self._update_confidence_streak(rule_result.is_drift, current_focus)
            return rule_result

        # Level 2: Embedding 检测 — cosine 相似度
        embed_result = await self._embedding_check(query, current_focus)
        if embed_result is not None:
            self._update_confidence_streak(embed_result.is_drift, current_focus)
            return embed_result

        # 规则和 Embedding 都无法判断 → 更新置信度计数
        self._update_confidence_streak(False, current_focus)

        # Level 3: 置信度衰减 — 连续低置信度
        decay_result = self._confidence_decay_check(current_focus)
        if decay_result is not None:
            return decay_result

        # 所有检测失败 → 不漂移，沿用 P3 焦点继承
        return DriftResult(
            is_drift=False, drift_score=0.0,
            previous_focus=current_focus,
            detection_method="none", action="keep_focus",
        )

    def _rule_check(
        self, query: str, focus: ConversationFocus,
    ) -> DriftResult | None:
        """规则检测 — 比较查询与焦点的话题关键词域。

        检测逻辑：
        - 查询中包含焦点 topic/entity → 不漂移
        - 查询中包含明显不同的话题关键词 → 漂移
        - 无法判断 → 返回 None（交给下一级）
        """
        query_lower = query.lower()

        # 查询中包含焦点主题或实体 → 不漂移
        focus_terms = {focus.topic.lower(), focus.entity.lower()}
        focus_terms.discard("")
        for term in focus_terms:
            if term and term in query_lower:
                return DriftResult(
                    is_drift=False, drift_score=0.1,
                    previous_focus=focus,
                    detection_method="rule", action="keep_focus",
                )

        # 查询中包含明显不同的话题关键词 → 漂移
        # 常见话题关键词域（与 focus_tracker 一致）
        topic_keywords = {
            "天气", "限号", "限行", "报销", "请假", "合同", "采购",
            "天气", "weather", "限号政策", "限行政策",
        }
        query_has_topic = any(kw in query_lower for kw in topic_keywords)
        focus_has_topic = any(
            kw in focus.topic.lower() or kw in focus.entity.lower()
            for kw in topic_keywords
        )

        if query_has_topic and focus_has_topic:
            # 两者都有明确话题关键词，但焦点话题不在查询中 → 漂移
            focus_topic_in_query = focus.topic.lower() in query_lower
            if not focus_topic_in_query:
                return DriftResult(
                    is_drift=True, drift_score=0.8,
                    previous_focus=focus,
                    detection_method="rule", action="reset_focus",
                )

        # 规则无法判断 → 交给 Embedding
        return None

    async def _embedding_check(
        self, query: str, focus: ConversationFocus,
    ) -> DriftResult | None:
        """Embedding 检测 — cosine 相似度。

        计算当前查询与焦点上下文的 embedding 相似度。
        """
        embedder = await self._get_embedder()
        if embedder is None:
            return None

        try:
            focus_text = f"{focus.topic} {focus.entity} {focus.intent}"
            vecs = await embedder.embed([query, focus_text])
            if len(vecs) < 2:
                return None

            similarity = self._cosine_similarity(vecs[0], vecs[1])

            if similarity < self._DRIFT_SIMILARITY_THRESHOLD:
                return DriftResult(
                    is_drift=True, drift_score=1.0 - similarity,
                    previous_focus=focus,
                    detection_method="embedding", action="reset_focus",
                )
            elif similarity < self._POSSIBLE_DRIFT_THRESHOLD:
                return DriftResult(
                    is_drift=False, drift_score=1.0 - similarity,
                    previous_focus=focus,
                    detection_method="embedding", action="expand_retrieval",
                )
            else:
                return DriftResult(
                    is_drift=False, drift_score=1.0 - similarity,
                    previous_focus=focus,
                    detection_method="embedding", action="keep_focus",
                )
        except Exception as exc:
            log.warning("drift_detector.embedding_failed", error=str(exc))
            return None

    def _confidence_decay_check(
        self, focus: ConversationFocus,
    ) -> DriftResult | None:
        """置信度衰减检测 — 连续低置信度轮数。"""
        if self._low_confidence_streak >= self._CONFIDENCE_DECAY_ROUNDS:
            return DriftResult(
                is_drift=True, drift_score=0.7,
                previous_focus=focus,
                detection_method="confidence", action="reset_focus",
            )
        return None

    def _update_confidence_streak(
        self, is_drift: bool, focus: ConversationFocus,
    ) -> None:
        """更新低置信度连续计数。"""
        if focus.confidence < self._LOW_CONFIDENCE_THRESHOLD:
            self._low_confidence_streak += 1
        else:
            self._low_confidence_streak = 0

    async def _get_embedder(self) -> Any | None:
        """懒加载 Embedder — 首次调用时初始化。"""
        if self._embedder is not None:
            return self._embedder
        try:
            from app.llm.embedder import get_embedder

            self._embedder = get_embedder()
        except Exception as exc:
            log.debug("drift_detector.embedder_unavailable", error=str(exc))
        return self._embedder

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """计算两个向量的 cosine 相似度。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def reset(self) -> None:
        """重置检测器状态 — 供测试使用。"""
        self._low_confidence_streak = 0
