"""三态门控 — 技能进化循环的验证门（纯函数，可单测）。

规则（对应 SkillOpt 实践的「客观信号主判、判官只定夺」纪律）：

1. 红线否决（任一命中 → 拒绝）：
   - 候选引用准确性均值低于当前基线；
   - 候选无幻觉度均值低于当前基线。
   引用与幻觉是知识库产品的质量红线 — 完整性提升不允许以幻觉换。
2. 严格更优 + 死区：候选 judge 均分须超出基线至少 deadband 才接受；
   平手与微弱领先均拒绝（判官单次评分噪声大，死区压制噪声误接受）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricsSnapshot:
    """一组 rollout 的 judge 指标聚合。

    Attributes:
        avg_score: 总分均值（三维平均的均值）。
        avg_citation_accuracy: 引用准确性均值（0-5）。
        avg_hallucination_inverse: 无幻觉度均值（0-5，越高越无幻觉）。
        n_cases: 参与统计的用例数。
    """

    avg_score: float
    avg_citation_accuracy: float
    avg_hallucination_inverse: float
    n_cases: int

    def to_dict(self) -> dict:
        return {
            "avg_score": round(self.avg_score, 4),
            "avg_citation_accuracy": round(self.avg_citation_accuracy, 4),
            "avg_hallucination_inverse": round(
                self.avg_hallucination_inverse, 4
            ),
            "n_cases": self.n_cases,
        }


@dataclass(frozen=True)
class GateDecision:
    """门控裁决。

    Attributes:
        accepted: 是否接受候选。
        reason: 裁决原因（accepted/rejected_* 机器码 + 关键数值），入审计链。
    """

    accepted: bool
    reason: str


def evaluate_gate(
    cur: MetricsSnapshot,
    cand: MetricsSnapshot,
    *,
    deadband: float,
) -> GateDecision:
    """对比当前基线与候选指标，产出接受/拒绝裁决。"""
    if cand.avg_citation_accuracy < cur.avg_citation_accuracy:
        return GateDecision(
            False,
            f"redline_citation_regression "
            f"(cand={cand.avg_citation_accuracy:.3f} < cur={cur.avg_citation_accuracy:.3f})",
        )
    if cand.avg_hallucination_inverse < cur.avg_hallucination_inverse:
        return GateDecision(
            False,
            f"redline_hallucination_regression "
            f"(cand={cand.avg_hallucination_inverse:.3f} < cur={cur.avg_hallucination_inverse:.3f})",
        )
    if cand.avg_score > cur.avg_score + deadband:
        return GateDecision(
            True,
            f"improved (cand={cand.avg_score:.3f} > cur={cur.avg_score:.3f} "
            f"+ deadband={deadband})",
        )
    return GateDecision(
        False,
        f"not_improved (cand={cand.avg_score:.3f} <= cur={cur.avg_score:.3f} "
        f"+ deadband={deadband})",
    )
