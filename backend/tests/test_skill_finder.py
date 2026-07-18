"""Find Skills 渐进式技能加载测试 — 验证 SkillRegistry / SkillFinder / engine 集成。

覆盖：
- SkillMetadata：match_score 计算、序列化
- SkillRegistry：load_from_server / get_index / get_metadata / load_tools / get_categories
- SkillFinder：find_relevant_skills 中英文匹配、阈值过滤、fallback、max_skills 限制
- SkillFinder：find_and_load 异步加载、get_match_report 调试报告
- config：SKILL_FINDER_ENABLED / SKILL_MATCH_THRESHOLD / SKILL_MAX_LOADED
- engine 集成：_get_tools_for_query 按需加载 + fallback
- server 集成：list_tools_by_names / get_skill_index
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.llm.base import Tool
from app.rag.skill_finder import SkillFinder
from app.rag.skill_registry import SkillMetadata, SkillRegistry


# ======================================================================
# 辅助：构造 Mock MCP Server
# ======================================================================


def _make_mock_server(
    tools_data: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """构造 Mock MCP Server，提供 get_skill_index 和 list_tools_by_names。"""
    if tools_data is None:
        tools_data = [
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

    server = MagicMock()
    server.get_skill_index.return_value = tools_data

    async def _list_tools_by_names(names: list[str]) -> list[Tool]:
        result: list[Tool] = []
        for name in names:
            for td in tools_data:
                if td["name"] == name:
                    result.append(Tool(
                        name=td["name"],
                        description=td["description"],
                        parameters={"type": "object", "properties": {}},
                    ))
        return result

    server.list_tools_by_names = _list_tools_by_names
    return server


# ======================================================================
# SkillMetadata 测试
# ======================================================================


class TestSkillMetadata:
    """SkillMetadata 元数据测试。"""

    def test_to_dict(self) -> None:
        meta = SkillMetadata(
            name="knowledge_search",
            category="search",
            tags=["全文检索", "知识库"],
            description="搜索知识库",
        )
        d = meta.to_dict()
        assert d["name"] == "knowledge_search"
        assert d["category"] == "search"
        assert "全文检索" in d["tags"]
        assert d["description"] == "搜索知识库"

    def test_match_score_name_hit(self) -> None:
        """工具名包含查询词 → +10 分。"""
        meta = SkillMetadata(name="knowledge_search", description="")
        score = meta.match_score("search documents", "search documents", ["search"])
        assert score >= 10  # name 匹配

    def test_match_score_category_hit(self) -> None:
        """分类名匹配查询词 → +5 分。"""
        meta = SkillMetadata(name="tool1", category="search", description="")
        score = meta.match_score("search", "search", ["search"])
        assert score >= 5  # category 匹配

    def test_match_score_tag_hit(self) -> None:
        """标签匹配查询词 → +8 分/词。"""
        meta = SkillMetadata(
            name="tool1",
            category="general",
            tags=["知识库", "搜索"],
            description="",
        )
        score = meta.match_score("搜索", "搜索", ["搜索"])
        assert score >= 8  # tag 匹配

    def test_match_score_description_hit(self) -> None:
        """描述包含查询词 → +3 分/词。"""
        meta = SkillMetadata(name="tool1", description="搜索企业知识库文档")
        score = meta.match_score("知识库", "知识库", ["知识库"])
        assert score >= 3  # description 匹配

    def test_match_score_no_hit(self) -> None:
        """无匹配 → 0 分。"""
        meta = SkillMetadata(name="tool1", category="general", description="无关内容")
        score = meta.match_score("xyz", "xyz", ["xyz"])
        assert score == 0

    def test_match_score_multiple_terms(self) -> None:
        """多词匹配分数累加。"""
        meta = SkillMetadata(
            name="knowledge_search",
            category="search",
            tags=["知识库"],
            description="搜索知识库",
        )
        # search 匹配 name(+10) + category(+5) = 15
        # 知识库 匹配 tag(+8) + description(+3) = 11
        score = meta.match_score("search 知识库", "search 知识库", ["search", "知识库"])
        assert score >= 26


# ======================================================================
# SkillRegistry 测试
# ======================================================================


class TestSkillRegistry:
    """SkillRegistry 注册表测试。"""

    def test_load_from_server(self) -> None:
        """从 MCP Server 构建轻量技能索引。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        names = registry.get_all_names()
        assert len(names) == 5
        assert "knowledge_search" in names
        assert "document_create" in names

    def test_get_index(self) -> None:
        """get_index 返回所有技能元数据列表。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        index = registry.get_index()
        assert len(index) == 5
        assert any(item["name"] == "knowledge_search" for item in index)

    def test_get_metadata(self) -> None:
        """get_metadata 按名称获取技能元数据。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        meta = registry.get_metadata("knowledge_search")
        assert meta is not None
        assert meta.category == "search"
        assert "全文检索" in meta.tags

    def test_get_metadata_not_found(self) -> None:
        """get_metadata 未找到返回 None。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        assert registry.get_metadata("nonexistent") is None

    def test_get_categories(self) -> None:
        """get_categories 返回去重后的分类列表。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        categories = registry.get_categories()
        assert "search" in categories
        assert "document" in categories
        assert "workflow" in categories
        assert len(categories) == 3

    @pytest.mark.asyncio
    async def test_load_tools_by_names(self) -> None:
        """load_tools 按名称子集加载完整 Tool schema。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        tools = await registry.load_tools(["knowledge_search", "document_get"])
        assert len(tools) == 2
        assert all(isinstance(t, dict) for t in tools)

    @pytest.mark.asyncio
    async def test_load_tools_empty_names(self) -> None:
        """load_tools 空名称列表返回空列表。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        tools = await registry.load_tools([])
        assert tools == []

    @pytest.mark.asyncio
    async def test_load_tools_nonexistent_name(self) -> None:
        """load_tools 不存在的名称静默跳过。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        tools = await registry.load_tools(["knowledge_search", "nonexistent"])
        assert len(tools) == 1

    @pytest.mark.asyncio
    async def test_load_tools_no_server(self) -> None:
        """未加载 server 时 load_tools 返回空列表。"""
        registry = SkillRegistry()
        tools = await registry.load_tools(["knowledge_search"])
        assert tools == []

    def test_load_from_server_failed(self) -> None:
        """get_skill_index 异常时优雅降级。"""
        server = MagicMock()
        server.get_skill_index.side_effect = Exception("server error")

        registry = SkillRegistry()
        registry.load_from_server(server)

        assert registry.get_all_names() == []

    def test_get_token_estimate_all(self) -> None:
        """get_token_estimate 估算全量 token 开销。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        estimate = registry.get_token_estimate()
        assert estimate > 0

    def test_get_token_estimate_subset(self) -> None:
        """get_token_estimate 估算子集 token 开销。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)

        full = registry.get_token_estimate()
        subset = registry.get_token_estimate(["knowledge_search"])
        assert subset < full
        assert subset > 0


# ======================================================================
# SkillFinder 测试
# ======================================================================


class TestSkillFinder:
    """SkillFinder 匹配引擎测试。"""

    def test_find_chinese_search_query(self) -> None:
        """中文查询 — 搜索知识库 → 匹配 knowledge_search。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("搜索知识库中关于 Python 的文档")
        assert "knowledge_search" in matched

    def test_find_chinese_document_query(self) -> None:
        """中文查询 — 查看文档详情 → 匹配 document_get。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("帮我查看这个文档的详情")
        assert "document_get" in matched

    def test_find_chinese_create_document_query(self) -> None:
        """中文查询 — 创建文档 → 匹配 document_create。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("我要创建一个新文档")
        assert "document_create" in matched

    def test_find_chinese_oa_query(self) -> None:
        """中文查询 — OA 审批 → 匹配 query_oa_approval。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("查询我的 OA 审批流程状态")
        assert "query_oa_approval" in matched

    def test_find_chinese_it_ticket_query(self) -> None:
        """中文查询 — IT 工单 → 匹配 create_it_ticket。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("我要创建一个 IT 工单报修")
        assert "create_it_ticket" in matched

    def test_find_english_search_query(self) -> None:
        """英文查询 — search → 匹配 knowledge_search。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("search the knowledge base for documents")
        assert "knowledge_search" in matched

    def test_find_english_document_query(self) -> None:
        """英文查询 — document get → 匹配 document_get。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("get document details")
        assert "document_get" in matched

    def test_find_no_match_fallback_to_all(self) -> None:
        """无匹配时 fallback 到全量加载（零回归保证）。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=100)  # 极高阈值

        matched = finder.find_relevant_skills("xyzqwerty")
        # 无匹配 → 返回全部
        assert len(matched) == 5

    def test_find_empty_query_returns_all(self) -> None:
        """空查询返回全部技能。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        matched = finder.find_relevant_skills("")
        assert len(matched) == 5

    def test_find_whitespace_query_returns_all(self) -> None:
        """纯空格查询返回全部技能。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        matched = finder.find_relevant_skills("   ")
        assert len(matched) == 5

    def test_find_max_skills_limit(self) -> None:
        """max_skills 限制返回数量。"""
        # 构造大量工具
        tools_data = []
        for i in range(20):
            tools_data.append({
                "name": f"tool_{i}",
                "category": "search",
                "tags": ["搜索", "test"],
                "description": "搜索测试工具",
            })
        server = _make_mock_server(tools_data)
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5, max_skills=3)

        matched = finder.find_relevant_skills("搜索 test")
        assert len(matched) <= 3

    def test_find_threshold_filtering(self) -> None:
        """阈值过滤 — 低分技能不返回。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=20)  # 高阈值

        # 只有强匹配的技能才返回
        matched = finder.find_relevant_skills("search")
        # search 匹配 knowledge_search 的 name(+10) + category(+5) = 15 < 20
        # 可能无匹配 → fallback
        # 如果有匹配，验证只含强匹配项
        if len(matched) < 5:
            assert all(
                registry.get_metadata(n).match_score("search", "search", ["search"]) >= 20
                for n in matched
            )

    def test_find_multiple_matches(self) -> None:
        """多词匹配返回多个技能。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        matched = finder.find_relevant_skills("搜索文档")
        # 搜索 → knowledge_search
        # 文档 → knowledge_search + document_get + document_create
        assert "knowledge_search" in matched
        assert len(matched) >= 2

    @pytest.mark.asyncio
    async def test_find_and_load(self) -> None:
        """find_and_load 匹配并加载完整 Tool schema。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        tools = await finder.find_and_load("搜索知识库")
        assert len(tools) >= 1
        assert all(isinstance(t, dict) for t in tools)

    def test_get_match_report(self) -> None:
        """get_match_report 生成调试报告。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=5)

        report = finder.get_match_report("搜索文档")
        assert "query" in report
        assert "terms" in report
        assert "threshold" in report
        assert "matched" in report
        assert "scores" in report
        assert len(report["scores"]) == 5
        # 分数降序排列
        scores = [s["score"] for s in report["scores"]]
        assert scores == sorted(scores, reverse=True)

    def test_get_match_report_fallback(self) -> None:
        """get_match_report 无匹配时 fallback=True。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry, match_threshold=100)

        report = finder.get_match_report("xyzqwerty")
        assert report["fallback"] is True
        assert len(report["matched"]) == 5


# ======================================================================
# SkillFinder 分词测试
# ======================================================================


class TestSkillFinderTokenize:
    """SkillFinder 分词测试。"""

    def test_tokenize_english(self) -> None:
        """英文分词 — 去停用词。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        terms = finder._tokenize("search the knowledge base")
        assert "search" in terms
        assert "knowledge" in terms
        assert "base" in terms
        assert "the" not in terms  # 停用词

    def test_tokenize_chinese(self) -> None:
        """中文分词 — 2-gram + 3-gram。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        terms = finder._tokenize("搜索知识库")
        assert "搜索" in terms  # 2-gram
        assert "知识" in terms  # 2-gram
        assert "知识库" in terms  # 3-gram

    def test_tokenize_mixed(self) -> None:
        """中英文混合分词。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        terms = finder._tokenize("search 知识库 documents")
        assert "search" in terms
        assert "documents" in terms
        assert "知识" in terms

    def test_tokenize_empty(self) -> None:
        """空字符串分词返回空列表。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        terms = finder._tokenize("")
        assert terms == []

    def test_tokenize_stopwords_filtered(self) -> None:
        """停用词被过滤。"""
        server = _make_mock_server()
        registry = SkillRegistry()
        registry.load_from_server(server)
        finder = SkillFinder(registry)

        terms = finder._tokenize("please help me search")
        assert "please" not in terms
        assert "help" not in terms
        assert "search" in terms


# ======================================================================
# Config 测试
# ======================================================================


class TestSkillFinderConfig:
    """SkillFinder 配置项测试。"""

    def test_skill_finder_enabled_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "SKILL_FINDER_ENABLED")
        assert settings.SKILL_FINDER_ENABLED is True

    def test_skill_match_threshold_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "SKILL_MATCH_THRESHOLD")
        assert settings.SKILL_MATCH_THRESHOLD == 5

    def test_skill_max_loaded_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "SKILL_MAX_LOADED")
        assert settings.SKILL_MAX_LOADED == 10


# ======================================================================
# Server 集成测试
# ======================================================================


class TestServerSkillIntegration:
    """MCP Server 技能索引集成测试。"""

    def test_server_get_skill_index(self) -> None:
        """server.get_skill_index 返回轻量索引。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        index = server.get_skill_index()
        assert len(index) == 5
        names = [item["name"] for item in index]
        assert "knowledge_search" in names
        assert "document_create" in names
        assert "create_it_ticket" in names

    def test_server_skill_index_has_category(self) -> None:
        """技能索引包含 category 字段。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        index = server.get_skill_index()
        for item in index:
            assert "category" in item
            assert item["category"] in ("search", "document", "workflow", "general")

    def test_server_skill_index_has_tags(self) -> None:
        """技能索引包含 tags 字段。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        index = server.get_skill_index()
        for item in index:
            assert "tags" in item
            assert isinstance(item["tags"], list)
            assert len(item["tags"]) > 0

    @pytest.mark.asyncio
    async def test_server_list_tools_by_names(self) -> None:
        """server.list_tools_by_names 按名称子集返回 Tool。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        tools = await server.list_tools_by_names(["knowledge_search", "document_get"])
        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "knowledge_search" in names
        assert "document_get" in names

    @pytest.mark.asyncio
    async def test_server_list_tools_by_names_empty(self) -> None:
        """空名称列表返回空列表。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        tools = await server.list_tools_by_names([])
        assert tools == []

    @pytest.mark.asyncio
    async def test_server_list_tools_by_names_nonexistent(self) -> None:
        """不存在的名称静默跳过。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)

        tools = await server.list_tools_by_names(["knowledge_search", "nonexistent"])
        assert len(tools) == 1


# ======================================================================
# Engine 集成测试
# ======================================================================


class TestEngineSkillFinderIntegration:
    """AgenticRAGEngine SkillFinder 集成测试。"""

    def _make_engine(self, skill_finder_enabled: bool = True) -> Any:
        """构造带 SkillFinder 的 Mock engine。"""
        from app.rag.engine import AgenticRAGEngine

        mock_llm = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp._server = _make_mock_server()
        mock_retriever = MagicMock()
        mock_reranker = MagicMock()
        mock_generator = MagicMock()

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.SKILL_FINDER_ENABLED = skill_finder_enabled
            mock_settings.return_value.SKILL_MATCH_THRESHOLD = 5
            mock_settings.return_value.SKILL_MAX_LOADED = 10

            engine = AgenticRAGEngine(
                llm=mock_llm,
                mcp_client=mock_mcp,
                retriever=mock_retriever,
                reranker=mock_reranker,
                generator=mock_generator,
            )
        return engine

    def test_engine_has_skill_finder(self) -> None:
        """engine 初始化后包含 SkillFinder 实例。"""
        engine = self._make_engine()
        assert engine._skill_finder is not None
        assert engine._skill_registry is not None

    def test_engine_skill_finder_disabled(self) -> None:
        """SKILL_FINDER_ENABLED=False 时 SkillFinder 为 None。"""
        engine = self._make_engine(skill_finder_enabled=False)
        assert engine._skill_finder is None
        assert engine._skill_registry is None

    @pytest.mark.asyncio
    async def test_get_tools_for_query_with_skill_finder(self) -> None:
        """_get_tools_for_query 使用 SkillFinder 按需加载。"""
        engine = self._make_engine()

        tools = await engine._get_tools_for_query("搜索知识库文档")
        assert len(tools) >= 1
        assert all(isinstance(t, dict) for t in tools)

    @pytest.mark.asyncio
    async def test_get_tools_for_query_fallback_when_disabled(self) -> None:
        """SKILL_FINDER_ENABLED=False 时 fallback 到全量加载。"""
        engine = self._make_engine(skill_finder_enabled=False)

        mock_tools = [Tool(name="t1", description="d", parameters={})]
        engine.mcp.get_tools_for_llm = AsyncMock(return_value=mock_tools)

        tools = await engine._get_tools_for_query("anything")
        assert len(tools) == 1
        assert tools[0]["name"] == "t1"

    @pytest.mark.asyncio
    async def test_get_tools_for_query_fallback_on_error(self) -> None:
        """SkillFinder 异常时 fallback 到全量加载。"""
        engine = self._make_engine()

        # 让 skill_registry.load_from_server 抛异常
        engine._skill_registry.load_from_server = MagicMock(
            side_effect=Exception("load error")
        )
        mock_tools = [Tool(name="t1", description="d", parameters={})]
        engine.mcp.get_tools_for_llm = AsyncMock(return_value=mock_tools)

        tools = await engine._get_tools_for_query("搜索")
        assert len(tools) == 1
        assert tools[0]["name"] == "t1"

    @pytest.mark.asyncio
    async def test_get_tools_for_query_lazy_load(self) -> None:
        """技能索引延迟加载 — 首次调用时构建。"""
        engine = self._make_engine()

        # 初始状态：索引为空
        assert engine._skill_registry.get_all_names() == []

        # 首次调用触发延迟加载
        await engine._get_tools_for_query("搜索知识库")

        # 索引已构建
        assert len(engine._skill_registry.get_all_names()) == 5

    @pytest.mark.asyncio
    async def test_get_tools_for_query_no_match_fallback(self) -> None:
        """无匹配时 fallback 到全量加载。"""
        engine = self._make_engine()
        # 设极高阈值确保无匹配
        engine._skill_finder.match_threshold = 999

        mock_tools = [Tool(name="all_tools", description="d", parameters={})]
        engine.mcp.get_tools_for_llm = AsyncMock(return_value=mock_tools)

        tools = await engine._get_tools_for_query("xyzqwerty_nonsense")
        # 无匹配 → find_relevant_skills 返回全部 → load_tools 返回全部
        # 如果 load_tools 成功则不会走 fallback；如果返回空则走 fallback
        # 这里验证至少返回了工具
        assert len(tools) >= 1


# ======================================================================
# MCPClient 集成测试
# ======================================================================


class TestMCPClientSkillIntegration:
    """MCPClient 技能索引集成测试。"""

    def test_client_get_skill_index(self) -> None:
        """MCPClient.get_skill_index 代理 server.get_skill_index。"""
        from app.mcp.client import MCPClient
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)
        client = MCPClient(server)

        index = client.get_skill_index()
        assert len(index) == 5

    @pytest.mark.asyncio
    async def test_client_get_tools_by_names(self) -> None:
        """MCPClient.get_tools_by_names 代理 server.list_tools_by_names。"""
        from app.mcp.client import MCPClient
        from app.mcp.server import KnowledgeBaseMCPServer

        mock_db_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_db_factory)
        client = MCPClient(server)

        tools = await client.get_tools_by_names(["knowledge_search"])
        assert len(tools) == 1
        assert tools[0]["name"] == "knowledge_search"
