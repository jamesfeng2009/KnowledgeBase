"""引擎冲突裁决集成测试（P1-4）。

覆盖对象（不触发 __init__，避免重型依赖）：
- _resolve_doc_conflicts：同 key 文档按权威序取一，剔除其余；
- 无冲突键 / 无法裁决时原样保留（零侵入）；
- _doc_ref 唯一引用。
"""
from __future__ import annotations

from app.rag.engine import AgenticRAGEngine


def _engine() -> AgenticRAGEngine:
    """构造一个不执行 __init__ 的引擎实例（仅测纯方法）。"""
    return object.__new__(AgenticRAGEngine)


def test_resolve_keeps_authoritative_doc():
    """同 key 冲突时保留权威高的文档（系统规则 > 工具事实）。"""
    engine = _engine()
    docs = [
        {
            "doc_id": "rule-1", "title": "公司制度",
            "content": "报销上限 10000",
            "conflict_key": "报销上限", "authority": "system_rule",
            "updated_at": "2026-01-01",
        },
        {
            "doc_id": "tool-1", "title": "ERP 数据",
            "content": "报销上限 5000",
            "conflict_key": "报销上限", "authority": "tool_fact",
            "updated_at": "2026-02-01",
        },
    ]
    result = engine._resolve_doc_conflicts(docs)
    assert len(result) == 1
    assert result[0]["doc_id"] == "rule-1"


def test_resolve_same_authority_uses_last_win():
    """同 key 同权威时按 last win（最新时间胜出）。"""
    engine = _engine()
    docs = [
        {
            "doc_id": "v1", "title": "旧版规范",
            "content": "接口超时 3s",
            "conflict_key": "接口超时", "authority": "tool_fact",
            "updated_at": "2026-01-01",
        },
        {
            "doc_id": "v2", "title": "新版规范",
            "content": "接口超时 5s",
            "conflict_key": "接口超时", "authority": "tool_fact",
            "updated_at": "2026-03-01",
        },
    ]
    result = engine._resolve_doc_conflicts(docs)
    assert len(result) == 1
    assert result[0]["doc_id"] == "v2"


def test_resolve_mixed_group_keeps_others_untouched():
    """无冲突键的文档不参与裁决，原样保留。"""
    engine = _engine()
    docs = [
        {"doc_id": "a", "content": "无关文档"},
        {
            "doc_id": "b", "content": "冲突A",
            "conflict_key": "K", "authority": "tool_fact", "updated_at": "2026-01-01",
        },
        {
            "doc_id": "c", "content": "冲突B",
            "conflict_key": "K", "authority": "system_rule", "updated_at": "2026-02-01",
        },
        {"doc_id": "d", "content": "另一无关文档"},
    ]
    result = engine._resolve_doc_conflicts(docs)
    # 保留 a、d（无冲突键）+ 胜出者 c
    ids = [d["doc_id"] for d in result]
    assert ids == ["a", "c", "d"]


def test_resolve_no_conflict_key_unchanged():
    """所有文档无冲突键 → 原样保留。"""
    engine = _engine()
    docs = [
        {"doc_id": "a", "content": "x"},
        {"doc_id": "b", "content": "y"},
    ]
    assert engine._resolve_doc_conflicts(docs) == docs


def test_resolve_empty_docs():
    """空列表原样返回。"""
    engine = _engine()
    assert engine._resolve_doc_conflicts([]) == []


def test_resolve_unresolvable_keeps_all():
    """权威未知无法裁决 → 全部保留（零侵入）。"""
    engine = _engine()
    docs = [
        {
            "doc_id": "a", "content": "x",
            "conflict_key": "K", "authority": "unknown_src", "updated_at": "2026-01-01",
        },
        {
            "doc_id": "b", "content": "y",
            "conflict_key": "K", "authority": "unknown_src", "updated_at": "2026-02-01",
        },
    ]
    assert len(engine._resolve_doc_conflicts(docs)) == 2


def test_doc_ref():
    """_doc_ref 返回稳定唯一引用。"""
    engine = _engine()
    assert engine._doc_ref({"doc_id": "abc"}) == "abc"
    assert engine._doc_ref({"chunk_id": "ck"}) == "ck"
    assert engine._doc_ref({})  # 兜底非空