"""
Schema 层统一导出 — 导入并导出所有 Pydantic 请求/响应模型。

遵循单一职责：本文件仅做导出，不包含业务逻辑。
Schema 与 ORM 模型分离：Schema 只负责数据验证与序列化，不持久化数据。
所有响应 Schema 继承 BaseModel 并配置 ``model_config = ConfigDict(from_attributes=True)``，
以支持从 ORM 模型实例直接转换（``Schema.model_validate(orm_obj)``）。
"""

from app.schemas.agent import (
    AgentCreate,
    AgentInfo,
    AgentInvokeRequest,
    AgentListResponse,
    AgentUpdate,
)
from app.schemas.audit import (
    AuditAction,
    AuditActionType,
    AuditFlowResponse,
    AuditPriority,
    AuditStatus,
    ResourceType,
)
from app.schemas.auth import (
    ClearanceLevel,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
    UserRole,
)
from app.schemas.comment import CommentCreate, CommentResponse
from app.schemas.common import ApiResponse, PageResponse, PaginationParams
from app.schemas.conversation import (
    AgentType,
    ChatRequest,
    ConversationCreate,
    ConversationResponse,
    MessageCreate,
    MessageResponse,
    MessageRole,
)
from app.schemas.feedback import (
    FeedbackCreate,
    FeedbackPriority,
    FeedbackResponse,
    FeedbackStatus,
    FeedbackType,
    FeedbackUpdate,
)
from app.schemas.knowledge import (
    Classification,
    DocCreate,
    DocResponse,
    DocStatus,
    DocType,
    DocUpdate,
    DocVersionResponse,
    KbCreate,
    KbResponse,
    KbUpdate,
    KbVisibility,
)
from app.schemas.qa import (
    QaAnswerCreate,
    QaAnswerResponse,
    QaQuestionCreate,
    QaQuestionResponse,
    QaQuestionStatus,
)
from app.schemas.report import (
    CostReport,
    CostReportSeries,
    GroupBy,
    KnowledgeReport,
    MetricType,
    ReportFilter,
    UsageReport,
    UsageReportSeries,
)
from app.schemas.search import (
    ReindexRequest,
    ReindexResponse,
    SearchRequest,
    SearchResult,
    SearchResponse,
    SearchSuggestion,
    SearchType,
)
from app.schemas.settings import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    LLMConfig,
    LLMConfigUpdate,
    SystemConfig,
    SystemConfigUpdate,
    TenantConfig,
    TenantConfigUpdate,
    TenantUsage,
)

__all__ = [
    # common
    "ApiResponse",
    "PaginationParams",
    "PageResponse",
    # auth
    "UserRole",
    "ClearanceLevel",
    "UserCreate",
    "UserLogin",
    "UserResponse",
    "TokenResponse",
    # knowledge
    "KbVisibility",
    "DocType",
    "DocStatus",
    "Classification",
    "KbCreate",
    "KbUpdate",
    "KbResponse",
    "DocCreate",
    "DocUpdate",
    "DocResponse",
    "DocVersionResponse",
    # conversation
    "AgentType",
    "MessageRole",
    "ConversationCreate",
    "ConversationResponse",
    "MessageCreate",
    "MessageResponse",
    "ChatRequest",
    # qa
    "QaQuestionStatus",
    "QaQuestionCreate",
    "QaQuestionResponse",
    "QaAnswerCreate",
    "QaAnswerResponse",
    # comment
    "CommentCreate",
    "CommentResponse",
    # feedback
    "FeedbackType",
    "FeedbackStatus",
    "FeedbackPriority",
    "FeedbackCreate",
    "FeedbackUpdate",
    "FeedbackResponse",
    # audit
    "ResourceType",
    "AuditStatus",
    "AuditPriority",
    "AuditActionType",
    "AuditAction",
    "AuditFlowResponse",
    # search
    "SearchType",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
    "SearchSuggestion",
    "ReindexRequest",
    "ReindexResponse",
    # agent
    "AgentInfo",
    "AgentCreate",
    "AgentUpdate",
    "AgentInvokeRequest",
    "AgentListResponse",
    # settings
    "LLMConfig",
    "LLMConfigUpdate",
    "SystemConfig",
    "SystemConfigUpdate",
    "ApiKeyCreate",
    "ApiKeyResponse",
    "ApiKeyCreateResponse",
    "TenantConfig",
    "TenantConfigUpdate",
    "TenantUsage",
    # report
    "MetricType",
    "GroupBy",
    "ReportFilter",
    "UsageReport",
    "UsageReportSeries",
    "KnowledgeReport",
    "CostReport",
    "CostReportSeries",
]
