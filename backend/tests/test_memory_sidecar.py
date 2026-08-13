"""副车道检索测试 — 验证 P2-1 SidecarMemoryRetriever 与 MemoryManager 集成。

覆盖：
- SidecarMemoryRetriever.refine_memory_query：轻量模型改写、失败回退原查询
- SidecarMemoryRetriever.retrieve：L3/L4 召回编排、mem0 异常降级
- MemoryManager.build_context：注入 sidecar 时走副车道；未注入时走原逻辑
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory.memory_manager import MemoryManager
from app.memory.sidecar import SidecarMemoryRetriever


class _FakeFact:
    """模拟 MemoryFact ORM 对象。"""

    def __init__(self, fact_text: str, category: str = "general"):
        self.id = uuid.uuid4()
        self.fact_text = fact_text
        self.category = category
        self.fact_key = ""
        self.fact_value = ""


class _FakeMem0:
    """模拟 Mem0Manager.search_facts。"""

    def __init__(self, facts: list[_FakeFact] | None = None):
        self.facts = facts or []
        self.calls: list[dict] = []

    async def search_facts(self, user_id, query=None, category=None, limit=10, **kwargs):
        self.calls.append({"query": query, "category": category, "limit": limit})
        return [f for f in self.facts if (category is None or f.category == category)]


class _FakeLLM:
    """模拟 LLM Provider，返回固定改写结果。"""

    def __init__(self, response: str = "用户长期偏好：关注成本控制"):
        self.response = response

    async def chat(self, messages, stream=True, max_tokens=None, **kwargs):
        yield self.response


class _FailingLLM:
    """模拟 LLM Provider 抛错。"""

    async def chat(self, messages, stream=True, max_tokens=None, **kwargs):
        raise RuntimeError("llm down")
        yield  # pragma: no cover


class TestRefineMemoryQuery:
    """副车道记忆查询改写。"""

    @pytest.mark.asyncio
    async def test_refine_uses_llm(self):
        llm = _FakeLLM(response="成本控制与预算偏好")
        sidecar = SidecarMemoryRetriever(llm=llm)
        result = await sidecar.refine_memory_query("我平时怎么省钱")
        assert result == "成本控制与预算偏好"

    @pytest.mark.asyncio
    async def test_refine_empty_query_returns_empty(self):
        sidecar = SidecarMemoryRetriever(llm=_FakeLLM())
        assert await sidecar.refine_memory_query("") == ""

    @pytest.mark.asyncio
    async def test_refine_failure_falls_back_to_original(self):
        sidecar = SidecarMemoryRetriever(llm=_FailingLLM())
        result = await sidecar.refine_memory_query("原始问题")
        assert result == "原始问题"


class TestSidecarRetrieve:
    """副车道 L3/L4 召回编排。"""

    @pytest.mark.asyncio
    async def test_retrieve_returns_facts(self):
        mem0 = _FakeMem0(
            [
                _FakeFact("偏好A", "general"),
                _FakeFact("工作B", "working"),
            ]
        )
        sidecar = SidecarMemoryRetriever(llm=_FakeLLM())
        result = await sidecar.retrieve(user_id=uuid.uuid4(), query="问题", mem0=mem0)
        # user_facts 无 category 过滤 → 含全部；working_memory 仅 working 类
        assert len(result["user_facts"]) == 2
        assert result["user_facts"][0]["fact_text"] == "偏好A"
        assert len(result["working_memory"]) == 1
        assert result["working_memory"][0]["fact_text"] == "工作B"

    @pytest.mark.asyncio
    async def test_retrieve_without_mem0_raises(self):
        sidecar = SidecarMemoryRetriever(llm=_FakeLLM())
        with pytest.raises(ValueError):
            await sidecar.retrieve(user_id=uuid.uuid4(), query="q", mem0=None)


class TestMemoryManagerSidecarIntegration:
    """MemoryManager 集成：注入 sidecar 走副车道，未注入走原逻辑。"""

    def _make_manager(self, sidecar: Any | None = None) -> MemoryManager:
        db = MagicMock()
        return MemoryManager(db=db, sidecar=sidecar)

    @pytest.mark.asyncio
    async def test_injected_sidecar_used(self):
        mem0 = _FakeMem0([_FakeFact("偏好A", "general")])
        sidecar = SidecarMemoryRetriever(llm=_FakeLLM())
        manager = self._make_manager(sidecar=sidecar)
        manager.mem0 = mem0  # 替换为 fake

        ctx = await manager.build_context(
            user_id=uuid.uuid4(), query="问题", recent_messages=[{"role": "user", "content": "hi"}]
        )
        # 副车道返回的 user_facts 被采用
        assert ctx.user_facts[0]["fact_text"] == "偏好A"
        # 副车道内部走了一次 mem0 检索
        assert len(mem0.calls) >= 1

    @pytest.mark.asyncio
    async def test_no_sidecar_uses_original_logic(self):
        mem0 = _FakeMem0([_FakeFact("偏好A", "general")])
        manager = self._make_manager(sidecar=None)
        manager.mem0 = mem0

        ctx = await manager.build_context(user_id=uuid.uuid4(), query="问题")
        assert ctx.user_facts[0]["fact_text"] == "偏好A"
        # 原逻辑直接调 mem0.search_facts
        assert len(mem0.calls) == 2  # L3 + L4
