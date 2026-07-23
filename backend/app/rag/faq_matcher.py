"""
FAQ 快捷匹配器 — 单一职责：在完整 RAG 检索前尝试 FAQ 精准匹配。

借鉴竞争对手的三级调度链路（Redis缓存 → FAQ精准匹配 → 向量语义检索），
在缓存未命中后、向量检索前插入 FAQ 快捷路径：

    TokenCache.get()  →  FAQMatcher.match()  →  HybridRetriever.search()
         (L1/L2)              (BM25 精准)            (向量+全文)

FAQ 来源：Chunker 已生成的 content_type="faq" chunk，以「问：X\n答：Y」
格式存储于向量索引中。FAQMatcher 通过 OpenSearch BM25 检索 faq 类型 chunk，
score 高于阈值时直接返回答案，跳过向量检索 + 重排 + 生成全链路。

收益：
    - 高频问答毫秒级响应（BM25 < 5ms vs 完整 RAG 链路 2-5s）；
    - 降低 LLM 调用成本（高频问题不走生成）；
    - 复用已有 FAQ chunk（doc_intelligence_service 自动生成），零数据迁移。

遵循单一职责：本模块只负责 FAQ 匹配，不涉及向量检索与生成。
遵循优雅降级：OpenSearch 不可用时返回 None，上层自动降级到向量检索。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# FAQ 检索超时（秒）— FAQ 匹配要求低延迟，超时即降级
_FAQ_SEARCH_TIMEOUT: float = 3.0
# BM25 score 阈值 — 高于此值认为 FAQ 精准命中
_FAQ_SCORE_THRESHOLD: float = 15.0
# OpenSearch 故障后的重试探测间隔（秒）
_FAQ_RETRY_INTERVAL: float = 30.0


@dataclass
class FAQMatchResult:
    """FAQ 匹配结果。

    Attributes:
        matched: 是否命中 FAQ。
        answer: 命中时的答案文本（从 chunk content 中提取）。
        score: BM25 分数。
        chunk_id: 命中的 chunk ID（溯源用）。
        doc_id: 命中的文档 ID（溯源用）。
    """

    matched: bool
    answer: str = ""
    score: float = 0.0
    chunk_id: str = ""
    doc_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "answer": self.answer,
            "score": round(self.score, 4),
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
        }


class FAQMatcher:
    """FAQ 快捷匹配器 — BM25 精准匹配 faq 类型 chunk。

    使用方式::

        matcher = FAQMatcher()
        result = await matcher.match("报销流程是什么？", kb_ids=[...])
        if result.matched:
            return result.answer  # 跳过完整 RAG
        # 否则走 HybridRetriever
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        score_threshold: float | None = None,
    ) -> None:
        from app.utils.retry import build_retry_http_client

        self._http: httpx.AsyncClient = http_client or build_retry_http_client(
            timeout=_FAQ_SEARCH_TIMEOUT
        )
        self._score_threshold: float = (
            score_threshold if score_threshold is not None else _FAQ_SCORE_THRESHOLD
        )
        self._opensearch_available: bool | None = None
        self._retry_at: float = 0.0

    async def match(
        self,
        query: str,
        kb_ids: list[str] | None = None,
    ) -> FAQMatchResult:
        """尝试 FAQ 精准匹配 — BM25 检索 faq 类型 chunk。

        Args:
            query: 用户查询文本。
            kb_ids: 可选，限定检索的知识库 ID 列表。

        Returns:
            FAQMatchResult — matched=True 时可直接使用 answer。
        """
        if not query or not query.strip():
            return FAQMatchResult(matched=False)

        # OpenSearch 降级检查
        if self._opensearch_available is False:
            if time.monotonic() < self._retry_at:
                return FAQMatchResult(matched=False)
            log.debug("faq_matcher.opensearch.retry_probe")

        # 构建 BM25 查询 — 仅检索 content_type="faq" 的 chunk
        url = f"{settings.OPENSEARCH_URL}/{settings.OPENSEARCH_INDEX}/_search"
        query_clause: dict[str, Any] = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["content", "title_path^2"],
                        }
                    }
                ],
                "filter": [
                    {"term": {"content_type": "faq"}},
                ],
            }
        }
        if kb_ids:
            query_clause["bool"]["filter"].append({"terms": {"kb_id": kb_ids}})

        payload: dict[str, Any] = {
            "size": 1,  # 只取 top-1
            "query": query_clause,
            "_source": ["doc_id", "chunk_id", "content", "title_path"],
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            data: Any = resp.json()
            self._opensearch_available = True
            self._retry_at = 0.0
        except Exception as exc:
            if self._opensearch_available is not False:
                log.warning("faq_matcher.opensearch.unavailable", error=str(exc))
            self._opensearch_available = False
            self._retry_at = time.monotonic() + _FAQ_RETRY_INTERVAL
            return FAQMatchResult(matched=False)

        # 解析结果
        hits: Any = (
            data.get("hits", {}).get("hits", []) if isinstance(data, dict) else []
        )
        if not hits:
            log.debug("faq_matcher.no_hits", query_len=len(query))
            return FAQMatchResult(matched=False)

        top_hit = hits[0]
        score = float(top_hit.get("_score", 0.0))
        source = top_hit.get("_source", {})

        if score < self._score_threshold:
            log.debug(
                "faq_matcher.below_threshold",
                score=score,
                threshold=self._score_threshold,
            )
            return FAQMatchResult(matched=False)

        # 从 faq chunk content 中提取答案部分
        content = str(source.get("content") or "")
        answer = _extract_answer(content)

        log.info(
            "faq_matcher.hit",
            score=score,
            chunk_id=source.get("chunk_id", ""),
            doc_id=source.get("doc_id", ""),
            answer_len=len(answer),
        )

        return FAQMatchResult(
            matched=True,
            answer=answer,
            score=score,
            chunk_id=str(source.get("chunk_id") or ""),
            doc_id=str(source.get("doc_id") or ""),
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._http.aclose()


def _extract_answer(faq_content: str) -> str:
    """从 FAQ chunk content 中提取答案部分。

    FAQ chunk 格式为「问：X\n\n答：Y」或「Q: X\nA: Y」，
    提取答部分返回。如果格式不匹配，返回原文。

    Args:
        faq_content: FAQ chunk 的完整内容。

    Returns:
        答案文本。
    """
    if not faq_content:
        return ""

    # 中文格式：问：... 答：...
    for separator in ["\n\n答：", "\n答：", "\n\n答:", "\n答:", "\n\nA:", "\nA:"]:
        idx = faq_content.find(separator)
        if idx != -1:
            answer = faq_content[idx + len(separator):].strip()
            if answer:
                return answer

    # 英文格式：Q: ... A: ...
    for separator in ["\n\nA:", "\nA:", "\n\nAnswer:", "\nAnswer:"]:
        idx = faq_content.find(separator)
        if idx != -1:
            answer = faq_content[idx + len(separator):].strip()
            if answer:
                return answer

    # 格式不匹配 — 返回原文（由上层处理）
    return faq_content.strip()
