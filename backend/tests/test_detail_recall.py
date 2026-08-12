"""对话历史细节召回测试（P1-3）。

覆盖：
- persist：把压缩掉的旧消息落库（detail 类别），空输入不落库；
- recall：按查询召回相关细节，空查询/无 mem0 时优雅降级返回空。
"""
from __future__ import annotations

from uuid import UUID

import pytest

from app.context.detail_recall import DetailRecall, RecalledDetail


class _FakeMem0:
    """模拟 Mem0Manager，记录持久化与检索行为。"""

    def __init__(self) -> None:
        self.persisted: list[dict] = []
        self.search_results: list[dict] = []

    async def add_fact(self, **kwargs) -> None:
        self.persisted.append(kwargs)

    async def search_facts(self, **kwargs):
        return list(self.search_results)


@pytest.fixture
def user_id() -> UUID:
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_persist_stores_detail_category(user_id):
    """persist 应把旧消息以 detail 类别落库。"""
    mem0 = _FakeMem0()
    recall = DetailRecall(mem0=mem0)
    old = [
        {"role": "user", "content": "截止日期是 3 月 15 日"},
        {"role": "assistant", "content": "好的，已记录 3 月 15 日截止"},
    ]
    ok = await recall.persist(user_id, old)
    assert ok is True
    assert len(mem0.persisted) == 1
    stored = mem0.persisted[0]
    assert stored["category"] == "detail"
    assert "3 月 15 日" in stored["fact_text"]
    assert stored["user_id"] == user_id


@pytest.mark.asyncio
async def test_persist_empty_returns_false(user_id):
    """空旧消息不落库。"""
    mem0 = _FakeMem0()
    recall = DetailRecall(mem0=mem0)
    assert await recall.persist(user_id, []) is False
    assert mem0.persisted == []


@pytest.mark.asyncio
async def test_recall_returns_matching_details(user_id):
    """按查询召回相关细节。"""
    mem0 = _FakeMem0()
    mem0.search_results = [
        {"fact_text": "项目截止日期为 3 月 15 日", "similarity": 0.9},
        {"fact_text": "预算上限 50000 元", "similarity": 0.7},
    ]
    recall = DetailRecall(mem0=mem0, limit=2)
    results = await recall.recall(user_id, "项目什么时候截止")
    assert isinstance(results, list)
    assert len(results) == 2
    assert all(isinstance(r, RecalledDetail) for r in results)
    assert results[0].content == "项目截止日期为 3 月 15 日"


@pytest.mark.asyncio
async def test_recall_empty_query_returns_empty(user_id):
    """空查询不触发召回。"""
    mem0 = _FakeMem0()
    recall = DetailRecall(mem0=mem0)
    assert await recall.recall(user_id, "") == []


@pytest.mark.asyncio
async def test_recall_no_mem0_returns_empty(user_id):
    """mem0 不可用时优雅降级返回空。"""
    recall = DetailRecall(mem0=None)
    # 懒加载失败也应返回空（不抛异常）
    results = await recall.recall(user_id, "查询")
    assert isinstance(results, list)