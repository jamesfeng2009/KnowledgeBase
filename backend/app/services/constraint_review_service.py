"""约束规则人审服务 — P2 写入打标闭环（GAP-3 · 设计 §5.3）。

pending_review 规则照常注入（安全优先，宁可先生效），但审计记录打标；
人审接口仿 HighRiskAuditService.review_audit 模式：
    approve → active（确认条款有效）
    reject  → retired（误判，软退休禁 DELETE）
reviewed_at 与 superseded_by 区分「人审退休」与「版本链退休」，
误判率统计反哺 CONSTRAINT_AUTO_CONFIDENCE 阈值（对标
get_misjudgment_stats 的反哺模式）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select

from app.models.constraint import ConstraintRule
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 人审动作 → 目标状态（白名单）
_REVIEW_ACTIONS: dict[str, str] = {
    "approve": "active",
    "reject": "retired",
}


class ConstraintReviewService:
    """约束规则人审 — 队列查询 / 复核流转 / 误判率统计。"""

    def __init__(self, session_factory: Callable | None = None) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> Callable:
        if self._session_factory is None:
            from app.database import async_session_factory

            self._session_factory = async_session_factory
        return self._session_factory

    async def list_rules(
        self,
        *,
        status_filter: str | None = "pending_review",
        kb_id: UUID | None = None,
        severity: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """人审队列查询 — 默认 pending_review，按创建时间倒序分页。"""
        try:
            async with self.session_factory() as session:
                stmt = select(ConstraintRule)
                if status_filter:
                    stmt = stmt.where(ConstraintRule.status == status_filter)
                if kb_id:
                    stmt = stmt.where(ConstraintRule.kb_id == kb_id)
                if severity:
                    stmt = stmt.where(ConstraintRule.severity == severity)
                total = (
                    await session.execute(
                        select(func.count()).select_from(stmt.subquery())
                    )
                ).scalar_one()
                rows = (
                    (
                        await session.execute(
                            stmt.order_by(
                                ConstraintRule.classifier_confidence.desc(),
                                ConstraintRule.created_at.desc(),
                            ).limit(limit).offset(offset)
                        )
                    )
                    .scalars()
                    .all()
                )
                return {"total": total, "items": [self._to_dict(r) for r in rows]}
        except Exception as exc:
            log.warning("constraint_review.list_failed", error=str(exc))
            return {"total": 0, "items": [], "error": str(exc)}

    async def review_rule(
        self,
        rule_id: UUID,
        *,
        action: str,
        reviewer_id: UUID,
        comment: str | None = None,
    ) -> bool:
        """复核流转 — approve → active / reject → retired。

        Returns:
            是否更新成功（False = 规则不存在或动作非法）。
        """
        target_status = _REVIEW_ACTIONS.get(action)
        if target_status is None:
            raise ValueError(f"无效人审动作: {action}")
        try:
            async with self.session_factory() as session:
                rule = await session.get(ConstraintRule, rule_id)
                if rule is None:
                    return False
                rule.status = target_status
                rule.reviewed_by = reviewer_id
                rule.reviewed_at = datetime.now(timezone.utc)
                rule.review_comment = comment
                await session.commit()
            log.info(
                "constraint_review.reviewed",
                rule_id=str(rule_id),
                action=action,
            )
            return True
        except Exception as exc:
            log.warning("constraint_review.review_failed", error=str(exc))
            return False

    async def get_review_stats(self) -> dict[str, Any]:
        """误判率统计 — 反哺 CONSTRAINT_AUTO_CONFIDENCE 阈值。

        误判定义：自动生效（置信度 ≥ 阈值）后被人工 reject 的规则。
        人审退休（reviewed_at 非空）与版本链退休（superseded_by 非空）分开计。
        """
        from app.config import get_settings

        settings = get_settings()
        try:
            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(
                            ConstraintRule.status,
                            func.count(ConstraintRule.id),
                        ).group_by(ConstraintRule.status)
                    )
                ).all()
                counts = {status_value: cnt for status_value, cnt in rows}

                reviewed = (
                    await session.execute(
                        select(func.count(ConstraintRule.id)).where(
                            ConstraintRule.reviewed_at.isnot(None)
                        )
                    )
                ).scalar_one()
                # 误判：人审 reject（retired + reviewed_at 非空）
                misjudged = (
                    await session.execute(
                        select(func.count(ConstraintRule.id)).where(
                            ConstraintRule.status == "retired",
                            ConstraintRule.reviewed_at.isnot(None),
                        )
                    )
                ).scalar_one()
                # 高置信自动生效仍被 reject — 阈值偏低的直接证据
                auto_conf = settings.CONSTRAINT_AUTO_CONFIDENCE
                high_conf_rejected = (
                    await session.execute(
                        select(func.count(ConstraintRule.id)).where(
                            ConstraintRule.status == "retired",
                            ConstraintRule.reviewed_at.isnot(None),
                            ConstraintRule.classifier_confidence >= auto_conf,
                        )
                    )
                ).scalar_one()
                # 版本链退休（reindex 正常流转，非误判）
                version_retired = (
                    await session.execute(
                        select(func.count(ConstraintRule.id)).where(
                            ConstraintRule.status == "retired",
                            ConstraintRule.superseded_by.isnot(None),
                        )
                    )
                ).scalar_one()

                total = sum(counts.values())
                return {
                    "total_rules": total,
                    "pending_review": counts.get("pending_review", 0),
                    "active": counts.get("active", 0),
                    "retired": counts.get("retired", 0),
                    "reviewed": reviewed,
                    "misjudged": misjudged,
                    "misjudgment_rate": (
                        round(misjudged / reviewed, 4) if reviewed > 0 else None
                    ),
                    "auto_high_confidence_rejected": high_conf_rejected,
                    "version_chain_retired": version_retired,
                    "current_thresholds": {
                        "auto_confidence": settings.CONSTRAINT_AUTO_CONFIDENCE,
                        "review_confidence": settings.CONSTRAINT_REVIEW_CONFIDENCE,
                    },
                }
        except Exception as exc:
            log.warning("constraint_review.stats_failed", error=str(exc))
            return {
                "total_rules": 0,
                "pending_review": 0,
                "active": 0,
                "retired": 0,
                "reviewed": 0,
                "misjudged": 0,
                "misjudgment_rate": None,
                "auto_high_confidence_rejected": 0,
                "version_chain_retired": 0,
                "current_thresholds": {},
                "error": str(exc),
            }

    @staticmethod
    def _to_dict(rule: ConstraintRule) -> dict[str, Any]:
        """规则 → 管理台展示 dict。"""
        return {
            "id": str(rule.id),
            "kb_id": str(rule.kb_id),
            "document_id": str(rule.document_id),
            "chunk_id": rule.chunk_id,
            "rule_text": rule.rule_text,
            "severity": rule.severity,
            "status": rule.status,
            "trigger_entities": list(rule.trigger_entities or []),
            "trigger_domains": list(rule.trigger_domains or []),
            "classifier_confidence": rule.classifier_confidence,
            "version": rule.version,
            "superseded_by": (
                str(rule.superseded_by) if rule.superseded_by else None
            ),
            "reviewed_by": str(rule.reviewed_by) if rule.reviewed_by else None,
            "reviewed_at": (
                rule.reviewed_at.isoformat() if rule.reviewed_at else None
            ),
            "review_comment": rule.review_comment,
            "created_at": rule.created_at.isoformat(),
        }


_service: ConstraintReviewService | None = None


def get_constraint_review_service() -> ConstraintReviewService:
    """模块级单例。"""
    global _service
    if _service is None:
        _service = ConstraintReviewService()
    return _service
