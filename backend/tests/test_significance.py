"""
显著性检验测试 — app/eval/significance.py。

覆盖：
    - 配对自助：无差异时 p 不显著、真实差异时可检出、长度不等报错
    - 可复现性（同 seed 同结果）
    - McNemar：只用不一致格、b=c 时 p=1、大样本走正态近似
    - 两比例检验与 Wilson 区间（0/12 不越界）
    - 配对取数按 case_id 对齐并披露剔除数
    - 门禁：红线优先 / 显著变差回滚 / 样本不足 hold / 效应过小 hold / 齐备 ship
"""

import random

import pytest

from app.eval.significance import (
    extract_metric_pairs,
    extract_passed_pairs,
    judge_experiment,
    mcnemar_test,
    paired_bootstrap,
    two_proportion_test,
    wilson_interval,
)


def _make_pairs(n, base, effect, *, noise=0.1, seed=1):
    """造一组配对分数：treatment = control + effect（同噪声项）。"""
    rng = random.Random(seed)
    a, b = [], []
    for _ in range(n):
        x = base + rng.uniform(-noise, noise)
        x = min(1.0, max(0.0, x))
        y = min(1.0, max(0.0, x + effect))
        a.append(x)
        b.append(y)
    return a, b


class TestPairedBootstrap:
    def test_no_effect_is_not_significant(self):
        a, b = _make_pairs(120, 0.7, 0.0, seed=7)
        result = paired_bootstrap(a, b)
        assert result.n == 120
        assert not result.significant, f"p={result.p_value}"

    def test_real_effect_is_detected(self):
        a, b = _make_pairs(300, 0.6, 0.15, seed=11)
        result = paired_bootstrap(a, b)
        assert result.significant
        assert result.delta > 0
        # 置信区间应完全落在 0 右侧
        assert result.ci_low > 0

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="equal lengths"):
            paired_bootstrap([0.1, 0.2], [0.3])

    def test_tiny_sample_returns_null_result(self):
        result = paired_bootstrap([0.5], [0.9])
        assert result.p_value == 1.0
        assert result.n == 1

    def test_deterministic_with_seed(self):
        a, b = _make_pairs(80, 0.5, 0.1, seed=3)
        first = paired_bootstrap(a, b, seed=99)
        second = paired_bootstrap(a, b, seed=99)
        assert first.p_value == second.p_value
        assert first.ci_low == second.ci_low

    def test_ci_contains_zero_when_not_significant(self):
        a, b = _make_pairs(60, 0.7, 0.0, seed=21)
        result = paired_bootstrap(a, b)
        if not result.significant:
            assert result.ci_low <= 0 <= result.ci_high


class TestMcNemar:
    def test_no_discordant_is_null(self):
        flags = [True, False, True, True]
        out = mcnemar_test(flags, list(flags))
        assert out["b"] == 0 and out["c"] == 0
        assert out["p_value"] == 1.0 and not out["significant"]

    def test_balanced_discordant_is_not_significant(self):
        a = [True] * 20 + [False] * 20
        b = [False] * 20 + [True] * 20
        out = mcnemar_test(a, b)
        assert out["b"] == 20 and out["c"] == 20
        assert out["p_value"] == pytest.approx(1.0, abs=1e-9)

    def test_skewed_discordant_is_significant(self):
        a = [True] * 30 + [False] * 4
        b = [False] * 30 + [True] * 4
        out = mcnemar_test(a, b)
        assert out["significant"]
        assert out["exact"]

    def test_large_n_uses_normal_approximation(self):
        n = 1200  # 超过精确二项的组合数上限
        a = [True] * n + [False] * 400
        b = [False] * n + [True] * 400
        out = mcnemar_test(a, b)
        assert not out["exact"]
        assert out["significant"]

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="equal lengths"):
            mcnemar_test([True], [True, False])


class TestProportions:
    def test_wilson_interval_stays_in_range(self):
        lo, hi = wilson_interval(0, 12)
        assert lo >= 0.0
        assert hi > 0.0  # Wald 会给出 [0,0]，Wilson 不会把「0/12」当成确定无风险

    def test_wilson_empty_denominator(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_two_proportion_detects_gap(self):
        out = two_proportion_test(40, 400, 80, 400)
        assert out.delta > 0
        assert out.p_value < 0.05
        assert out.ci_low > 0

    def test_two_proportion_null_when_identical(self):
        out = two_proportion_test(100, 400, 100, 400)
        assert out.p_value == pytest.approx(1.0, abs=1e-9)
        assert out.delta == 0.0
        assert out.significant is False

    def test_zero_denominator_is_safe(self):
        out = two_proportion_test(0, 0, 5, 10)
        assert out.p_value == 1.0


class TestPairExtraction:
    def test_pairs_by_case_id_and_reports_dropped(self):
        control = [
            {"case_id": "c1", "query": "q1", "recall_at_5": 0.5, "passed": True},
            {"case_id": "c2", "query": "q2", "recall_at_5": 0.7, "passed": False},
            {"case_id": "c3", "query": "q3", "recall_at_5": 0.9, "passed": True},
        ]
        treatment = [
            {"case_id": "c2", "query": "q2", "recall_at_5": 0.8, "passed": True},
            {"case_id": "c1", "query": "q1", "recall_at_5": 0.6, "passed": True},
        ]
        a, b, dropped = extract_metric_pairs(control, treatment, "recall_at_5")
        assert a == [0.5, 0.7]
        assert b == [0.6, 0.8]
        assert dropped == 1

    def test_nested_metric_path(self):
        control = [{"case_id": "c1", "judge_scores": {"total": 8.0}}]
        treatment = [{"case_id": "c1", "judge_scores": {"total": 9.0}}]
        a, b, dropped = extract_metric_pairs(
            control, treatment, "judge_scores.total"
        )
        assert (a, b, dropped) == ([8.0], [9.0], 0)

    def test_boolean_metric_reads_as_zero_one(self):
        control = [
            {"case_id": "c1", "passed": True},
            {"case_id": "c2", "passed": False},
        ]
        treatment = [
            {"case_id": "c1", "passed": False},
            {"case_id": "c2", "passed": False},
        ]
        a, b, dropped = extract_metric_pairs(control, treatment, "passed")
        assert (a, b, dropped) == ([1.0, 0.0], [0.0, 0.0], 0)

    def test_missing_metric_drops_case(self):
        control = [{"case_id": "c1", "mrr": None}]
        treatment = [{"case_id": "c1", "mrr": 0.5}]
        a, b, dropped = extract_metric_pairs(control, treatment, "mrr")
        assert a == [] and b == [] and dropped == 1

    def test_passed_pairs_align(self):
        control = [{"case_id": "c1", "passed": True}, {"case_id": "c2", "passed": False}]
        treatment = [{"case_id": "c1", "passed": False}, {"case_id": "c2", "passed": False}]
        pa, pb = extract_passed_pairs(control, treatment)
        assert pa == [True, False]
        assert pb == [False, False]


class TestGate:
    def test_guardrail_forces_rollback_even_with_gain(self):
        verdict = judge_experiment(
            metric="recall_at_5",
            delta=0.2,
            p_value=0.0001,
            n=500,
            min_effect=0.02,
            guardrail_violations=["refusal_rate 显著上升"],
        )
        assert verdict.decision == "rollback"
        assert any("guardrail" in r for r in verdict.reasons)

    def test_significant_regression_is_rollback(self):
        verdict = judge_experiment(
            metric="recall_at_5", delta=-0.05, p_value=0.001, n=500, min_effect=0.02
        )
        assert verdict.decision == "rollback"
        assert "significantly worse" in verdict.reasons[0]

    def test_small_sample_is_hold_not_failure(self):
        verdict = judge_experiment(
            metric="recall_at_5", delta=0.05, p_value=0.4, n=8, min_effect=0.02
        )
        assert verdict.decision == "hold"
        assert any("insufficient samples" in r for r in verdict.reasons)

    def test_significant_but_tiny_effect_is_hold(self):
        verdict = judge_experiment(
            metric="recall_at_5", delta=0.001, p_value=0.0001, n=5000, min_effect=0.02
        )
        assert verdict.decision == "hold"
        assert any("effect too small" in r for r in verdict.reasons)

    def test_all_conditions_met_is_ship(self):
        verdict = judge_experiment(
            metric="recall_at_5",
            delta=0.08,
            p_value=0.002,
            n=400,
            min_effect=0.02,
            ci_low=0.03,
            ci_high=0.13,
        )
        assert verdict.decision == "ship"
        assert verdict.reasons == []
        assert verdict.enough_evidence
        assert verdict.to_dict()["decision"] == "ship"
