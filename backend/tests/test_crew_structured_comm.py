"""
多 Agent 结构化通信测试 — app/agents/crew.py。

覆盖范围：
    - _aggregate_results 结构化结果汇总
    - _build_crew_tasks 原始需求透传 + 结构化输出指令
    - execute_complex_task 防传话游戏设计（original_query 透传）
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# _aggregate_results 测试
# ======================================================================


class TestAggregateResults:
    """_aggregate_results 方法测试。"""

    def test_structured_output_contains_original_query(self) -> None:
        """结构化结果包含原始用户需求。"""
        from app.agents.crew import KnowledgeBaseCrew

        crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
        sub_tasks = [
            {"type": "qa", "description": "查报销政策", "expected_output": "政策摘要",
             "original_query": "查报销单 BG001 状态并创建新报销单"},
        ]
        result = crew._aggregate_results("报销政策已找到", sub_tasks)
        data = json.loads(result)
        assert data["original_query"] == "查报销单 BG001 状态并创建新报销单"

    def test_structured_output_contains_all_steps(self) -> None:
        """结构化结果包含所有子任务步骤。"""
        from app.agents.crew import KnowledgeBaseCrew

        crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
        sub_tasks = [
            {"type": "qa", "description": "查政策", "expected_output": "摘要"},
            {"type": "workflow", "description": "查审批", "expected_output": "进度"},
            {"type": "action", "description": "建报销单", "expected_output": "单号"},
        ]
        result = crew._aggregate_results("完成", sub_tasks)
        data = json.loads(result)
        assert data["task_count"] == 3
        assert len(data["results"]) == 3
        assert data["results"][0]["step"] == 1
        assert data["results"][1]["step"] == 2
        assert data["results"][2]["step"] == 3

    def test_structured_output_contains_summary(self) -> None:
        """结构化结果包含原始输出的摘要。"""
        from app.agents.crew import KnowledgeBaseCrew

        crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
        sub_tasks = [{"type": "qa", "description": "测试", "expected_output": "结果"}]
        raw = "这是一个很长的结果" + "x" * 600
        result = crew._aggregate_results(raw, sub_tasks)
        data = json.loads(result)
        assert len(data["summary"]) <= 500

    def test_empty_sub_tasks(self) -> None:
        """空子任务列表不崩溃。"""
        from app.agents.crew import KnowledgeBaseCrew

        crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
        result = crew._aggregate_results("test", [])
        data = json.loads(result)
        assert data["task_count"] == 0
        assert data["results"] == []

    def test_json_serialization_fallback(self) -> None:
        """JSON 序列化失败时回退到原始文本。"""
        from app.agents.crew import KnowledgeBaseCrew

        crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
        # 使用 set 触发 TypeError（set 不可 JSON 序列化）
        sub_tasks = [{"type": "qa", "description": "测试", "expected_output": "结果"}]
        with patch("json.dumps", side_effect=TypeError("not serializable")):
            result = crew._aggregate_results("raw text", sub_tasks)
        assert result == "raw text"


# ======================================================================
# _build_crew_tasks 原始需求透传测试
# ======================================================================


class TestBuildCrewTasksStructured:
    """_build_crew_tasks 结构化通信测试。"""

    def test_original_query_injected_into_description(self) -> None:
        """原始用户需求注入到每个任务描述中。"""
        # Mock CrewAI 模块
        with patch("app.agents.crew.CREWAI_AVAILABLE", True), \
             patch("app.agents.crew.CrewTask") as mock_task_cls:
            from app.agents.crew import KnowledgeBaseCrew

            crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
            mock_agent = MagicMock()
            agents = {"qa": mock_agent}
            sub_tasks = [
                {"type": "qa", "description": "查报销政策", "expected_output": "政策摘要"},
                {"type": "action", "description": "创建报销单", "expected_output": "单号"},
            ]

            crew._build_crew_tasks(sub_tasks, agents, original_query="退款但保留会员资格")

            # 验证每个 Task 的 description 包含原始需求
            assert mock_task_cls.call_count == 2
            for call in mock_task_cls.call_args_list:
                desc = call.kwargs.get("description", "")
                assert "退款但保留会员资格" in desc
                assert "不可修改" in desc

    def test_structured_output_instruction_in_description(self) -> None:
        """任务描述包含结构化 JSON 输出指令。"""
        with patch("app.agents.crew.CREWAI_AVAILABLE", True), \
             patch("app.agents.crew.CrewTask") as mock_task_cls:
            from app.agents.crew import KnowledgeBaseCrew

            crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
            mock_agent = MagicMock()
            agents = {"qa": mock_agent}
            sub_tasks = [{"type": "qa", "description": "测试", "expected_output": "结果"}]

            crew._build_crew_tasks(sub_tasks, agents, original_query="测试需求")

            call = mock_task_cls.call_args_list[0]
            desc = call.kwargs.get("description", "")
            assert "action_type" in desc
            assert "result_data" in desc
            assert "status" in desc

    def test_expected_output_marked_as_json(self) -> None:
        """期望输出标记为结构化 JSON 格式。"""
        with patch("app.agents.crew.CREWAI_AVAILABLE", True), \
             patch("app.agents.crew.CrewTask") as mock_task_cls:
            from app.agents.crew import KnowledgeBaseCrew

            crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
            mock_agent = MagicMock()
            agents = {"qa": mock_agent}
            sub_tasks = [{"type": "qa", "description": "测试", "expected_output": "政策摘要"}]

            crew._build_crew_tasks(sub_tasks, agents, original_query="测试")

            call = mock_task_cls.call_args_list[0]
            expected = call.kwargs.get("expected_output", "")
            assert "结构化 JSON" in expected
            assert "政策摘要" in expected

    def test_no_original_query_falls_back(self) -> None:
        """无 original_query 时使用原始描述。"""
        with patch("app.agents.crew.CREWAI_AVAILABLE", True), \
             patch("app.agents.crew.CrewTask") as mock_task_cls:
            from app.agents.crew import KnowledgeBaseCrew

            crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
            mock_agent = MagicMock()
            agents = {"qa": mock_agent}
            sub_tasks = [{"type": "qa", "description": "简单任务", "expected_output": "结果"}]

            crew._build_crew_tasks(sub_tasks, agents, original_query="")

            call = mock_task_cls.call_args_list[0]
            desc = call.kwargs.get("description", "")
            assert desc == "简单任务"
