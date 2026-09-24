#!/usr/bin/env python
"""两次评测结果的显著性对比 —— 回答「涨了 1.3 分算不算更好」。

用法::

    python scripts/ab_report.py \\
        --control /tmp/run_control.json --treatment /tmp/run_treatment.json \\
        --metric recall_at_5 --min-effect 0.02

输入是 ``EvalRunResult.to_dict()`` 落盘的 JSON（run_eval.py --json 产物）。
两个 run 必须跑同一份数据集 —— 脚本按 case_id 配对，并报告配对不上的条数；
配对率过低时结论只覆盖交集样本，不能当成全量结论。

判定口径见 app.eval.significance.judge_experiment：既要统计显著（p < alpha），
也要效应够大（delta ≥ min_effect），还要红线指标没恶化，三者齐备才给 ship。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.eval.significance import (  # noqa: E402
    extract_metric_pairs,
    extract_passed_pairs,
    judge_experiment,
    mcnemar_test,
    paired_bootstrap,
)

#: 默认一并复检的红线指标（恶化即回滚，不看主指标涨了多少）
DEFAULT_GUARDRAILS = ("ndcg_at_5", "mrr")


def _load_cases(path: str) -> list[dict[str, Any]]:
    """读 run JSON 的 case_results（容忍传入的就是 case 列表）。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    cases = data.get("case_results") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise SystemExit(f"{path} 里没有 case_results 字段")
    return [c for c in cases if isinstance(c, dict)]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测结果显著性对比")
    parser.add_argument("--control", required=True, help="对照 run JSON")
    parser.add_argument("--treatment", required=True, help="实验 run JSON")
    parser.add_argument(
        "--metric",
        default="recall_at_5",
        help="主指标字段名（支持 judge_scores.total 嵌套路径与 passed 布尔字段）",
    )
    parser.add_argument(
        "--min-effect", type=float, default=0.02, help="值得上线的最小提升"
    )
    parser.add_argument("--alpha", type=float, default=0.05, help="显著性水平")
    parser.add_argument(
        "--min-samples", type=int, default=30, help="最小配对样本量（低于则 hold）"
    )
    parser.add_argument(
        "--resamples", type=int, default=2000, help="自助重采样次数"
    )
    parser.add_argument(
        "--guardrails",
        default=",".join(DEFAULT_GUARDRAILS),
        help="红线指标（逗号分隔，空字符串表示不检查）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    return parser.parse_args(argv)


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    """执行配对检验 + 红线复检 + 门禁判定。"""
    control_cases = _load_cases(args.control)
    treatment_cases = _load_cases(args.treatment)

    scores_a, scores_b, dropped = extract_metric_pairs(
        control_cases, treatment_cases, args.metric
    )
    boot = paired_bootstrap(
        scores_a, scores_b, resamples=args.resamples, alpha=args.alpha
    )
    passed_a, passed_b = extract_passed_pairs(control_cases, treatment_cases)
    mcn = mcnemar_test(passed_a, passed_b, alpha=args.alpha)

    violations: list[str] = []
    guardrail_rows: list[dict[str, Any]] = []
    for guard in [g.strip() for g in (args.guardrails or "").split(",") if g.strip()]:
        ga, gb, _ = extract_metric_pairs(control_cases, treatment_cases, guard)
        if not ga:
            continue
        g = paired_bootstrap(ga, gb, resamples=args.resamples, alpha=args.alpha)
        guardrail_rows.append({"metric": guard, **g.to_dict()})
        if g.delta < 0 and g.significant:
            violations.append(f"{guard} 显著下降 {g.delta:+.4f} (p={g.p_value:.4f})")

    verdict = judge_experiment(
        metric=args.metric,
        delta=boot.delta,
        p_value=boot.p_value,
        n=boot.n,
        min_effect=args.min_effect,
        alpha=args.alpha,
        min_samples=args.min_samples,
        ci_low=boot.ci_low,
        ci_high=boot.ci_high,
        guardrail_violations=violations,
    )
    return {
        "pairing": {
            "control_cases": len(control_cases),
            "treatment_cases": len(treatment_cases),
            "paired": boot.n,
            "dropped": dropped,
        },
        "primary": {"metric": args.metric, **boot.to_dict()},
        "passed_mcnemar": mcn,
        "guardrails": guardrail_rows,
        "verdict": verdict.to_dict(),
    }


def _print_text(report: dict[str, Any]) -> None:
    pairing = report["pairing"]
    primary = report["primary"]
    verdict = report["verdict"]
    print("=== 配对情况 ===")
    print(
        f"  control={pairing['control_cases']} treatment={pairing['treatment_cases']} "
        f"配对成功={pairing['paired']} 剔除={pairing['dropped']}"
    )
    if pairing["paired"] == 0:
        print("  没有可配对的用例（case_id 不匹配？），无法给出结论")
        return
    print(f"\n=== 主指标 {primary['metric']}（配对自助检验）===")
    print(f"  control={primary['mean_a']}  treatment={primary['mean_b']}")
    print(
        f"  delta={primary['delta']}  95%CI=[{primary['ci_low']}, {primary['ci_high']}]"
        f"  p={primary['p_value']}"
    )
    mcn = report["passed_mcnemar"]
    print(f"\n=== 通过/失败（McNemar, b={mcn['b']} c={mcn['c']}）===")
    print(f"  p={mcn['p_value']}  significant={mcn['significant']}")
    if report["guardrails"]:
        print("\n=== 红线指标 ===")
        for row in report["guardrails"]:
            print(f"  {row['metric']}: delta={row['delta']} p={row['p_value']}")
    print(f"\n=== 判定：{verdict['decision'].upper()} ===")
    for reason in verdict["reasons"] or ["满足显著性、效应量与红线三项要求"]:
        print(f"  - {reason}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    decision = report["verdict"]["decision"]
    # 非 0 退出便于 CI 把「判定不通过」变成流水线红灯；hold 不算失败
    return 1 if decision == "rollback" else 0


if __name__ == "__main__":
    raise SystemExit(main())
