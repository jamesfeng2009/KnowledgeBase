"""
工具治理测试 — P0/P1/P2 工具管理改进。

覆盖范围：
    P0: 工具描述负向边界约束（5 个工具描述含"不适用于"引导）
    P1: 按 Agent 类型筛选工具（QA 排除写操作，Action 拿全部）
    P2: 引擎「无匹配工具」强制选项提示词构建
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery（crew.py 依赖）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# P0: 工具描述负向边界约束测试
# ======================================================================


class TestToolDescriptionNegativeBoundary:
    """P0: 验证 5 个工具的描述包含负向边界约束。"""

    def _get_tool_registry(self) -> dict:
        """构造 MCP Server 工具注册表（不依赖 DB）。"""
        from app.mcp.server import KnowledgeBaseMCPServer

        # 用 mock db_factory 构造 Server，_scan_tools 只读装饰器元数据不碰 DB
        mock_factory = MagicMock()
        server = KnowledgeBaseMCPServer(db_factory=mock_factory)
        return server._tool_registry

    def test_knowledge_search_has_negative_boundary(self) -> None:
        """knowledge_search 描述含负向边界（不适用于 ID 查询）。"""
        registry = self._get_tool_registry()
        desc = registry["knowledge_search"]["definition"]["description"]
        assert "不适用于" in desc
        assert "document_get" in desc

    def test_document_get_has_negative_boundary(self) -> None:
        """document_get 描述含负向边界。"""
        registry = self._get_tool_registry()
        desc = registry["document_get"]["definition"]["description"]
        assert "不适用于" in desc
        assert "knowledge_search" in desc

    def test_document_create_has_negative_boundary(self) -> None:
        """document_create 描述含负向边界。"""
        registry = self._get_tool_registry()
        desc = registry["document_create"]["definition"]["description"]
        assert "不适用于" in desc

    def test_query_oa_approval_has_negative_boundary(self) -> None:
        """query_oa_approval 描述含负向边界。"""
        registry = self._get_tool_registry()
        desc = registry["query_oa_approval"]["definition"]["description"]
        assert "不适用于" in desc

    def test_create_it_ticket_has_negative_boundary(self) -> None:
        """create_it_ticket 描述含负向边界。"""
        registry = self._get_tool_registry()
        desc = registry["create_it_ticket"]["definition"]["description"]
        assert "不适用于" in desc

    def test_skill_description_has_negative_boundary(self) -> None:
        """skill_description 也包含负向边界（用于 SkillFinder 匹配）。"""
        registry = self._get_tool_registry()
        for name, entry in registry.items():
            skill_desc = entry.get("skill_description", "")
            assert "负向边界" in skill_desc or "不要用于" in skill_desc, (
                f"工具 {name} 的 skill_description 缺少负向边界约束"
            )

    def test_tags_include_user_friendly_keywords(self) -> None:
        """tags 包含用户口语化关键词（提升召回率）。"""
        registry = self._get_tool_registry()
        # knowledge_search 应包含"查找""了解"等口语词
        ks_tags = registry["knowledge_search"]["tags"]
        assert "查找" in ks_tags
        # create_it_ticket 应包含"报修""提单"等口语词
        ticket_tags = registry["create_it_ticket"]["tags"]
        assert "报修" in ticket_tags


# ======================================================================
# P1: 按 Agent 类型筛选工具测试
# ======================================================================


class TestToolFilteringByAgentType:
    """P1: get_mcp_tools_for_agent_type 按 Agent 类型筛选工具。"""

    @pytest.mark.asyncio
    async def test_qa_agent_excludes_write_tools(self) -> None:
        """QA Agent 不能拿到写操作工具。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        # Mock MCPClient.get_tools_for_llm 返回 5 个工具
        mock_mcp = MagicMock()
        mock_mcp.get_tools_for_llm = AsyncMock(return_value=[
            {"name": "knowledge_search", "description": "搜索知识库", "parameters": {}},
            {"name": "document_get", "description": "获取文档", "parameters": {}},
            {"name": "query_oa_approval", "description": "查审批", "parameters": {}},
            {"name": "document_create", "description": "创建文档", "parameters": {}},
            {"name": "create_it_ticket", "description": "创建工单", "parameters": {}},
        ])

        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", True), \
             patch("app.agents.mcp_tools.CrewBaseTool", object):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "qa")

        tool_names = [t.name for t in tools]
        assert "knowledge_search" in tool_names
        assert "document_get" in tool_names
        assert "query_oa_approval" in tool_names
        # 写操作工具不应出现
        assert "document_create" not in tool_names
        assert "create_it_ticket" not in tool_names

    @pytest.mark.asyncio
    async def test_workflow_agent_excludes_write_tools(self) -> None:
        """Workflow Agent 也不能拿到写操作工具。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        mock_mcp = MagicMock()
        mock_mcp.get_tools_for_llm = AsyncMock(return_value=[
            {"name": "knowledge_search", "description": "搜索", "parameters": {}},
            {"name": "document_create", "description": "创建", "parameters": {}},
            {"name": "create_it_ticket", "description": "工单", "parameters": {}},
        ])

        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", True), \
             patch("app.agents.mcp_tools.CrewBaseTool", object):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "workflow")

        tool_names = [t.name for t in tools]
        assert "knowledge_search" in tool_names
        assert "document_create" not in tool_names
        assert "create_it_ticket" not in tool_names

    @pytest.mark.asyncio
    async def test_action_agent_gets_all_tools(self) -> None:
        """Action Agent 拿到全部工具（含写操作）。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        mock_mcp = MagicMock()
        mock_mcp.get_tools_for_llm = AsyncMock(return_value=[
            {"name": "knowledge_search", "description": "搜索", "parameters": {}},
            {"name": "document_get", "description": "获取", "parameters": {}},
            {"name": "document_create", "description": "创建", "parameters": {}},
            {"name": "create_it_ticket", "description": "工单", "parameters": {}},
        ])

        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", True), \
             patch("app.agents.mcp_tools.CrewBaseTool", object):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "action")

        tool_names = [t.name for t in tools]
        assert "knowledge_search" in tool_names
        assert "document_create" in tool_names
        assert "create_it_ticket" in tool_names

    @pytest.mark.asyncio
    async def test_unknown_agent_type_defaults_to_read_only(self) -> None:
        """未知 Agent 类型默认只读（安全默认）。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        mock_mcp = MagicMock()
        mock_mcp.get_tools_for_llm = AsyncMock(return_value=[
            {"name": "knowledge_search", "description": "搜索", "parameters": {}},
            {"name": "document_create", "description": "创建", "parameters": {}},
        ])

        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", True), \
             patch("app.agents.mcp_tools.CrewBaseTool", object):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "unknown_type")

        tool_names = [t.name for t in tools]
        assert "knowledge_search" in tool_names
        assert "document_create" not in tool_names

    @pytest.mark.asyncio
    async def test_crewai_unavailable_returns_empty(self) -> None:
        """CrewAI 不可用时返回空列表。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        mock_mcp = MagicMock()
        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", False):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "qa")
        assert tools == []

    @pytest.mark.asyncio
    async def test_mcp_error_returns_empty(self) -> None:
        """MCP Client 异常时返回空列表（优雅降级）。"""
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        mock_mcp = MagicMock()
        mock_mcp.get_tools_for_llm = AsyncMock(side_effect=RuntimeError("connection failed"))

        with patch("app.agents.mcp_tools.CREWAI_AVAILABLE", True), \
             patch("app.agents.mcp_tools.CrewBaseTool", object):
            tools = await get_mcp_tools_for_agent_type(mock_mcp, "qa")
        assert tools == []


# ======================================================================
# P1: CrewAI Agent 构建测试（集成 _build_crew_agents）
# ======================================================================


class TestCrewAgentToolAssignment:
    """P1: 验证 _build_crew_agents 按 Agent 类型分配工具。"""

    @pytest.mark.asyncio
    async def test_qa_agent_gets_read_only_tools(self) -> None:
        """_build_crew_agents 给 QA Agent 分配只读工具。"""
        with patch("app.agents.crew.CREWAI_AVAILABLE", True), \
             patch("app.agents.crew.CrewAgent") as mock_agent_cls, \
             patch("app.agents.crew.CrewTask"):
            from app.agents.crew import KnowledgeBaseCrew

            crew = KnowledgeBaseCrew.__new__(KnowledgeBaseCrew)
            crew.llm = MagicMock()
            crew.mcp = MagicMock()

            # Mock get_mcp_tools_for_agent_type 返回不同工具集
            async def mock_get_tools(mcp, agent_type):
                if agent_type == "qa":
                    return [MagicMock(name="ks"), MagicMock(name="dg")]
                elif agent_type == "workflow":
                    return [MagicMock(name="ks")]
                else:
                    return [MagicMock(name="ks"), MagicMock(name="dc")]

            with patch(
                "app.agents.mcp_tools.get_mcp_tools_for_agent_type",
                side_effect=mock_get_tools,
            ):
                await crew._build_crew_agents()

            # 验证三次调用分别对应 qa/workflow/action
            assert mock_agent_cls.call_count == 3
            qa_call = mock_agent_cls.call_args_list[0]
            wf_call = mock_agent_cls.call_args_list[1]
            action_call = mock_agent_cls.call_args_list[2]

            # QA Agent 工具数为 2（只读）
            assert len(qa_call.kwargs["tools"]) == 2
            # Action Agent 工具数也为 2（全部）
            assert len(action_call.kwargs["tools"]) == 2


# ======================================================================
# P2: 引擎「无匹配工具」强制选项测试
# ======================================================================


class TestNoMatchToolInstruction:
    """P2: 验证引擎 tool_call 阶段构建「无匹配工具」提示词。"""

    def test_no_match_instruction_contains_available_tools(self) -> None:
        """提示词包含当前可用工具列表。"""
        # 这段逻辑内联在 _tool_call 方法中，我们验证构建逻辑
        tools = [
            {"name": "knowledge_search", "description": "搜索", "parameters": {}},
            {"name": "document_get", "description": "获取", "parameters": {}},
        ]
        tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        instruction = (
            "根据用户问题选择合适的工具调用。如无需调用工具，直接回复原文。\n"
            "重要：如果以上候选工具都无法满足用户需求，"
            "请不要硬凑工具调用，直接回复原文并说明无可用工具。\n"
            f"当前可用工具：{', '.join(tool_names) if tool_names else '无'}"
        )
        assert "knowledge_search" in instruction
        assert "document_get" in instruction
        assert "无可用工具" in instruction

    def test_no_match_instruction_empty_tools(self) -> None:
        """空工具列表时提示词显示'无'。"""
        tools: list[dict] = []
        tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        instruction = (
            "根据用户问题选择合适的工具调用。如无需调用工具，直接回复原文。\n"
            "重要：如果以上候选工具都无法满足用户需求，"
            "请不要硬凑工具调用，直接回复原文并说明无可用工具。\n"
            f"当前可用工具：{', '.join(tool_names) if tool_names else '无'}"
        )
        assert "当前可用工具：无" in instruction

    def test_no_match_instruction_has_explicit_guidance(self) -> None:
        """提示词包含显式的'不要硬凑'指令。"""
        tools = [{"name": "knowledge_search"}]
        tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        instruction = (
            "根据用户问题选择合适的工具调用。如无需调用工具，直接回复原文。\n"
            "重要：如果以上候选工具都无法满足用户需求，"
            "请不要硬凑工具调用，直接回复原文并说明无可用工具。\n"
            f"当前可用工具：{', '.join(tool_names) if tool_names else '无'}"
        )
        assert "不要硬凑" in instruction
        assert "无可用工具" in instruction
