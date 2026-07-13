"""
服务层统一导出 — 业务逻辑编排服务。

遵循单一职责：本文件仅做导出，不包含业务逻辑。
遵循依赖倒置：API 层通过 Service 接口访问业务逻辑，
不直接操作 Repository，实现业务逻辑与数据访问解耦。

使用方式::

    from app.services import FeedbackService, AuditService

    async def feedback_endpoint(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
        service = FeedbackService(db, user)
        feedback = await service.create_feedback("bug", "页面加载缓慢")
"""

from app.services.apikey_service import ApiKeyService
from app.services.audit_service import AuditService
from app.services.feedback_loop_service import FeedbackLoopService
from app.services.feedback_service import FeedbackService
from app.services.gap_detector_service import GapDetectorService
from app.services.quality_service import QualityService
from app.services.search_service import SearchService

__all__ = [
    "FeedbackService",
    "AuditService",
    "SearchService",
    "ApiKeyService",
    "QualityService",
    "GapDetectorService",
    "FeedbackLoopService",
]
