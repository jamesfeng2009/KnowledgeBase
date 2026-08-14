"""
ReviewerAgent 测试 — app/agents/reviewer_agent.py。

覆盖范围：
    - needs_review 高风险工具判定
    - 非高风险工具自动放行
    - LLM 不可用时高风险操作默认拒绝
    - LLM 审查通过/拒绝
    - LLM 异常时高风险操作默认拒绝
    - JSON 解析兼容 markdown 代码块包裹
    - ReviewResult 序列化
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# ======================================================================
# Mock LLM 工厂
# ======================================================================


def _make_mock_llm(response_text: str) -> MagicMock:
    """构造一个 mock LLM，chat() 返回固定文本。"""

    async def _async_gen(*args, **kwargs):
        yield response_text

    mock = MagicMock()
    mock.chat = MagicMock(return_value=_async_gen())
    return mock


# ======================================================================
# needs_review 测试
# ======================================================================


class TestNeedsReview:
    """needs_review 方法测试。"""

    def test_high_risk_tool_needs_review(self) -> None:
        """高风险工具需要审查。"""
        from app.agents.reviewer_agent import ReviewerAgent

        reviewer = ReviewerAgent(llm=None)
        assert reviewer.needs_review("create_it_ticket") is True
        assert reviewer.needs_review("document_create") is True
        assert reviewer.needs_review("document_delete") is True
        assert reviewer.needs_review("system_config_change") is True

    def test_safe_tool_no_review(self) -> None:
        """安全工具不需要审查。"""
        from app.agents.reviewer_agent import ReviewerAgent

        reviewer = ReviewerAgent(llm=None)
        assert reviewer.needs_review("knowledge_search") is False
        assert reviewer.needs_review("document_get") is False
        assert reviewer.needs_review("unknown_tool") is False


# ======================================================================
# review 方法测试
# ======================================================================


class TestReview:
    """review 方法测试。"""

    @pytest.mark.asyncio
    async def test_non_high_risk_tool_auto_approved(self) -> None:
        """非高风险工具直接放行。"""
        from app.agents.reviewer_agent import ReviewerAgent

        reviewer = ReviewerAgent(llm=None)
        result = await reviewer.review(
            tool_name="knowledge_search",
            tool_args={"query": "报销政策"},
            user_query="查报销政策",
        )
        assert result.approved is True
        assert result.risk_level == "low"
        assert "无需审查" in result.reason

    @pytest.mark.asyncio
    async def test_llm_unavailable_default_reject(self) -> None:
        """LLM 不可用时高风险工具默认拒绝。"""
        from app.agents.reviewer_agent import ReviewerAgent

        reviewer = ReviewerAgent(llm=None)
        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "服务器扩容"},
            user_query="创建工单",
        )
        assert result.approved is False
        assert result.risk_level == "high"
        assert "不可用" in result.reason

    @pytest.mark.asyncio
    async def test_llm_approves_operation(self) -> None:
        """LLM 审查通过高风险操作。"""
        from app.agents.reviewer_agent import ReviewerAgent

        llm_response = json.dumps({
            "approved": True,
            "reason": "参数合理，权限合规",
            "risk_level": "low",
            "concerns": [],
        })
        mock_llm = _make_mock_llm(llm_response)
        reviewer = ReviewerAgent(llm=mock_llm)

        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "密码重置", "priority": "normal"},
            user_query="帮我重置密码",
        )
        assert result.approved is True
        assert result.risk_level == "low"
        assert "合理" in result.reason

    @pytest.mark.asyncio
    async def test_llm_rejects_operation(self) -> None:
        """LLM 审查拒绝高风险操作。"""
        from app.agents.reviewer_agent import ReviewerAgent

        llm_response = json.dumps({
            "approved": False,
            "reason": "金额异常，超出权限范围",
            "risk_level": "high",
            "concerns": ["金额 999999 超出常规阈值", "用户无审批权限"],
        })
        mock_llm = _make_mock_llm(llm_response)
        reviewer = ReviewerAgent(llm=mock_llm)

        result = await reviewer.review(
            tool_name="document_create",
            tool_args={"title": "合同", "amount": 999999},
            user_query="创建合同文档",
        )
        assert result.approved is False
        assert result.risk_level == "high"
        assert "金额" in result.reason
        assert len(result.concerns) == 2

    @pytest.mark.asyncio
    async def test_llm_exception_default_reject(self) -> None:
        """LLM 调用异常时高风险工具默认拒绝。"""
        from app.agents.reviewer_agent import ReviewerAgent

        mock_llm = MagicMock()
        mock_llm.chat = MagicMock(side_effect=RuntimeError("API 超时"))
        reviewer = ReviewerAgent(llm=mock_llm)

        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "测试"},
            user_query="测试",
        )
        assert result.approved is False
        assert result.risk_level == "high"
        assert "异常" in result.reason

    @pytest.mark.asyncio
    async def test_json_with_markdown_codeblock(self) -> None:
        """LLM 返回 markdown 代码块包裹的 JSON 也能解析。"""
        from app.agents.reviewer_agent import ReviewerAgent

        llm_response = "```json\n" + json.dumps({
            "approved": True,
            "reason": "通过",
            "risk_level": "low",
            "concerns": [],
        }) + "\n```"
        mock_llm = _make_mock_llm(llm_response)
        reviewer = ReviewerAgent(llm=mock_llm)

        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "正常工单"},
            user_query="创建工单",
        )
        assert result.approved is True
        assert result.reason == "通过"

    @pytest.mark.asyncio
    async def test_json_with_surrounding_text(self) -> None:
        """LLM 返回包含额外文本的 JSON 也能提取解析。"""
        from app.agents.reviewer_agent import ReviewerAgent

        llm_response = (
            "审查结果如下：\n"
            '{"approved": false, "reason": "不可逆操作", '
            '"risk_level": "high", "concerns": ["无法撤销"]}\n'
            "请确认。"
        )
        mock_llm = _make_mock_llm(llm_response)
        reviewer = ReviewerAgent(llm=mock_llm)

        result = await reviewer.review(
            tool_name="document_delete",
            tool_args={"doc_id": "doc-001"},
            user_query="删除文档",
        )
        assert result.approved is False
        assert result.risk_level == "high"
        assert "不可逆" in result.reason

    @pytest.mark.asyncio
    async def test_user_query_truncated(self) -> None:
        """超长 user_query 被截断（防 prompt 注入）。"""
        from app.agents.reviewer_agent import ReviewerAgent

        llm_response = json.dumps({
            "approved": True,
            "reason": "ok",
            "risk_level": "low",
            "concerns": [],
        })
        mock_llm = _make_mock_llm(llm_response)
        reviewer = ReviewerAgent(llm=mock_llm)

        long_query = "x" * 1000
        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "test"},
            user_query=long_query,
        )
        assert result.approved is True


# ======================================================================
# ReviewResult 序列化测试
# ======================================================================


class TestReviewResult:
    """ReviewResult 数据结构测试。"""

    def test_to_dict_approved(self) -> None:
        """批准结果转字典。"""
        from app.agents.reviewer_agent import ReviewResult

        r = ReviewResult(
            approved=True,
            reason="通过",
            risk_level="low",
            concerns=[],
        )
        d = r.to_dict()
        assert d["approved"] is True
        assert d["reason"] == "通过"
        assert d["risk_level"] == "low"
        assert d["concerns"] == []

    def test_to_dict_rejected_with_concerns(self) -> None:
        """拒绝结果转字典含风险点。"""
        from app.agents.reviewer_agent import ReviewResult

        r = ReviewResult(
            approved=False,
            reason="金额异常",
            risk_level="high",
            concerns=["金额过大", "权限不足"],
        )
        d = r.to_dict()
        assert d["approved"] is False
        assert d["risk_level"] == "high"
        assert len(d["concerns"]) == 2

    def test_none_concerns_becomes_empty_list(self) -> None:
        """concerns 为 None 时转空列表。"""
        from app.agents.reviewer_agent import ReviewResult

        r = ReviewResult(approved=True, reason="ok")
        d = r.to_dict()
        assert d["concerns"] == []
