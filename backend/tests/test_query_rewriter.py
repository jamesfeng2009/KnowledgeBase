"""P2-B 查询重写模块测试。

测试覆盖：
    1. QueryRewriteResult 数据结构
    2. QueryRewriter 各策略（rewrite/expansion/decomposition/hyde）
    3. 缓存幂等性
    4. LLM 失败降级
    5. 工厂函数 get_query_rewriter
    6. 配置参数
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.query_rewriter import (
    QueryRewriteResult,
    QueryRewriter,
    get_query_rewriter,
    reset_query_rewriter,
)


# ======================================================================
# 辅助函数
# ======================================================================


def _make_mock_llm(responses: dict[str, str] | None = None):
    """创建 Mock LLM Provider。

    Args:
        responses: prompt 关键词 → 响应文本 的映射。
                   未匹配时返回默认响应。
    """
    responses = responses or {}
    llm = MagicMock()

    async def _chat(messages, tools=None, stream=False, **kwargs):
        user_msg = messages[-1]["content"] if messages else ""
        # 根据 prompt 内容返回不同响应
        for keyword, resp in responses.items():
            if keyword in user_msg:
                yield resp
                return
        yield "default response"

    llm.chat = _chat
    return llm


def _make_failing_llm():
    """创建一个总是失败的 Mock LLM。"""
    llm = MagicMock()

    async def _chat(messages, tools=None, stream=False, **kwargs):
        raise RuntimeError("LLM unavailable")
        yield  # noqa — 使其成为 async generator

    llm.chat = _chat
    return llm


# ======================================================================
# QueryRewriteResult 数据结构测试
# ======================================================================


class TestQueryRewriteResult:
    """QueryRewriteResult 数据结构测试。"""

    def test_default_values(self):
        """默认值正确。"""
        result = QueryRewriteResult(original="test query")
        assert result.original == "test query"
        assert result.rewritten == ""
        assert result.expanded_terms == []
        assert result.sub_queries == []
        assert result.hyde_document is None
        assert result.strategy == []
        assert result.latency_ms == 0.0
        assert result.cache_hit is False

    def test_get_search_query_prefers_hyde(self):
        """get_search_query 优先使用 HyDE 文档。"""
        result = QueryRewriteResult(
            original="原查询",
            rewritten="重写查询",
            hyde_document="假设文档内容",
        )
        assert result.get_search_query() == "假设文档内容"

    def test_get_search_query_falls_back_to_rewritten(self):
        """无 HyDE 时使用重写查询。"""
        result = QueryRewriteResult(
            original="原查询",
            rewritten="重写查询",
        )
        assert result.get_search_query() == "重写查询"

    def test_get_search_query_falls_back_to_original(self):
        """无重写时使用原始查询。"""
        result = QueryRewriteResult(original="原查询")
        assert result.get_search_query() == "原查询"

    def test_get_all_queries_includes_sub_queries(self):
        """get_all_queries 包含子查询。"""
        result = QueryRewriteResult(
            original="原查询",
            rewritten="重写查询",
            sub_queries=["子查询1", "子查询2"],
        )
        queries = result.get_all_queries()
        assert "重写查询" in queries
        assert "子查询1" in queries
        assert "子查询2" in queries

    def test_get_all_queries_minimum_one(self):
        """get_all_queries 至少返回一个查询。"""
        result = QueryRewriteResult(original="原查询")
        queries = result.get_all_queries()
        assert len(queries) >= 1
        assert "原查询" in queries

    def test_to_dict_contains_all_fields(self):
        """to_dict 包含所有字段。"""
        result = QueryRewriteResult(
            original="原查询",
            rewritten="重写查询",
            expanded_terms=["同义词"],
            sub_queries=["子查询"],
            hyde_document="假设文档",
            strategy=["rewrite", "hyde"],
            latency_ms=42.5,
        )
        d = result.to_dict()
        assert d["original"] == "原查询"
        assert d["rewritten"] == "重写查询"
        assert d["expanded_terms"] == ["同义词"]
        assert d["sub_queries"] == ["子查询"]
        assert d["hyde_document"] == "假设文档"
        assert d["strategy"] == ["rewrite", "hyde"]
        assert d["latency_ms"] == 42.5
        assert "search_query" in d


# ======================================================================
# QueryRewriter 策略测试
# ======================================================================


class TestQueryRewriterStrategies:
    """QueryRewriter 各策略测试。"""

    async def test_rewrite_strategy(self):
        """查询重写策略。"""
        llm = _make_mock_llm({"重写查询": "公司休假政策是什么"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
        )
        result = await rewriter.rewrite("公司的休假政策")
        assert result.rewritten == "公司休假政策是什么"
        assert "rewrite" in result.strategy

    async def test_rewrite_strips_quotes(self):
        """重写结果去除引号。"""
        llm = _make_mock_llm({"重写查询": '"公司休假政策"'})
        rewriter = QueryRewriter(llm, enable_rewrite=True, enable_expansion=False, enable_decomposition=False, enable_hyde=False)
        result = await rewriter.rewrite("休假")
        assert result.rewritten == "公司休假政策"

    async def test_expansion_strategy(self):
        """查询扩展策略。"""
        llm = _make_mock_llm({
            "扩展词": "年假\n调休\n带薪休假\n事假\n病假"
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=False,
            enable_expansion=True,
            enable_decomposition=False,
            enable_hyde=False,
        )
        result = await rewriter.rewrite("休假政策")
        assert len(result.expanded_terms) == 5
        assert "年假" in result.expanded_terms
        assert "expansion" in result.strategy

    async def test_expansion_deduplicates(self):
        """扩展词去重。"""
        llm = _make_mock_llm({
            "扩展词": "年假\n年假\n调休\n调休\n年假"
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=False,
            enable_expansion=True,
            enable_decomposition=False,
            enable_hyde=False,
        )
        result = await rewriter.rewrite("休假")
        # 去重后只保留 2 个
        assert len(result.expanded_terms) == 2

    async def test_decomposition_strategy(self):
        """查询分解策略。"""
        # 使用异步方式测试分解逻辑
        llm = _make_mock_llm({
            "子查询": "公司年假天数\n年假申请流程\n年假结转规则"
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=False,
            enable_expansion=False,
            enable_decomposition=True,
            enable_hyde=False,
        )

        result = await rewriter._do_decomposition("公司年假政策和申请流程")
        assert len(result) == 3

    async def test_decomposition_filters_original(self):
        """分解结果过滤掉与原查询相同的。"""
        llm = _make_mock_llm({
            "子查询": "原查询\n子查询1\n子查询2"
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=False,
            enable_expansion=False,
            enable_decomposition=True,
            enable_hyde=False,
        )
        result = await rewriter.rewrite("原查询")
        assert "原查询" not in result.sub_queries
        assert "子查询1" in result.sub_queries

    async def test_hyde_strategy(self):
        """HyDE 假设文档生成。"""
        hyde_text = "根据公司休假政策，员工每年享有15天年假。年假可在当年使用，也可结转至次年3月31日。"
        llm = _make_mock_llm({"假设文档": hyde_text})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=False,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=True,
        )
        result = await rewriter.rewrite("年假政策")
        assert result.hyde_document == hyde_text
        assert "hyde" in result.strategy

    async def test_all_strategies_combined(self):
        """所有策略同时启用。"""
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1\n扩展词2",
            "子查询": "子查询1\n子查询2",
            "假设文档": "假设文档内容",
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=True,
            enable_hyde=True,
        )
        result = await rewriter.rewrite("测试查询")
        assert result.rewritten == "重写后的查询"
        assert len(result.expanded_terms) == 2
        assert len(result.sub_queries) == 2
        assert result.hyde_document == "假设文档内容"
        assert len(result.strategy) == 4
        assert result.latency_ms > 0


# ======================================================================
# 缓存与幂等性测试
# ======================================================================


class TestQueryRewriterCaching:
    """查询重写缓存与幂等性测试。"""

    async def test_same_query_returns_cached_result(self):
        """相同查询返回缓存结果。"""
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
        )

        result1 = await rewriter.rewrite("测试查询")
        assert result1.cache_hit is False

        result2 = await rewriter.rewrite("测试查询")
        assert result2.cache_hit is True
        assert result2.rewritten == result1.rewritten

    async def test_different_query_not_cached(self):
        """不同查询不命中缓存。"""
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
        )

        result1 = await rewriter.rewrite("查询A")
        result2 = await rewriter.rewrite("查询B")
        assert result1.cache_hit is False
        assert result2.cache_hit is False

    async def test_context_affects_cache(self):
        """不同上下文的同一查询不命中缓存。"""
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
        )

        result1 = await rewriter.rewrite("查询", context="上下文A")
        result2 = await rewriter.rewrite("查询", context="上下文B")
        assert result1.cache_hit is False
        assert result2.cache_hit is False

    async def test_clear_cache(self):
        """清除缓存后重新生成。"""
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
        )

        await rewriter.rewrite("查询")
        rewriter.clear_cache()
        result = await rewriter.rewrite("查询")
        assert result.cache_hit is False

    async def test_cache_lru_eviction(self):
        """缓存超过上限时淘汰最早的。"""
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=False,
            enable_decomposition=False,
            enable_hyde=False,
            cache_size=3,
        )

        # 填满缓存
        await rewriter.rewrite("查询1")
        await rewriter.rewrite("查询2")
        await rewriter.rewrite("查询3")

        # 超出上限 — 淘汰查询1
        await rewriter.rewrite("查询4")

        # 查询1 应该不命中缓存
        result = await rewriter.rewrite("查询1")
        assert result.cache_hit is False


# ======================================================================
# LLM 失败降级测试
# ======================================================================


class TestQueryRewriterFailure:
    """LLM 失败时的降级测试。"""

    async def test_llm_failure_returns_original_query(self):
        """LLM 失败时返回原始查询。"""
        llm = _make_failing_llm()
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=True,
            enable_hyde=True,
        )
        result = await rewriter.rewrite("测试查询")
        assert result.original == "测试查询"
        assert result.rewritten == ""
        assert result.expanded_terms == []
        assert result.sub_queries == []
        assert result.hyde_document is None
        assert result.strategy == []  # 所有策略都失败了
        # 仍然可以获取原始查询
        assert result.get_search_query() == "测试查询"

    async def test_partial_failure(self):
        """部分策略失败时其他策略仍可成功。"""
        # rewrite 成功，expansion 失败
        llm = MagicMock()
        call_count = {"n": 0}

        async def _chat(messages, tools=None, stream=False, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # rewrite 成功
                yield "重写后的查询"
            else:
                # expansion 失败
                raise RuntimeError("expansion failed")
                yield  # noqa

        llm.chat = _chat
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=False,
            enable_hyde=False,
        )
        result = await rewriter.rewrite("测试查询")
        assert result.rewritten == "重写后的查询"
        assert "rewrite" in result.strategy
        # expansion 失败 — 不在 strategy 中
        assert "expansion" not in result.strategy


# ======================================================================
# 工厂函数测试
# ======================================================================


class TestQueryRewriterFactory:
    """get_query_rewriter 工厂函数测试。"""

    def setup_method(self):
        reset_query_rewriter()

    def test_returns_none_when_all_disabled(self):
        """所有策略禁用时返回 None。"""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                QUERY_REWRITE_ENABLED=False,
                QUERY_EXPANSION_ENABLED=False,
                QUERY_DECOMPOSITION_ENABLED=False,
                HYDE_ENABLED=False,
            )
            result = get_query_rewriter()
            assert result is None

    def test_returns_rewriter_when_enabled(self):
        """启用策略时返回 QueryRewriter。"""
        with patch("app.config.get_settings") as mock_settings, patch(
            "app.llm.factory.get_llm_provider"
        ) as mock_llm:
            mock_settings.return_value = MagicMock(
                QUERY_REWRITE_ENABLED=True,
                QUERY_EXPANSION_ENABLED=True,
                QUERY_DECOMPOSITION_ENABLED=False,
                HYDE_ENABLED=False,
            )
            mock_llm.return_value = _make_mock_llm()
            result = get_query_rewriter()
            assert result is not None
            assert isinstance(result, QueryRewriter)

    def test_singleton(self):
        """工厂函数返回单例。"""
        with patch("app.config.get_settings") as mock_settings, patch(
            "app.llm.factory.get_llm_provider"
        ) as mock_llm:
            mock_settings.return_value = MagicMock(
                QUERY_REWRITE_ENABLED=True,
                QUERY_EXPANSION_ENABLED=False,
                QUERY_DECOMPOSITION_ENABLED=False,
                HYDE_ENABLED=False,
            )
            mock_llm.return_value = _make_mock_llm()
            r1 = get_query_rewriter()
            r2 = get_query_rewriter()
            assert r1 is r2

    def test_reset_creates_new_instance(self):
        """reset 后创建新实例。"""
        with patch("app.config.get_settings") as mock_settings, patch(
            "app.llm.factory.get_llm_provider"
        ) as mock_llm:
            mock_settings.return_value = MagicMock(
                QUERY_REWRITE_ENABLED=True,
                QUERY_EXPANSION_ENABLED=False,
                QUERY_DECOMPOSITION_ENABLED=False,
                HYDE_ENABLED=False,
            )
            mock_llm.return_value = _make_mock_llm()
            r1 = get_query_rewriter()
            reset_query_rewriter()
            r2 = get_query_rewriter()
            assert r1 is not r2


# ======================================================================
# 配置参数测试
# ======================================================================


class TestQueryRewriterConfig:
    """P2-B 查询重写配置参数测试。"""

    def test_config_has_query_rewrite_enabled(self):
        """Settings 包含 QUERY_REWRITE_ENABLED。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_REWRITE_ENABLED")
        assert isinstance(s.QUERY_REWRITE_ENABLED, bool)

    def test_config_has_query_expansion_enabled(self):
        """Settings 包含 QUERY_EXPANSION_ENABLED。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_EXPANSION_ENABLED")
        assert isinstance(s.QUERY_EXPANSION_ENABLED, bool)

    def test_config_has_query_decomposition_enabled(self):
        """Settings 包含 QUERY_DECOMPOSITION_ENABLED。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_DECOMPOSITION_ENABLED")
        assert isinstance(s.QUERY_DECOMPOSITION_ENABLED, bool)

    def test_config_has_hyde_enabled(self):
        """Settings 包含 HYDE_ENABLED。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "HYDE_ENABLED")
        assert isinstance(s.HYDE_ENABLED, bool)

    def test_default_values(self):
        """默认值正确。"""
        from app.config import get_settings

        s = get_settings()
        assert s.QUERY_REWRITE_ENABLED is True
        assert s.QUERY_EXPANSION_ENABLED is True
        assert s.QUERY_DECOMPOSITION_ENABLED is False
        assert s.HYDE_ENABLED is False
