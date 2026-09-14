"""技能进化 CLI — 对生成层基础指引执行一轮 SkillOptLite 进化 run。

用法（backend/ 目录下）::

    python scripts/run_evolution.py \
        --dataset eval_cases/sel_qa.jsonl \
        --weak-pool eval_cases/weak_pool.jsonl \
        --target app/rag/prompts/generate_base.md

产物（审计链，位于 evolution_runs/run_<ts>/）::
    summary.json / best_guidance.md / best.diff / rounds/ / rejected_edits.jsonl

合入流程：人工 review best.diff → 应用到目标文件 → git commit（可回滚）。
本脚本不自动写回线上文件。

D_sel / 弱样本池构建见 scripts/build_eval_datasets.py。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成层指引进化 run（SkillOptLite）")
    parser.add_argument(
        "--dataset",
        default="eval_cases/sel_qa.jsonl",
        help="D_sel 固定评测集（JSONL：case_id/query/contexts）",
    )
    parser.add_argument(
        "--weak-pool",
        default="eval_cases/weak_pool.jsonl",
        help="D_train 弱样本池（诊断用，同格式）",
    )
    parser.add_argument(
        "--target",
        default="app/rag/prompts/generate_base.md",
        help="进化目标文件",
    )
    parser.add_argument("--run-dir", default=None, help="审计链输出目录（缺省按时间戳新建）")
    parser.add_argument("--rounds", type=int, default=None, help="覆盖最大轮数")
    parser.add_argument("--deadband", type=float, default=None, help="覆盖门控死区")
    parser.add_argument("--budget", type=int, default=None, help="覆盖每轮编辑预算")
    parser.add_argument(
        "--max-llm-calls", type=int, default=None, help="覆盖 LLM 调用总预算"
    )
    parser.add_argument(
        "--optimizer-model",
        default=None,
        help=(
            "optimizer 使用的模型 ID（models.json 中的 id，如 qwen-plus / "
            "qwen-max）；缺省读 settings.EVOLUTION_OPTIMIZER_MODEL，"
            "为空则与 rollout 同模型"
        ),
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> int:
    from app.config import get_settings
    from app.evolution.loop import EvolutionConfig, EvolutionLoop
    from app.llm.factory import get_llm_provider, get_llm_provider_by_model

    def _abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else _BACKEND_ROOT / path

    config = EvolutionConfig.from_settings()
    overrides = {
        "max_rounds": args.rounds,
        "deadband": args.deadband,
        "edit_budget": args.budget,
        "max_llm_calls": args.max_llm_calls,
    }
    changed = {k: v for k, v in overrides.items() if v is not None}
    if changed:
        config = EvolutionConfig(
            **{**config.__dict__, **changed}
        )  # frozen dataclass：整体重建

    llm = get_llm_provider()
    optimizer_model = args.optimizer_model
    if optimizer_model is None:
        optimizer_model = get_settings().EVOLUTION_OPTIMIZER_MODEL
    optimizer_llm = None
    if optimizer_model:
        optimizer_llm = get_llm_provider_by_model(optimizer_model)
        print(f"optimizer 模型：{optimizer_model}（rollout/judge 用默认模型）")
    run_dir = (
        Path(args.run_dir)
        if args.run_dir
        else _BACKEND_ROOT
        / "evolution_runs"
        / f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )

    evo_loop = EvolutionLoop(
        llm=llm,
        target_file=_abs(args.target),
        dataset_path=_abs(args.dataset),
        weak_pool_path=_abs(args.weak_pool),
        run_dir=run_dir,
        config=config,
        optimizer_llm=optimizer_llm,
    )
    summary = await evo_loop.run()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n审计链目录：{run_dir}")
    print(f"合入请人工 review：{run_dir / 'best.diff'}")
    return 0


if __name__ == "__main__":
    raise asyncio.run(main(parse_args()))
