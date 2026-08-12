"""知识回流审批服务 — P2 审批工作流。

单一职责：审批记录的 CRUD + 自动检测分流 + 状态流转。

P2 核心流程：
    P0 沉淀 FAQ 资产 → submit_for_review 自动检测 →
        高质量(quality_score >= 阈值 且 无冲突 且 无 PII) → 自动 approve
        否则 → pending（人工审批）→ approve/reject

复用：
    - ApprovalService 模式（pending → approved/rejected + 过期机制）
    - KnowledgeAsset.status 流转（pending_review → active/deprecated）
    - Document.status 流转（pending_review → published / 软删除）
    - PIIScrubber（PII 检测）

状态护栏：
    - 只能从 pending → approved/rejected（不能从 approved 回到 pending）
    - expired 状态不可审批
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document
from app.models.knowledge_approval import KnowledgeApproval
from app.models.knowledge_compounding import KnowledgeAsset
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.pagination import PageResult, PaginationParams, paginate
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)


class KnowledgeApprovalService:
    """知识回流审批服务 — FAQ 沉淀后的审批流转。"""

    def __init__(
        self, db: AsyncSession, tenant_id: UUID | None = None
    ) -> None:
        """初始化审批服务。

        Args:
            db: 异步数据库会话。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 提交审批（P0 沉淀后自动调用）
    # ------------------------------------------------------------------

    async def submit_for_review(
        self,
        asset: KnowledgeAsset,
        doc_id: uuid.UUID | None,
        kb_id: uuid.UUID,
        conflict_count: int = 0,
    ) -> KnowledgeApproval:
        """P0 沉淀后自动提交审批 — 自动检测 + 分流。

        前置检测：
            1. PII 检测（PIIScrubber 检查 title + content）
            2. 自动通过判断（quality_score >= 阈值 且 conflict_count=0 且 !pii_detected）

        分流：
            - 自动通过 → asset.status=active, doc.status=published,
              approval.status=approved, auto_approved=True
            - 人工审批 → asset.status=pending_review, doc.status=pending_review,
              approval.status=pending, expire_at=now+TTL

        Args:
            asset: 已沉淀的知识资产（含 title/content/quality_score）。
            doc_id: 关联的文档 ID（可为 None）。
            kb_id: 目标知识库 ID。
            conflict_count: 冲突检测发现的冲突数量。

        Returns:
            创建的 KnowledgeApproval 实例。
        """
        from app.config import get_settings

        settings = get_settings()

        # ① PII 检测
        pii_detected, pii_risks = self._detect_pii(
            asset.title or "", asset.content or ""
        )

        # ② 自动通过判断
        quality_score = asset.confidence_score or 0.0
        auto_approve = (
            quality_score >= settings.CHAT_FAQ_AUTO_APPROVE_THRESHOLD
            and conflict_count == 0
            and not pii_detected
        )

        # ③ 构建 approval 记录
        risks: list[dict[str, Any]] = []
        if pii_detected:
            risks.append({"type": "pii", "details": pii_risks})
        if conflict_count > 0:
            risks.append({"type": "conflict", "count": conflict_count})
        if quality_score < settings.CHAT_FAQ_AUTO_APPROVE_THRESHOLD:
            risks.append({"type": "low_quality", "score": quality_score})

        now = datetime.now(timezone.utc)
        approval = KnowledgeApproval(
            asset_id=asset.id,
            doc_id=doc_id,
            kb_id=kb_id,
            status="approved" if auto_approve else "pending",
            quality_score=quality_score,
            pii_detected=pii_detected,
            conflict_count=conflict_count,
            auto_detected_risks=risks if risks else None,
            expire_at=None if auto_approve else (
                now + timedelta(seconds=settings.CHAT_FAQ_APPROVAL_TTL_SECONDS)
            ),
            reviewed_at=now if auto_approve else None,
            auto_approved=auto_approve,
            tenant_id=self._tenant_id,
        )
        self.db.add(approval)

        # ④ 状态流转：资产 + 文档
        if auto_approve:
            asset.status = "active"
            await self._update_doc_status(doc_id, "published")
            log.info(
                "knowledge_approval.auto_approved",
                asset_id=str(asset.id),
                quality_score=quality_score,
            )
        else:
            asset.status = "pending_review"
            await self._update_doc_status(doc_id, "pending_review")
            log.info(
                "knowledge_approval.submitted",
                asset_id=str(asset.id),
                pii_detected=pii_detected,
                conflict_count=conflict_count,
                quality_score=quality_score,
            )

        await self.db.flush()
        return approval

    # ------------------------------------------------------------------
    # 人工审批
    # ------------------------------------------------------------------

    async def approve(
        self,
        approval_id: uuid.UUID,
        user: User,
        note: str | None = None,
    ) -> KnowledgeApproval:
        """人工批准 — asset.status=active, doc.status=published。

        Args:
            approval_id: 审批记录 ID。
            user: 审批人。
            note: 审批备注（可选）。

        Returns:
            更新后的 KnowledgeApproval 实例。

        Raises:
            ValueError: 审批不存在或状态非 pending。
        """
        approval = await self._get_approval(approval_id)
        if approval is None:
            raise ValueError(f"审批不存在: {approval_id}")
        if approval.status != "pending":
            raise ValueError(f"审批状态非 pending，当前: {approval.status}")

        approval.status = "approved"
        approval.reviewer_id = user.id
        approval.reviewed_at = datetime.now(timezone.utc)
        approval.review_note = note

        # 资产 → active
        asset = await self._get_asset(approval.asset_id)
        if asset is not None:
            asset.status = "active"

        # 文档 → published
        await self._update_doc_status(approval.doc_id, "published")

        await self.db.flush()
        log.info(
            "knowledge_approval.approved",
            approval_id=str(approval_id),
            reviewer=str(user.id),
        )
        return approval

    async def reject(
        self,
        approval_id: uuid.UUID,
        user: User,
        reason: str,
    ) -> KnowledgeApproval:
        """人工拒绝 — asset.status=deprecated, doc 软删除。

        Args:
            approval_id: 审批记录 ID。
            user: 审批人。
            reason: 拒绝原因。

        Returns:
            更新后的 KnowledgeApproval 实例。

        Raises:
            ValueError: 审批不存在或状态非 pending。
        """
        approval = await self._get_approval(approval_id)
        if approval is None:
            raise ValueError(f"审批不存在: {approval_id}")
        if approval.status != "pending":
            raise ValueError(f"审批状态非 pending，当前: {approval.status}")

        approval.status = "rejected"
        approval.reviewer_id = user.id
        approval.reviewed_at = datetime.now(timezone.utc)
        approval.review_note = reason

        # 资产 → deprecated
        asset = await self._get_asset(approval.asset_id)
        if asset is not None:
            asset.status = "deprecated"

        # 文档软删除（不物理删除，保留审计痕迹）
        await self._soft_delete_doc(approval.doc_id)

        await self.db.flush()
        log.info(
            "knowledge_approval.rejected",
            approval_id=str(approval_id),
            reviewer=str(user.id),
            reason=reason[:100],
        )
        return approval

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_pending(
        self,
        page: int = 1,
        size: int = 20,
    ) -> PageResult:
        """分页查询待审批列表。"""
        stmt = select(KnowledgeApproval).where(
            KnowledgeApproval.status == "pending"
        )
        stmt = apply_tenant_filter(stmt, KnowledgeApproval, self._tenant_id)
        stmt = stmt.order_by(KnowledgeApproval.created_at.desc())
        params = PaginationParams(page=page, size=size)
        return await paginate(stmt, params, self.db)

    async def list_approvals(
        self,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> PageResult:
        """分页查询审批列表（可按状态过滤）。"""
        stmt = select(KnowledgeApproval)
        if status:
            stmt = stmt.where(KnowledgeApproval.status == status)
        stmt = apply_tenant_filter(stmt, KnowledgeApproval, self._tenant_id)
        stmt = stmt.order_by(KnowledgeApproval.created_at.desc())
        params = PaginationParams(page=page, size=size)
        return await paginate(stmt, params, self.db)

    async def get_stats(self) -> dict[str, Any]:
        """审批统计 — 各状态计数 + 自动通过率。"""
        stmt = select(
            KnowledgeApproval.status,
            func.count(KnowledgeApproval.id),
        ).group_by(KnowledgeApproval.status)
        stmt = apply_tenant_filter(stmt, KnowledgeApproval, self._tenant_id)
        result = await self.db.execute(stmt)

        status_counts: dict[str, int] = {}
        total = 0
        for status_val, count in result.all():
            status_counts[status_val] = count
            total += count

        auto_approved = sum(
            1 for s, c in status_counts.items()
            if s == "approved"
        )  # approved 含自动 + 人工，精确计数需 auto_approved 字段

        # 精确统计 auto_approved
        auto_stmt = select(func.count(KnowledgeApproval.id)).where(
            KnowledgeApproval.auto_approved.is_(True)
        )
        auto_stmt = apply_tenant_filter(
            auto_stmt, KnowledgeApproval, self._tenant_id
        )
        auto_count = (await self.db.execute(auto_stmt)).scalar() or 0

        return {
            "total": total,
            "by_status": status_counts,
            "auto_approved": auto_count,
            "auto_approve_rate": (auto_count / total) if total > 0 else 0.0,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _detect_pii(self, *texts: str) -> tuple[bool, list[str]]:
        """检测文本中是否含 PII（手机号/身份证/邮箱/银行卡）。

        复用 PIIScrubber 的正则，通过脱敏前后对比判断是否命中。

        Returns:
            (pii_detected, risks) — 是否命中 + 命中的 PII 类型列表。
        """
        try:
            from app.observability.pii_scrubber import get_default_scrubber

            scrubber = get_default_scrubber()
            risks: list[str] = []
            for text in texts:
                if not text:
                    continue
                scrubbed = scrubber.scrub_text(text)
                if scrubbed != text:
                    # 脱敏后有变化 → 命中 PII
                    # 识别命中类型（通过占位符）
                    if "[PHONE]" in scrubbed:
                        risks.append("phone")
                    if "[IDCARD]" in scrubbed:
                        risks.append("idcard")
                    if "[EMAIL]" in scrubbed:
                        risks.append("email")
                    if "[BANKCARD]" in scrubbed:
                        risks.append("bankcard")
            return (len(risks) > 0, risks)
        except Exception as exc:
            log.warning("knowledge_approval.pii_detect_failed", error=str(exc)[:200])
            # PII 检测失败 → 保守起见标记为需人工审批（不自动通过）
            return (True, ["detect_failed"])

    async def _get_approval(
        self, approval_id: uuid.UUID
    ) -> KnowledgeApproval | None:
        """按 ID 加载审批记录。"""
        stmt = select(KnowledgeApproval).where(KnowledgeApproval.id == approval_id)
        stmt = apply_tenant_filter(stmt, KnowledgeApproval, self._tenant_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _get_asset(
        self, asset_id: uuid.UUID
    ) -> KnowledgeAsset | None:
        """按 ID 加载知识资产。"""
        stmt = select(KnowledgeAsset).where(KnowledgeAsset.id == asset_id)
        stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _update_doc_status(
        self,
        doc_id: uuid.UUID | None,
        status: str,
    ) -> None:
        """更新文档状态。"""
        if doc_id is None:
            return
        stmt = select(Document).where(Document.id == doc_id)
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        doc = (await self.db.execute(stmt)).scalar_one_or_none()
        if doc is not None:
            doc.status = status

    async def _soft_delete_doc(
        self, doc_id: uuid.UUID | None
    ) -> None:
        """软删除文档（拒绝审批时，保留审计痕迹）。"""
        if doc_id is None:
            return
        stmt = select(Document).where(Document.id == doc_id)
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        doc = (await self.db.execute(stmt)).scalar_one_or_none()
        if doc is not None:
            doc.deleted_at = datetime.now(timezone.utc)
            doc.status = "rejected"
