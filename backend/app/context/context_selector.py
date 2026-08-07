"""
语义上下文选择器 — 根据当前查询相关性筛选历史消息。

替换当前的"固定16条窗口全量注入"策略：
    1. 向量化当前查询和每条历史消息
    2. 余弦相似度排序
    3. 取 top_k 最相关消息
    4. 保证最近 2 轮始终入选（近因优先）
    5. 总 token 不超过预算

遵循优雅降级：Embedder 不可用时回退到固定窗口策略。
"""

from __future__ import annotations

import math
from typing import Any

from app.llm.embedder import EmbeddingProvider
from app.utils.logger import get_logger

log = get_logger(__name__)


class ContextSelector:
    """语义上下文选择器 — 向量相似度筛选历史消息。

    使用方式::

        selector = ContextSelector(embedder)
        selected = await selector.select("上海限号", history, top_k=5)
    """

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        max_tokens: int = 800,
        always_keep_recent: int = 4,
        similarity_threshold: float = 0.3,
    ) -> None:
        """初始化语义选择器。

        Args:
            embedder: EmbeddingProvider，为 None 时降级为固定窗口。
            max_tokens: 选中历史的总 token 预算。
            always_keep_recent: 始终保留最近 N 条消息（近因优先）。
            similarity_threshold: 相似度低于此值的历史不选。
        """
        self._embedder = embedder
        self._max_tokens = max_tokens
        self._always_keep_recent = always_keep_recent
        self._similarity_threshold = similarity_threshold

    async def select(
        self,
        query: str,
        history: list[dict[str, str]],
        top_k: int = 5,
    ) -> list[dict[str, str]]:
        """从历史消息中选择与当前查询语义相关的消息。

        Args:
            query: 当前用户查询（消解后）。
            history: 完整对话历史。
            top_k: 最多选择的消息条数。

        Returns:
            选中的消息列表（按时间正序排列）。
        """
        if not history:
            return []

        # 历史不足时全量返回
        if len(history) <= self._always_keep_recent:
            return list(history)

        try:
            embedder = await self._get_embedder()
            if embedder is None:
                return self._fallback_select(history)

            # 向量化查询和历史消息
            query_vec = (await embedder.embed([query]))[0]
            history_texts = [
                f"{m.get('role', 'user')}: {m.get('content', '')[:200]}" for m in history
            ]
            history_vecs = await embedder.embed(history_texts)

            # 计算余弦相似度
            similarities = self._cosine_similarity_batch(query_vec, history_vecs)

            # 排序：相似度高的优先
            scored = list(enumerate(similarities))
            scored.sort(key=lambda x: x[1], reverse=True)

            selected_indices: set[int] = set()
            total_tokens = 0

            # 1. 近因优先：最近 N 条始终保留，但条数受 top_k 约束、
            #    token 先从预算扣除（避免无条件并入突破预算/top_k 上限）
            recent_count = min(self._always_keep_recent, top_k)
            recent_start = len(history) - recent_count
            for i in range(recent_start, len(history)):
                selected_indices.add(i)
                total_tokens += len(history_texts[i]) // 3  # 粗估 token

            # 2. 再选相似度高的（超过阈值的），受剩余 top_k 名额与预算约束
            for idx, sim in scored:
                if len(selected_indices) >= top_k:
                    break
                if idx in selected_indices:
                    continue  # 已被近因保留
                if sim < self._similarity_threshold:
                    continue
                msg_tokens = len(history_texts[idx]) // 3  # 粗估 token
                if total_tokens + msg_tokens > self._max_tokens:
                    continue
                selected_indices.add(idx)
                total_tokens += msg_tokens

            # 3. 按时间正序排列
            result = [history[i] for i in sorted(selected_indices)]

            log.info(
                "context_selector.selected",
                total_history=len(history),
                selected=len(result),
                top_similarity=max(similarities) if similarities else 0.0,
            )
            return result

        except Exception as exc:
            log.warning("context_selector.select_failed", error=str(exc))
            return self._fallback_select(history)

    def _fallback_select(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """降级策略：固定窗口（取最近 N 条）。"""
        return list(history[-self._always_keep_recent * 2:])

    async def _get_embedder(self) -> EmbeddingProvider | None:
        """懒初始化 Embedder。"""
        if self._embedder is not None:
            return self._embedder
        try:
            from app.llm.embedder import get_embedder

            self._embedder = get_embedder()
        except Exception as exc:
            log.debug("context_selector.embedder_unavailable", error=str(exc))
        return self._embedder

    @staticmethod
    def _cosine_similarity_batch(
        query_vec: list[float],
        history_vecs: list[list[float]],
    ) -> list[float]:
        """批量计算余弦相似度。"""
        query_norm = math.sqrt(sum(x * x for x in query_vec))
        if query_norm == 0:
            return [0.0] * len(history_vecs)
        results: list[float] = []
        for vec in history_vecs:
            vec_norm = math.sqrt(sum(x * x for x in vec))
            if vec_norm == 0:
                results.append(0.0)
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec))
            results.append(dot / (query_norm * vec_norm))
        return results
