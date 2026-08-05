"""
高风险拦截审计服务 — P1-8：block 决策落审计表 + 误判率复查统计。

职责：
    1. record_block_audit：将 block 决策上下文落库（fire-and-forget，异常降级为日志）；
    2. list_audits：按复查状态分页查询审计记录（管理员复查入口）；
    3. review_audit：标记复查结论（confirmed 正确拦截 / misjudged 误判）；
    4. get_misjudgment_stats：统计误判率，反哺三档分级阈值调整。

设计要点：
    - 写路径与主流程解耦：调用方用 asyncio.create_task 触发，失败不影响问答链路；
    - session_factory 可注入（测试隔离），默认复用全局 async_session_factory；
    - 遵循优雅降级：任何 DB 异常仅记录日志，不抛出。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select

from app.models.high_risk import HighRiskAuditRecord
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 答案快照最大长度（防止超长答案撑爆审计行）
_MAX_ANSWER_SNIPPET = 2000

#: 有效复查状态
REVIEW_STATUSES = ("pending", "confirmed", "misjudged")


class HighRiskAuditService:
    """高风险拦截审计服务。

    Args:
        session_factory: 异步会话工厂；None 时延迟复用全局 async_session_factory。
    """

    def __init__(self, session_factory: Callable | None = None) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> Callable:
        if self._session_factory is None:
            from app.database import async_session_factory

            self._session_factory = async_session_factory
        return self._session_factory

    async def record_block_audit(
        self,
        *,
        query: str,
        answer: str,
        session_id: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
        total_count: int,
        unverified_count: int,
        max_risk_level: str,
        items: list[dict[str, Any]],
    ) -> uuid.UUID | None:
        """记录一条 block 决策审计。

        Args:
            query: 用户问题。
            answer: 被阻断的完整答案（自动截断至 2000 字）。
            session_id: 会话 ID。
            user_id: 用户 ID（可选）。
            tenant_id: 租户 ID（可选）。
            total_count: 检测到的高风险信息总数。
            unverified_count: 未核验通过的信息数。
            max_risk_level: 最高风险等级（low/medium/high）。
            items: 核验明细（含 risk_level 与 deviation）。

        Returns:
            审计记录 ID；失败时返回 None（异常降级为日志）。
        """
        try:
            record = HighRiskAuditRecord(
                query=query[:_MAX_ANSWER_SNIPPET],
                answer_snippet=answer[:_MAX_ANSWER_SNIPPET],
                session_id=session_id,
                user_id=uuid.UUID(user_id) if user_id else None,
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
                total_count=total_count,
                unverified_count=unverified_count,
                max_risk_level=max_risk_level,
                items=items,
                review_status="pending",
            )
            async with self.session_factory() as session:
                session.add(record)
                await session.commit()
            log.info(
                "high_risk_audit.recorded",
                record_id=str(record.id),
                session_id=session_id,
                max_risk_level=max_risk_level,
                unverified_count=unverified_count,
            )
            return record.id
        except Exception as exc:
            # 审计失败不阻断主流程
            log.warning("high_risk_audit.record_failed", error=str(exc))
            return None

    async def list_audits(
        self,
        *,
        review_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询审计记录（管理员复查入口）。

        Args:
            review_status: 按复查状态过滤；None 返回全部。
            limit: 每页条数（上限 100）。
            offset: 偏移量。

        Returns:
            {"total": int, "items": [dict]}；失败时返回空结果。
        """
        try:
            async with self.session_factory() as session:
                stmt = select(HighRiskAuditRecord)
                count_stmt = select(func.count(HighRiskAuditRecord.id))
                if review_status:
                    stmt = stmt.where(
                        HighRiskAuditRecord.review_status == review_status
                    )
                    count_stmt = count_stmt.where(
                        HighRiskAuditRecord.review_status == review_status
                    )
                total = (await session.execute(count_stmt)).scalar_one()
                rows = (
                    await session.execute(
                        stmt.order_by(HighRiskAuditRecord.created_at.desc())
                        .limit(min(limit, 100))
                        .offset(offset)
                    )
                ).scalars().all()
                return {
                    "total": total,
                    "items": [self._to_dict(r) for r in rows],
                }
        except Exception as exc:
            log.warning("high_risk_audit.list_failed", error=str(exc))
            return {"total": 0, "items": []}

    async def review_audit(
        self,
        record_id: uuid.UUID,
        *,
        review_status: str,
        reviewer_id: uuid.UUID,
        comment: str | None = None,
    ) -> bool:
        """标记复查结论。

        Args:
            record_id: 审计记录 ID。
            review_status: "confirmed"（正确拦截）或 "misjudged"（误判）。
            reviewer_id: 复查人 ID。
            comment: 复查意见（可选）。

        Returns:
            是否更新成功。
        """
        if review_status not in ("confirmed", "misjudged"):
            raise ValueError(f"无效复查状态: {review_status}")
        try:
            async with self.session_factory() as session:
                record = await session.get(HighRiskAuditRecord, record_id)
                if record is None:
                    return False
                record.review_status = review_status
                record.reviewed_by = reviewer_id
                record.reviewed_at = datetime.now(timezone.utc)
                record.review_comment = comment
                await session.commit()
            log.info(
                "high_risk_audit.reviewed",
                record_id=str(record_id),
                review_status=review_status,
            )
            return True
        except Exception as exc:
            log.warning("high_risk_audit.review_failed", error=str(exc))
            return False

    async def get_misjudgment_stats(self) -> dict[str, Any]:
        """误判率统计 — 反哺三档分级阈值调整。

        Returns:
            总量 / 已复查 / 误判数 / 误判率 / 当前分级阈值。
            误判率 = misjudged / (confirmed + misjudged)，无复查数据时为 None。
        """
        try:
            async with self.session_factory() as session:
                total = (
                    await session.execute(
                        select(func.count(HighRiskAuditRecord.id))
                    )
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(
                            HighRiskAuditRecord.review_status,
                            func.count(HighRiskAuditRecord.id),
                        ).group_by(HighRiskAuditRecord.review_status)
                    )
                ).all()
                counts = {status: cnt for status, cnt in rows}
                confirmed = counts.get("confirmed", 0)
                misjudged = counts.get("misjudged", 0)
                reviewed = confirmed + misjudged
                from app.context.high_risk_detector import HighRiskDetector

                return {
                    "total_blocks": total,
                    "pending": counts.get("pending", 0),
                    "confirmed": confirmed,
                    "misjudged": misjudged,
                    "misjudgment_rate": (
                        round(misjudged / reviewed, 4) if reviewed > 0 else None
                    ),
                    "current_thresholds": {
                        "deviation_low": HighRiskDetector._DEVIATION_LOW,
                        "deviation_medium": HighRiskDetector._DEVIATION_MEDIUM,
                    },
                }
        except Exception as exc:
            log.warning("high_risk_audit.stats_failed", error=str(exc))
            return {
                "total_blocks": 0,
                "pending": 0,
                "confirmed": 0,
                "misjudged": 0,
                "misjudgment_rate": None,
                "current_thresholds": {},
                "error": str(exc),
            }

    @staticmethod
    def _to_dict(record: HighRiskAuditRecord) -> dict[str, Any]:
        return {
            "id": str(record.id),
            "query": record.query[:200],
            "answer_snippet": record.answer_snippet[:500],
            "session_id": record.session_id,
            "user_id": str(record.user_id) if record.user_id else None,
            "tenant_id": str(record.tenant_id) if record.tenant_id else None,
            "total_count": record.total_count,
            "unverified_count": record.unverified_count,
            "max_risk_level": record.max_risk_level,
            "items": record.items,
            "review_status": record.review_status,
            "reviewed_by": str(record.reviewed_by) if record.reviewed_by else None,
            "reviewed_at": (
                record.reviewed_at.isoformat() if record.reviewed_at else None
            ),
            "review_comment": record.review_comment,
            "created_at": (
                record.created_at.isoformat() if record.created_at else None
            ),
        }


#: 全局单例（引擎内 fire-and-forget 复用）
_service: HighRiskAuditService | None = None


def get_high_risk_audit_service() -> HighRiskAuditService:
    """获取全局审计服务单例。"""
    global _service
    if _service is None:
        _service = HighRiskAuditService()
    return _service
