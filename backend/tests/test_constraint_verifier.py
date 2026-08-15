"""约束核验器测试 — Phase 3 三层消费（L2 post_verify / L3 tool_gate）。

覆盖：
    ConstraintVerifier   禁词（正则/非法正则回退字面）/ 必提词（话题门）/
                         金额上限（单位折算 / 外币不比较）/ still_violates
    ToolGate             工具名过滤（tools / 缺省 *）/ 参数金额（裸数字按元）/
                         禁用模式 / param_keys 限定扫描
    Engine L2            _check_constraints：block 重生成采用 / 仍违规则拒答 /
                         warn 透出 / 开关关闭 / 非 post_verify 规则跳过
    Engine L3            _execute_tool_use：tool_gate block 阻断 / confirm
                         并入守卫审批分支 / 开关关闭放行

mock 策略：engine 用 __new__ 绕过重型构造（只赋被测属性）；
generator 为可控 token 流；审计 _schedule_constraint_audit 打桩 —
不依赖真实 PG / LLM。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Mock celery（测试环境未安装）— 与 test_constraint_channel.py 同款
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.config import get_settings
from app.rag.constraint_verifier import (
    ConstraintVerifier,
    ToolGate,
)
from app.rag.tool_guard import DangerousToolGuard

RULE_ID = str(uuid4())


# ======================================================================
# 构造工具
# ======================================================================


def _rule(
    *,
    rule_id: str = RULE_ID,
    actions: tuple[str, ...] = ("inject", "post_verify"),
    severity: str = "block",
    normalized: dict[str, Any] | None = None,
    rule_text: str = "单笔金额超过 5000 元的报销必须双人签批",
) -> dict[str, Any]:
    if normalized is None:
        normalized = {
            "statement": rule_text,
            "condition": {"topic": ["报销"]},
            "required_mentions": ["双签", "双人签批"],
            "forbidden_patterns": [],
            "amount_limits": [
                {"op": "gt", "value": 5000, "on_violation": "block"}
            ],
        }
    return {
        "rule_id": rule_id,
        "rule_text": rule_text,
        "severity": severity,
        "actions": list(actions),
        "normalized": normalized,
        "triggers": ["T2:entity"],
    }


class _FakeGenerator:
    """token 流 generator — 记录调用参数供断言。"""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any):
        self.calls.append(kwargs)
        for token in self.tokens:
            yield token


def _engine(**attrs: Any) -> Any:
    """绕过重型构造的 engine 实例（只赋被测属性）。"""
    from app.rag.engine import AgenticRAGEngine

    engine = AgenticRAGEngine.__new__(AgenticRAGEngine)
    engine._trace_ctx = None
    engine._tool_guard = DangerousToolGuard()
    engine.mcp = SimpleNamespace(call_tool=AsyncMock(return_value="ok"))
    engine._schedule_constraint_audit = MagicMock()
    for key, value in attrs.items():
        setattr(engine, key, value)
    return engine


# ======================================================================
# ConstraintVerifier — L2 零 LLM 核验
# ======================================================================


class TestVerifierForbiddenPattern:
    def test_hit_yields_violation(self) -> None:
        rule = _rule(
            normalized={"forbidden_patterns": ["跨级审批"]},
        )
        violations = ConstraintVerifier.verify(
            "报销单可以直接跨级审批提交", rule
        )
        assert len(violations) == 1
        assert violations[0].check == "forbidden_pattern"
        assert violations[0].severity == "block"
        assert "跨级审批" in violations[0].detail

    def test_regex_pattern(self) -> None:
        rule = _rule(normalized={"forbidden_patterns": ["免签\\d+次"]})
        assert ConstraintVerifier.verify("允许免签3次", rule)
        assert not ConstraintVerifier.verify("不允许免签", rule)

    def test_invalid_regex_falls_back_literal(self) -> None:
        rule = _rule(normalized={"forbidden_patterns": ["跨级(审批"]})
        assert ConstraintVerifier.verify("可以跨级(审批提交", rule)

    def test_clean_answer_passes(self) -> None:
        rule = _rule(normalized={"forbidden_patterns": ["跨级审批"]})
        assert ConstraintVerifier.verify("正常逐级审批", rule) == []


class TestVerifierRequiredMentions:
    def test_topic_hit_mention_missing_violates(self) -> None:
        rule = _rule(
            normalized={
                "condition": {"topic": ["报销"]},
                "required_mentions": ["双签", "双人签批"],
            }
        )
        violations = ConstraintVerifier.verify(
            "8000 元的报销可以直接提交", rule
        )
        assert len(violations) == 1
        assert violations[0].check == "required_mentions"

    def test_mention_present_passes(self) -> None:
        rule = _rule(
            normalized={
                "condition": {"topic": ["报销"]},
                "required_mentions": ["双签"],
            }
        )
        assert ConstraintVerifier.verify("报销需双签", rule) == []

    def test_topic_not_satisfied_skips_check(self) -> None:
        rule = _rule(
            normalized={
                "condition": {"topic": ["报销"]},
                "required_mentions": ["双签"],
            }
        )
        # 无话题词 → 必提词不适用
        assert ConstraintVerifier.verify("采购需要审批", rule) == []

    def test_no_condition_always_enforced(self) -> None:
        rule = _rule(normalized={"required_mentions": ["双签"]})
        assert ConstraintVerifier.verify("采购需要审批", rule)


class TestVerifierAmountLimits:
    def test_over_limit_violates(self) -> None:
        rule = _rule(
            normalized={
                "amount_limits": [
                    {"op": "gt", "value": 5000, "on_violation": "block"}
                ]
            }
        )
        violations = ConstraintVerifier.verify("报销 8000 元", rule)
        assert len(violations) == 1
        assert violations[0].check == "amount_limit"
        assert violations[0].severity == "block"

    def test_wan_unit_converted_to_yuan_base(self) -> None:
        rule = _rule(
            normalized={
                "amount_limits": [{"op": "gt", "value": 5000}]
            }
        )
        # 5 万元 = 50000 元 > 5000 → 违规
        assert ConstraintVerifier.verify("预算 5 万元", rule)

    def test_under_limit_passes(self) -> None:
        rule = _rule(
            normalized={
                "amount_limits": [{"op": "gt", "value": 5000}]
            }
        )
        assert ConstraintVerifier.verify("报销 3000 元", rule) == []

    def test_foreign_currency_not_compared(self) -> None:
        rule = _rule(
            normalized={
                "amount_limits": [{"op": "gt", "value": 5000}]
            }
        )
        # 1000 美元 ≈ 7000 元，但无汇率来源不跨币种比较 — 不误判
        assert ConstraintVerifier.verify("采购 1000 美元", rule) == []

    def test_on_violation_overrides_severity(self) -> None:
        rule = _rule(
            severity="warn",
            normalized={
                "amount_limits": [
                    {"op": "gt", "value": 100, "on_violation": "confirm"}
                ]
            },
        )
        violations = ConstraintVerifier.verify("报销 200 元", rule)
        assert violations[0].severity == "confirm"


class TestVerifierStillViolates:
    def test_clean_answer_passes(self) -> None:
        rule = _rule()
        violations = ConstraintVerifier.verify("8000 元的报销直接提交", rule)
        assert any(v.severity == "block" for v in violations)
        assert not ConstraintVerifier.still_violates(
            "报销 3000 元需双签", violations
        )

    def test_still_violating_answer_fails(self) -> None:
        rule = _rule()
        violations = ConstraintVerifier.verify(
            "8000 元的报销无需双签直接提交", rule
        )
        assert ConstraintVerifier.still_violates(
            "8000 元的报销无需双签直接提交", violations
        )

    def test_warn_only_not_blocking(self) -> None:
        rule = _rule(
            severity="warn", normalized={"forbidden_patterns": ["跨级审批"]}
        )
        violations = ConstraintVerifier.verify("可跨级审批", rule)
        assert violations[0].severity == "warn"
        assert not ConstraintVerifier.still_violates("任意答案", violations)

    def test_no_normalized_no_violation(self) -> None:
        assert ConstraintVerifier.verify("任何内容", _rule(normalized={})) == []


# ======================================================================
# ToolGate — L3 工具门
# ======================================================================


def _gate_rule(
    *,
    actions: tuple[str, ...] = ("inject", "tool_gate"),
    severity: str = "block",
    tools: list[str] | None = None,
    param_keys: list[str] | None = None,
    amount_limits: list[dict] | None = None,
    forbidden_patterns: list[str] | None = None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if tools is not None or param_keys is not None:
        cfg: dict[str, Any] = {}
        if tools is not None:
            cfg["tools"] = tools
        if param_keys is not None:
            cfg["param_keys"] = param_keys
        normalized["tool_gate"] = cfg
    if amount_limits is not None:
        normalized["amount_limits"] = amount_limits
    if forbidden_patterns is not None:
        normalized["forbidden_patterns"] = forbidden_patterns
    return _rule(actions=actions, severity=severity, normalized=normalized)


class TestToolGate:
    def test_rule_without_tool_gate_action_ignored(self) -> None:
        rule = _gate_rule(
            actions=("inject", "post_verify"),
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "block"}],
        )
        assert ToolGate.check([rule], "place_order", {"amount": 8000}) is None

    def test_tool_not_in_tools_none(self) -> None:
        rule = _gate_rule(
            tools=["place_order"],
            amount_limits=[{"op": "gt", "value": 5000}],
        )
        assert ToolGate.check([rule], "knowledge_search", {"amount": 8000}) is None

    def test_default_star_applies_to_all_tools(self) -> None:
        rule = _gate_rule(amount_limits=[{"op": "gt", "value": 5000}])
        decision = ToolGate.check([rule], "knowledge_search", {"amount": 8000})
        assert decision is not None
        assert decision.action == "block"

    def test_numeric_param_amount_blocks(self) -> None:
        rule = _gate_rule(
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "block"}]
        )
        decision = ToolGate.check([rule], "place_order", {"amount": 8000})
        assert decision is not None
        assert decision.action == "block"
        assert decision.rule_id == RULE_ID
        assert decision.violation is not None

    def test_string_param_amount_parsed(self) -> None:
        rule = _gate_rule(amount_limits=[{"op": "gt", "value": 5000}])
        decision = ToolGate.check(
            [rule], "place_order", {"note": "采购金额 6 万元"}
        )
        assert decision is not None and decision.action == "block"

    def test_on_violation_confirm(self) -> None:
        rule = _gate_rule(
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "confirm"}]
        )
        decision = ToolGate.check([rule], "place_order", {"amount": 8000})
        assert decision is not None and decision.action == "confirm"

    def test_forbidden_pattern_in_args(self) -> None:
        rule = _gate_rule(
            severity="block", forbidden_patterns=["跳过审批"]
        )
        decision = ToolGate.check(
            [rule], "place_order", {"note": "本次跳过审批直接下单"}
        )
        assert decision is not None and decision.action == "block"

    def test_param_keys_restrict_scan(self) -> None:
        rule = _gate_rule(
            param_keys=["note"],
            amount_limits=[{"op": "gt", "value": 5000}],
        )
        # 金额在 amount 键，param_keys 限定只扫 note → 不拦截
        assert (
            ToolGate.check([rule], "place_order", {"amount": 8000, "note": "常规"})
            is None
        )

    def test_clean_input_passes(self) -> None:
        rule = _gate_rule(amount_limits=[{"op": "gt", "value": 5000}])
        assert ToolGate.check([rule], "place_order", {"amount": 3000}) is None


# ======================================================================
# Engine L2 — _check_constraints（挂 _reflect，§8.1）
# ======================================================================


class TestEngineCheckConstraints:
    @pytest.mark.asyncio
    async def test_block_regenerated_and_adopted(self, monkeypatch) -> None:
        """block 违规 → 重生成通过核验 → 采用新答案 + regenerate 审计。"""
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", True
        )
        gen = _FakeGenerator(["报销 ", "3000 元需双签", "审批"])
        engine = _engine(generator=gen)
        state = {
            "query": "8000 元报销怎么走",
            "answer": "8000 元的报销直接提交即可",
            "injected_constraints": [_rule()],
            "session_id": "s1",
        }
        await engine._check_constraints(state)

        assert not state.get("constraint_blocked")
        assert state["answer"] == "报销 3000 元需双签审批"
        assert state["answer_regenerated"] is True
        # strict prompt 携带违规条款原文
        assert "双人签批" in gen.calls[0]["memory_context"]
        engine._schedule_constraint_audit.assert_called_once()
        assert (
            engine._schedule_constraint_audit.call_args.kwargs["action"]
            == "post_verify_regenerate"
        )

    @pytest.mark.asyncio
    async def test_block_still_violates_refuses(self, monkeypatch) -> None:
        """block 违规 → 重生成仍违规 → 拒答话术 + constraint_blocked。"""
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", True
        )
        gen = _FakeGenerator(["8000 元的报销无需双签直接提交"])
        engine = _engine(generator=gen)
        state = {
            "query": "8000 元报销怎么走",
            "answer": "8000 元的报销直接提交即可",
            "injected_constraints": [_rule()],
            "session_id": "s1",
        }
        await engine._check_constraints(state)

        assert state["constraint_blocked"] is True
        assert state["low_confidence"] is True
        assert "无法在遵守约束" in state["answer"]
        assert state["answer_regenerated"] is True
        assert (
            engine._schedule_constraint_audit.call_args.kwargs["action"]
            == "post_verify_block"
        )

    @pytest.mark.asyncio
    async def test_warn_violation_surfaced(self, monkeypatch) -> None:
        """warn 违规 → constraint_warnings 透出，答案不变。"""
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", True
        )
        engine = _engine(generator=_FakeGenerator(["不应被调用"]))
        state = {
            "query": "报销流程",
            "answer": "可以跨级审批",
            "injected_constraints": [
                _rule(
                    severity="warn",
                    normalized={"forbidden_patterns": ["跨级审批"]},
                )
            ],
            "session_id": "s1",
        }
        await engine._check_constraints(state)

        assert not state.get("constraint_blocked")
        assert state["answer"] == "可以跨级审批"
        warnings = state["constraint_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["check"] == "forbidden_pattern"
        assert warnings[0]["severity"] == "warn"
        assert (
            engine._schedule_constraint_audit.call_args.kwargs["action"]
            == "post_verify_warn"
        )

    @pytest.mark.asyncio
    async def test_switch_off_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", False
        )
        engine = _engine(generator=_FakeGenerator(["x"]))
        state = {
            "query": "q",
            "answer": "8000 元的报销直接提交即可",
            "injected_constraints": [_rule()],
        }
        await engine._check_constraints(state)
        assert not state.get("constraint_blocked")
        assert not state.get("constraint_warnings")
        engine._schedule_constraint_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_post_verify_rule_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", True
        )
        engine = _engine(generator=_FakeGenerator(["x"]))
        state = {
            "query": "q",
            "answer": "8000 元的报销直接提交即可",
            "injected_constraints": [_rule(actions=("inject",))],
        }
        await engine._check_constraints(state)
        assert not state.get("constraint_blocked")
        assert not state.get("constraint_warnings")
        engine._schedule_constraint_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_injected_rules_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            get_settings(), "CONSTRAINT_VERIFY_ON_GENERATION", True
        )
        engine = _engine(generator=_FakeGenerator(["x"]))
        await engine._check_constraints({"query": "q", "answer": "a"})
        engine._schedule_constraint_audit.assert_not_called()


# ======================================================================
# Engine L3 — tool_gate 接入 _execute_tool_use（§8.2）
# ======================================================================


def _tool_use(name: str, input: dict) -> dict:
    return {"name": name, "input": input, "id": "tu-1"}


def _gate_state(rules: list[dict]) -> dict:
    return {
        "query": "下单",
        "session_id": "s1",
        "tool_results": [],
        "injected_constraints": rules,
    }


class TestEngineToolGate:
    @pytest.mark.asyncio
    async def test_gate_blocks_before_execution(self, monkeypatch) -> None:
        """block 决策 → 不执行真实工具 + 结构化错误 + 审计。"""
        monkeypatch.setattr(get_settings(), "CONSTRAINT_TOOL_GATE_ENABLED", True)
        engine = _engine()
        rule = _gate_rule(
            tools=["place_order"],
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "block"}],
        )
        state = _gate_state([rule])

        events = [
            e
            async for e in engine._execute_tool_use(
                state, _tool_use("place_order", {"amount": 8000})
            )
        ]
        assert events == []  # block 路径不发审批事件
        engine.mcp.call_tool.assert_not_awaited()
        assert len(state["tool_results"]) == 1
        result = state["tool_results"][0]["result"]
        assert "被约束条款阻断" in result
        assert rule["rule_id"] in result
        engine._schedule_constraint_audit.assert_called_once()
        assert (
            engine._schedule_constraint_audit.call_args.kwargs["action"]
            == "tool_gate_block"
        )

    @pytest.mark.asyncio
    async def test_gate_confirm_routes_to_approval_flow(
        self, monkeypatch
    ) -> None:
        """confirm 决策 → 并入守卫 CONFIRM 分支（审批话术 + 不执行）。"""
        monkeypatch.setattr(get_settings(), "CONSTRAINT_TOOL_GATE_ENABLED", True)
        engine = _engine()
        rule = _gate_rule(
            tools=["knowledge_search"],
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "confirm"}],
        )
        state = _gate_state([rule])

        events = [
            e
            async for e in engine._execute_tool_use(
                state, _tool_use("knowledge_search", {"amount": 8000})
            )
        ]
        # db=None → 不创建审批记录，不发 approval_required，但工具被拦截
        assert events == []
        engine.mcp.call_tool.assert_not_awaited()
        assert len(state["tool_results"]) == 1
        assert "需要用户确认" in state["tool_results"][0]["result"]
        assert state.get("high_risk_confirm") is True

    @pytest.mark.asyncio
    async def test_gate_disabled_allows(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "CONSTRAINT_TOOL_GATE_ENABLED", False)
        engine = _engine()
        rule = _gate_rule(
            tools=["knowledge_search"],
            amount_limits=[{"op": "gt", "value": 5000, "on_violation": "block"}],
        )
        state = _gate_state([rule])

        async for _ in engine._execute_tool_use(
            state, _tool_use("knowledge_search", {"amount": 8000})
        ):
            pass
        engine.mcp.call_tool.assert_awaited_once()
        assert len(state["tool_results"]) == 1
        assert state["tool_results"][0]["result"] == "ok"

    @pytest.mark.asyncio
    async def test_clean_tool_call_unaffected(self, monkeypatch) -> None:
        """金额合规 → 工具正常执行（gate 返回 None）。"""
        monkeypatch.setattr(get_settings(), "CONSTRAINT_TOOL_GATE_ENABLED", True)
        engine = _engine()
        rule = _gate_rule(
            tools=["knowledge_search"],
            amount_limits=[{"op": "gt", "value": 5000}],
        )
        state = _gate_state([rule])

        async for _ in engine._execute_tool_use(
            state, _tool_use("knowledge_search", {"amount": 3000})
        ):
            pass
        engine.mcp.call_tool.assert_awaited_once()
        engine._schedule_constraint_audit.assert_not_called()
