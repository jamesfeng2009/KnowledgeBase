"""三态门控测试 — 验证 app/evolution/gate.py（红线否决 + 严格更优 + 死区）。"""

from __future__ import annotations

from app.evolution.gate import MetricsSnapshot, evaluate_gate


def _snap(score: float, citation: float = 4.0, halluc: float = 4.0) -> MetricsSnapshot:
    return MetricsSnapshot(
        avg_score=score,
        avg_citation_accuracy=citation,
        avg_hallucination_inverse=halluc,
        n_cases=20,
    )


def test_accept_on_clear_improvement() -> None:
    decision = evaluate_gate(_snap(4.0), _snap(4.5), deadband=0.1)
    assert decision.accepted
    assert decision.reason.startswith("improved")


def test_reject_within_deadband() -> None:
    """微弱领先（0.05 < 0.1 死区）→ 拒绝（压制判官噪声）。"""
    decision = evaluate_gate(_snap(4.0), _snap(4.05), deadband=0.1)
    assert not decision.accepted
    assert decision.reason.startswith("not_improved")


def test_reject_on_tie() -> None:
    decision = evaluate_gate(_snap(4.0), _snap(4.0), deadband=0.1)
    assert not decision.accepted


def test_reject_on_regression() -> None:
    decision = evaluate_gate(_snap(4.0), _snap(3.5), deadband=0.1)
    assert not decision.accepted


def test_redline_veto_citation_regression() -> None:
    """引用准确性回退 → 一票否决，即使总分大幅提升。"""
    cur = _snap(4.0, citation=4.5, halluc=4.5)
    cand = _snap(4.8, citation=4.2, halluc=4.5)
    decision = evaluate_gate(cur, cand, deadband=0.1)
    assert not decision.accepted
    assert decision.reason.startswith("redline_citation_regression")


def test_redline_veto_hallucination_regression() -> None:
    """无幻觉度回退 → 一票否决（完整性提升不允许以幻觉换）。"""
    cur = _snap(4.0, citation=4.5, halluc=4.5)
    cand = _snap(4.8, citation=4.6, halluc=4.4)
    decision = evaluate_gate(cur, cand, deadband=0.1)
    assert not decision.accepted
    assert decision.reason.startswith("redline_hallucination_regression")


def test_accept_with_equal_redlines() -> None:
    """红线持平 + 总分过死区 → 接受。"""
    cur = _snap(4.0, citation=4.5, halluc=4.5)
    cand = _snap(4.3, citation=4.5, halluc=4.5)
    assert evaluate_gate(cur, cand, deadband=0.1).accepted


def test_zero_baseline_accepts_any_positive() -> None:
    """冷启动（基线 0 分）场景。"""
    decision = evaluate_gate(
        MetricsSnapshot(0.0, 0.0, 0.0, 0), _snap(2.0), deadband=0.1
    )
    assert decision.accepted
