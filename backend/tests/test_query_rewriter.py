"""P2-B 查询重写模块测试。

测试覆盖：
    1. QueryRewriteResult 数据结构
    2. QueryRewriter 各策略（rewrite/expansion/decomposition/hyde）
    3. 缓存幂等性
    4. LLM 失败降级
    5. 工厂函数 get_query_rewriter
    6. 配置参数
    7. P1-10 规则式 query 分类 + 策略自动路由
    8. P1-10 在线回退（双跑对比召回质量，差则回退原 query）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.query_rewriter import (
    QueryRewriteResult,
    QueryRewriter,
    QueryType,
    classify_query_type,
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


# ======================================================================
# P1-10: 规则式 query 分类测试
# ======================================================================


class TestClassifyQueryType:
    """classify_query_type 规则分类测试（零 LLM）。"""

    def test_single_intent_clear_query(self):
        """单意图明确查询 → SINGLE。"""
        assert classify_query_type("公司的休假政策是什么？") is QueryType.SINGLE

    def test_single_short_keyword(self):
        """短关键词查询 → SINGLE。"""
        assert classify_query_type("年假") is QueryType.SINGLE

    def test_single_weak_marker_short_query(self):
        """短查询中"和"为词组内连接，不判多意图。"""
        # "签证和护照" 是一个名词短语，且长度 < 12
        assert classify_query_type("签证和护照办理") is QueryType.SINGLE

    def test_multi_strong_marker(self):
        """强标记命中 → MULTI。"""
        assert classify_query_type("报销流程以及审批时效") is QueryType.MULTI
        assert classify_query_type("查年假余额，同时提交请假单") is QueryType.MULTI

    def test_multi_two_question_marks(self):
        """多个问号 → MULTI。"""
        assert classify_query_type("年假几天？怎么申请？") is QueryType.MULTI

    def test_multi_weak_marker_with_length(self):
        """弱标记 + 长度阈值 → MULTI。"""
        assert classify_query_type("查报销单 BG001 状态并创建新报销单") is QueryType.MULTI
        assert classify_query_type("报销流程和审批时效分别是多少") is QueryType.MULTI

    def test_multi_two_weak_markers(self):
        """两个弱标记 → MULTI（无需长度）。"""
        assert classify_query_type("工资和奖金与补贴") is QueryType.MULTI

    def test_vague_pronoun(self):
        """含指示代词 → VAGUE。"""
        assert classify_query_type("这个怎么走？") is QueryType.VAGUE

    def test_vague_generic_verb(self):
        """含泛化动词 → VAGUE。"""
        assert classify_query_type("报销单被退回怎么办") is QueryType.VAGUE

    def test_vague_takes_precedence_over_multi(self):
        """模糊优先于多意图。"""
        # 同时含 "这个"（模糊）与 "和"（弱多意图）
        assert classify_query_type("这个和那个怎么处理") is QueryType.VAGUE


# ======================================================================
# P1-10: 策略自动路由测试
# ======================================================================


class TestStrategyAutoRoute:
    """auto_route=True 时按 query 类型路由策略。"""

    def _make_router(self, **overrides) -> QueryRewriter:
        kwargs = {
            "enable_rewrite": True,
            "enable_expansion": True,
            "enable_decomposition": True,
            "enable_hyde": True,
            "auto_route": True,
        }
        kwargs.update(overrides)
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1\n扩展词2",
            "子查询": "子查询1\n子查询2",
            "假设文档": "假设文档内容",
        })
        return QueryRewriter(llm, **kwargs)

    async def test_single_routes_to_rewrite_and_expansion(self):
        """单意图 → rewrite + expansion。"""
        rewriter = self._make_router()
        result = await rewriter.rewrite("公司的休假政策是什么？")
        assert result.query_type == "single"
        assert set(result.strategy) == {"rewrite", "expansion"}
        assert result.sub_queries == []
        assert result.hyde_document is None

    async def test_multi_routes_to_rewrite_and_decomposition(self):
        """多意图 → rewrite + decomposition。"""
        rewriter = self._make_router()
        result = await rewriter.rewrite("报销流程以及审批时效分别是什么")
        assert result.query_type == "multi"
        assert set(result.strategy) == {"rewrite", "decomposition"}
        assert result.expanded_terms == []
        assert result.hyde_document is None

    async def test_vague_routes_to_rewrite_and_hyde(self):
        """模糊 → rewrite + hyde。"""
        rewriter = self._make_router()
        result = await rewriter.rewrite("这个怎么走？")
        assert result.query_type == "vague"
        assert set(result.strategy) == {"rewrite", "hyde"}
        assert result.expanded_terms == []
        assert result.sub_queries == []

    async def test_route_intersects_with_config(self):
        """路由结果与配置开关取交集（配置是总闸）。"""
        # hyde 配置禁用 — 模糊查询只执行 rewrite
        rewriter = self._make_router(enable_hyde=False)
        result = await rewriter.rewrite("这个怎么走？")
        assert result.query_type == "vague"
        assert set(result.strategy) == {"rewrite"}
        assert result.hyde_document is None

    async def test_route_empty_falls_back_to_config_set(self):
        """路由目标策略全被禁用时回退到配置启用集合。"""
        # 模糊查询路由 [rewrite, hyde] 全禁用 → 回退到配置启用集合 {expansion}
        rewriter = self._make_router(
            enable_rewrite=False, enable_hyde=False, enable_decomposition=False
        )
        result = await rewriter.rewrite("这个怎么走？")
        assert set(result.strategy) == {"expansion"}

    async def test_auto_route_disabled_preserves_original_behavior(self):
        """auto_route=False 时执行全部配置启用策略，query_type 为 None。"""
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1\n扩展词2",
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=False,
            enable_hyde=False,
            auto_route=False,
        )
        # 即使是多意图查询，也不做路由
        result = await rewriter.rewrite("报销流程以及审批时效分别是什么")
        assert result.query_type is None
        assert set(result.strategy) == {"rewrite", "expansion"}


# ======================================================================
# P1-10: 在线回退测试
# ======================================================================


class TestOnlineFallback:
    """在线回退 — 双跑对比召回质量，差则回退原 query。"""

    def _make_rewriter(self, **overrides) -> QueryRewriter:
        kwargs = {
            "enable_rewrite": True,
            "enable_expansion": False,
            "enable_decomposition": False,
            "enable_hyde": False,
        }
        kwargs.update(overrides)
        llm = _make_mock_llm({"重写查询": "重写后的查询"})
        return QueryRewriter(llm, **kwargs)

    def _make_evaluator(self, scores: dict[str, float]):
        """构造召回评估器 — query 关键词 → 召回分。"""
        calls: list[str] = []

        async def _evaluate(q: str) -> float:
            calls.append(q)
            for keyword, score in scores.items():
                if keyword in q:
                    return score
            return 0.5

        _evaluate.calls = calls  # type: ignore[attr-defined]
        return _evaluate

    async def test_fallback_when_rewritten_recall_worse(self):
        """改写后召回分更低 → 回退原 query。"""
        rewriter = self._make_rewriter()
        evaluator = self._make_evaluator({"重写后的查询": 0.1})  # 原 query 走默认 0.5
        result = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert result.fallback_to_original is True
        assert result.recall_original == 0.5
        assert result.recall_rewritten == 0.1
        # get_search_query 回退到原始查询
        assert result.get_search_query() == "原始查询"

    async def test_no_fallback_when_rewritten_recall_better(self):
        """改写后召回分更高 → 不回退。"""
        rewriter = self._make_rewriter()
        evaluator = self._make_evaluator({"重写后的查询": 0.9})
        result = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert result.fallback_to_original is False
        assert result.get_search_query() == "重写后的查询"

    async def test_margin_prevents_jitter_fallback(self):
        """余量内的小幅下降不触发回退。"""
        rewriter = self._make_rewriter(fallback_margin=0.2)
        evaluator = self._make_evaluator({"重写后的查询": 0.4})  # 原 0.5，差 0.1 < 余量
        result = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert result.fallback_to_original is False

    async def test_fallback_verdict_cached(self):
        """回退判定随 LRU 缓存复用 — 同一查询只双跑一次。"""
        rewriter = self._make_rewriter()
        evaluator = self._make_evaluator({"重写后的查询": 0.1})
        result1 = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert result1.fallback_to_original is True
        call_count_after_first = len(evaluator.calls)

        # 第二次调用命中缓存 — 不再调用评估器
        result2 = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert result2.cache_hit is True
        assert result2.fallback_to_original is True
        assert len(evaluator.calls) == call_count_after_first

    async def test_evaluator_exception_keeps_rewritten(self):
        """评估器异常不影响主链路 — 保持改写结果。"""
        rewriter = self._make_rewriter()

        async def _failing_evaluator(q: str) -> float:
            raise RuntimeError("retriever down")

        result = await rewriter.rewrite("原始查询", recall_evaluator=_failing_evaluator)
        assert result.fallback_to_original is False
        assert result.recall_original is None
        assert result.get_search_query() == "重写后的查询"

    async def test_no_evaluator_skips_evaluation(self):
        """不传评估器 → 无双跑，recall 字段为空。"""
        rewriter = self._make_rewriter()
        result = await rewriter.rewrite("原始查询")
        assert result.fallback_to_original is False
        assert result.recall_original is None
        assert result.recall_rewritten is None

    async def test_no_evaluation_when_query_unchanged(self):
        """改写结果与原查询相同时不触发双跑。"""
        llm = _make_mock_llm({"重写查询": "原始查询"})  # 改写结果 == 原查询
        rewriter = QueryRewriter(
            llm, enable_rewrite=True, enable_expansion=False,
            enable_decomposition=False, enable_hyde=False,
        )
        evaluator = self._make_evaluator({})
        result = await rewriter.rewrite("原始查询", recall_evaluator=evaluator)
        assert evaluator.calls == []
        assert result.fallback_to_original is False

    async def test_fallback_applies_to_hyde_query(self):
        """HyDE 文档召回更差时同样回退原 query。"""
        llm = _make_mock_llm({"假设文档": "语义不相关的假设文档"})
        rewriter = QueryRewriter(
            llm, enable_rewrite=False, enable_expansion=False,
            enable_decomposition=False, enable_hyde=True,
        )
        evaluator = self._make_evaluator({"语义不相关的假设文档": 0.05})
        result = await rewriter.rewrite("这个怎么办", recall_evaluator=evaluator)
        assert result.fallback_to_original is True
        assert result.get_search_query() == "这个怎么办"


# ======================================================================
# P1-10: 新增配置参数测试
# ======================================================================


class TestAutoRouteConfig:
    """P1-10 配置参数测试。"""

    def test_config_has_auto_route(self):
        """Settings 包含 QUERY_REWRITE_AUTO_ROUTE。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_REWRITE_AUTO_ROUTE")
        assert isinstance(s.QUERY_REWRITE_AUTO_ROUTE, bool)

    def test_config_has_online_fallback(self):
        """Settings 包含 QUERY_REWRITE_ONLINE_FALLBACK。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_REWRITE_ONLINE_FALLBACK")
        assert isinstance(s.QUERY_REWRITE_ONLINE_FALLBACK, bool)

    def test_config_has_fallback_margin(self):
        """Settings 包含 QUERY_REWRITE_FALLBACK_MARGIN。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "QUERY_REWRITE_FALLBACK_MARGIN")
        assert isinstance(s.QUERY_REWRITE_FALLBACK_MARGIN, float)

    def test_factory_wires_new_settings(self):
        """工厂函数透传新配置项。"""
        reset_query_rewriter()
        with patch("app.config.get_settings") as mock_settings, patch(
            "app.llm.factory.get_llm_provider"
        ) as mock_llm:
            mock_settings.return_value = MagicMock(
                QUERY_REWRITE_ENABLED=True,
                QUERY_EXPANSION_ENABLED=False,
                QUERY_DECOMPOSITION_ENABLED=False,
                HYDE_ENABLED=False,
                QUERY_REWRITE_AUTO_ROUTE=True,
                QUERY_REWRITE_ONLINE_FALLBACK=True,
                QUERY_REWRITE_FALLBACK_MARGIN=0.1,
            )
            mock_llm.return_value = _make_mock_llm()
            rewriter = get_query_rewriter()
            assert rewriter is not None
            assert rewriter.auto_route is True
            assert rewriter.enable_online_fallback is True
            assert rewriter._fallback_margin == 0.1
        reset_query_rewriter()


# ======================================================================
# P2-12: 降级模式测试（DEGRADE_MODE_ENABLED）
# ======================================================================


class TestDegradeMode:
    """降级模式 — 高负载时关闭 HyDE / Decomposition 保核心链路。"""

    def _make_settings(self, degrade: bool) -> MagicMock:
        return MagicMock(DEGRADE_MODE_ENABLED=degrade)

    async def test_degrade_removes_hyde_and_decomposition(self):
        """降级开启时 hyde / decomposition 被移除。"""
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1",
            "子查询": "子查询1",
            "假设文档": "假设文档内容",
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=True,
            enable_hyde=True,
            auto_route=False,
        )
        with patch(
            "app.config.get_settings", return_value=self._make_settings(True)
        ):
            result = await rewriter.rewrite("测试查询")
        assert set(result.strategy) == {"rewrite", "expansion"}
        assert result.hyde_document is None
        assert result.sub_queries == []

    async def test_degrade_off_keeps_all_strategies(self):
        """降级关闭时全部配置策略正常执行。"""
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1",
            "子查询": "子查询1",
            "假设文档": "假设文档内容",
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=True,
            enable_hyde=True,
            auto_route=False,
        )
        with patch(
            "app.config.get_settings", return_value=self._make_settings(False)
        ):
            result = await rewriter.rewrite("测试查询")
        assert set(result.strategy) == {"rewrite", "expansion", "decomposition", "hyde"}

    async def test_degrade_applies_with_auto_route(self):
        """降级与自动路由叠加：路由到 hyde 但被降级移除时回退配置集。"""
        llm = _make_mock_llm({
            "重写查询": "重写后的查询",
            "扩展词": "扩展词1",
        })
        rewriter = QueryRewriter(
            llm,
            enable_rewrite=True,
            enable_expansion=True,
            enable_decomposition=True,
            enable_hyde=True,
            auto_route=True,
        )
        with patch(
            "app.config.get_settings", return_value=self._make_settings(True)
        ):
            # 模糊查询路由 [rewrite, hyde]，hyde 被降级移除 → 只剩 rewrite
            result = await rewriter.rewrite("这个怎么办？")
        assert result.query_type == "vague"
        assert set(result.strategy) == {"rewrite"}
        assert result.hyde_document is None
