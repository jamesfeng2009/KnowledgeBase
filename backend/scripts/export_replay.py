#!/usr/bin/env python
"""线上轨迹 → 评测回放候选集 CLI（app.eval.replay 的入口）。

用途：把 LangFuse 导出 / 本地 span 落盘的**真实执行轨迹**转成评测用例候选，
让每日回归跑在用户真的问过的问题上，而不是只跑手写用例。

用法::

    # 导出候选（默认只导 bad case，自动与现有评测集去重）
    python scripts/export_replay.py --traces /tmp/langfuse_export.json \\
        --existing eval_datasets/ --out eval_datasets/replay_candidates.jsonl

    # 人工在候选文件里补 expected_doc_ids、把 label_source 改成 human 后合入
    python scripts/export_replay.py --promote \\
        --candidates eval_datasets/replay_candidates.jsonl \\
        --target eval_datasets/p6_replay.jsonl

输入格式（每行一个 JSON，或顶层为 JSON 数组）—— 字段宽松匹配：
    {"trace_id": "t1", "query": "...", "answer": "...",
     "retrieved_doc_ids": ["doc_a"], "feedback": "complaint",
     "judge_score": 0.4, "max_iterations_reached": false,
     "spans": [ {SpanRecord.to_dict() 形态，可选} ]}

退出码：0 正常；1 表示无输入或写盘失败（CI 里不因「今天没有新 bad case」而红）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.eval.replay import ReplayExporter, load_traces  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="线上轨迹回放导出器")
    parser.add_argument("--traces", help="轨迹文件（JSONL 或 JSON 数组）")
    parser.add_argument(
        "--out",
        default="eval_datasets/replay_candidates.jsonl",
        help="候选输出路径（追加写）",
    )
    parser.add_argument(
        "--existing",
        action="append",
        default=[],
        help="用于去重的现有评测集路径（目录或文件，可重复传）",
    )
    parser.add_argument(
        "--all-traces",
        action="store_true",
        help="导出全部轨迹而非仅 bad case（默认关闭：好轨迹进评测集只会稀释信号）",
    )
    parser.add_argument(
        "--judge-floor",
        type=float,
        default=0.6,
        help="judge 分数下限，低于此值算差（默认 0.6）",
    )
    parser.add_argument(
        "--use-citations-as-expected",
        action="store_true",
        help="把答案引用的文档当弱 ground truth（仅趋势观察，门禁不认）",
    )
    parser.add_argument("--limit", type=int, default=None, help="本次最多导出条数")
    parser.add_argument(
        "--promote", action="store_true", help="执行合入模式（候选 → 正式评测集）"
    )
    parser.add_argument("--candidates", help="合入模式的候选文件")
    parser.add_argument("--target", help="合入模式的目标评测集文件")
    return parser.parse_args(argv)


def _do_export(args: argparse.Namespace) -> int:
    if not args.traces:
        print("错误：导出模式需要 --traces", file=sys.stderr)
        return 1
    snapshots = load_traces(args.traces)
    if not snapshots:
        print(f"未读到任何轨迹：{args.traces}", file=sys.stderr)
        return 1
    exporter = ReplayExporter(existing_paths=list(args.existing))
    stats = exporter.export(
        snapshots,
        args.out,
        only_bad=not args.all_traces,
        judge_floor=args.judge_floor,
        use_citations_as_expected=args.use_citations_as_expected,
        limit=args.limit,
    )
    print("回放候选导出结果：")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"\n输出：{args.out}")
    print(
        "下一步：人工为 needs_review 用例补 expected_doc_ids，"
        "并把 context_expect.label_source 改为 human，再用 --promote 合入。"
    )
    return 0


def _do_promote(args: argparse.Namespace) -> int:
    if not args.candidates or not args.target:
        print("错误：--promote 需要 --candidates 与 --target", file=sys.stderr)
        return 1
    stats = ReplayExporter.promote(args.candidates, args.target)
    print("合入结果：")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    if stats["rejected_unlabeled"]:
        print(
            f"\n注意：{stats['rejected_unlabeled']} 条因缺人工标注被拒绝合入 —— "
            "这是设计行为，未标注用例进评测集会让指标变成自我确认。"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return _do_promote(args) if args.promote else _do_export(args)


if __name__ == "__main__":
    raise SystemExit(main())
