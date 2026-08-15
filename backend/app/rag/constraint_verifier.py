"""约束核验器 — L2 post_verify / L3 tool_gate 的零 LLM 执行层。

设计：constraint-recall-design §8（三层消费防线）。约束在 constraint_rules
表只定义一次（normalized JSONB），本模块让它可执行：

    ConstraintVerifier  L2 post_verify — 挂 engine._reflect，核验最终答案：
        forbidden_patterns   答案命中禁用模式即违规
        required_mentions    condition 话题满足而必提词全缺即违规
        amount_limits        答案金额越限即违规（on_violation 决定处置）
    ToolGate            L3 tool_gate — 挂 engine._execute_tool_use，工具
        调用执行前拦截：
        normalized.tool_gate.tools       适用的工具名（缺省 ["*"] 全部）
        normalized.tool_gate.param_keys  金额校验的参数键（缺省扫描全部值）
        amount_limits                    参数金额越限即拦截
        forbidden_patterns               参数文本命中即拦截

零 LLM 承诺：金额抽取复用 HighRiskDetector._AMOUNT_PATTERN / _parse_amount
（high_risk_detector.py 既有正则资产）；模式匹配优先按正则解释，非法正则
回退字面量子串（防运营写入坏正则拖垮核验）；币种只在人民币族内折算比较
（元/万元/亿元，外币不跨币种比较，与 HighRiskDetector 的保守语义一致）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 人民币族单位 → 元基准折算系数（外币不在表内 → 不跨币种比较）
_CNY_FACTORS: dict[str, float] = {
    "元": 1.0,
    "万元": 1e4,
    "亿元": 1e8,
    "人民币": 1.0,
    "cny": 1.0,
    "rmb": 1.0,
}

# amount_limits 支持的比较算子
_OPS: dict[str, Any] = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


@dataclass
class ConstraintViolation:
    """单条违规 — 审计 / SSE 透出 / 重生成核验的最小单元。"""

    rule_id: str
    rule_text: str
    # 违规处置：block | confirm | warn（规则 severity，金额上限可被
    # on_violation 覆盖）
    severity: str
    # forbidden_pattern | required_mentions | amount_limit
    check: str
    detail: str
    # 规则 dict 引用（重核验复用，不参与序列化）
    rule: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_text": self.rule_text,
            "severity": self.severity,
            "check": self.check,
            "detail": self.detail,
        }


class ConstraintVerifier:
    """L2 post_verify — 对答案文本核验 normalized 三类检查（零 LLM）。

    使用方式（engine._reflect 内）::

        violations = [
            v for r in rules if "post_verify" in (r.get("actions") or [])
            for v in ConstraintVerifier.verify(answer, r)
        ]
    """

    @classmethod
    def verify(
        cls, answer: str, rule: dict[str, Any]
    ) -> list[ConstraintViolation]:
        """核验答案 — 返回该规则的全部违规（空列表 = 通过）。"""
        normalized = rule.get("normalized")
        if not isinstance(normalized, dict) or not normalized:
            return []
        if not answer:
            return []
        violations: list[ConstraintViolation] = []
        violations.extend(cls._check_forbidden(answer, rule, normalized))
        violations.extend(cls._check_required(answer, rule, normalized))
        violations.extend(cls._check_amounts(answer, rule, normalized))
        return violations

    @classmethod
    def still_violates(
        cls, answer: str, violations: list[ConstraintViolation]
    ) -> bool:
        """重生成结果的采纳门 — block 级违规在 新答案 上是否仍存在。

        仅复核产生 block 级违规的规则（warn 级不阻断，无需复核）。
        """
        rules: dict[str, dict[str, Any]] = {}
        for v in violations:
            if v.severity == "block":
                rules.setdefault(str(v.rule.get("rule_id")), v.rule)
        return any(
            v.severity == "block"
            for rule in rules.values()
            for v in cls.verify(answer, rule)
        )

    # ------------------------------------------------------------------
    # 三类检查
    # ------------------------------------------------------------------

    @classmethod
    def _check_forbidden(
        cls,
        text: str,
        rule: dict[str, Any],
        normalized: dict[str, Any],
    ) -> list[ConstraintViolation]:
        patterns = [
            str(p) for p in (normalized.get("forbidden_patterns") or []) if p
        ]
        if not patterns:
            return []
        hits = [p for p in patterns if cls._pattern_hit(text, p)]
        if not hits:
            return []
        return [
            ConstraintViolation(
                rule_id=str(rule.get("rule_id") or ""),
                rule_text=str(rule.get("rule_text") or ""),
                severity=str(rule.get("severity") or "warn"),
                check="forbidden_pattern",
                detail=f"命中禁用表述：{'、'.join(hits)}",
                rule=rule,
            )
        ]

    @classmethod
    def _check_required(
        cls,
        answer: str,
        rule: dict[str, Any],
        normalized: dict[str, Any],
    ) -> list[ConstraintViolation]:
        required = [
            str(m)
            for m in (normalized.get("required_mentions") or [])
            if str(m).strip()
        ]
        if not required:
            return []
        condition = normalized.get("condition")
        topics = (
            [str(t) for t in condition.get("topic") or [] if str(t).strip()]
            if isinstance(condition, dict)
            else []
        )
        # 话题域不满足 → 必提词不适用（无 condition 视为恒满足）
        if topics and not any(
            cls._pattern_hit(answer, t) for t in topics
        ):
            return []
        if any(cls._pattern_hit(answer, m) for m in required):
            return []
        return [
            ConstraintViolation(
                rule_id=str(rule.get("rule_id") or ""),
                rule_text=str(rule.get("rule_text") or ""),
                severity=str(rule.get("severity") or "warn"),
                check="required_mentions",
                detail=(
                    f"话题命中（{'、'.join(topics)}）但答案未包含必提词"
                    f"（任一）：{'、'.join(required)}"
                ),
                rule=rule,
            )
        ]

    @classmethod
    def _check_amounts(
        cls,
        text: str,
        rule: dict[str, Any],
        normalized: dict[str, Any],
    ) -> list[ConstraintViolation]:
        limits = [
            lim
            for lim in (normalized.get("amount_limits") or [])
            if isinstance(lim, dict)
        ]
        if not limits:
            return []
        amounts = cls._extract_amounts(text)
        if not amounts:
            return []

        for lim in limits:
            try:
                limit_value = float(lim.get("value"))
            except (TypeError, ValueError):
                continue
            op = str(lim.get("op") or "gt")
            compare = _OPS.get(op)
            if compare is None:
                continue
            limit_unit = str(lim.get("unit") or "元")
            hits = [
                amount
                for amount in amounts
                if cls._amount_violates(amount, compare, limit_value, limit_unit)
            ]
            if hits:
                shown = "、".join(
                    f"{v:g}{u}" for v, u in hits
                )
                severity = str(
                    lim.get("on_violation") or rule.get("severity") or "warn"
                )
                return [
                    ConstraintViolation(
                        rule_id=str(rule.get("rule_id") or ""),
                        rule_text=str(rule.get("rule_text") or ""),
                        severity=severity,
                        check="amount_limit",
                        detail=(
                            f"金额越限（{op} {limit_value:g}{limit_unit}）：{shown}"
                        ),
                        rule=rule,
                    )
                ]
        return []

    # ------------------------------------------------------------------
    # 匹配原语
    # ------------------------------------------------------------------

    @staticmethod
    def _pattern_hit(text: str, pattern: str) -> bool:
        """模式匹配 — 优先正则（IGNORECASE），非法正则回退字面子串。"""
        if not pattern:
            return False
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return pattern.lower() in text.lower()

    @staticmethod
    def _extract_amounts(text: str) -> list[tuple[float, str]]:
        """复用 HighRiskDetector 正则资产抽取 (数值, 单位) 金额。"""
        if not text:
            return []
        try:
            from app.context.high_risk_detector import HighRiskDetector

            amounts: list[tuple[float, str]] = []
            for match in HighRiskDetector._AMOUNT_PATTERN.finditer(text):
                parsed = HighRiskDetector._parse_amount(match.group(0))
                if parsed is not None:
                    amounts.append(parsed)
            return amounts
        except Exception as exc:  # 正则资产不可用 — 金额检查静默降级
            log.warning("constraint.verifier.amount_extract_failed", error=str(exc))
            return []

    @classmethod
    def _amount_violates(
        cls,
        amount: tuple[float, str],
        compare: Any,
        limit_value: float,
        limit_unit: str,
    ) -> bool:
        """金额越限判定 — 同单位直接比较，人民币族折算到元基准比较。

        外币 ↔ 人民币不比较（无汇率来源，宁可漏判不可误判）。
        """
        value, unit = amount
        if unit == limit_unit:
            return compare(value, limit_value)
        amount_base = cls._cny_value(value, unit)
        limit_base = cls._cny_value(limit_value, limit_unit)
        if amount_base is None or limit_base is None:
            return False
        return compare(amount_base, limit_base)

    @staticmethod
    def _cny_value(value: float, unit: str) -> float | None:
        """(数值, 单位) → 人民币元基准值；外币返回 None。"""
        factor = _CNY_FACTORS.get(unit) or _CNY_FACTORS.get(unit.lower())
        if factor is None:
            return None
        return value * factor


@dataclass
class ToolGateDecision:
    """L3 工具门决策 — engine._execute_tool_use 执行前拦截。"""

    # block（阻断 + 审计）| confirm（走人工审批流）| warn（放行 + 日志）
    action: str
    rule_id: str
    reason: str
    violation: ConstraintViolation | None = None


class ToolGate:
    """L3 tool_gate — 工具调用执行前按 normalized 拦截（零 LLM）。

    规则匹配口径（§8.2 工具名 / 参数 / amount_limits）：
        1. actions 含 tool_gate 且工具名适用（tool_gate.tools 缺省 ["*"]）；
        2. 参数金额越限（amount_limits，on_violation 可覆盖处置）；
        3. 参数文本命中禁用模式（forbidden_patterns）。

    首个命中的规则即返回（一条决策足够 — 阻断 / 审批理由需明确单一）。
    """

    @classmethod
    def check(
        cls,
        rules: list[dict[str, Any]],
        tool_name: str,
        tool_input: Any,
    ) -> ToolGateDecision | None:
        for rule in rules:
            if "tool_gate" not in (rule.get("actions") or []):
                continue
            normalized = rule.get("normalized")
            if not isinstance(normalized, dict):
                continue
            gate_cfg = normalized.get("tool_gate")
            gate_cfg = gate_cfg if isinstance(gate_cfg, dict) else {}
            tools = [
                str(t)
                for t in (gate_cfg.get("tools") or ["*"])
                if str(t).strip()
            ] or ["*"]
            if "*" not in tools and tool_name not in tools:
                continue

            param_keys = [
                str(k) for k in (gate_cfg.get("param_keys") or []) if k
            ]
            scan_text = cls._scan_arguments(tool_input, param_keys)

            violation = ConstraintVerifier._check_amounts(
                scan_text, rule, normalized
            )
            if not violation:
                violation = ConstraintVerifier._check_forbidden(
                    scan_text, rule, normalized
                )
            if violation:
                v = violation[0]
                action = v.severity if v.severity in ("block", "confirm") else "warn"
                return ToolGateDecision(
                    action=action,
                    rule_id=v.rule_id,
                    reason=f"约束条款拦截：{v.rule_text}（{v.detail}）",
                    violation=v,
                )
        return None

    @staticmethod
    def _scan_arguments(tool_input: Any, param_keys: list[str]) -> str:
        """参数 → 金额/模式扫描文本（指定键优先，缺省全量拼接）。"""
        if not isinstance(tool_input, dict):
            return str(tool_input or "")
        items: list[Any] = []
        if param_keys:
            items = [tool_input.get(k) for k in param_keys if k in tool_input]
        if not items:
            items = list(tool_input.values())
        parts: list[str] = []
        for value in items:
            if value is None:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                # 裸数字参数（amount: 8000）按元处理 — 拼出可抽取的
                # "数字+元" 形态供 _AMOUNT_PATTERN 命中
                parts.append(f"{value}元")
            else:
                parts.append(str(value))
        return " ".join(parts)
