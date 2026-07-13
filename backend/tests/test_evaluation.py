"""
LangFuse 追踪与 LLM-as-Judge 评测测试。

不依赖真实 LangFuse Server 或 LLM API，使用 Mock 模拟。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.observability.llm_judge import (
    EvalReport,
    EvalResult,
    LLMJudgeService,
)
from app.observability.langfuse_tracer import (
    TraceContext,
    trace_node,
    _get_langfuse_client,
)


# ==================================================================
# 1. LangFuse 追踪测试
# ==================================================================


class TestLangFuseTracer:
    """LangFuse 追踪 — 优雅降级和节点追踪。"""

    def test_get_client_not_configured(self):
        """LangFuse 未配置时返回 None。"""
        with patch(
            "app.observability.langfuse_tracer._langfuse_available", False
        ):
            client = _get_langfuse_client()
            assert client is None

    def test_trace_context_start_no_client(self):
        """无 LangFuse 客户端时 Trace 启动不报错。"""
        ctx = TraceContext(session_id="test-123", user_id="user-1")
        with patch(
            "app.observability.langfuse_tracer._get_langfuse_client",
            return_value=None,
        ):
            ctx.start()
        # _trace 应该为 None（不报错）
        assert ctx._trace is None

    def test_trace_context_span_no_trace(self):
        """无 trace 时 span 不报错。"""
        ctx = TraceContext()
        ctx.span("think", input_data={"test": 1}, output_data={"result": "ok"})
        # 不报错即可
        assert len(ctx._spans) == 0

    def test_trace_context_finalize_no_trace(self):
        """无 trace 时 finalize 不报错。"""
        ctx = TraceContext()
        ctx.finalize(output="answer", metadata={"score": 4.2})

    def test_trace_context_with_mock_client(self):
        """模拟 LangFuse 客户端 — 验证 trace/span/finalize 调用。"""
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_span = MagicMock()
        mock_client.trace.return_value = mock_trace
        mock_trace.span.return_value = mock_span

        ctx = TraceContext(
            trace_name="test_loop",
            session_id="session-1",
            user_id="user-1",
        )

        with patch(
            "app.observability.langfuse_tracer._get_langfuse_client",
            return_value=mock_client,
        ):
            ctx.start()
            ctx.span("think", input_data={"q": "test"}, output_data={"decision": "retrieve"})
            ctx.span("retrieve", output_data={"docs": 5})
            ctx.finalize(output="answer", metadata={"iterations": 2})

        mock_client.trace.assert_called_once()
        assert mock_trace.span.call_count == 2
        mock_trace.update.assert_called_once()

    async def test_trace_node_decorator(self):
        """trace_node 装饰器正确追踪节点执行。"""

        class MockEngine:
            def __init__(self):
                self._trace_ctx = None

            @trace_node("think")
            async def _think(self, state: dict) -> str:
                await asyncio.sleep(0.01)
                return "retrieve"

        engine = MockEngine()
        state = {"session_id": "s1", "iteration": 1, "retrieved_docs": [], "tool_results": []}

        result = await engine._think(state)
        assert result == "retrieve"

    async def test_trace_node_decorator_with_error(self):
        """trace_node 装饰器捕获异常并记录。"""

        class MockEngine:
            def __init__(self):
                self._trace_ctx = None

            @trace_node("think")
            async def _think(self, state: dict) -> str:
                raise RuntimeError("测试异常")

        engine = MockEngine()
        state = {"session_id": "s1", "iteration": 1, "retrieved_docs": [], "tool_results": []}

        with pytest.raises(RuntimeError, match="测试异常"):
            await engine._think(state)

    async def test_trace_node_decorator_with_trace_context(self):
        """trace_node 装饰器与 TraceContext 配合使用。"""
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_span = MagicMock()
        mock_client.trace.return_value = mock_trace
        mock_trace.span.return_value = mock_span

        class MockEngine:
            def __init__(self):
                self._trace_ctx = TraceContext(session_id="s1", user_id="u1")
                with patch(
                    "app.observability.langfuse_tracer._get_langfuse_client",
                    return_value=mock_client,
                ):
                    self._trace_ctx.start()

            @trace_node("think")
            async def _think(self, state: dict) -> str:
                return "generate"

        engine = MockEngine()
        state = {"session_id": "s1", "iteration": 1, "retrieved_docs": [{"id": 1}], "tool_results": []}

        result = await engine._think(state)
        assert result == "generate"
        # 应该记录了一个 span
        assert mock_trace.span.call_count == 1


# ==================================================================
# 2. LLM-as-Judge 评测测试
# ==================================================================


class TestLLMJudgeService:
    """LLM-as-Judge — JSON 解析、评测流程、批量报告。"""

    def test_extract_json_pure(self):
        """纯 JSON 文本提取。"""
        text = '{"citation_accuracy": 4, "completeness": 5}'
        result = LLMJudgeService._extract_json(text)
        assert result is not None
        assert "citation_accuracy" in result

    def test_extract_json_markdown_block(self):
        """markdown 代码块 JSON 提取。"""
        text = '```json\n{"citation_accuracy": 4}\n```'
        result = LLMJudgeService._extract_json(text)
        assert result is not None
        assert "citation_accuracy" in result

    def test_extract_json_code_block(self):
        """普通代码块 JSON 提取。"""
        text = '```\n{"completeness": 3}\n```'
        result = LLMJudgeService._extract_json(text)
        assert result is not None
        assert "completeness" in result

    def test_extract_json_embedded(self):
        """文本中嵌入的 JSON 提取。"""
        text = '评测结果如下：\n{"citation_accuracy": 4, "completeness": 5}\n以上是评分。'
        result = LLMJudgeService._extract_json(text)
        assert result is not None
        assert "citation_accuracy" in result

    def test_extract_json_not_found(self):
        """无 JSON 时返回 None。"""
        result = LLMJudgeService._extract_json("这不是 JSON")
        assert result is None

    def test_parse_judge_response_success(self):
        """成功解析 Judge 响应。"""
        service = LLMJudgeService()
        response = json.dumps({
            "citation_accuracy": 4,
            "completeness": 5,
            "hallucination_inverse": 4,
            "total_score": 4.33,
            "reasoning": "答案准确引用了文档",
        })

        result = service._parse_judge_response("问题", "答案", response)

        assert result.citation_accuracy == 4
        assert result.completeness == 5
        assert result.hallucination_inverse == 4
        assert result.total_score == 4.33
        assert result.passed is True
        assert result.error is None

    def test_parse_judge_response_calculate_total(self):
        """无 total_score 时自动计算。"""
        service = LLMJudgeService()
        response = json.dumps({
            "citation_accuracy": 3,
            "completeness": 4,
            "hallucination_inverse": 5,
        })

        result = service._parse_judge_response("Q", "A", response)
        assert result.total_score == round((3 + 4 + 5) / 3, 2)

    def test_parse_judge_response_invalid_json(self):
        """无效 JSON 返回错误结果。"""
        service = LLMJudgeService()
        result = service._parse_judge_response("Q", "A", "不是 JSON")
        assert result.error is not None
        assert result.passed is False

    def test_eval_result_passed(self):
        """EvalResult.passed 阈值判断。"""
        r1 = EvalResult(question="Q", answer="A", total_score=4.5)
        assert r1.passed is True

        r2 = EvalResult(question="Q", answer="A", total_score=2.0)
        assert r2.passed is False

        r3 = EvalResult(question="Q", answer="A", error="出错")
        assert r3.passed is False

    async def test_evaluate_single_success(self):
        """单条评测成功。"""
        mock_llm = AsyncMock()
        response_json = json.dumps({
            "citation_accuracy": 5,
            "completeness": 4,
            "hallucination_inverse": 5,
            "total_score": 4.67,
            "reasoning": "答案准确",
        })

        async def mock_chat(messages, **kwargs):
            for msg in messages:
                pass
            yield response_json

        mock_llm.chat = mock_chat

        service = LLMJudgeService(judge_llm=mock_llm)
        result = await service.evaluate_single(
            question="什么是微服务？",
            answer="微服务是一种架构风格...",
            contexts=["微服务架构将应用拆分为小型服务..."],
        )

        assert result.citation_accuracy == 5
        assert result.completeness == 4
        assert result.hallucination_inverse == 5
        assert result.total_score == 4.67
        assert result.passed is True

    async def test_evaluate_single_llm_error(self):
        """LLM 调用异常时返回错误结果。"""
        mock_llm = AsyncMock()

        async def mock_chat(messages, **kwargs):
            raise RuntimeError("API 不可用")
            yield  # never reached

        mock_llm.chat = mock_chat

        service = LLMJudgeService(judge_llm=mock_llm)
        result = await service.evaluate_single(
            question="问题",
            answer="答案",
            contexts=["上下文"],
        )

        assert result.error is not None
        assert result.passed is False

    async def test_evaluate_batch(self):
        """批量评测生成报告。"""
        mock_llm = AsyncMock()

        call_count = 0
        responses = [
            json.dumps({"citation_accuracy": 5, "completeness": 5, "hallucination_inverse": 5, "total_score": 5.0, "reasoning": "完美"}),
            json.dumps({"citation_accuracy": 3, "completeness": 2, "hallucination_inverse": 3, "total_score": 2.67, "reasoning": "不完整"}),
        ]

        async def mock_chat(messages, **kwargs):
            nonlocal call_count
            yield responses[call_count]
            call_count += 1

        mock_llm.chat = mock_chat

        service = LLMJudgeService(judge_llm=mock_llm)
        report = await service.evaluate_batch([
            {"question": "Q1", "answer": "A1", "contexts": ["C1"]},
            {"question": "Q2", "answer": "A2", "contexts": ["C2"]},
        ])

        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        assert round(report.avg_score, 2) == round((5.0 + 2.67) / 2, 2)
        report_dict = report.to_dict()
        assert report_dict["pass_rate"] == 0.5

    async def test_evaluate_batch_empty(self):
        """空数据集返回空报告。"""
        service = LLMJudgeService(judge_llm=AsyncMock())
        report = await service.evaluate_batch([])

        assert report.total == 0
        assert report.passed == 0
        assert report.avg_score == 0.0

    def test_eval_report_to_dict(self):
        """报告序列化包含所有字段。"""
        report = EvalReport(
            total=10,
            passed=8,
            failed=2,
            avg_score=4.1,
            avg_citation_accuracy=4.2,
            avg_completeness=4.0,
            avg_hallucination_inverse=4.1,
            results=[
                EvalResult(question="Q", answer="A", total_score=5.0),
            ],
            evaluated_at="2026-07-06T10:00:00",
        )

        d = report.to_dict()
        assert d["total"] == 10
        assert d["pass_rate"] == 0.8
        assert d["avg_score"] == 4.1
        assert len(d["results"]) == 1
        assert d["results"][0]["passed"] is True
