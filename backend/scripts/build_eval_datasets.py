"""评测数据集构建 — 从精选用例池构建 D_sel（固定评测集）与弱样本池。

用法（backend/ 目录下）::

    # 基础用法：从一个人工精选的候选池切分
    python scripts/build_eval_datasets.py \
        --inputs eval_cases/candidates.jsonl \
        --sel-size 20 --seed 42

    # badcase 导出补充进候选池（export_eval_cases.py 的输出无 contexts，
    # 需人工/检索回填 contexts 后才可作为输入）
    python scripts/build_eval_datasets.py \
        --inputs eval_cases/candidates.jsonl eval_cases/badcases_backfilled.jsonl

输入格式（JSONL，每行一个对象，缺一不可）::

    {"case_id": "c001", "query": "报销流程是什么？",
     "contexts": ["<检索上下文片段1>", "..."]}

    - contexts 为该问题的预置检索上下文（2-5 条为宜）— 进化 rollout 时
      D_sel 上新旧指引共享同一上下文，控制变量隔离 prompt 差异。

输出：
    --sel-out  eval_cases/sel_qa.jsonl     D_sel（固定评测集，门控用）
    --weak-out eval_cases/weak_pool.jsonl  弱样本池（诊断用 = 候选池剩余）

分规则：seeded shuffle 后前 sel_size 条进 D_sel，其余进弱样本池；
候选池不足 sel_size 时全部进 D_sel并告警（此时弱样本池为空，诊断退化）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 D_sel / 弱样本池")
    parser.add_argument(
        "--inputs", nargs="+", required=True, help="候选池 JSONL（可多文件）"
    )
    parser.add_argument("--sel-size", type=int, default=20, help="D_sel 用例数")
    parser.add_argument("--seed", type=int, default=42, help="切分随机种子")
    parser.add_argument(
        "--sel-out", default="eval_cases/sel_qa.jsonl", help="D_sel 输出路径"
    )
    parser.add_argument(
        "--weak-out", default="eval_cases/weak_pool.jsonl", help="弱样本池输出路径"
    )
    return parser.parse_args()


def load_and_validate(paths: list[Path]) -> tuple[list[dict], list[str]]:
    """加载并校验候选池；返回（有效记录, 无效原因列表）。"""
    seen_ids: set[str] = set()
    valid: list[dict] = []
    invalid: list[str] = []
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                where = f"{path.name}:{idx}"
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    invalid.append(f"{where}: JSON 解析失败 {exc}")
                    continue
                case_id = str(raw.get("case_id", "")).strip()
                query = str(raw.get("query", "")).strip()
                contexts = [
                    str(c) for c in raw.get("contexts", []) if str(c).strip()
                ]
                if not case_id or not query or not contexts:
                    invalid.append(f"{where}: 缺少 case_id/query/非空 contexts")
                    continue
                if case_id in seen_ids:
                    invalid.append(f"{where}: case_id 重复 {case_id}")
                    continue
                seen_ids.add(case_id)
                valid.append(
                    {
                        "case_id": case_id,
                        "query": query,
                        "contexts": contexts,
                    }
                )
    return valid, invalid


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    inputs = [
        p if p.is_absolute() else _BACKEND_ROOT / p
        for p in map(Path, args.inputs)
    ]
    records, invalid = load_and_validate(inputs)

    for reason in invalid:
        print(f"[跳过] {reason}")

    if not records:
        print("错误：候选池为空（或全部无效），无法构建数据集")
        return 1

    rng = random.Random(args.seed)
    rng.shuffle(records)

    sel_size = min(args.sel_size, len(records))
    sel, weak = records[:sel_size], records[sel_size:]
    if len(records) < args.sel_size:
        print(
            f"[告警] 候选池 {len(records)} 条 < sel-size {args.sel_size}，"
            f"全部进 D_sel；弱样本池为空（诊断阶段将退化为无诊断输入）"
        )

    def _abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else _BACKEND_ROOT / path

    sel_out, weak_out = _abs(args.sel_out), _abs(args.weak_out)
    write_jsonl(sel_out, sel)
    write_jsonl(weak_out, weak)

    print(f"D_sel：{len(sel)} 条 → {sel_out}（seed={args.seed}，勿再改动）")
    print(f"弱样本池：{len(weak)} 条 → {weak_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
