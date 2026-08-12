"""冲突裁决器测试（P1-4）。

覆盖：
- last win：同 key 多值取最新时间戳；
- 权威优先：系统规则 > 用户输入 > 工具事实 > 已确认摘要 > 模型推测；
- 权威并列取最新；
- 单条/空声称边界。
"""
from __future__ import annotations

from app.context.conflict_resolver import ConflictClaim, ConflictResolver


def test_last_win_for_same_key():
    """同 key 且同权威 → last win（最新时间戳胜出）。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("5000", "tool_fact", "ERP", "2026-01-01", key="报销上限"),
        ConflictClaim("8000", "tool_fact", "ERP", "2026-02-01", key="报销上限"),
    ])
    assert result is not None
    assert result.resolved_value == "8000"
    assert result.reason == "last win"


def test_same_key_different_authority_uses_authority():
    """同 key 但不同权威 → 仍走权威优先（多源冲突）。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("10000", "system_rule", "公司制度", key="报销上限"),
        ConflictClaim("5000", "tool_fact", "ERP", "2026-02-01", key="报销上限"),
    ])
    assert result.resolved_value == "10000"
    assert result.reason == "权威优先"
    assert result.winner_authority == "system_rule"


def test_authority_priority_system_over_user():
    """系统规则 > 用户输入。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("10天", "system_rule", "公司制度"),
        ConflictClaim("3天", "user_input", "用户"),
    ])
    assert result is not None
    assert result.resolved_value == "10天"
    assert result.reason == "权威优先"
    assert result.winner_authority == "system_rule"


def test_authority_priority_full_chain():
    """完整权威序：系统规则 > 用户 > 工具 > 摘要 > 推测。"""
    resolver = ConflictResolver()
    # 全部冲突，应取最高权威 system_rule
    result = resolver.resolve([
        ConflictClaim("推测值", "model_inference", "模型"),
        ConflictClaim("工具值", "tool_fact", "ERP"),
        ConflictClaim("规则值", "system_rule", "制度"),
        ConflictClaim("用户值", "user_input", "用户"),
    ], same_key_only=False)
    assert result.resolved_value == "规则值"
    assert result.winner_authority == "system_rule"


def test_user_input_over_tool_and_summary():
    """用户输入 > 工具事实 > 已确认摘要。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("摘要值", "confirmed_summary", "历史摘要"),
        ConflictClaim("工具值", "tool_fact", "CRM"),
        ConflictClaim("用户新要求", "user_input", "用户"),
    ], same_key_only=False)
    assert result.resolved_value == "用户新要求"
    assert result.winner_authority == "user_input"


def test_authority_tie_uses_last_win():
    """权威并列时取最新时间戳。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("旧值", "tool_fact", "ERP", "2026-01-01"),
        ConflictClaim("新值", "tool_fact", "ERP", "2026-03-01"),
    ], same_key_only=False)
    assert result.resolved_value == "新值"
    assert result.reason == "权威并列,last win"


def test_single_claim_wins():
    """单条有效声称直接胜出。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("唯一值", "system_rule", "制度"),
    ])
    assert result.resolved_value == "唯一值"
    assert result.reason == "唯一来源"


def test_no_valid_claims_returns_none():
    """全部声称权威分 0（未知类型）时返回 None。"""
    resolver = ConflictResolver()
    result = resolver.resolve([
        ConflictClaim("x", "unknown_source", "s1"),
        ConflictClaim("y", "unknown_source", "s2"),
    ])
    assert result is None


def test_empty_claims_returns_none():
    """空声称返回 None。"""
    resolver = ConflictResolver()
    assert resolver.resolve([]) is None


def test_static_resolve_authority():
    """静态便捷入口按权威序裁决。"""
    result = ConflictResolver.resolve_authority([
        ConflictClaim("A", "user_input", "用户"),
        ConflictClaim("B", "system_rule", "制度"),
    ])
    assert result.resolved_value == "B"


def test_authority_score_ordering():
    """权威分值单调递减。"""
    resolver = ConflictResolver()
    scores = [resolver.authority_score(s) for s in (
        "system_rule", "user_input", "tool_fact", "confirmed_summary", "model_inference",
    )]
    assert scores == sorted(scores, reverse=True)
    assert resolver.authority_score("unknown") == 0