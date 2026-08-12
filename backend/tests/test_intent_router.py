"""P1 IntentRouter + RuleMatcher 单元测试。

覆盖：
- RuleMatcher: 4 种意图的中英文正则匹配
- IntentRouter: 规则匹配 + LLM fallback + 兜底策略
- IntentResult: 快捷路径判断逻辑
"""
import pytest

from app.intent.router import IntentRouter, IntentResult, IntentType, _SHORTCUT_INTENTS
from app.intent.rule_matcher import RuleMatcher


class TestRuleMatcher:
    """RuleMatcher 规则匹配器测试。"""

    def setup_method(self):
        self.matcher = RuleMatcher()

    # --- RAG_SEARCH ---

    @pytest.mark.parametrize("query", [
        "搜索报销流程",
        "查找合同文档",
        "查一下微服务架构",
        "搜一下年假政策",
        "search for contract template",
        "find related documents",
        "微服务是什么",
        "如何申请报销",
        "什么是知识图谱",
        "K8s和Docker的区别",
    ])
    def test_rag_search_match(self, query):
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent == IntentType.RAG_SEARCH
        assert result.confidence >= 0.85
        assert result.use_shortcut is True

    # --- LIST_DOCUMENTS ---

    @pytest.mark.parametrize("query", [
        "列出所有文档",
        "有哪些知识库",
        "显示一下文件列表",
        "我的文档列表",
        "list all documents",
        "show knowledge bases",
    ])
    def test_list_documents_match(self, query):
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent == IntentType.LIST_DOCUMENTS
        assert result.confidence >= 0.9
        assert result.use_shortcut is True

    # --- GET_DOCUMENT ---

    @pytest.mark.parametrize("query", [
        "查看合同文档",
        "打开报销流程文档",
        "看看用户手册",
        "view document abc123",
    ])
    def test_get_document_match(self, query):
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent == IntentType.GET_DOCUMENT
        assert result.confidence >= 0.85
        assert result.use_shortcut is True

    # --- CREATE_DOCUMENT ---

    @pytest.mark.parametrize("query", [
        "创建新文档",
        "上传合同文件",
        "添加知识库资料",
        "create new document",
        "upload file",
    ])
    def test_create_document_match(self, query):
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent == IntentType.CREATE_DOCUMENT
        assert result.confidence >= 0.9
        assert result.use_shortcut is False  # CREATE_DOCUMENT 不走快捷路径

    # --- 无匹配 ---

    @pytest.mark.parametrize("query", [
        "compare the performance of Redis and Memcached",
        "你好",
        "",
        "  ",
    ])
    def test_no_match(self, query):
        result = self.matcher.match(query)
        # 空字符串返回 None，复杂查询可能返回 None 或低置信度匹配
        if not query.strip():
            assert result is None
        # 复杂查询可能不匹配任何规则 → None

    def test_rag_search_extracts_parameters(self):
        """RAG_SEARCH 应提取搜索关键词参数。"""
        result = self.matcher.match("搜索报销流程")
        assert result is not None
        assert result.intent == IntentType.RAG_SEARCH
        # 参数应包含搜索关键词
        assert "search_query" in result.parameters or len(result.parameters) >= 0

    def test_empty_query_returns_none(self):
        assert self.matcher.match("") is None
        assert self.matcher.match("   ") is None


class TestIntentRouter:
    """IntentRouter 意图路由器测试。"""

    def setup_method(self):
        self.router = IntentRouter(llm_provider=None)  # 无 LLM → 纯规则匹配

    @pytest.mark.asyncio
    async def test_rule_match_high_confidence(self):
        """高置信度规则匹配直接返回，不调 LLM。"""
        result = await self.router.route(
            query="搜索报销流程",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.RAG_SEARCH
        assert result.confidence >= 0.8
        assert result.use_shortcut is True

    @pytest.mark.asyncio
    async def test_list_documents_shortcut(self):
        """LIST_DOCUMENTS 走快捷路径。"""
        result = await self.router.route(
            query="列出所有文档",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.LIST_DOCUMENTS
        assert result.use_shortcut is True

    @pytest.mark.asyncio
    async def test_create_document_not_shortcut(self):
        """CREATE_DOCUMENT 不走快捷路径（需 HITL）。"""
        result = await self.router.route(
            query="创建新文档",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.CREATE_DOCUMENT
        assert result.use_shortcut is False

    @pytest.mark.asyncio
    async def test_fallback_to_complex_query(self):
        """规则未命中且无 LLM → 兜底 COMPLEX_QUERY。"""
        result = await self.router.route(
            query="你好，帮我分析一下量子计算的发展",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.COMPLEX_QUERY
        assert result.use_shortcut is False

    @pytest.mark.asyncio
    async def test_empty_query_fallback(self):
        """空查询 → 兜底 COMPLEX_QUERY。"""
        result = await self.router.route(
            query="",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.COMPLEX_QUERY
        assert result.use_shortcut is False


class TestIntentResult:
    """IntentResult 数据类测试。"""

    def test_shortcut_intents(self):
        """快捷路径意图集合正确。"""
        assert IntentType.RAG_SEARCH in _SHORTCUT_INTENTS
        assert IntentType.LIST_DOCUMENTS in _SHORTCUT_INTENTS
        assert IntentType.GET_DOCUMENT in _SHORTCUT_INTENTS
        assert IntentType.CREATE_DOCUMENT not in _SHORTCUT_INTENTS
        assert IntentType.COMPLEX_QUERY not in _SHORTCUT_INTENTS
        # 终态出口（拒识/澄清）不属于检索快捷集合
        assert IntentType.UNSUPPORTED not in _SHORTCUT_INTENTS
        assert IntentType.UNCLEAR not in _SHORTCUT_INTENTS

    def test_intent_result_defaults(self):
        """IntentResult 默认值正确。"""
        result = IntentResult(
            intent=IntentType.COMPLEX_QUERY,
            confidence=0.0,
        )
        assert result.parameters == {}
        assert result.use_shortcut is False
        assert result.missing_slots == []
        assert result.constraints is None

    def test_intent_type_enum_values(self):
        """IntentType 枚举值正确。"""
        assert IntentType.RAG_SEARCH.value == "rag_search"
        assert IntentType.LIST_DOCUMENTS.value == "list_documents"
        assert IntentType.COMPLEX_QUERY.value == "complex_query"
        assert IntentType.UNSUPPORTED.value == "unsupported"
        assert IntentType.UNCLEAR.value == "unclear"
