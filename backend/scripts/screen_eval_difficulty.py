#!/usr/bin/env python
"""评测用例难度筛选 — 用当前指引 rollout+judge，按判官分挑出低分（难）用例。

目的：构建有区分度的 D_sel。若候选池整体接近满分（如 run_20260913_141250
的 baseline 4.9665），门控死区在结构上不可能通过 — 进化循环只能保守拒绝。
本脚本对每条候选独立打分，输出难度报告并把低分用例筛为「难度池」，
供 build_eval_datasets.py 构建 D_sel v2。

用法（backend/ 目录下）::

    python scripts/screen_eval_difficulty.py \
        --input eval_cases/candidates_feedback.jsonl \
        --hard-out eval_cases/hard_pool.jsonl --threshold 4.5

输出：
    --hard-out  总分 ≤ threshold 的用例（难度池，建议进 D_sel）
    --report    全部用例的逐条分数（difficulty_report.json）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.prompt_files import (  # noqa: E402
    compose_guidance_prompt,
    load_prompt_file,
    split_guidance_sections,
)
from app.llm.factory import get_llm_provider  # noqa: E402
from app.observability.llm_judge import LLMJudgeService  # noqa: E402
from app.rag.generator import Generator  # noqa: E402

_TARGET = "app/rag/prompts/generate_base.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测用例难度筛选")
    parser.add_argument(
        "--input", default="eval_cases/candidates_feedback.jsonl",
        help="候选池 JSONL（case_id/query/contexts）",
    )
    parser.add_argument(
        "--hard-out", default="eval_cases/hard_pool.jsonl",
        help="难度池输出（总分 ≤ threshold）",
    )
    parser.add_argument(
        "--report", default="eval_cases/difficulty_report.json",
        help="逐条分数报告输出",
    )
    parser.add_argument(
        "--threshold", type=float, default=4.5,
        help="难度判定阈值（总分 ≤ threshold 计为难例）",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    input_path = (
        Path(args.input)
        if Path(args.input).is_absolute()
        else _BACKEND_ROOT / args.input
    )
    cases = [
        json.loads(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        print("错误：候选池为空")
        return 1

    target = _BACKEND_ROOT / _TARGET
    guidance, redline = split_guidance_sections(
        target.read_text(encoding="utf-8")
    )
    base_text = compose_guidance_prompt(guidance, redline)

    llm = get_llm_provider()
    judge = LLMJudgeService(judge_llm=llm)

    report: list[dict] = []
    hard: list[dict] = []
    for case in cases:
        generator = Generator(llm, base_guidance=base_text)
        docs = [
            {
                "doc_id": f"{case['case_id']}-c{i}",
                "title": f"引用片段 {i}",
                "content": ctx,
            }
            for i, ctx in enumerate(case["contexts"], start=1)
        ]
        parts: list[str] = []
        async for token in generator.generate(
            query=case["query"],
            retrieved_docs=docs,
            tool_results=[],
            temperature=0.0,  # 与进化循环 rollout 一致（贪心解码，指标可比）
        ):
            if isinstance(token, str):
                parts.append(token)
        answer = "".join(parts).strip()

        result = await judge.evaluate_single(case["query"], answer, case["contexts"])
        score = float(result.total_score)
        record = {
            "case_id": case["case_id"],
            "query": case["query"],
            "total_score": score,
            "citation_accuracy": result.citation_accuracy,
            "completeness": result.completeness,
            "hallucination_inverse": result.hallucination_inverse,
            "reasoning": result.reasoning,
            "error": result.error,
        }
        report.append(record)
        if result.error is None and score <= args.threshold:
            hard.append(case)
        mark = "ERR" if result.error else ("HARD" if score <= args.threshold else "easy")
        print(
            f"{case['case_id']} [{mark}] {score:.2f} "
            f"(引用 {result.citation_accuracy} 完整 {result.completeness} "
            f"无幻觉 {result.hallucination_inverse}) {case['query'][:30]}"
        )

    scores = [r["total_score"] for r in report if r["error"] is None]
    hard_out = (
        Path(args.hard_out)
        if Path(args.hard_out).is_absolute()
        else _BACKEND_ROOT / args.hard_out
    )
    hard_out.parent.mkdir(parents=True, exist_ok=True)
    with hard_out.open("w", encoding="utf-8") as f:
        for case in hard:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    report_out = (
        Path(args.report)
        if Path(args.report).is_absolute()
        else _BACKEND_ROOT / args.report
    )
    report_out.write_text(
        json.dumps(
            {
                "target": _TARGET,
                "threshold": args.threshold,
                "n_cases": len(report),
                "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
                "min_score": min(scores) if scores else 0.0,
                "max_score": max(scores) if scores else 0.0,
                "n_hard": len(hard),
                "results": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"\n完成：{len(hard)}/{len(cases)} 条难例 → {hard_out}"
        f"（均分 {report_out.stem}: 见 {report_out}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
