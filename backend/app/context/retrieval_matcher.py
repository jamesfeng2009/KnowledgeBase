"""
检索匹配检测器 — 检测检索结果是否与当前查询匹配。

检测方法：
    1. 对 query 和每篇 retrieved_doc 的 title+snippet 做 embedding
    2. 计算 cosine similarity
    3. top-1 文档相似度 < 阈值 → 检索不匹配

设计要点：
    - 复用 EmbeddingProvider，零额外 LLM Token
    - Embedder 不可用时跳过检测（优雅降级）
    - 不匹配时建议扩大检索范围（expand_retrieval）

遵循单一职责：本模块只负责匹配检测，不做检索策略调整。
遵循优雅降级：Embedder 不可用时返回 match=True，不阻断对话。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RetrievalMatchResult:
    """检索匹配检测结果。

    Attributes:
        is_match: 检索结果是否与查询匹配。
        match_score: top-1 文档与查询的相似度 [0.0, 1.0]。
        action: 建议动作 "expand_retrieval" / "none"。
    """

    is_match: bool
    match_score: float = 0.0
    action: str = "none"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "is_match": self.is_match,
            "match_score": round(self.match_score, 3),
            "action": self.action,
        }


class RetrievalMatcher:
    """检索匹配检测器 — 检测检索结果与查询的匹配度。

    使用方式::

        matcher = RetrievalMatcher(embedder)
        result = await matcher.check(query, retrieved_docs)
        if not result.is_match:
            # 扩大检索范围或标记低置信度
            ...
    """

    #: top-1 相似度低于此值 = 不匹配
    _MATCH_THRESHOLD: float = 0.3
    #: 文档内容截取字符数
    _DOC_SNIPPET_CHARS: int = 200

    def __init__(self, embedder: Any | None = None) -> None:
        """初始化检索匹配检测器。

        Args:
            embedder: EmbeddingProvider 实例，为 None 时跳过检测。
        """
        self._embedder = embedder

    async def check(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
    ) -> RetrievalMatchResult:
        """检测检索结果是否与查询匹配。

        Args:
            query: 当前用户查询（或 resolved_query）。
            retrieved_docs: 检索到的文档列表。

        Returns:
            RetrievalMatchResult: 匹配检测结果。
        """
        # 无文档或空查询 → 跳过（视为匹配）
        if not retrieved_docs or not query or not query.strip():
            return RetrievalMatchResult(is_match=True)

        embedder = await self._get_embedder()
        if embedder is None:
            return RetrievalMatchResult(is_match=True)

        try:
            # 构建待 embedding 的文本列表：query + 每篇文档的 title+snippet
            doc_texts = []
            for doc in retrieved_docs:
                title = doc.get("title", "")
                content = doc.get("content", doc.get("text", ""))
                snippet = (title + " " + content)[: self._DOC_SNIPPET_CHARS]
                doc_texts.append(snippet)

            all_texts = [query] + doc_texts
            vecs = await embedder.embed(all_texts)
            if len(vecs) < 2:
                return RetrievalMatchResult(is_match=True)

            query_vec = vecs[0]
            doc_vecs = vecs[1:]

            # 计算 query 与每篇文档的相似度，取 top-1
            max_sim = 0.0
            for doc_vec in doc_vecs:
                sim = self._cosine_similarity(query_vec, doc_vec)
                if sim > max_sim:
                    max_sim = sim

            if max_sim < self._MATCH_THRESHOLD:
                log.info(
                    "retrieval_matcher.mismatch",
                    match_score=round(max_sim, 3),
                    doc_count=len(retrieved_docs),
                )
                return RetrievalMatchResult(
                    is_match=False,
                    match_score=max_sim,
                    action="expand_retrieval",
                )

            return RetrievalMatchResult(
                is_match=True,
                match_score=max_sim,
                action="none",
            )
        except Exception as exc:
            log.warning("retrieval_matcher.check_failed", error=str(exc))
            return RetrievalMatchResult(is_match=True)

    async def _get_embedder(self) -> Any | None:
        """懒加载 Embedder — 首次调用时初始化。"""
        if self._embedder is not None:
            return self._embedder
        try:
            from app.llm.embedder import get_embedder

            self._embedder = get_embedder()
        except Exception as exc:
            log.debug("retrieval_matcher.embedder_unavailable", error=str(exc))
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
