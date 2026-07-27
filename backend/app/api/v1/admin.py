"""
治理后台 API — 单一职责：标签治理与知识库健康度明细的 HTTP 端点。

端点：
    GET /admin/tags    — 标签列表（聚合 knowledge_bases.tags，按引用 KB 数倒序）
    GET /admin/health  — 知识库健康度明细（每 KB 评分 + 低质量文档列表）

数据来源：
    标签：文档智能处理（DocIntelligenceService.auto_tag）提取后合并到所属 KB 的 tags。
    健康度：documents.parse_status（parsed/partial/failed/pending）按 KB 聚合。
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge import Document, KnowledgeBase
from app.models.user import User
from app.schemas.common import ApiResponse
from app.utils.tenant import apply_tenant_filter

router = APIRouter(prefix="/admin", tags=["治理后台"])

#: 解析异常状态 — 计入低质量与扣分
_BAD_PARSE_STATUS = ("failed", "partial")


@router.get("/tags")
async def list_tags(
    request: Request,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """标签列表 — 聚合所有知识库标签，count 为引用该标签的知识库数。

    标签暂无独立分组数据，group 统一返回 "其他"（前端可正常归类展示）。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    tenant_id = getattr(request.state, "tenant_id", None)
    stmt = select(KnowledgeBase.tags).where(KnowledgeBase.deleted_at.is_(None))
    stmt = apply_tenant_filter(stmt, KnowledgeBase, tenant_id)
    rows = (await db.execute(stmt)).scalars().all()

    counter: Counter[str] = Counter()
    for tags in rows:
        if not tags:
            continue
        # 同一 KB 内去重，count 语义为"引用该标签的 KB 数"
        counter.update({str(t).strip() for t in tags if str(t).strip()})

    data = [
        {"id": name, "name": name, "group": "其他", "count": count}
        for name, count in counter.most_common()
    ]
    return ApiResponse(code=0, data=data, message="success")


@router.get("/health")
async def get_admin_health(
    request: Request,
    low_quality_limit: int = Query(20, ge=1, le=100, description="低质量文档返回上限"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """知识库健康度明细。

    评分口径（每 KB）：score = 100 × (1 - 解析异常文档数 / 文档总数)，
    解析异常 = parse_status ∈ (failed, partial)；无文档的 KB 不参与评分。
    低质量文档：parse_status ∈ (failed, partial)，failed 计 20 分、partial 计 60 分。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    tenant_id = getattr(request.state, "tenant_id", None)

    # ---- 每 KB 健康度评分 ----
    bad_count = func.sum(
        case((Document.parse_status.in_(_BAD_PARSE_STATUS), 1), else_=0)
    ).label("bad")
    kb_stmt = (
        select(
            KnowledgeBase.id,
            KnowledgeBase.name,
            func.count(Document.id).label("total"),
            bad_count,
        )
        .join(Document, Document.kb_id == KnowledgeBase.id)
        .where(KnowledgeBase.deleted_at.is_(None))
        .where(Document.deleted_at.is_(None))
        .group_by(KnowledgeBase.id, KnowledgeBase.name)
    )
    kb_stmt = apply_tenant_filter(kb_stmt, KnowledgeBase, tenant_id)
    kb_stmt = apply_tenant_filter(kb_stmt, Document, tenant_id)

    kbs = []
    for row in (await db.execute(kb_stmt)).all():
        total = int(row.total or 0)
        bad = int(row.bad or 0)
        if total == 0:
            continue
        score = round(100 * (1 - bad / total))
        kbs.append({"name": row.name, "score": score})
    kbs.sort(key=lambda x: x["score"])

    # ---- 低质量文档列表 ----
    lq_stmt = (
        select(
            Document.id,
            Document.title,
            Document.parse_status,
            KnowledgeBase.name.label("kb_name"),
        )
        .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
        .where(Document.deleted_at.is_(None))
        .where(Document.parse_status.in_(_BAD_PARSE_STATUS))
        .order_by(Document.updated_at.desc())
        .limit(low_quality_limit)
    )
    lq_stmt = apply_tenant_filter(lq_stmt, Document, tenant_id)

    issue_map = {"failed": "解析失败", "partial": "部分解析失败"}
    score_map = {"failed": 20, "partial": 60}
    low_quality = [
        {
            "id": str(row.id),
            "title": row.title,
            "kb": row.kb_name,
            "issue": issue_map.get(row.parse_status, "需优化"),
            "score": score_map.get(row.parse_status, 50),
        }
        for row in (await db.execute(lq_stmt)).all()
    ]

    return ApiResponse(
        code=0,
        data={"kbs": kbs, "low_quality": low_quality},
        message="success",
    )
