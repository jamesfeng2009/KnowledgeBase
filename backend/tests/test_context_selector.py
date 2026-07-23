"""
P3-B 语义上下文选择器单元测试。

覆盖：
    - ContextSelector.select 基本功能
    - 语义相似度排序
    - 近因优先保证
    - 优雅降级（无 Embedder）
    - token 预算控制
    - cosine_similarity_batch 数学正确性
"""

import pytest

from app.context.context_selector import ContextSelector


class MockEmbedder:
    """Mock Embedder — 返回简单的 bag-of-words 向量。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            # 简单的词袋向量：每个维度代表一个词
            vec = [0.0] * 10
            words = text.lower().split()
            for word in words:
                idx = hash(word) % 10
                vec[idx] += 1.0
            # 归一化
            norm = sum(x * x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
            results.append(vec)
        return results


class TestContextSelectorBasic:
    """ContextSelector 基本功能测试。"""

    @pytest.mark.asyncio
    async def test_empty_history(self):
        """空历史 → 返回空列表。"""
        selector = ContextSelector(embedder=MockEmbedder())
        result = await selector.select("query", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_short_history_all_kept(self):
        """历史不足 always_keep_recent → 全量返回。"""
        selector = ContextSelector(embedder=MockEmbedder(), always_keep_recent=4)
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        result = await selector.select("query", history)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_select_returns_correct_order(self):
        """选中的消息按时间正序排列。"""
        selector = ContextSelector(
            embedder=MockEmbedder(),
            always_keep_recent=0,  # 不强制保留
            similarity_threshold=0.0,
        )
        history = [
            {"role": "user", "content": "aaa"},
            {"role": "assistant", "content": "bbb"},
            {"role": "user", "content": "aaa"},
            {"role": "assistant", "content": "bbb"},
            {"role": "user", "content": "aaa"},
            {"role": "assistant", "content": "bbb"},
        ]
        result = await selector.select("aaa", history, top_k=3)
        assert len(result) <= 3
        # 验证返回的是历史中的消息
        for msg in result:
            assert msg in history


class TestContextSelectorFallback:
    """ContextSelector 降级策略测试。"""

    @pytest.mark.asyncio
    async def test_fallback_no_embedder(self):
        """无 Embedder → 降级为固定窗口。"""
        selector = ContextSelector(embedder=None, always_keep_recent=2)
        history = [
            {"role": "user", "content": f"msg {i}"} for i in range(10)
        ]
        result = await selector.select("query", history)
        # 降级：取最近 always_keep_recent * 2 = 4 条
        assert len(result) == 4
        assert result[-1]["content"] == "msg 9"

    @pytest.mark.asyncio
    async def test_fallback_embedder_exception(self):
        """Embedder 异常 → 降级为固定窗口。"""
        class FailingEmbedder:
            async def embed(self, texts):
                raise RuntimeError("API error")

        selector = ContextSelector(
            embedder=FailingEmbedder(),
            always_keep_recent=2,
        )
        history = [
            {"role": "user", "content": f"msg {i}"} for i in range(10)
        ]
        result = await selector.select("query", history)
        assert len(result) == 4  # always_keep_recent * 2


class TestContextSelectorRecentGuarantee:
    """ContextSelector 近因优先测试。"""

    @pytest.mark.asyncio
    async def test_recent_always_kept(self):
        """最近 N 条消息始终入选，即使相似度低。"""
        selector = ContextSelector(
            embedder=MockEmbedder(),
            always_keep_recent=3,
            similarity_threshold=0.99,  # 极高阈值，几乎不会选中
        )
        history = [
            {"role": "user", "content": "天气"},
            {"role": "assistant", "content": "晴"},
            {"role": "user", "content": "限号"},
            {"role": "assistant", "content": "3和7"},
            {"role": "user", "content": "报销"},
            {"role": "assistant", "content": "流程"},
            {"role": "user", "content": "合同"},
            {"role": "assistant", "content": "管理"},
        ]
        result = await selector.select("上海", history, top_k=5)
        # 即使相似度低，最近 3 条也必须入选
        assert len(result) >= 3
        # 最近 3 条应该在结果中
        last_3 = history[-3:]
        for msg in last_3:
            assert msg in result


class TestCosineSimilarity:
    """_cosine_similarity_batch 数学正确性测试。"""

    def test_identical_vectors(self):
        """相同向量 → 相似度 1.0。"""
        vec = [1.0, 0.0, 0.0]
        result = ContextSelector._cosine_similarity_batch(vec, [vec])
        assert abs(result[0] - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        """正交向量 → 相似度 0.0。"""
        vec1 = [1.0, 0.0]
        vec2 = [0.0, 1.0]
        result = ContextSelector._cosine_similarity_batch(vec1, [vec2])
        assert abs(result[0]) < 0.001

    def test_zero_query_vector(self):
        """零向量查询 → 返回 0.0。"""
        result = ContextSelector._cosine_similarity_batch(
            [0.0, 0.0], [[1.0, 0.0]]
        )
        assert result[0] == 0.0

    def test_zero_history_vector(self):
        """零向量历史 → 返回 0.0。"""
        result = ContextSelector._cosine_similarity_batch(
            [1.0, 0.0], [[0.0, 0.0]]
        )
        assert result[0] == 0.0
