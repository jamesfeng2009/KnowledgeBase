"""
冲突裁决器 — 在矛盾检测之上，进一步裁决"该用哪条信息"。

对应附件第 12/13 讲核心：仅检测矛盾不够，还要裁决用哪条。两条固定套路：
    1. 同 key 多值 → last win（最新时间戳胜出）
    2. 多源文档冲突 → 权威序优先
       系统规则 > 用户输入 > 工具事实 > 已确认摘要 > 模型推测

企业知识库多级制度（公司/部门/项目）冲突时，可直接落地：
    - 公司制度（系统规则）> 部门细则（工具事实）> 项目实践（已确认摘要）
    冲突时按权威等级裁决，避免模型在矛盾信息间自行摇摆。

遵循单一职责：本模块只负责"裁决用哪条"，不负责检测矛盾（矛盾检测在
contradiction_detector.py）。遵循开放封闭：新增来源类型只需在
AUTHORITY_ORDER 追加，不修改裁决逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from app.utils.logger import get_logger

log = get_logger(__name__)

# 权威等级（从高到低）— 系统规则 > 用户输入 > 工具事实 > 已确认摘要 > 模型推测
AUTHORITY_ORDER: tuple[str, ...] = (
    "system_rule",      # 系统规则（公司制度、硬约束）
    "user_input",       # 用户输入（当前用户明确陈述）
    "tool_fact",        # 工具事实（ERP/CRM 等系统返回）
    "confirmed_summary",  # 已确认摘要（历史达成的确认）
    "model_inference",  # 模型推测（低置信推理）
)

# 权威等级 → 数值（越大越高）
_AUTHORITY_SCORE: dict[str, int] = {
    src: len(AUTHORITY_ORDER) - i for i, src in enumerate(AUTHORITY_ORDER)
}

T = TypeVar("T")


@dataclass(frozen=True)
class ConflictClaim(Generic[T]):
    """一条参与裁决的冲突信息。

    Attributes:
        value: 声称的值（数字/字符串/事实文本）。
        authority: 权威来源类型（必须在 AUTHORITY_ORDER 中）。
        source: 来源标识（文档名/工具名/用户句）。
        timestamp: 声称时间（ISO 字符串或 datetime），用于 last win。
        key: 冲突所在的键（如同一字段，如 "报销上限"）。
    """

    value: T
    authority: str = "model_inference"
    source: str = ""
    timestamp: str | float | datetime | None = None
    key: str = ""


@dataclass(frozen=True)
class ConflictResolution(Generic[T]):
    """冲突裁决结果。

    Attributes:
        resolved_value: 裁决胜出的值。
        winner_authority: 胜出来源的权威类型。
        winner_source: 胜出来源标识。
        reason: 裁决原因（"权威优先" / "last win" / "唯一来源"）。
        runner_up_authority: 次优权威（冲突的另一方，便于呈现）。
        conflicting_claims: P0-3 保留所有被否决的声称列表 — 不让模型偷偷选边，
            上层可据此呈现冲突详情或触发补证流程。空列表表示无冲突。
        needs_clarification: P0-3 是否需要补证 — 当多源声称的值存在真实冲突
            （非同源自我矛盾）时设为 True，上层应触发澄清/补证流程。
    """

    resolved_value: T
    winner_authority: str
    winner_source: str
    reason: str
    runner_up_authority: str | None = None
    conflicting_claims: list[ConflictClaim] = field(default_factory=list)
    needs_clarification: bool = False


class ConflictResolver:
    """冲突裁决器 — last win / 权威优先。

    使用方式::

        resolver = ConflictResolver()
        # 同 key 多值 → last win
        result = resolver.resolve([
            ConflictClaim("5000", "tool_fact", "ERP", "2026-01-01"),
            ConflictClaim("8000", "tool_fact", "ERP", "2026-02-01"),
        ])  # resolved_value == "8000"

        # 不同权威 → 权威优先
        result = resolver.resolve([
            ConflictClaim("10天", "system_rule", "公司制度"),
            ConflictClaim("3天", "user_input", "用户"),
        ])  # resolved_value == "10天"
    """

    def __init__(self, authority_order: tuple[str, ...] = AUTHORITY_ORDER) -> None:
        """初始化裁决器。

        Args:
            authority_order: 权威顺序（从高到低）。默认
                系统规则 > 用户输入 > 工具事实 > 已确认摘要 > 模型推测。
        """
        self._order = authority_order
        self._score = {
            src: len(authority_order) - i for i, src in enumerate(authority_order)
        }

    def authority_score(self, authority: str) -> int:
        """返回来源类型的权威分值（越高越权威）。未知类型按最低分。"""
        return self._score.get(authority, 0)

    def resolve(
        self,
        claims: list[ConflictClaim[T]],
        same_key_only: bool = True,
    ) -> ConflictResolution[T] | None:
        """裁决一组冲突声称。

        Args:
            claims: 冲突声称列表（至少 2 条才有冲突可裁）。
            same_key_only: 仅当所有声称同 key 且同权威时按 last win 处理；
                同 key 但不同权威（多源冲突）仍按权威优先。

        Returns:
            ConflictResolution: 裁决结果；声称不足或无有效声称时返回 None。
        """
        valid = [c for c in claims if self.authority_score(c.authority) > 0]
        if not valid:
            return None
        if len(valid) < 2:
            # 单条有效声称直接胜出 — 无冲突
            c = valid[0]
            return ConflictResolution(
                resolved_value=c.value,
                winner_authority=c.authority,
                winner_source=c.source,
                reason="唯一来源",
                conflicting_claims=[],
                needs_clarification=False,
            )

        # 情形 1: 同 key 且同权威 → 同源自我矛盾，last win（最新时间胜出）
        keys = {c.key for c in valid if c.key}
        authorities = {c.authority for c in valid}
        same_key = len(keys) == 1 and keys != {""}
        same_authority = len(authorities) == 1
        if same_key_only and same_key and same_authority:
            winner = self._last_win(valid)
            # P0-3: 同源自我矛盾不触发补证（同权威 last win 足够），
            # 但保留被否决的声称供上层审计
            losers = [c for c in valid if c is not winner]
            return ConflictResolution(
                resolved_value=winner.value,
                winner_authority=winner.authority,
                winner_source=winner.source,
                reason="last win",
                runner_up_authority=self._runner_up_authority(valid, winner),
                conflicting_claims=losers,
                needs_clarification=False,
            )

        # 情形 2: 多源冲突（同 key 不同权威 / 不同 key）→ 权威优先
        winner = self._authority_first(valid)
        # 最高权威并列时退化为 last win — 标注决胜原因
        top_score = self.authority_score(winner.authority)
        tied = [c for c in valid if self.authority_score(c.authority) == top_score]
        reason = "权威优先" if len(tied) <= 1 else "权威并列,last win"
        # P0-3: 保留所有被否决的声称；多源值冲突时触发补证
        losers = [c for c in valid if c is not winner]
        # needs_clarification: 多源声称值存在真实冲突（值不同）时需要补证
        loser_has_diff_value = any(str(c.value) != str(winner.value) for c in losers)
        needs_clarify = loser_has_diff_value and not same_authority
        return ConflictResolution(
            resolved_value=winner.value,
            winner_authority=winner.authority,
            winner_source=winner.source,
            reason=reason,
            runner_up_authority=self._runner_up_authority(valid, winner),
            conflicting_claims=losers,
            needs_clarification=needs_clarify,
        )

    # ------------------------------------------------------------------
    # 内部裁决策略
    # ------------------------------------------------------------------

    def _last_win(
        self, claims: list[ConflictClaim[T]],
    ) -> ConflictClaim[T]:
        """同 key 多值取最新时间戳（last win）。"""
        # 有有效时间戳的按时间排序取最新；无时间戳的按权威>输入顺序兜底
        dated = [c for c in claims if self._to_timestamp(c) is not None]
        if dated:
            return max(dated, key=lambda c: self._to_timestamp(c) or 0.0)
        # 全部无时间戳 → 按权威分取最高
        return max(claims, key=lambda c: self.authority_score(c.authority))

    def _authority_first(
        self, claims: list[ConflictClaim[T]],
    ) -> ConflictClaim[T]:
        """多源冲突取权威最高者。权威相同则取最新时间戳。"""
        highest = max(claims, key=lambda c: self.authority_score(c.authority))
        top_authority = self.authority_score(highest.authority)
        candidates = [c for c in claims if self.authority_score(c.authority) == top_authority]
        if len(candidates) == 1:
            return candidates[0]
        # 权威并列 → last win
        return self._last_win(candidates)

    @staticmethod
    def _runner_up_authority(
        claims: list[ConflictClaim[T]],
        winner: ConflictClaim[T],
    ) -> str | None:
        """返回非胜出方中最高的权威类型（用于呈现冲突）。"""
        others = [c for c in claims if c is not winner]
        if not others:
            return None
        return max(others, key=lambda c: AUTHORITY_ORDER.index(c.authority) if c.authority in AUTHORITY_ORDER else 99).authority

    @staticmethod
    def _to_timestamp(claim: ConflictClaim[T]) -> float | None:
        """把声称时间转为时间戳（浮点秒）。无时间戳返回 None。"""
        ts = claim.timestamp
        if ts is None:
            return None
        if isinstance(ts, datetime):
            return ts.timestamp()
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts).timestamp()
            except ValueError:
                return None
        return None

    @staticmethod
    def resolve_authority(
        claims: list[ConflictClaim[T]],
        authority_order: tuple[str, ...] = AUTHORITY_ORDER,
    ) -> ConflictResolution[T] | None:
        """静态便捷入口 — 直接按权威序裁决，无需实例化。

        Args:
            claims: 冲突声称列表。
            authority_order: 权威顺序（从高到低）。

        Returns:
            ConflictResolution: 权威优先裁决结果；无有效声称返回 None。
        """
        return ConflictResolver(authority_order).resolve(claims, same_key_only=False)