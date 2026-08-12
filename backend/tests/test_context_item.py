"""Context Item 统一抽象 + 预算分配式注入测试（P0-1）。

覆盖：
- ContextItemBuilder：文档/工具/记忆统一为 ContextItem，token_cost 正确估算；
- BudgetAllocator：强制注入 mandatory 项、按 {优先级,相关性} 贪心择优、
  预算内放不下的被淘汰、单项超预算跳过、保持输入顺序。
"""
from __future__ import annotations

from app.rag.context_item import (
    BudgetAllocator,
    ContextItem,
    ContextItemBuilder,
)


class TestContextItemBuilder:
    """Context Item 构建测试。"""

    def test_build_merges_three_sources(self) -> None:
        """三类来源统一为 ContextItem。"""
        items = ContextItemBuilder.build(
            retrieved_docs=[
                {"content": "文档内容A", "title": "甲", "relevance": 0.9},
                {"content": "文档内容B", "title": "乙"},
            ],
            tool_results=["工具返回结果"],
            memory_context="用户偏好简洁回答",
        )
        kinds = [it.kind for it in items]
        assert kinds == ["memory", "tool", "document", "document"]

    def test_memory_has_highest_priority_and_mandatory(self) -> None:
        """记忆项优先级最高且强制注入。"""
        items = ContextItemBuilder.build(
            retrieved_docs=[{"content": "文档", "title": "t"}],
            tool_results=[],
            memory_context="必须遵守安全规范",
        )
        memory = items[0]
        assert memory.kind == "memory"
        assert memory.mandatory is True
        assert memory.priority > items[1].priority

    def test_token_cost_estimated(self) -> None:
        """token_cost 正确估算。"""
        items = ContextItemBuilder.build(
            retrieved_docs=[{"content": "A" * 140, "title": "t"}],
            tool_results=[],
            memory_context="",
        )
        assert items[0].token_cost > 0

    def test_document_relevance_defaults_to_order_decay(self) -> None:
        """无 relevance 时按顺序衰减。"""
        items = ContextItemBuilder.build(
            retrieved_docs=[{"content": "a", "title": "1"}, {"content": "b", "title": "2"}],
            tool_results=[],
            memory_context="",
        )
        assert items[0].relevance > items[1].relevance

    def test_empty_sources(self) -> None:
        """空输入返回空列表。"""
        assert ContextItemBuilder.build([], [], "") == []


class TestBudgetAllocator:
    """预算分配器测试。"""

    def test_select_all_within_budget(self) -> None:
        """预算充足时全量注入。"""
        items = [
            ContextItem(kind="document", content="a", token_cost=100),
            ContextItem(kind="document", content="b", token_cost=100),
        ]
        selected = BudgetAllocator(budget=500).select(items)
        assert len(selected) == 2

    def test_select_drops_overflowing_items(self) -> None:
        """预算不足时淘汰放不下的候选。"""
        items = [
            ContextItem(kind="document", content="a", token_cost=100, relevance=0.9),
            ContextItem(kind="document", content="b", token_cost=100, relevance=0.4),
        ]
        selected = BudgetAllocator(budget=120).select(items)
        assert len(selected) == 1
        assert selected[0].relevance == 0.9

    def test_mandatory_always_kept(self) -> None:
        """mandatory 项即使超出预算也保留。"""
        items = [
            ContextItem(kind="memory", content="约束", token_cost=300, mandatory=True),
            ContextItem(kind="document", content="d", token_cost=100),
        ]
        selected = BudgetAllocator(budget=100).select(items)
        assert [it.kind for it in selected] == ["memory"]

    def test_single_item_over_budget_skipped(self) -> None:
        """单项超预算直接跳过（不截断）。"""
        items = [ContextItem(kind="document", content="huge", token_cost=500)]
        selected = BudgetAllocator(budget=100).select(items)
        assert selected == []

    def test_preserves_input_order(self) -> None:
        """返回结果保持输入顺序。"""
        items = [
            ContextItem(kind="document", content="c1", token_cost=50, relevance=0.5),
            ContextItem(kind="document", content="c2", token_cost=50, relevance=0.9),
        ]
        selected = BudgetAllocator(budget=200).select(items)
        assert [it.content for it in selected] == ["c1", "c2"]

    def test_empty_and_zero_budget(self) -> None:
        """空列表与零预算均返回空。"""
        assert BudgetAllocator().select([]) == []
        assert BudgetAllocator().select(
            [ContextItem(kind="document", content="x", token_cost=10)], budget=0
        ) == []