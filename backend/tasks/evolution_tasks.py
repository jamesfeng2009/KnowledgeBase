"""技能进化 Celery 任务 — 异步执行生成层基础指引的进化 run。

将耗时的 LLM 调用（rollout + judge + optimizer，单次 run 数十至上百次）
从 HTTP 请求中剥离；产物为审计链目录（summary.json / best.diff 等），
合入线上 prompt 文件由人工 review best.diff 后完成。

在 ``celery_app`` 不可用时（如开发环境）优雅降级，仅输出告警日志。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 后端根目录（backend/）— 任务默认以相对路径寻址数据集与目标文件
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _run_async(coro):
    """在同步 Celery 任务中执行异步协程。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _evolve_generate_base(
    dataset: str,
    weak_pool: str,
    target: str,
    run_dir: str | None,
) -> dict:
    """异步执行一次进化 run — 构造 EvolutionLoop 并跑完。"""
    from app.config import get_settings
    from app.evolution.loop import EvolutionLoop
    from app.llm.factory import get_llm_provider, get_llm_provider_by_model

    try:
        llm = get_llm_provider()
    except Exception as exc:
        logger.warning("evolution.llm_unavailable", error=str(exc))
        return {"status": "skipped", "reason": "llm_unavailable"}

    # optimizer 独立模型（settings.EVOLUTION_OPTIMIZER_MODEL；空=同主模型）
    optimizer_llm = None
    optimizer_model = get_settings().EVOLUTION_OPTIMIZER_MODEL
    if optimizer_model:
        try:
            optimizer_llm = get_llm_provider_by_model(optimizer_model)
        except Exception as exc:
            logger.warning(
                "evolution.optimizer_model_unavailable",
                extra={"model": optimizer_model, "error": str(exc)},
            )

    dataset_path = Path(dataset)
    weak_pool_path = Path(weak_pool)
    target_path = Path(target)
    if not dataset_path.is_absolute():
        dataset_path = _BACKEND_ROOT / dataset_path
    if not weak_pool_path.is_absolute():
        weak_pool_path = _BACKEND_ROOT / weak_pool_path
    if not target_path.is_absolute():
        target_path = _BACKEND_ROOT / target_path

    if run_dir:
        out_dir = Path(run_dir)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = _BACKEND_ROOT / "evolution_runs" / f"run_{ts}"

    evo_loop = EvolutionLoop(
        llm=llm,
        target_file=target_path,
        dataset_path=dataset_path,
        weak_pool_path=weak_pool_path,
        run_dir=out_dir,
        optimizer_llm=optimizer_llm,
    )
    summary = await evo_loop.run()
    return {"status": "success", "run_dir": str(out_dir), **_slim(summary)}


def _slim(summary: dict) -> dict:
    """压缩 summary 供任务返回值（明细看 run_dir/summary.json）。"""
    return {
        "run_id": summary.get("run_id"),
        "best_round": summary.get("best_round"),
        "stop_reason": summary.get("stop_reason"),
        "llm_calls_used": summary.get("llm_calls_used"),
        "best_metrics": summary.get("best_metrics"),
    }


# ======================================================================
# Celery 任务定义 — 延迟导入 celery_app 避免循环依赖
# ======================================================================

try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.evolution_tasks.evolve_generate_base_task")
    def evolve_generate_base_task(
        dataset: str = "eval_cases/sel_qa.jsonl",
        weak_pool: str = "eval_cases/weak_pool.jsonl",
        target: str = "app/rag/prompts/generate_base.md",
        run_dir: str | None = None,
    ) -> dict:
        """执行一次生成层指引进化 run（手动触发优先，beat 周调度可选）。"""
        logger.info(
            "evolution.task.start",
            extra={"dataset": dataset, "weak_pool": weak_pool, "target": target},
        )
        return _run_async(
            _evolve_generate_base(dataset, weak_pool, target, run_dir)
        )

except ImportError:  # pragma: no cover - 开发环境无 celery_app
    logger.warning("evolution.celery_unavailable")
