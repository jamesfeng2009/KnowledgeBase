"""P4-D 检索匹配检测器测试。"""

import math

import pytest

from app.context.retrieval_matcher import RetrievalMatcher, RetrievalMatchResult


class MockEmbedder:
    """Mock Embedder — 根据文本生成可控相似度的向量。"""

    def __init__(self, query_vec: list[float], doc_vecs: list[list[float]]):
        self._query_vec = query_vec
        self._doc_vecs = doc_vecs
        self._call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for i, text in enumerate(texts):
            if i == 0:
                result.append(self._query_vec)
            else:
                # 按顺序返回预设的文档向量
                idx = min(i - 1, len(self._doc_vecs) - 1)
                result.append(self._doc_vecs[idx])
        return result


class FailingEmbedder:
    """Mock Embedder — 总是抛异常。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embedding API error")


def _make_vecs(cosine_sim: float, dim: int = 10) -> tuple[list[float], list[float]]:
    """生成两个向量，使其 cosine 相似度接近目标值。

    使用公式: vec_a = [1, 0, 0, ...], vec_b = [s, sqrt(1-s²), 0, ...]
    cosine = s
    """
    vec_a = [1.0] + [0.0] * (dim - 1)
    vec_b = [cosine_sim] + [math.sqrt(max(0, 1 - cosine_sim**2))] + [0.0] * (dim - 2)
    return vec_a, vec_b


class TestRetrievalMatchResult:
    """RetrievalMatchResult 序列化测试。"""

    def test_to_dict_match(self):
        result = RetrievalMatchResult(is_match=True, match_score=0.85, action="none")
        d = result.to_dict()
        assert d["is_match"] is True
        assert d["match_score"] == 0.85
        assert d["action"] == "none"

    def test_to_dict_mismatch(self):
        result = RetrievalMatchResult(is_match=False, match_score=0.15, action="expand_retrieval")
        d = result.to_dict()
        assert d["is_match"] is False
        assert d["action"] == "expand_retrieval"


class TestRetrievalMatcherCheck:
    """RetrievalMatcher.check 测试。"""

    @pytest.mark.asyncio
    async def test_match_high_similarity(self):
        """相似度 > 0.3 → 匹配。"""
        query_vec, doc_vec = _make_vecs(0.8)
        embedder = MockEmbedder(query_vec, [doc_vec])
        matcher = RetrievalMatcher(embedder)

        result = await matcher.check("北京限号政策", [
            {"title": "限号政策", "content": "北京今天限行尾号3和7"},
        ])

        assert result.is_match is True
        assert result.match_score > 0.3
        assert result.action == "none"

    @pytest.mark.asyncio
    async def test_mismatch_low_similarity(self):
        """相似度 < 0.3 → 不匹配。"""
        query_vec, doc_vec = _make_vecs(0.1)
        embedder = MockEmbedder(query_vec, [doc_vec])
        matcher = RetrievalMatcher(embedder)

        result = await matcher.check("北京限号政策", [
            {"title": "天气预报", "content": "今天晴天25度"},
        ])

        assert result.is_match is False
        assert result.match_score < 0.3
        assert result.action == "expand_retrieval"

    @pytest.mark.asyncio
    async def test_no_docs_skip(self):
        """无文档 → 跳过（视为匹配）。"""
        matcher = RetrievalMatcher(MockEmbedder([1.0], [[1.0]]))
        result = await matcher.check("查询", [])
        assert result.is_match is True

    @pytest.mark.asyncio
    async def test_empty_query_skip(self):
        """空查询 → 跳过。"""
        matcher = RetrievalMatcher(MockEmbedder([1.0], [[1.0]]))
        result = await matcher.check("", [{"title": "doc", "content": "content"}])
        assert result.is_match is True

    @pytest.mark.asyncio
    async def test_embedder_exception_degrade(self):
        """Embedder 异常 → 优雅降级（视为匹配）。"""
        matcher = RetrievalMatcher(FailingEmbedder())
        result = await matcher.check("查询", [{"title": "doc", "content": "content"}])
        assert result.is_match is True

    @pytest.mark.asyncio
    async def test_no_embedder_degrade(self):
        """无 Embedder → 优雅降级（视为匹配）。"""
        matcher = RetrievalMatcher(embedder=None)
        result = await matcher.check("查询", [{"title": "doc", "content": "content"}])
        assert result.is_match is True

    @pytest.mark.asyncio
    async def test_multiple_docs_top1(self):
        """多文档时取 top-1 相似度。"""
        query_vec = [1.0, 0.0, 0.0]
        # doc1: sim=0.1, doc2: sim=0.9 → top-1 = 0.9 → match
        doc_vecs = [
            [0.1, 0.995, 0.0],  # low similarity
            [0.9, 0.436, 0.0],  # high similarity
        ]
        embedder = MockEmbedder(query_vec, doc_vecs)
        matcher = RetrievalMatcher(embedder)

        result = await matcher.check("查询", [
            {"title": "doc1", "content": "内容1"},
            {"title": "doc2", "content": "内容2"},
        ])

        assert result.is_match is True
        assert result.match_score > 0.3

    @pytest.mark.asyncio
    async def test_multiple_docs_all_mismatch(self):
        """多文档全部不匹配 → 不匹配。"""
        query_vec = [1.0, 0.0, 0.0]
        doc_vecs = [
            [0.05, 0.999, 0.0],
            [0.1, 0.995, 0.0],
        ]
        embedder = MockEmbedder(query_vec, doc_vecs)
        matcher = RetrievalMatcher(embedder)

        result = await matcher.check("查询", [
            {"title": "doc1", "content": "内容1"},
            {"title": "doc2", "content": "内容2"},
        ])

        assert result.is_match is False
        assert result.action == "expand_retrieval"

    @pytest.mark.asyncio
    async def test_doc_with_text_field(self):
        """文档使用 text 字段而非 content。"""
        query_vec, doc_vec = _make_vecs(0.8)
        embedder = MockEmbedder(query_vec, [doc_vec])
        matcher = RetrievalMatcher(embedder)

        result = await matcher.check("查询", [
            {"title": "doc", "text": "内容内容内容"},
        ])

        assert result.is_match is True
