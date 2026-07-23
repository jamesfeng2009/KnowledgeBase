"""
P4-A 漂移检测器单元测试。

覆盖：
    - DriftResult 数据结构
    - DriftDetector 规则检测（话题切换、实体切换、无焦点）
    - DriftDetector Embedding 检测（mock）
    - DriftDetector 置信度衰减
    - DriftDetector 优雅降级
    - reset() 状态重置
"""

import pytest

from app.context.drift_detector import DriftDetector, DriftResult
from app.context.focus_tracker import ConversationFocus


# ============================================================
# DriftResult
# ============================================================

class TestDriftResult:
    """DriftResult 数据结构测试。"""

    def test_to_dict_no_focus(self):
        result = DriftResult(is_drift=True, drift_score=0.8, detection_method="rule")
        d = result.to_dict()
        assert d["is_drift"] is True
        assert d["drift_score"] == 0.8
        assert d["detection_method"] == "rule"
        assert d["previous_focus"] is None

    def test_to_dict_with_focus(self):
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = DriftResult(
            is_drift=True, drift_score=0.9,
            previous_focus=focus,
            detection_method="embedding", action="reset_focus",
        )
        d = result.to_dict()
        assert d["is_drift"] is True
        assert d["previous_focus"]["topic"] == "限号政策"
        assert d["action"] == "reset_focus"


# ============================================================
# DriftDetector — 规则检测
# ============================================================

class TestDriftDetectorRule:
    """DriftDetector 规则检测测试。"""

    def setup_method(self):
        self.detector = DriftDetector(embedder=None)

    @pytest.mark.asyncio
    async def test_no_focus_no_drift(self):
        """无焦点 → 不检测漂移。"""
        result = await self.detector.check("任何问题", None)
        assert result.is_drift is False
        assert result.detection_method == "none"

    @pytest.mark.asyncio
    async def test_rule_topic_in_query_no_drift(self):
        """查询中包含焦点主题 → 不漂移。"""
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await self.detector.check("北京今天限号多少？", focus)
        assert result.is_drift is False
        assert result.detection_method == "rule"
        assert result.action == "keep_focus"

    @pytest.mark.asyncio
    async def test_rule_entity_in_query_no_drift(self):
        """查询中包含焦点实体 → 不漂移。"""
        focus = ConversationFocus(topic="天气", entity="上海")
        result = await self.detector.check("上海明天天气怎么样？", focus)
        assert result.is_drift is False
        assert result.detection_method == "rule"

    @pytest.mark.asyncio
    async def test_rule_topic_switch_drift(self):
        """查询中有不同话题关键词且不含焦点话题 → 漂移。"""
        focus = ConversationFocus(topic="限号政策", entity="北京")
        # 查询包含"天气"但不含"限号" → 规则判定漂移
        result = await self.detector.check("上海今天天气怎么样？", focus)
        assert result.is_drift is True
        assert result.detection_method == "rule"
        assert result.action == "reset_focus"

    @pytest.mark.asyncio
    async def test_rule_no_topic_keyword_passes_through(self):
        """查询无明确话题关键词 → 规则无法判断（交给下一级）。"""
        focus = ConversationFocus(topic="某话题", entity="某实体")
        # 无 embedder，embedding 检测返回 None，置信度不满足 → no drift
        result = await self.detector.check("帮我看看这个", focus)
        assert result.is_drift is False


# ============================================================
# DriftDetector — Embedding 检测
# ============================================================

class MockEmbedder:
    """Mock Embedder — 返回预设向量。"""

    def __init__(self, similarity: float = 0.5):
        """初始化 mock embedder。

        Args:
            similarity: 0.0-1.0，模拟两个向量的 cosine 相似度。
        """
        self._similarity = similarity

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回模拟向量。"""
        if self._similarity >= 0.99:
            # 完全相同向量
            return [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        elif self._similarity <= 0.01:
            # 完全正交向量
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        else:
            # 部分重叠向量
            # cosine = dot / (norm_a * norm_b)
            # 设 a = [1, x, 0], b = [x, 1, 0]
            # cosine = (x + x) / (sqrt(1+x^2) * sqrt(x^2+1)) = 2x / (1+x^2)
            # 解 x 使得 cosine = similarity
            # 2x / (1+x^2) = s → s*x^2 - 2x + s = 0 → x = (2 ± sqrt(4-4s^2)) / (2s)
            import math
            s = self._similarity
            if s < 0.01:
                return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
            discriminant = max(0, 4 - 4 * s * s)
            x = (2 - math.sqrt(discriminant)) / (2 * s)
            return [[1.0, x, 0.0], [x, 1.0, 0.0]]


class TestDriftDetectorEmbedding:
    """DriftDetector Embedding 检测测试。"""

    @pytest.mark.asyncio
    async def test_embedding_high_similarity_no_drift(self):
        """相似度 > 0.6 → 不漂移。"""
        embedder = MockEmbedder(similarity=0.8)
        detector = DriftDetector(embedder=embedder)
        focus = ConversationFocus(topic="某话题", entity="某实体")
        result = await detector.check("相关的问题", focus)
        assert result.is_drift is False
        assert result.detection_method == "embedding"
        assert result.action == "keep_focus"

    @pytest.mark.asyncio
    async def test_embedding_low_similarity_drift(self):
        """相似度 < 0.4 → 漂移。"""
        embedder = MockEmbedder(similarity=0.2)
        detector = DriftDetector(embedder=embedder)
        focus = ConversationFocus(topic="某话题", entity="某实体")
        result = await detector.check("完全不同的问题", focus)
        assert result.is_drift is True
        assert result.detection_method == "embedding"
        assert result.action == "reset_focus"

    @pytest.mark.asyncio
    async def test_embedding_medium_similarity_possible_drift(self):
        """相似度 0.4-0.6 → 可能漂移（expand_retrieval）。"""
        embedder = MockEmbedder(similarity=0.5)
        detector = DriftDetector(embedder=embedder)
        focus = ConversationFocus(topic="某话题", entity="某实体")
        result = await detector.check("部分相关的问题", focus)
        assert result.is_drift is False
        assert result.detection_method == "embedding"
        assert result.action == "expand_retrieval"


# ============================================================
# DriftDetector — 置信度衰减
# ============================================================

class TestDriftDetectorConfidence:
    """DriftDetector 置信度衰减测试。"""

    @pytest.mark.asyncio
    async def test_confidence_decay_drift(self):
        """连续 3 轮低置信度 → 漂移。"""
        detector = DriftDetector(embedder=None)
        focus_low = ConversationFocus(
            topic="某话题", entity="某实体", confidence=0.2,
        )
        # 模拟 3 轮低置信度
        # 规则无法判断 + 无 embedder → 更新 confidence streak
        for _ in range(3):
            await detector.check("模糊问题", focus_low)

        # 第 4 轮 → 置信度衰减触发
        result = await detector.check("又一个模糊问题", focus_low)
        assert result.is_drift is True
        assert result.detection_method == "confidence"
        assert result.action == "reset_focus"

    @pytest.mark.asyncio
    async def test_confidence_reset_on_high(self):
        """高置信度重置计数。"""
        detector = DriftDetector(embedder=None)
        focus_low = ConversationFocus(
            topic="某话题", entity="某实体", confidence=0.2,
        )
        focus_high = ConversationFocus(
            topic="某话题", entity="某实体", confidence=0.8,
        )
        # 2 轮低置信度
        for _ in range(2):
            await detector.check("模糊问题", focus_low)
        # 1 轮高置信度 → 重置
        await detector.check("模糊问题", focus_high)
        # 再 1 轮低置信度 → 不到 3 轮
        result = await detector.check("模糊问题", focus_low)
        assert result.is_drift is False


# ============================================================
# DriftDetector — 降级 & 重置
# ============================================================

class TestDriftDetectorDegradation:
    """DriftDetector 降级和重置测试。"""

    @pytest.mark.asyncio
    async def test_embedder_exception_degrade(self):
        """Embedder 异常 → 降级为规则。"""

        class FailingEmbedder:
            async def embed(self, texts):
                raise RuntimeError("embedder unavailable")

        detector = DriftDetector(embedder=FailingEmbedder())
        focus = ConversationFocus(topic="某话题", entity="某实体")
        result = await detector.check("相关问题", focus)
        # 规则无法判断 + embedding 失败 + 置信度不满足 → no drift
        assert result.is_drift is False

    def test_reset(self):
        """reset() 清空状态。"""
        detector = DriftDetector(embedder=None)
        detector._low_confidence_streak = 5
        detector.reset()
        assert detector._low_confidence_streak == 0
