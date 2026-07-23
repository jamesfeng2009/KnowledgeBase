"""P4-G 重复提问检测器测试。"""

import math

import pytest

from app.context.repetition_detector import RepetitionDetector, RepetitionResult


class MockEmbedder:
    """Mock Embedder — 根据文本映射返回预设向量。"""

    def __init__(self, vec_map: dict[str, list[float]]):
        self._vec_map = vec_map

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec_map.get(text, [0.0] * 10) for text in texts]


class FailingEmbedder:
    """Mock Embedder — 总是抛异常。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embedding API error")


def _make_vecs(cosine_sim: float, dim: int = 10) -> tuple[list[float], list[float]]:
    """生成两个向量，使其 cosine 相似度接近目标值。"""
    vec_a = [1.0] + [0.0] * (dim - 1)
    vec_b = [cosine_sim] + [math.sqrt(max(0, 1 - cosine_sim**2))] + [0.0] * (dim - 2)
    return vec_a, vec_b


class TestRepetitionResult:
    """RepetitionResult 序列化测试。"""

    def test_to_dict_repetition(self):
        result = RepetitionResult(
            is_repetition=True,
            similarity_score=0.92,
            previous_query="北京限号多少",
            repetition_count=2,
            action="expand_retrieval",
        )
        d = result.to_dict()
        assert d["is_repetition"] is True
        assert d["similarity_score"] == 0.92
        assert d["previous_query"] == "北京限号多少"
        assert d["repetition_count"] == 2
        assert d["action"] == "expand_retrieval"

    def test_to_dict_no_repetition(self):
        result = RepetitionResult(is_repetition=False)
        d = result.to_dict()
        assert d["is_repetition"] is False
        assert d["repetition_count"] == 0
        assert d["action"] == "none"


class TestRepetitionDetectorCheck:
    """RepetitionDetector.check 测试。"""

    @pytest.mark.asyncio
    async def test_high_similarity_repetition(self):
        """高相似度 (>0.85) → 重复。"""
        vec_a, vec_b = _make_vecs(0.9)
        embedder = MockEmbedder({"q1": vec_a, "q2": vec_b})
        detector = RepetitionDetector(embedder)

        result = await detector.check("q1", [
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "回复"},
        ])

        assert result.is_repetition is True
        assert result.similarity_score > 0.85
        assert result.previous_query == "q2"
        assert result.repetition_count >= 1

    @pytest.mark.asyncio
    async def test_low_similarity_no_repetition(self):
        """低相似度 (<0.85) → 非重复。"""
        vec_a, vec_b = _make_vecs(0.3)
        embedder = MockEmbedder({"q1": vec_a, "q2": vec_b})
        detector = RepetitionDetector(embedder)

        result = await detector.check("q1", [
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "回复"},
        ])

        assert result.is_repetition is False
        assert result.similarity_score < 0.85

    @pytest.mark.asyncio
    async def test_consecutive_repetition_expand(self):
        """连续重复 2 次以上 → expand_retrieval。"""
        vec_a, vec_b = _make_vecs(0.95)
        # q1, q2, q3 都高度相似
        embedder = MockEmbedder({
            "current": vec_a,
            "prev1": vec_b,
            "prev2": vec_b,
        })
        detector = RepetitionDetector(embedder)

        result = await detector.check("current", [
            {"role": "user", "content": "prev2"},
            {"role": "assistant", "content": "回复1"},
            {"role": "user", "content": "prev1"},
            {"role": "assistant", "content": "回复2"},
        ])

        assert result.is_repetition is True
        assert result.repetition_count >= 2
        assert result.action == "expand_retrieval"

    @pytest.mark.asyncio
    async def test_single_repetition_no_expand(self):
        """单次重复（count=1）→ action="none"。"""
        vec_a, vec_b = _make_vecs(0.9)
        embedder = MockEmbedder({"current": vec_a, "prev": vec_b})
        detector = RepetitionDetector(embedder)

        result = await detector.check("current", [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "回复"},
            # prev2 不相似，打断连续
            {"role": "user", "content": "完全不同的话题"},
            {"role": "assistant", "content": "回复2"},
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "回复3"},
        ])

        # prev 与 current 相似，但 prev 前面的消息不相似 → count=1
        assert result.is_repetition is True
        assert result.action == "none"

    @pytest.mark.asyncio
    async def test_no_history(self):
        """无历史 → 非重复。"""
        detector = RepetitionDetector(MockEmbedder({}))
        result = await detector.check("查询", [])
        assert result.is_repetition is False

    @pytest.mark.asyncio
    async def test_no_user_messages(self):
        """历史中无 user 消息 → 非重复。"""
        detector = RepetitionDetector(MockEmbedder({}))
        result = await detector.check("查询", [
            {"role": "assistant", "content": "回复"},
        ])
        assert result.is_repetition is False

    @pytest.mark.asyncio
    async def test_embedder_exception_degrade(self):
        """Embedder 异常 → 优雅降级（非重复）。"""
        detector = RepetitionDetector(FailingEmbedder())
        result = await detector.check("查询", [
            {"role": "user", "content": "查询"},
            {"role": "assistant", "content": "回复"},
        ])
        assert result.is_repetition is False

    @pytest.mark.asyncio
    async def test_no_embedder_degrade(self):
        """无 Embedder → 优雅降级（非重复）。"""
        detector = RepetitionDetector(embedder=None)
        result = await detector.check("查询", [
            {"role": "user", "content": "查询"},
            {"role": "assistant", "content": "回复"},
        ])
        assert result.is_repetition is False

    @pytest.mark.asyncio
    async def test_current_query_in_history_excluded(self):
        """当前查询已在历史末尾时排除自身。"""
        vec_a, vec_b = _make_vecs(0.9)
        embedder = MockEmbedder({"current": vec_a, "prev": vec_b})
        detector = RepetitionDetector(embedder)

        result = await detector.check("current", [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "current"},  # 当前查询已在历史中
        ])

        # 应与 "prev" 比较，而非与 "current" 自身比较
        assert result.previous_query == "prev"

    @pytest.mark.asyncio
    async def test_reuse_current_embedding(self):
        """复用 current_embedding 参数 — 零额外 embed(query) 调用。"""
        vec_a, vec_b = _make_vecs(0.9)
        embedder = MockEmbedder({"prev": vec_b})
        detector = RepetitionDetector(embedder)

        result = await detector.check(
            "current",
            [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "回复"}],
            current_embedding=vec_a,  # 复用预计算的 embedding
        )

        assert result.is_repetition is True
        assert result.similarity_score > 0.85
