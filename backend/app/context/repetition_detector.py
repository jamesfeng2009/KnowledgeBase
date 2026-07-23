"""
重复提问检测器 — 检测用户是否连续重复提问。

检测方法：
    1. 提取历史中的 user 消息
    2. 计算当前查询与最近 user 查询的 embedding 相似度
    3. cosine > 0.85 → 重复提问（说明上轮回答未满足需求）
    4. 连续重复 >= 2 次 → 建议扩大检索范围

设计要点：
    - 可复用 DriftDetector 已计算的 embedding（current_embedding 参数）
    - Embedder 不可用时跳过检测（优雅降级）
    - 重复提问时建议切换检索策略（expand_retrieval）

遵循单一职责：本模块只负责重复检测，不做检索策略调整。
遵循优雅降级：Embedder 不可用时返回非重复，不阻断对话。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RepetitionResult:
    """重复提问检测结果。

    Attributes:
        is_repetition: 是否检测到重复提问。
        similarity_score: 当前查询与最近查询的相似度 [0.0, 1.0]。
        previous_query: 最近的用户查询文本（无历史时为 None）。
        repetition_count: 连续重复次数。
        action: 建议动作 "expand_retrieval" / "none"。
    """

    is_repetition: bool
    similarity_score: float = 0.0
    previous_query: str | None = None
    repetition_count: int = 0
    action: str = "none"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "is_repetition": self.is_repetition,
            "similarity_score": round(self.similarity_score, 3),
            "previous_query": self.previous_query,
            "repetition_count": self.repetition_count,
            "action": self.action,
        }


class RepetitionDetector:
    """重复提问检测器 — 复用 embedding，零额外成本。

    使用方式::

        detector = RepetitionDetector(embedder)
        result = await detector.check("北京限号多少？", history)
        if result.is_repetition:
            # 上轮回答可能未满足需求，切换检索策略
            ...
    """

    #: cosine 相似度高于此值 = 重复
    _REPETITION_THRESHOLD: float = 0.85
    #: 连续重复次数达到此值 → 扩大检索
    _EXPAND_RETRIEVAL_COUNT: int = 2
    #: 历史窗口大小
    _HISTORY_WINDOW: int = 6

    def __init__(self, embedder: Any | None = None) -> None:
        """初始化重复提问检测器。

        Args:
            embedder: EmbeddingProvider 实例，为 None 时跳过检测。
        """
        self._embedder = embedder

    async def check(
        self,
        current_query: str,
        history: list[dict[str, str]],
        current_embedding: list[float] | None = None,
    ) -> RepetitionResult:
        """检测当前查询是否与最近的用户查询高度相似。

        Args:
            current_query: 当前用户查询。
            history: 对话历史 [{"role": "user/assistant", "content": "..."}]。
            current_embedding: 当前查询的 embedding（可选，复用 DriftDetector 的计算）。

        Returns:
            RepetitionResult: 重复检测结果。
        """
        # 提取历史中的 user 消息
        recent_user_msgs = [
            m for m in history[-self._HISTORY_WINDOW :]
            if m.get("role") == "user"
        ]
        if not recent_user_msgs:
            return RepetitionResult(is_repetition=False)

        # 排除当前查询（如果已存在于历史末尾）
        prev_msgs = list(recent_user_msgs)
        if prev_msgs and prev_msgs[-1].get("content", "") == current_query:
            prev_msgs = prev_msgs[:-1]

        if not prev_msgs:
            return RepetitionResult(is_repetition=False)

        last_query = prev_msgs[-1].get("content", "")
        if not last_query:
            return RepetitionResult(is_repetition=False)

        # 计算相似度
        similarity = await self._compute_similarity(
            current_query, last_query, current_embedding,
        )

        if similarity < self._REPETITION_THRESHOLD:
            return RepetitionResult(
                is_repetition=False,
                similarity_score=similarity,
                previous_query=last_query,
            )

        # 计算连续重复次数
        count = 1
        for msg in reversed(prev_msgs[:-1]):
            sim = await self._compute_similarity(current_query, msg.get("content", ""))
            if sim >= self._REPETITION_THRESHOLD:
                count += 1
            else:
                break

        action = "expand_retrieval" if count >= self._EXPAND_RETRIEVAL_COUNT else "none"

        log.info(
            "repetition_detector.detected",
            similarity=round(similarity, 3),
            count=count,
            action=action,
        )

        return RepetitionResult(
            is_repetition=True,
            similarity_score=similarity,
            previous_query=last_query,
            repetition_count=count,
            action=action,
        )

    async def _compute_similarity(
        self,
        query_a: str,
        query_b: str,
        query_a_embedding: list[float] | None = None,
    ) -> float:
        """计算两个查询的 cosine 相似度。

        Args:
            query_a: 查询 A。
            query_b: 查询 B。
            query_a_embedding: 查询 A 的预计算 embedding（可选，复用）。

        Returns:
            cosine 相似度 [0.0, 1.0]。
        """
        embedder = await self._get_embedder()
        if embedder is None:
            return 0.0

        try:
            if query_a_embedding is not None:
                # 复用 query_a 的 embedding，只需 embed query_b
                vecs = await embedder.embed([query_b])
                if not vecs:
                    return 0.0
                return self._cosine_similarity(query_a_embedding, vecs[0])
            else:
                vecs = await embedder.embed([query_a, query_b])
                if len(vecs) < 2:
                    return 0.0
                return self._cosine_similarity(vecs[0], vecs[1])
        except Exception as exc:
            log.warning("repetition_detector.similarity_failed", error=str(exc))
            return 0.0

    async def _get_embedder(self) -> Any | None:
        """懒加载 Embedder — 首次调用时初始化。"""
        if self._embedder is not None:
            return self._embedder
        try:
            from app.llm.embedder import get_embedder

            self._embedder = get_embedder()
        except Exception as exc:
            log.debug("repetition_detector.embedder_unavailable", error=str(exc))
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
