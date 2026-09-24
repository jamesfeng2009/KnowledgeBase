#!/usr/bin/env python
"""决策 → 下游结果闭环 demo（app.services.outcome_attribution 的可运行样例）。

为什么用合成数据：
    结果归因这条链路最难的不是算法，是**没有数据可验证**。等业务系统
    接好再写分析代码，等于在没有测试的情况下改生产。这里造一份形态
    与真实 outreach→fill 一致的数据（含混杂、含 join 不上的样本、含
    重复事件），把整条链路跑通并**故意让朴素结论是错的**，用来验证
    分析层确实能挡住这些坑。

合成数据里埋了三个陷阱（demo 会逐个暴露）：
    1. 辛普森反转：treatment 在每一层（急单/缓单）内都比 control 差，
       但它被分到了更多「急单」（本来就更容易填上），合并算出的比率
       反而更高 —— 只看合并数字会得出方向完全相反的结论。
    2. 匹配率缺口：约 8% 的决策没有对应结果事件（下游系统没回写），
       报告必须把匹配率摊开，而不是只统计 join 上的样本。
    3. 重复结果事件：同一 decision_id 出现多条结果（上游按「触达次数」
       而不是「决策」发事件），若不去重会放大样本量、把方差算小。

用法::

    python scripts/run_outcome_demo.py                 # 打印报告
    python scripts/run_outcome_demo.py --json /tmp/o.json   # 同时落盘
    python scripts/run_outcome_demo.py --dump-dir /tmp/ev   # 导出事件 JSONL
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.outcome_attribution import (  # noqa: E402
    aa_negative_control,
    outcome_report,
)

EXPERIMENT = "outreach_ranking_v2"

#: 每层的真实填单率（treatment 两层都比 control 低 —— 真实效应是负的）
_FILL_RATE: dict[str, dict[str, float]] = {
    "high": {"control": 0.55, "treatment": 0.48},
    "low": {"control": 0.22, "treatment": 0.15},
}

#: 回应率（红线指标）：treatment 在两层内和合并后都更差
_RESPOND_RATE: dict[str, dict[str, float]] = {
    "high": {"control": 0.70, "treatment": 0.42},
    "low": {"control": 0.35, "treatment": 0.12},
}

#: 各臂里「急单」占比 —— 混杂来源：treatment 被分到更多容易成功的单
_URGENCY_HIGH_SHARE: dict[str, float] = {"control": 0.30, "treatment": 0.80}


def generate_events(
    n_decisions: int = 4000, seed: int = 20260923
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成决策事件与结果事件（含上述三个陷阱）。

    Returns:
        ``(decisions, outcomes)`` 两个 dict 列表。
    """
    rng = random.Random(seed)
    decisions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []

    for i in range(n_decisions):
        arm = "control" if i % 2 == 0 else "treatment"
        urgency = (
            "high" if rng.random() < _URGENCY_HIGH_SHARE[arm] else "low"
        )
        decision_id = f"d{i:05d}"
        decisions.append(
            {
                "decision_id": decision_id,
                "experiment": EXPERIMENT,
                "arm": arm,
                "unit_id": f"shift-{i % (n_decisions // 3)}",
                "strata": {"urgency": urgency},
                "correlation_id": f"conv-{i}",
                "model": "qwen-dpo-v3-7b",
                "prompt_variant": "answer_first" if arm == "treatment" else "",
                "ts": f"2026-09-{(i % 28) + 1:02d}T09:00:00Z",
            }
        )

        # 陷阱 2：约 8% 的决策没有回写结果
        if rng.random() < 0.08:
            continue
        filled = rng.random() < _FILL_RATE[urgency][arm]
        responded = rng.random() < _RESPOND_RATE[urgency][arm]
        outcomes.append(
            {
                "decision_id": decision_id,
                "filled": filled,
                "responded": responded,
                "retained_30d": filled and rng.random() < 0.62,
                "utilization": round(rng.uniform(0.55, 0.95), 3) if filled else None,
                "filled_latency_hours": round(rng.uniform(1, 72), 1) if filled else None,
                "source_system": "staffing-core",
            }
        )
        # 陷阱 3：约 3% 的结果事件重复（上游按触达次数发事件）
        if rng.random() < 0.03:
            outcomes.append(
                {
                    "decision_id": decision_id,
                    "filled": filled,
                    "responded": responded,
                    "source_system": "outreach-log",
                }
            )

    rng.shuffle(outcomes)
    return decisions, outcomes


def _print_report(report: dict[str, Any], title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")
    diag = report["diagnostics"]
    print(
        f"join 诊断：决策 {diag['n_decisions']} 条 / 结果 {diag['n_outcomes']} 条 / "
        f"匹配 {diag['matched']}（匹配率 {diag['match_rate']}）/ "
        f"重复结果 {diag['duplicate_outcomes']} / 孤儿结果 {diag['orphan_outcomes']}"
    )
    for arm, row in report["arms"].items():
        extra = ""
        if row["avg_utilization"] is not None:
            extra += f"  平均利用率={row['avg_utilization']}"
        print(
            f"  {arm:<10} n={row['n']:<6} {row['metric']}={row['rate']} "
            f"[{row['ci_low']}, {row['ci_high']}]{extra}"
        )
    sig = report["significance"]
    if sig:
        print(
            f"\n合并差异 delta={sig['delta']:+.4f}  p={sig['p_value']}  "
            f"significant={sig['significant']}"
        )
    conf = report["confounding"]
    if conf:
        print(
            f"分层校正（{'+'.join(conf['strata_keys'])}）："
            f"adjusted_delta={conf['adjusted_delta']:+.4f}，"
            f"使用 {conf['n_strata_used']} 层"
        )
        for row in conf["per_stratum"]:
            print(
                f"    {'/'.join(row['strata']):<12} "
                f"control={row['rate_control']}({row['n_control']}) "
                f"treatment={row['rate_treatment']}({row['n_treatment']}) "
                f"delta={row['delta']:+.4f}"
            )
    for guard in report["guardrails"]:
        flag = "恶化 ✗" if guard["worse"] else "正常"
        print(
            f"红线 {guard['metric']}: control={guard['rate_control']} "
            f"treatment={guard['rate_treatment']} p={guard['p_value']} → {flag}"
        )
    for warn in report["warnings"]:
        print(f"⚠ {warn}")
    verdict = report.get("verdict") or {}
    print(f"\n判定：{verdict.get('decision', 'n/a')}")
    for reason in verdict.get("reasons", []):
        print(f"  - {reason}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="决策→结果闭环 demo")
    parser.add_argument("--n", type=int, default=4000, help="决策事件条数")
    parser.add_argument("--seed", type=int, default=20260923, help="随机种子")
    parser.add_argument("--json", help="把报告写到该 JSON 文件")
    parser.add_argument("--dump-dir", help="把两份事件表导出为 JSONL 的目录")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    decisions, outcomes = generate_events(args.n, args.seed)
    print(
        f"合成事件：决策 {len(decisions)} 条，结果 {len(outcomes)} 条 "
        f"（含混杂分层 / 匹配缺口 / 重复结果三类陷阱）"
    )

    if args.dump_dir:
        out_dir = Path(args.dump_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in (("decisions", decisions), ("outcomes", outcomes)):
            with open(out_dir / f"{name}.jsonl", "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"事件已导出到 {out_dir}/decisions.jsonl 与 outcomes.jsonl")

    report = outcome_report(
        decisions,
        outcomes,
        metric="filled",
        strata_keys=("urgency",),
        min_effect=0.02,
        min_samples=200,
        guardrail_metrics=("responded",),
    )
    _print_report(report, "结果归因：outreach ranking 实验（filled）")

    # A/A 负对照：同一臂随机劈半多次，看显著率是否接近 alpha
    aa = aa_negative_control(decisions, outcomes, arm="control", metric="filled")
    print(f"\n{'=' * 62}\nA/A 负对照（control 臂随机劈半）\n{'=' * 62}")
    if aa.get("splits"):
        print(
            f"  每次劈半 n≈{aa['n_per_split']}，共 {aa['splits']} 次："
            f"显著 {aa['significant_splits']} 次（显著率 {aa['significant_rate']}，"
            f"应接近 alpha={aa['alpha']}），中位 p={aa['median_p']}"
        )
    else:
        print(f"  未执行：{aa.get('reason')}")
    print(
        f"  结论：{'通过 — 管道未自造效应' if aa['passed'] else '未通过 — 先查分流与去重'}"
    )

    if args.json:
        payload = {"report": report, "aa_negative_control": aa}
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
