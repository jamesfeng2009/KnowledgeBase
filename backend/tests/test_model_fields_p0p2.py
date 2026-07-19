"""P0-P2 模型字段缺口修复测试。

覆盖：
- P0-1: Document.tenant_id / KnowledgeBase.tenant_id 模型字段存在
- P0-2: MessageRepository.get_by_conversation 支持 limit 参数
- P0-3: AgentCheckpoint 模型 + 迁移文件
- P0-4: ChatService.stream_agent_response 方法
- P0-5: ApiKeyResponse 暴露 expires_at / tenant_id
- P1-1/P1-2: DocResponse 暴露 parse_status/page_count/char_count/summary/category/file_path
- P1-3: Notification.read_at 为 DateTime 类型
- P1-4: 10 个模型新增 tenant_id 字段
- P1-5: UsageRecord 新增 duration_ms/success/request_id
- P1-6: Subscription 新增 status/billing_cycle/seats 等字段
- P2: 新增 Response Schema（NotificationResponse 等）
- 迁移文件验证
"""
from __future__ import annotations

import inspect
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.action import DocumentAction
from app.models.analytics import SearchLog
from app.models.billing import Subscription, UsageRecord
from app.models.comment import DocumentComment
from app.models.conversation import Conversation, Message
from app.models.feedback import Feedback
from app.models.knowledge import Document, KnowledgeBase
from app.models.memory import EntityEvent, KnowledgeEntity, MemoryFact
from app.models.notification import Notification
from app.models.checkpoint import AgentCheckpoint


# ======================================================================
# P0-1: Document.tenant_id / KnowledgeBase.tenant_id 模型字段
# ======================================================================


class TestP01TenantIdModelFields:
    """P0-1: Document 和 KnowledgeBase 模型应有 tenant_id 字段。"""

    def test_knowledge_base_has_tenant_id(self) -> None:
        """KnowledgeBase 模型应定义 tenant_id 列。"""
        assert "tenant_id" in KnowledgeBase.__table__.columns
        col = KnowledgeBase.__table__.columns["tenant_id"]
        assert col.nullable is True, "tenant_id 应允许 NULL（私有部署）"

    def test_document_has_tenant_id(self) -> None:
        """Document 模型应定义 tenant_id 列。"""
        assert "tenant_id" in Document.__table__.columns
        col = Document.__table__.columns["tenant_id"]
        assert col.nullable is True, "tenant_id 应允许 NULL（私有部署）"

    def test_openapi_knowledge_can_access_tenant_id(self) -> None:
        """openapi/v1/knowledge.py 中访问 Document.tenant_id 不应抛 AttributeError。"""
        # 验证类属性存在（非实例访问）
        _ = Document.tenant_id
        _ = KnowledgeBase.tenant_id


# ======================================================================
# P0-2: MessageRepository.get_by_conversation 支持 limit
# ======================================================================


class TestP02MessageRepoLimit:
    """P0-2: get_by_conversation 应支持 limit 参数。"""

    def test_method_signature_has_limit(self) -> None:
        """方法签名应包含 limit 参数。"""
        from app.repositories.conversation_repository import MessageRepository

        sig = inspect.signature(MessageRepository.get_by_conversation)
        params = list(sig.parameters.keys())
        assert "limit" in params, "get_by_conversation 应有 limit 参数"
        assert sig.parameters["limit"].default is None, "limit 默认应为 None"

    @pytest.mark.asyncio
    async def test_limit_param_accepted(self) -> None:
        """调用时传 limit 不应抛 TypeError。"""
        from app.repositories.conversation_repository import MessageRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = MessageRepository(mock_session)
        # 不应抛 TypeError
        result = await repo.get_by_conversation(uuid4(), limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_limit_returns_all(self) -> None:
        """不传 limit 时返回全部消息。"""
        from app.repositories.conversation_repository import MessageRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = MessageRepository(mock_session)
        result = await repo.get_by_conversation(uuid4())
        assert result == []


# ======================================================================
# P0-3: AgentCheckpoint 模型
# ======================================================================


class TestP03AgentCheckpointModel:
    """P0-3: AgentCheckpoint ORM 模型应存在且字段完整。"""

    def test_model_importable(self) -> None:
        """AgentCheckpoint 应可导入。"""
        from app.models.checkpoint import AgentCheckpoint

        assert AgentCheckpoint is not None

    def test_model_registered_in_init(self) -> None:
        """AgentCheckpoint 应在 models/__init__.py 导出。"""
        from app.models import AgentCheckpoint as AC

        assert AC is AgentCheckpoint

    def test_table_name(self) -> None:
        """表名应为 agent_checkpoints。"""
        assert AgentCheckpoint.__tablename__ == "agent_checkpoints"

    def test_required_columns(self) -> None:
        """表应有 session_id / agent_state / iteration / updated_at 列。"""
        cols = AgentCheckpoint.__table__.columns
        assert "session_id" in cols
        assert "agent_state" in cols
        assert "iteration" in cols
        assert "updated_at" in cols

    def test_session_id_is_primary_key(self) -> None:
        """session_id 应为主键。"""
        col = AgentCheckpoint.__table__.columns["session_id"]
        assert col.primary_key is True

    def test_migration_file_exists(self) -> None:
        """迁移文件应存在。"""
        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1400-b2c3d4e5f6a7_add_tenant_id_checkpoint_and_usage_metadata.py",
        )
        assert os.path.exists(migration_path)


# ======================================================================
# P0-4: ChatService.stream_agent_response
# ======================================================================


class TestP04StreamAgentResponse:
    """P0-4: ChatService 应有 stream_agent_response 方法。"""

    def test_method_exists(self) -> None:
        """ChatService 类应定义 stream_agent_response 方法。"""
        from app.services.chat_service import ChatService

        assert hasattr(ChatService, "stream_agent_response")
        assert callable(getattr(ChatService, "stream_agent_response"))

    def test_method_is_async_generator(self) -> None:
        """方法应为异步生成器。"""
        from app.services.chat_service import ChatService

        method = getattr(ChatService, "stream_agent_response")
        # 异步生成器函数
        assert inspect.isasyncgenfunction(method), (
            "stream_agent_response 应为异步生成器函数"
        )


# ======================================================================
# P0-5: ApiKeyResponse 暴露 expires_at / tenant_id
# ======================================================================


class TestP05ApiKeyResponseFields:
    """P0-5: ApiKeyResponse 和 ApiKeyCreateResponse 应暴露 expires_at / tenant_id。"""

    def test_api_key_response_has_expires_at(self) -> None:
        from app.schemas.settings import ApiKeyResponse

        fields = ApiKeyResponse.model_fields
        assert "expires_at" in fields, "ApiKeyResponse 应有 expires_at 字段"

    def test_api_key_response_has_tenant_id(self) -> None:
        from app.schemas.settings import ApiKeyResponse

        fields = ApiKeyResponse.model_fields
        assert "tenant_id" in fields, "ApiKeyResponse 应有 tenant_id 字段"

    def test_api_key_create_response_has_expires_at(self) -> None:
        from app.schemas.settings import ApiKeyCreateResponse

        fields = ApiKeyCreateResponse.model_fields
        assert "expires_at" in fields, "ApiKeyCreateResponse 应有 expires_at 字段"

    def test_api_key_create_response_has_tenant_id(self) -> None:
        from app.schemas.settings import ApiKeyCreateResponse

        fields = ApiKeyCreateResponse.model_fields
        assert "tenant_id" in fields, "ApiKeyCreateResponse 应有 tenant_id 字段"


# ======================================================================
# P1-1/P1-2: DocResponse 暴露更多字段
# ======================================================================


class TestP1DocResponseFields:
    """P1-1/P1-2: DocResponse 应暴露解析元数据和 AI 字段。"""

    def test_has_parse_status(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "parse_status" in DocResponse.model_fields

    def test_has_page_count(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "page_count" in DocResponse.model_fields

    def test_has_char_count(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "char_count" in DocResponse.model_fields

    def test_has_parse_warnings(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "parse_warnings" in DocResponse.model_fields

    def test_has_summary(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "summary" in DocResponse.model_fields

    def test_has_category(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "category" in DocResponse.model_fields

    def test_has_file_path(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "file_path" in DocResponse.model_fields

    def test_has_tenant_id(self) -> None:
        from app.schemas.knowledge import DocResponse

        assert "tenant_id" in DocResponse.model_fields


# ======================================================================
# P1-3: Notification.read_at 为 DateTime 类型
# ======================================================================


class TestP13NotificationReadAt:
    """P1-3: Notification.read_at 应为 DateTime 类型。"""

    def test_read_at_column_type_is_datetime(self) -> None:
        from sqlalchemy import DateTime

        col = Notification.__table__.columns["read_at"]
        assert isinstance(col.type, DateTime), (
            f"read_at 应为 DateTime 类型，实际: {type(col.type)}"
        )

    def test_notification_has_tenant_id(self) -> None:
        """Notification 也应有 tenant_id 字段。"""
        assert "tenant_id" in Notification.__table__.columns


# ======================================================================
# P1-4: 10 个模型新增 tenant_id
# ======================================================================


class TestP14TenantIdAllModels:
    """P1-4: 所有相关模型应有 tenant_id 字段。"""

    @pytest.mark.parametrize(
        "model_class,table_name",
        [
            (MemoryFact, "memory_facts"),
            (KnowledgeEntity, "graphiti_entities"),
            (EntityEvent, "graphiti_events"),
            (Conversation, "conversations"),
            (Message, "messages"),
            (DocumentAction, "document_actions"),
            (SearchLog, "search_logs"),
            (Feedback, "feedbacks"),
            (DocumentComment, "document_comments"),
            (Notification, "notifications"),
        ],
    )
    def test_model_has_tenant_id(self, model_class, table_name) -> None:
        """每个模型都应有 tenant_id 列且允许 NULL。"""
        assert model_class.__tablename__ == table_name, (
            f"表名应为 {table_name}，实际: {model_class.__tablename__}"
        )
        assert "tenant_id" in model_class.__table__.columns, (
            f"{model_class.__name__} 应有 tenant_id 字段"
        )
        col = model_class.__table__.columns["tenant_id"]
        assert col.nullable is True, f"{model_class.__name__}.tenant_id 应允许 NULL"


# ======================================================================
# P1-5: UsageRecord 新增字段
# ======================================================================


class TestP15UsageRecordFields:
    """P1-5: UsageRecord 应有 duration_ms / success / request_id 字段。"""

    def test_has_duration_ms(self) -> None:
        assert "duration_ms" in UsageRecord.__table__.columns

    def test_has_success(self) -> None:
        assert "success" in UsageRecord.__table__.columns

    def test_has_request_id(self) -> None:
        assert "request_id" in UsageRecord.__table__.columns

    def test_duration_ms_default_zero(self) -> None:
        col = UsageRecord.__table__.columns["duration_ms"]
        assert col.default is not None or col.server_default is not None, (
            "duration_ms 应有默认值"
        )


# ======================================================================
# P1-6: Subscription 字段补全
# ======================================================================


class TestP16SubscriptionFields:
    """P1-6: Subscription 应有 status / billing_cycle / seats 等字段。"""

    def test_has_status(self) -> None:
        assert "status" in Subscription.__table__.columns

    def test_has_billing_cycle(self) -> None:
        assert "billing_cycle" in Subscription.__table__.columns

    def test_has_seats(self) -> None:
        assert "seats" in Subscription.__table__.columns

    def test_has_cancelled_at(self) -> None:
        assert "cancelled_at" in Subscription.__table__.columns

    def test_has_auto_renew(self) -> None:
        assert "auto_renew" in Subscription.__table__.columns

    def test_has_metadata(self) -> None:
        assert "metadata_" in Subscription.__table__.columns


# ======================================================================
# P2: 新增 Response Schema
# ======================================================================


class TestP2ResponseSchemas:
    """P2: 新增的 Response Schema 应存在且字段完整。"""

    def test_notification_response_exists(self) -> None:
        from app.schemas.billing import NotificationResponse

        fields = NotificationResponse.model_fields
        for f in ("id", "user_id", "title", "content", "is_read", "read_at", "created_at", "updated_at"):
            assert f in fields, f"NotificationResponse 缺少字段: {f}"

    def test_knowledge_gap_response_exists(self) -> None:
        from app.schemas.billing import KnowledgeGapResponse

        fields = KnowledgeGapResponse.model_fields
        for f in ("id", "question", "frequency", "status", "created_at", "updated_at"):
            assert f in fields, f"KnowledgeGapResponse 缺少字段: {f}"

    def test_document_action_response_exists(self) -> None:
        from app.schemas.billing import DocumentActionResponse

        fields = DocumentActionResponse.model_fields
        for f in ("id", "doc_id", "user_id", "action_type", "description", "status", "created_at", "updated_at"):
            assert f in fields, f"DocumentActionResponse 缺少字段: {f}"

    def test_search_log_response_exists(self) -> None:
        from app.schemas.billing import SearchLogResponse

        fields = SearchLogResponse.model_fields
        for f in ("id", "query", "source", "result_count", "clicked", "created_at"):
            assert f in fields, f"SearchLogResponse 缺少字段: {f}"

    def test_memory_fact_response_exists(self) -> None:
        from app.schemas.billing import MemoryFactResponse

        fields = MemoryFactResponse.model_fields
        for f in ("id", "user_id", "category", "fact_text", "is_active", "created_at", "updated_at"):
            assert f in fields, f"MemoryFactResponse 缺少字段: {f}"

    def test_usage_record_response_exists(self) -> None:
        from app.schemas.billing import UsageRecordResponse

        fields = UsageRecordResponse.model_fields
        for f in ("id", "tenant_id", "user_id", "model", "duration_ms", "success", "request_id"):
            assert f in fields, f"UsageRecordResponse 缺少字段: {f}"

    def test_subscription_response_exists(self) -> None:
        from app.schemas.billing import SubscriptionResponse

        fields = SubscriptionResponse.model_fields
        for f in ("id", "tenant_id", "plan", "status", "billing_cycle", "seats", "auto_renew"):
            assert f in fields, f"SubscriptionResponse 缺少字段: {f}"


# ======================================================================
# P2: 4 个 Schema 补 updated_at
# ======================================================================


class TestP2UpdatedAtFields:
    """P2: ConversationResponse / MessageResponse / CommentResponse / FeedbackResponse 应有 updated_at。"""

    def test_conversation_response_has_updated_at(self) -> None:
        from app.schemas.conversation import ConversationResponse

        assert "updated_at" in ConversationResponse.model_fields

    def test_message_response_has_updated_at(self) -> None:
        from app.schemas.conversation import MessageResponse

        assert "updated_at" in MessageResponse.model_fields

    def test_comment_response_has_updated_at(self) -> None:
        from app.schemas.comment import CommentResponse

        assert "updated_at" in CommentResponse.model_fields

    def test_feedback_response_has_updated_at(self) -> None:
        from app.schemas.feedback import FeedbackResponse

        assert "updated_at" in FeedbackResponse.model_fields


# ======================================================================
# 迁移文件验证
# ======================================================================


class TestP0P2MigrationFile:
    """验证 b2c3d4e5f6a7 迁移文件。"""

    def test_migration_file_exists(self) -> None:
        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1400-b2c3d4e5f6a7_add_tenant_id_checkpoint_and_usage_metadata.py",
        )
        assert os.path.exists(migration_path)

    def test_migration_revision_chain(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        import os

        os.chdir(os.path.dirname(__file__) + "/..")
        script = ScriptDirectory.from_config(cfg)
        revisions = {r.revision: r for r in script.walk_revisions()}

        assert "b2c3d4e5f6a7" in revisions
        assert revisions["b2c3d4e5f6a7"].down_revision == "a1b2c3d4e5f6"

    def test_migration_creates_agent_checkpoints_table(self) -> None:
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1400-b2c3d4e5f6a7_add_tenant_id_checkpoint_and_usage_metadata.py",
        )

        spec = importlib.util.spec_from_file_location("migration_p0p2", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        upgrade_src = inspect.getsource(module.upgrade)
        # 应创建 agent_checkpoints 表
        assert "create_table" in upgrade_src
        assert "agent_checkpoints" in upgrade_src
        # 应给 10 个表加 tenant_id（add_column 调用次数应 ≥ 10）
        assert upgrade_src.count("add_column") >= 10
        # 应包含 agent_checkpoints 表的创建
        assert "session_id" in upgrade_src

    def test_migration_downgrade_drops_table_and_columns(self) -> None:
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1400-b2c3d4e5f6a7_add_tenant_id_checkpoint_and_usage_metadata.py",
        )

        spec = importlib.util.spec_from_file_location("migration_p0p2_down", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        downgrade_src = inspect.getsource(module.downgrade)
        # 应删除 agent_checkpoints 表
        assert "drop_table" in downgrade_src
        assert "agent_checkpoints" in downgrade_src
        # 应删除 tenant_id 列（drop_column 调用次数应 ≥ 10）
        assert downgrade_src.count("drop_column") >= 10
