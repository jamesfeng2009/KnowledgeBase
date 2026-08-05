"""
case 级回归对比测试 — 评测.md §10.5 gate：均值不变但个案退化也算回归。

覆盖范围：
    - _compare_cases：pass→fail / metric_drop / new / missing / ok 各分支
    - compare_with_baseline：case 级退化翻转 is_regression、case_diffs 输出
    - CLI _format_comparison：回归用例展示
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery（测试环境未安装，参考 test_eval.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


def _make_run_result(
    case_specs: list[tuple[str, float, str | None]],
    **avg_kwargs: float,
) -> object:
    """构造 EvalRunResult。case_specs: (query, recall_at_5, error)。"""
    from app.eval.runner import EvalCaseResult, EvalRunResult

    cases = [
        EvalCaseResult(query=q, recall_at_5=r, error=e)
        for q, r, e in case_specs
    ]
    return EvalRunResult(
        case_results=cases,
        total=len(cases),
        passed=sum(1 for c in cases if c.passed),
        avg_recall_at_5=avg_kwargs.get("avg_recall_at_5", 0.0),
        avg_mrr=avg_kwargs.get("avg_mrr", 0.0),
        avg_ndcg_at_5=avg_kwargs.get("avg_ndcg_at_5", 0.0),
        avg_judge_score=avg_kwargs.get("avg_judge_score", 0.0),
    )


# ======================================================================
# _compare_cases 单元测试
# ======================================================================


class TestCompareCases:
    """_compare_cases 纯函数测试。"""

    def _compare(
        self,
        current: object,
        baseline: object,
        threshold: float = 0.05,
    ) -> tuple[list[dict], bool]:
        from app.eval.repository import EvalRepository

        return EvalRepository._compare_cases(current, baseline, threshold)

    def test_all_ok_no_regression(self) -> None:
        cur = _make_run_result([("q1", 1.0, None), ("q2", 0.8, None)])
        base = _make_run_result([("q1", 1.0, None), ("q2", 0.8, None)])
        diffs, regressed = self._compare(cur, base)
        assert regressed is False
        assert all(d["change"] == "ok" for d in diffs)

    def test_pass_to_fail_detected(self) -> None:
        cur = _make_run_result([("q1", 0.0, "retrieve_error: db down")])
        base = _make_run_result([("q1", 1.0, None)])
        diffs, regressed = self._compare(cur, base)
        assert regressed is True
        assert diffs[0]["change"] == "pass→fail"
        assert diffs[0]["regressed"] is True
        assert diffs[0]["error"] == "retrieve_error: db down"

    def test_fail_to_pass_not_regression(self) -> None:
        cur = _make_run_result([("q1", 1.0, None)])
        base = _make_run_result([("q1", 0.0, "some error")])
        diffs, regressed = self._compare(cur, base)
        assert regressed is False
        assert diffs[0]["change"] == "ok"

    def test_metric_drop_beyond_threshold(self) -> None:
        # recall 1.0 → 0.5，相对下降 50% > 5% 阈值
        cur = _make_run_result([("q1", 0.5, None)])
        base = _make_run_result([("q1", 1.0, None)])
        diffs, regressed = self._compare(cur, base, threshold=0.05)
        assert regressed is True
        assert diffs[0]["change"] == "metric_drop"
        assert diffs[0]["recall_relative_drop"] == 0.5

    def test_metric_drop_within_threshold_ok(self) -> None:
        # recall 1.0 → 0.98，相对下降 2% < 5% 阈值
        cur = _make_run_result([("q1", 0.98, None)])
        base = _make_run_result([("q1", 1.0, None)])
        diffs, regressed = self._compare(cur, base, threshold=0.05)
        assert regressed is False
        assert diffs[0]["change"] == "ok"

    def test_baseline_zero_recall_no_metric_regression(self) -> None:
        # 基线 recall=0 时无法计算相对下降，不算 metric 回归
        cur = _make_run_result([("q1", 0.0, None)])
        base = _make_run_result([("q1", 0.0, None)])
        diffs, regressed = self._compare(cur, base)
        assert regressed is False

    def test_new_case_marked(self) -> None:
        cur = _make_run_result([("q1", 1.0, None), ("q_new", 0.5, None)])
        base = _make_run_result([("q1", 1.0, None)])
        diffs, regressed = self._compare(cur, base)
        assert regressed is False
        new_diff = next(d for d in diffs if d["query"] == "q_new")
        assert new_diff["change"] == "new"
        assert new_diff["regressed"] is False

    def test_missing_case_marked(self) -> None:
        cur = _make_run_result([("q1", 1.0, None)])
        base = _make_run_result([("q1", 1.0, None), ("q_gone", 0.5, None)])
        diffs, regressed = self._compare(cur, base)
        assert regressed is False
        missing_diff = next(d for d in diffs if d["query"] == "q_gone")
        assert missing_diff["change"] == "missing"
        assert missing_diff["regressed"] is False

    def test_empty_cases_no_crash(self) -> None:
        cur = _make_run_result([])
        base = _make_run_result([])
        diffs, regressed = self._compare(cur, base)
        assert diffs == []
        assert regressed is False


# ======================================================================
# compare_with_baseline 集成 — case 级翻转 is_regression
# ======================================================================


class TestCompareWithBaselineCaseLevel:
    """compare_with_baseline 集成 case 级对比。"""

    def _compare(self, current: object, baseline: object) -> dict:
        from app.eval.repository import EvalRepository

        return EvalRepository.compare_with_baseline(current, baseline)

    def test_case_regression_flips_is_regression(self) -> None:
        """均值不变但个案 pass→fail 时 is_regression 应为 True（§10.5 gate）。"""
        # 均值相同（avg 指标均为 0），但 q1 出现 pass→fail
        cur = _make_run_result(
            [("q1", 0.0, "boom"), ("q2", 1.0, None)],
            avg_recall_at_5=0.5,
        )
        base = _make_run_result(
            [("q1", 1.0, None), ("q2", 0.0, "old err")],
            avg_recall_at_5=0.5,
        )
        comparison = self._compare(cur, base)
        assert comparison["is_regression"] is True
        assert comparison["regressed_case_count"] == 1
        regressed = [d for d in comparison["case_diffs"] if d["regressed"]]
        assert regressed[0]["query"] == "q1"
        assert regressed[0]["change"] == "pass→fail"

    def test_no_case_regression_and_no_metric_regression(self) -> None:
        cur = _make_run_result(
            [("q1", 1.0, None)],
            avg_recall_at_5=1.0,
            avg_mrr=1.0,
        )
        base = _make_run_result(
            [("q1", 1.0, None)],
            avg_recall_at_5=1.0,
            avg_mrr=1.0,
        )
        comparison = self._compare(cur, base)
        assert comparison["is_regression"] is False
        assert comparison["regressed_case_count"] == 0
        assert all(d["change"] == "ok" for d in comparison["case_diffs"])

    def test_metric_regression_still_works(self) -> None:
        """聚合指标回归路径不受 case 级对比影响。"""
        cur = _make_run_result([("q1", 0.5, None)], avg_recall_at_5=0.5)
        base = _make_run_result([("q1", 1.0, None)], avg_recall_at_5=1.0)
        comparison = self._compare(cur, base)
        assert comparison["is_regression"] is True
        assert comparison["metrics"]["avg_recall_at_5"]["regressed"] is True

    def test_case_diffs_structure(self) -> None:
        cur = _make_run_result([("q1", 0.5, None)])
        base = _make_run_result([("q1", 1.0, None)])
        comparison = self._compare(cur, base)
        diff = comparison["case_diffs"][0]
        assert set(diff.keys()) >= {
            "query",
            "change",
            "regressed",
            "recall_current",
            "recall_baseline",
            "recall_relative_drop",
            "error",
        }


# ======================================================================
# CLI 展示
# ======================================================================


class TestFormatComparisonCaseLevel:
    """CLI _format_comparison case 级展示测试。"""

    def test_regressed_cases_rendered(self) -> None:
        from scripts.run_eval import _format_comparison

        comparison = {
            "threshold": 0.05,
            "is_regression": True,
            "metrics": {},
            "regressed_case_count": 2,
            "case_diffs": [
                {
                    "query": "报销流程是什么",
                    "change": "pass→fail",
                    "regressed": True,
                    "recall_current": 0.0,
                    "recall_baseline": 1.0,
                    "recall_relative_drop": 1.0,
                    "error": "retrieve_error: timeout",
                },
                {
                    "query": "年假申请",
                    "change": "metric_drop",
                    "regressed": True,
                    "recall_current": 0.5,
                    "recall_baseline": 1.0,
                    "recall_relative_drop": 0.5,
                    "error": None,
                },
                {"query": "q3", "change": "ok", "regressed": False},
            ],
        }
        output = _format_comparison(comparison)
        assert "case 级回归: 2 条用例退化" in output
        assert "[pass→fail] 报销流程是什么" in output
        assert "[metric_drop] 年假申请" in output
        # ok 用例不展示
        assert "q3" not in output

    def test_no_regressed_cases_section_omitted(self) -> None:
        from scripts.run_eval import _format_comparison

        comparison = {
            "threshold": 0.05,
            "is_regression": False,
            "metrics": {},
            "case_diffs": [{"query": "q1", "change": "ok", "regressed": False}],
        }
        output = _format_comparison(comparison)
        assert "case 级回归" not in output
