"""
推荐模型重建任务 — 单一职责：离线预计算推荐模型数据并写入 Redis。

对应 SKILL 14.2 离线聚合：按租户统计 UserBehavior，构建协同过滤交互矩阵
（UserCF/ItemCF 共用）与用户偏好向量，写入 Redis（key 含租户隔离，与
RecommendationService 结果缓存同风格），供在线推荐优先读取，
未命中时回退现算（见 recommendation_service._precomputed_get）。

幂等性：Redis SETEX 覆盖写，重复执行结果一致，可安全重试。
遵循开闭原则：新增预计算数据只需在 _rebuild_tenant 中追加写入分支，
并在 RecommendationService 增加对应读取分支。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import redis

from celery_app import celery_app
from app.utils.logger import get_logger
from app.utils.retry import make_celery_retry_kwargs

logger = get_logger(__name__)

#: 预计算数据 Redis TTL — 7 天（rebuild 刷新；过期后服务自动回退现算）
_MODEL_TTL_SECONDS: int = 7 * 24 * 3600
#: 每租户最多取近 N 天行为（控制矩阵规模）
_BEHAVIOR_WINDOW_DAYS: int = 90
#: 每租户最多取 M 条行为（控制矩阵规模）
_BEHAVIOR_MAX_ROWS: int = 50000
#: 偏好向量聚合时每用户最多取 K 篇近期文档（控制 embedding 成本）
_PREFERENCE_MAX_DOCS: int = 20
#: embedding 批量大小（控制单次调用规模）
_EMBED_BATCH_SIZE: int = 64


def _model_key(tenant_id: uuid.UUID | None, suffix: str) -> str:
    """预计算模型数据 Redis key — 与推荐结果缓存同前缀，含租户隔离。"""
    return f"recommend:{tenant_id or 'default'}:{suffix}"


@celery_app.task(
    name="tasks.recommendation_tasks.rebuild_recommendation_model",
    bind=True,
    **make_celery_retry_kwargs(),
)
def rebuild_recommendation_model(self, tenant_id: str | None = None) -> dict[str, Any]:
    """重建推荐模型 — 按租户预计算协同过滤矩阵与用户偏好向量写 Redis。

    Args:
        tenant_id: 指定租户 ID（字符串）；None 则遍历全部租户。

    Returns:
        重建统计信息（tenants / behavior_rows / preference_vectors / rebuilt_at）。
    """
    logger.info("recommend.rebuild_started", tenant_id=tenant_id)
    try:
        result = asyncio.run(_rebuild_async(tenant_id))
        logger.info(
            "recommend.rebuild_completed",
            tenants=result.get("tenants", 0),
            behavior_rows=result.get("behavior_rows", 0),
            preference_vectors=result.get("preference_vectors", 0),
        )
        return result
    except Exception as exc:
        # 必须 retry 而非返回 failed dict：返回 dict 会让 Celery 判定任务成功，
        # autoretry 失效、监控无告警（与 scheduled_tasks 的重抛约定一致）。
        logger.error("recommend.rebuild_failed", error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------
# 异步实现
# ------------------------------------------------------------------


def _get_embedder_safe() -> Any | None:
    """懒加载 Embedder — 不可用时返回 None（跳过偏好向量，仅写交互矩阵）。"""
    try:
        from app.llm.embedder import get_embedder

        return get_embedder()
    except Exception as exc:
        logger.warning("recommend.rebuild.embedder_unavailable", error=str(exc))
        return None


async def _rebuild_async(tenant_id: str | None) -> dict[str, Any]:
    """异步重建实现 — 确定目标租户集合，逐租户预计算并写 Redis。"""
    from sqlalchemy import select

    from app.config import get_settings
    from app.database import task_db_session
    from app.models.billing import Tenant

    settings = get_settings()
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    now = datetime.now(timezone.utc)
    totals = {"tenants": 0, "behavior_rows": 0, "preference_vectors": 0}
    try:
        async with task_db_session() as session:
            if tenant_id:
                tenant_ids: list[uuid.UUID | None] = [uuid.UUID(tenant_id)]
            else:
                # 全量重建：遍历全部租户，确保多租户隔离
                rows = await session.execute(
                    select(Tenant).where(Tenant.deleted_at.is_(None))
                )
                tenant_ids = [t.id for t in rows.scalars().all()]
                # 单租户兜底：无 Tenant 记录时仍重建 default 桶（tenant_id 为 NULL 的行为）
                if not tenant_ids:
                    tenant_ids = [None]

            embedder = _get_embedder_safe()
            for tid in tenant_ids:
                stats = await _rebuild_tenant(session, redis_client, tid, embedder, now)
                totals["tenants"] += 1
                totals["behavior_rows"] += stats["behavior_rows"]
                totals["preference_vectors"] += stats["preference_vectors"]
    finally:
        redis_client.close()

    return {
        "status": "success",
        **totals,
        "rebuilt_at": now.isoformat(),
    }


async def _rebuild_tenant(
    session: Any,
    redis_client: Any,
    tenant_id: uuid.UUID | None,
    embedder: Any | None,
    now: datetime,
) -> dict[str, int]:
    """单租户重建 — 交互矩阵 + 用户偏好向量写 Redis（SETEX 覆盖，幂等）。"""
    from sqlalchemy import select

    from app.models.behavior import UserBehavior
    from app.models.knowledge import Document
    from app.utils.tenant import apply_tenant_filter

    since = now - timedelta(days=_BEHAVIOR_WINDOW_DAYS)
    stmt = (
        select(UserBehavior)
        .where(
            UserBehavior.deleted_at.is_(None),
            UserBehavior.acted_at >= since,
        )
        .order_by(UserBehavior.acted_at.desc())
        .limit(_BEHAVIOR_MAX_ROWS)
    )
    stmt = apply_tenant_filter(stmt, UserBehavior, tenant_id)
    behaviors = list((await session.execute(stmt)).scalars().all())

    # 1) 协同过滤交互矩阵：{user_id: {doc_id: weight}}，UserCF/ItemCF 共用
    matrix: dict[str, dict[str, float]] = {}
    for b in behaviors:
        matrix.setdefault(str(b.user_id), {})
        matrix[str(b.user_id)][str(b.doc_id)] = (
            matrix[str(b.user_id)].get(str(b.doc_id), 0.0) + b.weight
        )
    redis_client.setex(
        _model_key(tenant_id, "cf:matrix"),
        _MODEL_TTL_SECONDS,
        json.dumps(matrix),
    )

    # 2) 用户偏好向量：每用户近期 K 篇文档标题聚合 embedding（批量，控制成本）
    pref_count = 0
    if embedder is not None and matrix:
        # behaviors 已按 acted_at 倒序，逐用户取前 K 篇去重文档
        user_doc_ids: dict[str, list[uuid.UUID]] = {}
        for b in behaviors:
            doc_ids = user_doc_ids.setdefault(str(b.user_id), [])
            if b.doc_id not in doc_ids and len(doc_ids) < _PREFERENCE_MAX_DOCS:
                doc_ids.append(b.doc_id)

        all_doc_ids = {d for ids in user_doc_ids.values() for d in ids}
        doc_stmt = select(Document).where(
            Document.id.in_(all_doc_ids),
            Document.deleted_at.is_(None),
        )
        doc_stmt = apply_tenant_filter(doc_stmt, Document, tenant_id)
        docs = (await session.execute(doc_stmt)).scalars().all()
        title_map = {str(d.id): d.title for d in docs if d.title}

        users: list[str] = []
        texts: list[str] = []
        for uid, doc_ids in user_doc_ids.items():
            titles = [title_map[str(d)] for d in doc_ids if str(d) in title_map]
            if not titles:
                continue
            users.append(uid)
            texts.append(" ".join(titles))

        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch_users = users[start : start + _EMBED_BATCH_SIZE]
            batch_texts = texts[start : start + _EMBED_BATCH_SIZE]
            try:
                vectors = await embedder.embed(batch_texts)
            except Exception as exc:
                # 单批失败跳过该批，不阻塞其他用户与其他租户
                logger.warning("recommend.rebuild.embed_failed", error=str(exc))
                continue
            for uid, vec in zip(batch_users, vectors):
                redis_client.setex(
                    _model_key(tenant_id, f"prefvec:{uid}"),
                    _MODEL_TTL_SECONDS,
                    json.dumps(vec),
                )
                pref_count += 1

    return {"behavior_rows": len(behaviors), "preference_vectors": pref_count}
