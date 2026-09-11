"""P1 可靠性修复测试 — webhook 幂等标记 / 文档重索引。

覆盖三处修复：
    1. clear_event_mark：派发失败清除 webhook 幂等标记（成功 / Redis 异常容错）；
    2. _build_vector_index：写新向量前清理旧向量（顺序保证 / 删除失败不阻塞）；
    3. update_document：内容变更即置 parse_status=pending（补偿网接管的前提）。
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ==================================================================
# 1. clear_event_mark — webhook 幂等标记清除
# ==================================================================

class TestClearEventMark:
    """webhook_idempotency.clear_event_mark。"""

    @pytest.mark.asyncio
    async def test_clears_redis_key(self) -> None:
        """派发失败路径：删除 Redis 标记成功 → 返回 True。"""
        from app.services.webhook_idempotency import clear_event_mark

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)

        mock_settings = MagicMock()
        mock_settings.REDIS_URL = "redis://localhost:6379/0"

        # 函数内 import redis.asyncio as aioredis → 属性访问真实模块，
        # 需 patch redis.asyncio.from_url 才能拦截
        with (
            patch("app.config.get_settings", return_value=mock_settings),
            patch("redis.asyncio.from_url", return_value=mock_redis),
        ):
            result = await clear_event_mark("evt-001")

        assert result is True
        mock_redis.delete.assert_awaited_once_with("ekb:webhook:event:evt-001")

    @pytest.mark.asyncio
    async def test_redis_failure_returns_false(self) -> None:
        """Redis 异常 → 返回 False（不清除，由 patrol_external_docs 兜底），不抛异常。"""
        from app.services.webhook_idempotency import clear_event_mark

        mock_settings = MagicMock()
        mock_settings.REDIS_URL = "redis://localhost:6379/0"

        with (
            patch("app.config.get_settings", return_value=mock_settings),
            patch(
                "redis.asyncio.from_url",
                MagicMock(side_effect=Exception("redis down")),
            ),
        ):
            result = await clear_event_mark("evt-002")

        assert result is False

    @pytest.mark.asyncio
    async def test_empty_event_id_is_noop(self) -> None:
        """空 event_id → 直接返回 True（无标记可清）。"""
        from app.services.webhook_idempotency import clear_event_mark

        assert await clear_event_mark("") is True


# ==================================================================
# 2. _build_vector_index — 写新向量前清理旧向量
# ==================================================================

class TestBuildVectorIndexStaleDelete:
    """document_tasks._build_vector_index P1 修复：任务内写前清理。"""

    @pytest.mark.asyncio
    async def test_deletes_old_vectors_before_upsert(self) -> None:
        """写新向量前先 delete 旧向量（更新重解析时新 chunk_id 与旧不一致）。"""
        from tasks.document_tasks import _build_vector_index

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_store.upsert = AsyncMock(return_value=3)

        mock_chunk = MagicMock()
        mock_embedding = [0.1] * 8

        with patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            count = await _build_vector_index("doc-001", [mock_chunk], [mock_embedding])

        assert count == 3
        mock_store.delete.assert_awaited_once_with("doc-001")
        mock_store.upsert.assert_awaited_once()
        # 顺序断言：delete 必须先于 upsert（mock 管理器按调用顺序记录）
        names = [c[0] for c in mock_store.method_calls]
        assert names.index("delete") < names.index("upsert")

    @pytest.mark.asyncio
    async def test_delete_failure_does_not_block_upsert(self) -> None:
        """旧向量删除失败仅告警，不阻塞新向量写入。"""
        from tasks.document_tasks import _build_vector_index

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock(side_effect=Exception("opensearch down"))
        mock_store.upsert = AsyncMock(return_value=2)

        mock_chunk = MagicMock()

        with patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            count = await _build_vector_index("doc-002", [mock_chunk], [[0.2] * 8])

        assert count == 2
        mock_store.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_embeddings_skips_all(self) -> None:
        """空 embeddings → 直接返回 0，不触碰向量存储。"""
        from tasks.document_tasks import _build_vector_index

        count = await _build_vector_index("doc-003", [], [])

        assert count == 0


# ==================================================================
# 3. update_document — 内容变更即置 parse_status=pending
# ==================================================================

class TestUpdateDocumentMarksPending:
    """knowledge_service.update_document P1 修复：同事务置 pending。"""

    @pytest.mark.asyncio
    async def test_content_update_sets_parse_status_pending(self) -> None:
        """内容变更 → update 调用携带 parse_status=pending（补偿网可接管）。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        service = KnowledgeService(mock_db, mock_user)

        mock_doc = MagicMock()
        mock_doc.kb_id = uuid.uuid4()
        mock_doc.classification = "internal"
        service.doc_repo.get_by_id = AsyncMock(return_value=mock_doc)
        service.permission.check_write = AsyncMock(return_value=True)
        service.permission.allowed_classifications = MagicMock(return_value=["internal"])

        updated = MagicMock()
        updated.kb_id = mock_doc.kb_id
        service.doc_repo.update = AsyncMock(return_value=updated)
        service._invalidate_cache_for_doc = AsyncMock()
        service._trigger_reindex = AsyncMock()

        await service.update_document(mock_doc.kb_id, content_text="updated content")

        service.doc_repo.update.assert_awaited_once()
        call_kwargs = service.doc_repo.update.await_args.kwargs
        assert call_kwargs.get("parse_status") == "pending"
        assert call_kwargs.get("content_text") == "updated content"
        # 重索引仍被触发
        service._trigger_reindex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_without_content_does_not_touch_parse_status(self) -> None:
        """无内容字段变更（仅元数据）→ 不写 parse_status，不触发重索引。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        service = KnowledgeService(mock_db, mock_user)

        mock_doc = MagicMock()
        mock_doc.kb_id = uuid.uuid4()
        mock_doc.classification = "internal"
        service.doc_repo.get_by_id = AsyncMock(return_value=mock_doc)
        service.permission.check_write = AsyncMock(return_value=True)
        service.permission.allowed_classifications = MagicMock(return_value=["internal"])

        service.doc_repo.update = AsyncMock(return_value=mock_doc)
        service._invalidate_cache_for_doc = AsyncMock()
        service._trigger_reindex = AsyncMock()

        # 不传任何内容字段 — update_document 直接返回原 doc
        result = await service.update_document(mock_doc.kb_id)

        assert result is mock_doc
        service.doc_repo.update.assert_not_awaited()
        service._trigger_reindex.assert_not_awaited()
