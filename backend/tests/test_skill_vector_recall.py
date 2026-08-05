"""
P0-1 Skill embedding 向量召回单元测试。

覆盖：
    - SkillRegistry.build_embeddings / get_embedding / 索引重建后清空
    - SkillFinder.afind_relevant_skills 向量 + 关键词融合排序
    - 语义盲区补齐（"报销怎么走" → "费用审批流程"类技能）
    - 优雅降级（embedder 异常 / 未预计算 / 零向量）
    - 零回归语义：无命中 fallback 全量
    - 20 条同义表述 query 改造前后命中率对比（验证标准）
"""

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.rag.skill_finder import SkillFinder, _cosine
from app.rag.skill_registry import SkillRegistry


# ======================================================================
# 辅助：Mock Server + 概念空间 Mock Embedder
# ======================================================================

_SKILLS: list[dict[str, Any]] = [
    {
        "name": "knowledge_search",
        "category": "search",
        "tags": ["全文检索", "知识库", "搜索", "search", "文档"],
        "description": "在企业知识库中按关键词进行全文检索，返回匹配的文档列表。",
    },
    {
        "name": "document_get",
        "category": "document",
        "tags": ["文档", "详情", "查看", "document", "get"],
        "description": "获取指定文档的详细信息，包括标题、内容、状态、密级等字段。",
    },
    {
        "name": "document_create",
        "category": "document",
        "tags": ["文档", "创建", "新建", "create", "写入", "draft"],
        "description": "在指定知识库中创建新文档，文档初始状态为 draft 草稿。",
    },
    {
        "name": "query_oa_approval",
        "category": "workflow",
        "tags": ["OA", "审批", "流程", "查询", "approval", "单据"],
        "description": "查询 OA 系统的审批流程状态，包括当前审批节点、提交人、审批意见等信息。",
    },
    {
        "name": "create_it_ticket",
        "category": "workflow",
        "tags": ["IT", "工单", "创建", "ticket", "服务台", "报修"],
        "description": "创建 IT 服务台工单，支持设置优先级（low/normal/high/urgent）。",
    },
]

# 概念空间维度 — 模拟真实 embedder 的语义聚合能力：
# 同义表述映射到同一概念维度，关键词通道无法覆盖的语义由向量通道补齐
_CONCEPTS: dict[str, list[str]] = {
    "search_docs": ["搜索", "检索", "查找", "找", "search", "全文"],
    "doc_detail": ["详情", "查看", "detail", "内容"],
    "doc_create": ["创建", "新建", "写入", "create", "draft", "草稿"],
    "oa_approval": ["审批", "报销", "流程", "费用", "oa", "approval", "单据", "差旅"],
    "it_ticket": ["工单", "报修", "it", "ticket", "服务台", "故障", "修"],
}
_DIMS: list[str] = list(_CONCEPTS.keys())


class MockConceptEmbedder:
    """概念空间 Mock Embedder — 按关键词命中映射到概念维度向量。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vec = [
                float(sum(lowered.count(term) for term in _CONCEPTS[dim]))
                for dim in _DIMS
            ]
            vectors.append(vec)
        return vectors


class FailingEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder 不可用")


def _make_registry() -> SkillRegistry:
    server = MagicMock()
    server.get_skill_index.return_value = _SKILLS
    registry = SkillRegistry()
    registry.load_from_server(server)
    return registry


# ======================================================================
# _cosine
# ======================================================================

class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_safe(self):
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_dimension_mismatch_safe(self):
        assert _cosine([1.0], [1.0, 2.0]) == 0.0


# ======================================================================
# SkillRegistry 向量预计算
# ======================================================================

class TestBuildEmbeddings:
    @pytest.mark.asyncio
    async def test_build_embeddings_populates(self):
        registry = _make_registry()
        count = await registry.build_embeddings(MockConceptEmbedder())
        assert count == len(_SKILLS)
        assert registry.get_embedding("knowledge_search") is not None
        # 搜索类技能在 search_docs 维度上有值
        assert registry.get_embedding("knowledge_search")[0] > 0

    @pytest.mark.asyncio
    async def test_embeddings_cleared_on_reload(self):
        """索引重建后旧向量失效（需重新预计算）。"""
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        assert registry.get_all_embeddings()
        registry.load_from_server(MagicMock(get_skill_index=lambda: _SKILLS))
        assert registry.get_all_embeddings() == {}

    @pytest.mark.asyncio
    async def test_build_embeddings_failure_graceful(self):
        """embedder 异常 → 返回 0，不影响关键词通道。"""
        registry = _make_registry()
        count = await registry.build_embeddings(FailingEmbedder())
        assert count == 0
        assert registry.get_all_embeddings() == {}


# ======================================================================
# afind_relevant_skills — 向量 + 关键词融合
# ======================================================================

class TestVectorRecall:
    @pytest.mark.asyncio
    async def test_semantic_blind_spot_rescued(self):
        """核心场景："报销怎么走" 关键词通道语义盲区 → 向量通道补齐召回 query_oa_approval。

        注意：单字"报"会误命中 create_it_ticket 的 tag"报修"（关键词通道
        噪声），但正确的 query_oa_approval 关键词零命中 — 这正是向量
        通道要补齐的语义盲区。
        """
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)

        # 改造前（纯关键词）：正确技能未被召回（语义盲区）
        keyword_matched = finder.find_relevant_skills("报销怎么走")
        assert "query_oa_approval" not in keyword_matched

        # 改造后（向量融合）：语义命中审批技能
        fused_matched = await finder.afind_relevant_skills(
            "报销怎么走", embedder=MockConceptEmbedder()
        )
        assert "query_oa_approval" in fused_matched
        assert len(fused_matched) < len(registry.get_all_names())

    @pytest.mark.asyncio
    async def test_fallback_preserved_when_no_hit(self):
        """零回归语义：向量通道也无命中 → fallback 全量。"""
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)
        matched = await finder.afind_relevant_skills(
            "xyzqwerty123", embedder=MockConceptEmbedder()
        )
        assert matched == registry.get_all_names()

    @pytest.mark.asyncio
    async def test_no_embedder_degrades_to_keyword(self):
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)
        fused = await finder.afind_relevant_skills("搜索文档", embedder=None)
        assert "knowledge_search" in fused

    @pytest.mark.asyncio
    async def test_embedder_failure_degrades_to_keyword(self):
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)
        fused = await finder.afind_relevant_skills("搜索文档", embedder=FailingEmbedder())
        assert "knowledge_search" in fused

    @pytest.mark.asyncio
    async def test_embeddings_not_built_degrades_to_keyword(self):
        registry = _make_registry()  # 未预计算
        finder = SkillFinder(registry, match_threshold=5)
        fused = await finder.afind_relevant_skills("搜索文档", embedder=MockConceptEmbedder())
        assert "knowledge_search" in fused

    @pytest.mark.asyncio
    async def test_keyword_hit_ranking_preserved(self):
        """关键词强命中仍排首位（向量加分不喧宾夺主）。"""
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)
        matched = await finder.afind_relevant_skills(
            "创建 IT 工单报修", embedder=MockConceptEmbedder()
        )
        assert matched[0] == "create_it_ticket"


# ======================================================================
# 20 条同义表述 query 命中率对比（todo 验证标准）
# ======================================================================

# (query, 期望召回技能) — 每个技能 4 条同义表述，
# 其中标注 semantic 的 query 关键词通道无命中
_SYNONYMOUS_QUERIES: list[tuple[str, str]] = [
    # knowledge_search
    ("搜索知识库中关于 Python 的文档", "knowledge_search"),
    ("全文检索一下请假制度", "knowledge_search"),
    ("search documents in knowledge base", "knowledge_search"),
    ("帮我找找差旅标准相关的资料", "knowledge_search"),
    # document_get
    ("帮我查看这个文档的详情", "document_get"),
    ("get document details", "document_get"),
    ("看一下这篇文档的内容", "document_get"),
    ("文档的密级和状态信息", "document_get"),
    # document_create
    ("我要创建一个新文档", "document_create"),
    ("create a new draft document", "document_create"),
    ("新建一篇知识库文章", "document_create"),
    ("帮我写入一份会议纪要", "document_create"),
    # query_oa_approval
    ("查询我的 OA 审批流程状态", "query_oa_approval"),
    ("报销怎么走", "query_oa_approval"),
    ("差旅报销需要什么流程", "query_oa_approval"),
    ("费用单据现在谁在处理", "query_oa_approval"),
    # create_it_ticket
    ("我要创建一个 IT 工单报修", "create_it_ticket"),
    ("create an IT ticket", "create_it_ticket"),
    ("电脑坏了找谁修", "create_it_ticket"),
    ("打印机故障需要服务台支持", "create_it_ticket"),
]


class TestSynonymousQueryHitRate:
    """构造 20 条同义表述 query 对比改造前后 Top-K 命中率。"""

    @pytest.mark.asyncio
    async def test_hit_rate_comparison(self):
        registry = _make_registry()
        await registry.build_embeddings(MockConceptEmbedder())
        finder = SkillFinder(registry, match_threshold=5)
        embedder = MockConceptEmbedder()

        def _hit(matched: list[str], expected: str) -> bool:
            # fallback 全量不算精确命中（技能未被有效发现）
            if matched == registry.get_all_names():
                return False
            return expected in matched

        keyword_hits = 0
        fused_hits = 0
        missed: list[str] = []
        for query, expected in _SYNONYMOUS_QUERIES:
            kw_matched = finder.find_relevant_skills(query)
            fused_matched = await finder.afind_relevant_skills(query, embedder=embedder)
            if _hit(kw_matched, expected):
                keyword_hits += 1
            if _hit(fused_matched, expected):
                fused_hits += 1
            else:
                missed.append(query)

        total = len(_SYNONYMOUS_QUERIES)
        # 向量通道不劣化关键词通道
        assert fused_hits >= keyword_hits, (
            f"融合后命中率下降: {fused_hits}/{total} < {keyword_hits}/{total}, "
            f"漏召: {missed}"
        )
        # 20 条同义 query 融合后全量命中（语义盲区被补齐）
        assert fused_hits == total, f"融合后仍有漏召: {missed}"
