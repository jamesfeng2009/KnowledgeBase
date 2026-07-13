"""
仓储层统一导出 — 导入所有 Repository，供业务层（Service）使用。

遵循单一职责：本文件仅做导出，不包含业务逻辑。

遵循依赖倒置：业务层通过 Repository 接口访问数据，
不直接操作 SQLAlchemy Session，实现数据访问与业务逻辑解耦。

使用方式：

    from app.repositories import UserRepository

    async def my_service(db: AsyncSession):
        user_repo = UserRepository(db)
        user = await user_repo.get_by_email("user@example.com")
"""

from app.repositories.apikey_repository import ApiKeyRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.base import BaseRepository
from app.repositories.billing_repository import TenantRepository, UsageRecordRepository
from app.repositories.comment_repository import DocumentCommentRepository
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.gap_repository import KnowledgeGapRepository
from app.repositories.knowledge_repository import (
    DocumentRepository,
    KnowledgeBaseRepository,
)
from app.repositories.qa_repository import QaAnswerRepository, QaQuestionRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    # 基类
    "BaseRepository",
    # 用户领域
    "UserRepository",
    # 知识库领域
    "KnowledgeBaseRepository",
    "DocumentRepository",
    # 对话领域
    "ConversationRepository",
    "MessageRepository",
    # 问答领域
    "QaQuestionRepository",
    "QaAnswerRepository",
    # 评论领域
    "DocumentCommentRepository",
    # 反馈领域
    "FeedbackRepository",
    # 计费领域
    "TenantRepository",
    "UsageRecordRepository",
    # 审核流程领域
    "AuditRepository",
    # 知识缺口领域
    "KnowledgeGapRepository",
    # API 密钥领域
    "ApiKeyRepository",
    # 报表领域
    "ReportRepository",
]
