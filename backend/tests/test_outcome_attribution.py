"""
决策→结果归因测试 — app/services/outcome_attribution.py。

重点不是「算得出数」，而是**该报警的地方必须报警**：
    - 混杂导致合并结论与分层结论反向时，必须报出冲突并以校正后为准
    - 匹配率过低 / 重复结果事件必须进 warnings，不能静默只报可匹配样本
    - 红线指标显著恶化时判定必须 rollback（哪怕主指标涨）
    - A/A 负对照在无真实效应的数据上必须通过
"""

import random

import pytest

from app.services.outcome_attribution import (
    AttributedDecision,
    DecisionEvent,
    OutcomeEvent,
    aa_negative_control,
    arm_summary,
    join_decisions_outcomes,
    mantel_haenszel,
    outcome_report,
)


def _dec(i, arm, urgency, **kw):
    return {
        "decision_id": f"d{i}",
        "experiment": "exp",
        "arm": arm,
        "unit_id": f"u{i}",
        "strata": {"urgency": urgency},
        **kw,
    }


def _out(i, **kw):
    return {"decision_id": f"d{i}", **kw}


class TestJoin:
    def test_match_rate_and_orphans(self):
        decisions = [_dec(i, "control", "low") for i in range(10)]
        outcomes = [_out(i, filled=True) for i in range(6)] + [_out(999, filled=True)]
        rows, diag = join_decisions_outcomes(decisions, outcomes)
        assert diag["matched"] == 6
        assert diag["unmatched"] == 4
        assert diag["match_rate"] == 0.6
        assert diag["orphan_outcomes"] == 1
        assert sum(1 for r in rows if r.matched) == 6

    def test_duplicate_outcomes_kept_first_and_counted(self):
        """同一决策多条结果必须去重并暴露 —— 静默双计会把方差算小。"""
        decisions = [_dec(1, "control", "low")]
        outcomes = [
            _out(1, filled=True),
            _out(1, filled=False),
        ]
        rows, diag = join_decisions_outcomes(decisions, outcomes)
        assert diag["duplicate_outcomes"] == 1
        assert rows[0].outcome.filled is True

    def test_accepts_dataclasses(self):
        rows, diag = join_decisions_outcomes(
            [DecisionEvent(decision_id="d1", arm="control")],
            [OutcomeEvent(decision_id="d1", filled=True)],
        )
        assert diag["matched"] == 1
        assert rows[0].decision.arm == "control"


class TestArmSummary:
    def test_rate_and_wilson_interval(self):
        decisions = [_dec(i, "control" if i % 2 else "treatment", "low") for i in range(20)]
        outcomes = [_out(i, filled=(i % 3 == 0)) for i in range(20)]
        rows, _ = join_decisions_outcomes(decisions, outcomes)
        summary = arm_summary(rows, "filled")
        assert summary["control"]["n"] == 10
        assert summary["control"]["ci_low"] <= summary["control"]["rate"]
        assert summary["control"]["ci_high"] >= summary["control"]["rate"]

    def test_unmatched_rows_excluded_from_arms(self):
        decisions = [_dec(i, "control", "low") for i in range(5)]
        outcomes = [_out(0, filled=True)]
        rows, _ = join_decisions_outcomes(decisions, outcomes)
        assert arm_summary(rows, "filled")["control"]["n"] == 1


class TestConfounding:
    def test_simpson_reversal_is_detected_and_reported(self):
        """构造：treatment 每层都更差，但被分到更多容易成功的急单。

        合并看 treatment 更高，分层看每层都更低 —— 报告必须指出方向相反，
        并让判定基于校正后的差值。
        """
        rng = random.Random(7)
        decisions, outcomes = [], []
        idx = 0

        def emit(arm, urgency, p_fill):
            nonlocal idx
            decisions.append(_dec(idx, arm, urgency))
            outcomes.append(_out(idx, filled=rng.random() < p_fill))
            idx += 1

        for _ in range(600):
            emit("control", "low", 0.20)  # 多数缓单
        for _ in range(60):
            emit("control", "high", 0.50)
        for _ in range(60):
            emit("treatment", "low", 0.15)
        for _ in range(540):
            emit("treatment", "high", 0.45)  # 多数急单，且层内仍更差

        report = outcome_report(
            decisions, outcomes, metric="filled", strata_keys=("urgency",)
        )
        pooled = report["significance"]["delta"]
        adjusted = report["confounding"]["adjusted_delta"]
        assert pooled > 0, "构造失效：合并差应为正"
        assert adjusted < 0, "构造失效：层内差应为负"
        assert any("方向相反" in w for w in report["warnings"])
        assert report["verdict"]["decision"] == "rollback"

    def test_mantel_haenszel_weights_layers(self):
        rows = []
        for i in range(100):
            arm = "control" if i % 2 else "treatment"
            stratum = "a" if i < 50 else "b"
            rows.append(
                AttributedDecision(
                    decision=DecisionEvent(
                        decision_id=f"d{i}", arm=arm, strata={"k": stratum}
                    ),
                    outcome=OutcomeEvent(decision_id=f"d{i}", filled=i % 3 == 0),
                )
            )
        mh = mantel_haenszel(rows, "filled", ["k"])
        assert mh["n_strata_used"] == 2
        # 两臂在层内分布对称时，校正差应接近 0
        assert abs(mh["adjusted_delta"]) < 0.1


class TestGuardrails:
    def test_guardrail_regression_forces_rollback_despite_gain(self):
        rng = random.Random(3)
        decisions, outcomes = [], []
        for i in range(400):
            arm = "control" if i < 200 else "treatment"
            decisions.append(_dec(i, arm, "low"))
            # treatment：主指标更好，红线指标更差
            outcomes.append(
                _out(
                    i,
                    filled=rng.random() < (0.60 if arm == "treatment" else 0.30),
                    responded=rng.random() < (0.20 if arm == "treatment" else 0.70),
                )
            )
        report = outcome_report(
            decisions,
            outcomes,
            metric="filled",
            min_effect=0.02,
            min_samples=50,
            guardrail_metrics=("responded",),
        )
        assert report["significance"]["delta"] > 0
        assert report["guardrails"][0]["worse"] is True
        assert report["verdict"]["decision"] == "rollback"
        assert any("guardrail" in r for r in report["verdict"]["reasons"])


class TestVerdictPaths:
    def test_missing_arm_returns_warning(self):
        decisions = [_dec(i, "control", "low") for i in range(10)]
        outcomes = [_out(i, filled=True) for i in range(10)]
        report = outcome_report(decisions, outcomes)
        assert report["verdict"] is None
        assert any("两臂样本不全" in w for w in report["warnings"])

    def test_genuine_improvement_ships(self):
        rng = random.Random(5)
        decisions, outcomes = [], []
        for i in range(600):
            arm = "control" if i % 2 else "treatment"
            decisions.append(_dec(i, arm, "low"))
            outcomes.append(
                _out(i, filled=rng.random() < (0.45 if arm == "treatment" else 0.25))
            )
        report = outcome_report(
            decisions, outcomes, metric="filled", min_effect=0.05, min_samples=100
        )
        assert report["verdict"]["decision"] == "ship"
        assert report["verdict"]["reasons"] == []

    def test_low_match_rate_is_warned(self):
        decisions = [_dec(i, "control" if i % 2 else "treatment", "low") for i in range(100)]
        outcomes = [_out(i, filled=True) for i in range(20)]
        report = outcome_report(decisions, outcomes)
        assert any("match_rate" in w for w in report["warnings"])

    def test_continuous_metric_path(self):
        rng = random.Random(9)
        decisions, outcomes = [], []
        for i in range(200):
            arm = "control" if i % 2 else "treatment"
            decisions.append(_dec(i, arm, "low"))
            outcomes.append(
                _out(
                    i,
                    utilization=rng.uniform(0.5, 0.6)
                    if arm == "control"
                    else rng.uniform(0.7, 0.8),
                )
            )
        report = outcome_report(
            decisions,
            outcomes,
            metric="utilization",
            min_effect=0.05,
            min_samples=50,
        )
        assert report["significance"]["method"] == "unpaired_bootstrap"
        assert report["significance"]["delta"] > 0
        assert report["verdict"]["decision"] == "ship"


class TestAANegativeControl:
    def test_passes_on_null_data(self):
        """无真实效应时 A/A 的显著率应接近 alpha，不能系统性报出差异。"""
        rng = random.Random(13)
        decisions, outcomes = [], []
        for i in range(2000):
            arm = "control" if i % 2 else "treatment"
            decisions.append(_dec(i, arm, "low"))
            outcomes.append(_out(i, filled=rng.random() < 0.3))
        aa = aa_negative_control(decisions, outcomes, arm="control", metric="filled")
        assert aa["passed"] is True
        assert aa["significant_rate"] <= 0.15

    def test_insufficient_sample_is_not_a_pass(self):
        decisions = [_dec(i, "control", "low") for i in range(10)]
        outcomes = [_out(i, filled=True) for i in range(10)]
        aa = aa_negative_control(decisions, outcomes)
        assert aa["passed"] is False
        assert "样本不足" in aa["reason"]

    def test_deterministic_with_seed(self):
        rng = random.Random(17)
        decisions = [_dec(i, "control", "low") for i in range(400)]
        outcomes = [_out(i, filled=rng.random() < 0.4) for i in range(400)]
        first = aa_negative_control(decisions, outcomes, seed=1)
        second = aa_negative_control(decisions, outcomes, seed=1)
        assert first["significant_rate"] == second["significant_rate"]


class TestRealisticFixture:
    @pytest.mark.parametrize("scale", [200, 4000])
    def test_report_is_json_serializable(self, scale):
        """报告要能直接落盘 / 进 CI artifact，不能带不可序列化对象。"""
        import json

        from scripts.run_outcome_demo import generate_events

        decisions, outcomes = generate_events(scale, seed=23)
        report = outcome_report(
            decisions,
            outcomes,
            metric="filled",
            strata_keys=("urgency",),
            min_samples=20,
            guardrail_metrics=("responded",),
        )
        assert json.dumps(report, ensure_ascii=False)
