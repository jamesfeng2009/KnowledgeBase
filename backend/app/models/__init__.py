"""
模型层统一导出 — 导入所有 ORM 模型，确保 SQLAlchemy metadata 注册。

遵循单一职责：本文件仅做导出，不包含业务逻辑。
"""

from app.models.action import DocumentAction
from app.models.agent import AgentConfig
from app.models.analytics import SearchLog
from app.models.apikey import ApiKey
from app.models.approval import ToolApproval
from app.models.audit import AuditFlow
from app.models.base import Base
from app.models.billing import Subscription, Tenant, UsageRecord
from app.models.checkpoint import AgentCheckpoint
from app.models.comment import DocumentComment
from app.models.conversation import Conversation, Message
from app.models.feedback import Feedback
from app.models.gap import KnowledgeGap
from app.models.knowledge import Document, DocumentVersion, KnowledgeBase
from app.models.memory import EntityEvent, KnowledgeEntity, MemoryFact
from app.models.notification import Notification
from app.models.qa import QaAnswer, QaQuestion
from app.models.user import Department, KbMember, User
from app.models.user_model_preference import UserModelPreference
from app.models.testing import (
    TestExecution,
    TestPlan,
    TestProject,
    TestRequirement,
    TestReview,
    TestCase,
)

__all__ = [
    "Base",
    "Department",
    "User",
    "KbMember",
    "KnowledgeBase",
    "Document",
    "DocumentVersion",
    "Conversation",
    "Message",
    "QaQuestion",
    "QaAnswer",
    "DocumentComment",
    "AuditFlow",
    "Feedback",
    "Tenant",
    "Subscription",
    "UsageRecord",
    # 记忆层 ORM
    "MemoryFact",
    "KnowledgeEntity",
    "EntityEvent",
    # 知识缺口 ORM
    "KnowledgeGap",
    # Agent 配置与 API 密钥
    "AgentConfig",
    "ApiKey",
    # 文档智能处理（3.16）
    "DocumentAction",
    # 知识健康度仪表盘（3.17）
    "SearchLog",
    # 知识主动推送（3.15）
    "Notification",
    # LangGraph Agent Loop 状态检查点
    "AgentCheckpoint",
    # P1: 工具审批持久化
    "ToolApproval",
    # P2: 用户模型偏好
    "UserModelPreference",
    # 智能测试平台
    "TestProject",
    "TestRequirement",
    "TestCase",
    "TestReview",
    "TestPlan",
    "TestExecution",
]
