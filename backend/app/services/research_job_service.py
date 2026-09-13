"""ResearchJob 幂等创建服务 — 单一职责：同一次调研提交只落一条任务。

保证链路（缺一不可）：
    1. 前置查询命中 → 直接复用已提交任务（省一次插入的快捷路径）；
    2. 并发双插 → 部分唯一索引 uq_research_jobs_idem_active 兜底，
       只有一个赢家，输家收到 IntegrityError；
    3. 冲突回读 → input_hash 一致返回原任务（200 语义），
       不一致抛 IdempotencyConflictError（409 IDEMPOTENCY_CONFLICT）；
    4. failed 任务不占键 → 失败后可带原键重试。

并发语义：插入用 SAVEPOINT（begin_nested）包裹，冲突时只回滚插入本身，
外层事务（权限收敛等读取）不受影响；输家在赢家提交后回读必能命中 ——
若回读为 None 属异常状态，显式上抛而非编造成功。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research import ResearchJob
from app.services.submit_idempotency import (
    IdempotencyConflictError,
    canonical_input_hash,
    is_unique_violation,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

#: research_jobs 幂等部分唯一索引名 — IntegrityError 只认这一条
IDEM_CONSTRAINT: str = "uq_research_jobs_idem_active"

__all__ = [
    "IDEM_CONSTRAINT",
    "IdempotencyConflictError",
    "backfill_result_from_redis",
    "compute_input_hash",
    "create_research_job",
    "find_active_job",
    "mark_research_job_status",
]


def compute_input_hash(goal: str, kb_ids: list[str] | None) -> str:
    """计算调研提交指纹 — goal + 排序去重后的 kb_ids。

    用"请求输入"而非权限收敛结果计算：收敛集合随账号权限变化，
    若参与指纹，同一提交在不同时刻会得到不同哈希，制造假 409。
    """
    return canonical_input_hash(
        {"goal": goal, "kb_ids": sorted(set(kb_ids or []))}
    )


async def find_active_job(
    db: AsyncSession, user_id: uuid.UUID, idempotency_key: str
) -> ResearchJob | None:
    """查找该用户同键的活跃任务（failed 不占键，不参与命中）。"""
    stmt = (
        select(ResearchJob)
        .where(
            ResearchJob.user_id == user_id,
            ResearchJob.idempotency_key == idempotency_key,
            ResearchJob.status != "failed",
        )
        .order_by(ResearchJob.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _resolve_existing(
    existing: ResearchJob, input_hash: str
) -> tuple[ResearchJob, bool]:
    """命中旧任务后的裁决：同输入复用，异输入 409。"""
    if existing.input_hash != input_hash:
        raise IdempotencyConflictError(
            "idempotency_key 已绑定不同的提交内容"
        )
    return existing, False


async def create_research_job(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    idempotency_key: str | None,
    input_hash: str,
    goal: str,
    kb_ids: list[str] | None,
) -> tuple[ResearchJob, bool]:
    """创建调研任务 — 返回 (job, created)；重复提交返回 (existing, False)。

    不负责 commit / Celery 派发：调用方在派发成功后再提交事务，
    保证"插入 + 派发"整体成败一致（派发失败回滚，键不被占用）。
    """
    job = ResearchJob(
        id=uuid.uuid4(),
        user_id=user_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        input_hash=input_hash,
        goal=goal,
        kb_ids=list(kb_ids or []),
        status="queued",
    )

    if not idempotency_key:
        # 无键不参与去重（部分唯一索引对 NULL 键天然放行）
        db.add(job)
        await db.flush()
        return job, True

    # 快捷路径：前置查询命中 → 复用（并发空档由唯一索引兜底）
    existing = await find_active_job(db, user_id, idempotency_key)
    if existing is not None:
        return _resolve_existing(existing, input_hash)

    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError as exc:
        # 只认幂等唯一约束；其余约束冲突原样上抛
        if not is_unique_violation(exc, IDEM_CONSTRAINT):
            raise
        existing = await find_active_job(db, user_id, idempotency_key)
        if existing is None:
            # 赢家已提交却查不到 — 异常状态，不编造成功
            raise RuntimeError(
                "唯一约束冲突但未找到已提交任务，请携带原键重试"
            ) from exc
        return _resolve_existing(existing, input_hash)

    return job, True


async def mark_research_job_status(
    job_id: str,
    status: str,
    last_error: str | None = None,
    output: dict | None = None,
    only_when_queued: bool = False,
) -> bool:
    """回写任务终态（Celery worker / 补偿扫描调用）— 失败仅告警不抛错。

    status / output_json / finished_at 在同一条 UPDATE 写入 — 单条 UPDATE
    即原子，查询侧不会读到 "success 配空 output" 的中间态（P0-1 结果权威落库）。

    Args:
        output: status=success 时的最终报告（存 output_json，DB 为权威存储）。
        only_when_queued: True 时仅当记录仍为 queued 才写入（回填/补偿扫描
            用，防止覆盖并发收尾已写入的更新终态）；worker 收尾保持 False。
        status=failed 使部分唯一索引放行该键，调用方可原键重试。

    Returns:
        UPDATE 是否实际命中（rowcount==1）— only_when_queued 守卫落空、
        记录不存在或 DB 写失败时为 False，供回填方判定是否真实写入。
    """
    try:
        jid = uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        return False  # 非 job.id 形态的 task_id（旧路径/手动派发），忽略

    from sqlalchemy import func

    values: dict = {
        "status": status,
        "last_error": last_error,
        # 显式刷新（补偿扫描依赖 updated_at 做"无进展"判定，不依赖 onupdate 生效）
        "updated_at": func.now(),
    }
    if status in ("success", "failed"):
        values["finished_at"] = func.now()
    if output is not None:
        values["output_json"] = output

    from app.database import task_db_session

    try:
        async with task_db_session() as session:
            stmt = update(ResearchJob).where(ResearchJob.id == jid)
            if only_when_queued:
                stmt = stmt.where(ResearchJob.status == "queued")
            result = await session.execute(stmt.values(**values))
            await session.commit()
            return bool(result.rowcount)
    except Exception as exc:
        log.warning(
            "research_job.status_update_failed",
            job_id=str(job_id),
            status=status,
            error=str(exc)[:200],
        )
        return False


async def backfill_result_from_redis(task_id: str, data: dict) -> bool:
    """把 Redis 持久化的终态结果回填 DB — 查询自愈与补偿扫描共用。

    场景：worker 收尾 mark_research_job_status 静默失败（DB 抖动）、或
    output_json 列上线前的存量任务 — Redis 有终态而 DB 仍 queued。
    仅当记录仍为 queued（only_when_queued 守卫）才写入，绝不覆盖终态。

    Args:
        data: research_progress.save_result/save_failure 的持久化结构：
              {"status": "success", "report": {...}} 或
              {"status": "failed", "error": "..."}。

    Returns:
        True 表示确实发生了回填（守卫落空 / 非终态数据为 False）。
    """
    status = data.get("status")
    if status == "success":
        report = data.get("report")
        if not isinstance(report, dict):
            return False
        return await mark_research_job_status(
            task_id, "success", last_error=None, output=report, only_when_queued=True
        )
    if status == "failed":
        return await mark_research_job_status(
            task_id,
            "failed",
            last_error=str(data.get("error", "unknown error"))[:200],
            only_when_queued=True,
        )
    return False
