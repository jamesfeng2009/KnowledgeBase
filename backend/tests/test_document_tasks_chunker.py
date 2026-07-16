"""document_tasks.py 分块策略接入测试 — 验证 SemanticChunker 四级分块策略接入。

覆盖：
- _chunk_document() 函数：正确调用 SemanticChunker 并返回 Chunk 对象
- _chunk_text() 向后兼容：Deprecated 函数仍可调用
- _build_indexes / _build_opensearch_index / _build_milvus_index 接收 Chunk 元数据
- _process_document_async 集成：完整流程使用 SemanticChunker
- 回归测试：确保流程不因重构而中断
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

# Mock celery_app 模块（避免实际 Celery 实例化）
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

# Mock opensearchpy（测试环境未安装）
if "opensearchpy" not in sys.modules:
    sys.modules["opensearchpy"] = MagicMock()

# Mock pymilvus（测试环境未安装）
if "pymilvus" not in sys.modules:
    sys.modules["pymilvus"] = MagicMock()

import uuid as _uuid

from app.rag.chunker import Chunk, SemanticChunker
from tasks.document_tasks import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    _build_indexes,
    _build_milvus_index,
    _build_opensearch_index,
    _chunk_document,
    _chunk_text,
)

_TEST_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_TEST_UUID_NOT_FOUND = "00000000-0000-0000-0000-000000000000"


# ======================================================================
# _chunk_document 测试
# ======================================================================


class TestChunkDocument:
    """_chunk_document() 函数测试 — SemanticChunker 四级分块接入。"""

    def test_empty_text_returns_empty_list(self) -> None:
        """空文本返回空列表。"""
        result = _chunk_document("", "md")
        assert result == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        """纯空白文本返回空列表。"""
        result = _chunk_document("   \n\n  \t  ", "md")
        assert result == []

    def test_returns_chunk_objects(self) -> None:
        """返回的是 Chunk 对象列表而非字符串。"""
        text = "# 标题\n\n这是一段内容。" * 10
        result = _chunk_document(text, "md")
        assert len(result) > 0
        assert all(isinstance(c, Chunk) for c in result)

    def test_markdown_structural_chunking(self) -> None:
        """Markdown 文档应使用结构化分块（按标题分割）。"""
        text = (
            "# 第一章\n\n第一章的内容。\n\n"
            "# 第二章\n\n第二章的内容。\n\n"
            "# 第三章\n\n第三章的内容。\n\n"
        )
        result = _chunk_document(text, "md")
        assert len(result) >= 2
        # 结构化分块应有 chunk_strategy 标记
        strategies = {c.chunk_strategy for c in result}
        assert "structural" in strategies or "qa" in strategies

    def test_faq_content_type_routing(self) -> None:
        """content_type=faq 时应路由到 Q&A 分块。"""
        text = (
            "## 问题：如何报销？\n\n"
            "## 回答：填写报销单后提交审批。\n\n"
            "## 问题：如何请假？\n\n"
            "## 回答：在 OA 系统提交请假申请。\n\n"
        )
        result = _chunk_document(text, "md", content_type="faq")
        assert len(result) >= 2
        # FAQ 分块应标记为 qa 策略
        assert any(c.chunk_strategy == "qa" for c in result)

    def test_html_structural_chunking(self) -> None:
        """HTML 文档应使用结构化分块。"""
        text = (
            "<h1>标题一</h1><p>内容一</p>"
            "<h1>标题二</h1><p>内容二</p>"
            "<h1>标题三</h1><p>内容三</p>"
        )
        result = _chunk_document(text, "html")
        assert len(result) >= 2

    def test_plain_text_semantic_chunking(self) -> None:
        """纯文本应走语义分块或兜底。"""
        text = "这是一段文本。" * 200
        result = _chunk_document(text, "txt")
        assert len(result) >= 1
        # 应有有效的策略标记
        strategies = {c.chunk_strategy for c in result}
        assert strategies  # 非空

    def test_chunks_have_title_path(self) -> None:
        """结构化分块的 Chunk 应包含 title_path。"""
        text = (
            "# Redis 深度解析\n\n"
            "## 集群\n\n"
            "### 哈希槽分配\n\n"
            "哈希槽是 Redis Cluster 的核心机制。\n\n"
        )
        result = _chunk_document(text, "md")
        # 至少有一个 chunk 有 title_path
        has_title_path = any(c.title_path for c in result)
        assert has_title_path

    def test_chunks_have_content_type(self) -> None:
        """Chunk 对象应包含 content_type 字段。"""
        text = "# 标题\n\n内容" * 5
        result = _chunk_document(text, "md")
        assert all(hasattr(c, "content_type") for c in result)

    def test_default_doc_type_is_md(self) -> None:
        """默认 doc_type 为 md。"""
        text = "# 标题\n\n内容内容内容。"
        result = _chunk_document(text)
        assert len(result) >= 1

    def test_default_content_type_is_auto(self) -> None:
        """默认 content_type 为 auto（走四级兜底链）。"""
        text = "# 标题\n\n内容" * 10
        result = _chunk_document(text, "md")
        # auto 模式应走结构化/语义/兜底之一
        strategies = {c.chunk_strategy for c in result}
        valid_strategies = {"structural", "semantic", "fallback", "qa"}
        assert strategies & valid_strategies  # 至少有一个有效策略


# ======================================================================
# _chunk_text 向后兼容测试
# ======================================================================


class TestChunkTextBackwardCompat:
    """_chunk_text() Deprecated 函数向后兼容测试。"""

    def test_chunk_text_still_works(self) -> None:
        """Deprecated _chunk_text 仍可正常调用。"""
        text = "a" * 1200
        chunks = _chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
        assert len(chunks) >= 2
        assert all(isinstance(c, str) for c in chunks)

    def test_chunk_text_empty(self) -> None:
        """空文本返回空列表。"""
        assert _chunk_text("", 500, 50) == []

    def test_chunk_text_overlap(self) -> None:
        """滑动窗口有 overlap。"""
        text = "abcdefghij" * 100  # 1000 chars
        chunks = _chunk_text(text, 500, 50)
        # 第一个 chunk 是 0:500，第二个从 450 开始
        assert len(chunks) >= 2

    def test_chunk_size_constant_preserved(self) -> None:
        """CHUNK_SIZE 常量保留。"""
        assert CHUNK_SIZE == 500

    def test_chunk_overlap_constant_preserved(self) -> None:
        """CHUNK_OVERLAP 常量保留。"""
        assert CHUNK_OVERLAP == 50


# ======================================================================
# _build_indexes 测试 — 接收 Chunk 元数据
# ======================================================================


class TestBuildIndexes:
    """_build_indexes / _build_opensearch_index / _build_milvus_index 测试。"""

    def _make_chunk_objects(self, count: int = 3) -> list[Chunk]:
        """生成测试用 Chunk 对象列表。"""
        return [
            Chunk(
                id=f"chunk-{i}",
                doc_id="doc-001",
                content=f"这是第{i+1}个分块的内容。",
                parent_id=None if i == 0 else "chunk-0",
                start_pos=i * 100,
                end_pos=(i + 1) * 100,
                token_count=50,
                title_path=f"标题 > 子标题{i+1}" if i > 0 else "标题",
                content_type="tutorial",
                chunk_strategy="structural",
            )
            for i in range(count)
        ]

    @pytest.mark.asyncio
    async def test_build_indexes_accepts_chunk_objects(self) -> None:
        """_build_indexes 接收 Chunk 对象列表。"""
        chunk_objects = self._make_chunk_objects()
        chunks_text = [c.content for c in chunk_objects]
        embeddings = [[0.1] * 128 for _ in chunk_objects]

        # Mock 内部函数避免实际连接外部服务
        with patch(
            "tasks.document_tasks._build_opensearch_index",
            new_callable=AsyncMock,
        ) as mock_os, patch(
            "tasks.document_tasks._build_vector_index",
            new_callable=AsyncMock,
            return_value=len(embeddings),
        ) as mock_vec:
            await _build_indexes("doc-001", chunk_objects, chunks_text, embeddings)
            mock_os.assert_called_once_with("doc-001", chunk_objects)
            mock_vec.assert_called_once_with("doc-001", chunk_objects, embeddings)

    @pytest.mark.asyncio
    async def test_build_opensearch_index_with_chunk_metadata(self) -> None:
        """_build_opensearch_index 存储 Chunk 元数据。"""
        chunk_objects = self._make_chunk_objects(2)

        # Mock OpenSearch 客户端
        mock_client = AsyncMock()
        mock_client.indices.exists = AsyncMock(return_value=True)
        mock_client.index = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("opensearchpy.AsyncOpenSearch", return_value=mock_client):
            await _build_opensearch_index("doc-001", chunk_objects)

        # 验证每个 chunk 被索引且包含元数据
        assert mock_client.index.call_count == 2
        for i, call in enumerate(mock_client.index.call_args_list):
            body = call.kwargs.get("body", {})
            assert body["doc_id"] == "doc-001"
            assert body["chunk_id"] == f"chunk-{i}"
            assert "content" in body
            assert "title_path" in body
            assert "content_type" in body
            assert "chunk_strategy" in body
            assert body["content_type"] == "tutorial"
            assert body["chunk_strategy"] == "structural"

    @pytest.mark.asyncio
    async def test_build_opensearch_index_creates_index_with_metadata_fields(self) -> None:
        """OpenSearch 索引创建时应包含元数据字段映射。"""
        chunk_objects = self._make_chunk_objects(1)

        mock_client = AsyncMock()
        mock_client.indices.exists = AsyncMock(return_value=False)
        mock_client.indices.create = AsyncMock()
        mock_client.index = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("opensearchpy.AsyncOpenSearch", return_value=mock_client):
            await _build_opensearch_index("doc-001", chunk_objects)

        # 验证索引创建的 mapping 包含元数据字段
        create_body = mock_client.indices.create.call_args.kwargs.get("body", {})
        properties = create_body.get("mappings", {}).get("properties", {})
        assert "title_path" in properties
        assert "content_type" in properties
        assert "chunk_strategy" in properties
        assert "parent_id" in properties
        assert "token_count" in properties
        assert "chunk_id" in properties

    @pytest.mark.asyncio
    async def test_build_milvus_index_with_chunk_metadata(self) -> None:
        """_build_milvus_index（向后兼容）通过 VectorStoreBase 适配器写入。"""
        chunk_objects = self._make_chunk_objects(2)
        embeddings = [[0.1] * 128, [0.2] * 128]

        # Mock 向量存储适配器
        mock_store = MagicMock()
        mock_store.upsert = AsyncMock(return_value=2)

        with patch("app.rag.vector_store.get_vector_store", return_value=mock_store):
            await _build_milvus_index("doc-001", chunk_objects, embeddings)

        # 验证 upsert 被调用且接收 Chunk 元数据
        mock_store.upsert.assert_called_once_with("doc-001", chunk_objects, embeddings)

    @pytest.mark.asyncio
    async def test_build_opensearch_skipped_when_not_installed(self) -> None:
        """opensearch-py 未安装时优雅降级。"""
        chunk_objects = self._make_chunk_objects(1)

        with patch.dict("sys.modules", {"opensearchpy": None}):
            # 不应抛出异常
            await _build_opensearch_index("doc-001", chunk_objects)

    @pytest.mark.asyncio
    async def test_build_milvus_skipped_when_not_installed(self) -> None:
        """向量存储适配器不可用时优雅降级。"""
        chunk_objects = self._make_chunk_objects(1)
        embeddings = [[0.1] * 128]

        # Mock 适配器 upsert 返回 0（服务不可用）
        mock_store = MagicMock()
        mock_store.upsert = AsyncMock(return_value=0)

        with patch("app.rag.vector_store.get_vector_store", return_value=mock_store):
            # 不应抛出异常
            await _build_milvus_index("doc-001", chunk_objects, embeddings)


# ======================================================================
# _process_document_async 集成测试
# ======================================================================


class TestProcessDocumentAsyncIntegration:
    """_process_document_async 集成测试 — 验证完整流程使用 SemanticChunker。"""

    @pytest.mark.asyncio
    async def test_process_document_uses_semantic_chunker(self) -> None:
        """_process_document_async 应使用 SemanticChunker 而非 _chunk_text。"""
        from tasks.document_tasks import _process_document_async

        # Mock 数据库会话和 Document
        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = "# 标题\n\n这是文档内容。" * 20
        mock_doc.content_html = None
        mock_doc.doc_type = "md"
        mock_doc.status = "draft"
        mock_doc.file_path = None
        mock_doc.classification = "internal"
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        # Mock async_session_factory
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        assert result["chunk_count"] > 0
        # 应包含 chunk_strategies
        assert "chunk_strategies" in result
        assert isinstance(result["chunk_strategies"], list)

    @pytest.mark.asyncio
    async def test_process_document_empty_text(self) -> None:
        """空文档内容应返回 0 个分块但不报错。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = ""
        mock_doc.content_html = None
        mock_doc.doc_type = "md"
        mock_doc.status = "draft"
        mock_doc.file_path = None
        mock_doc.classification = "internal"
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        assert result["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_process_document_doc_not_found(self) -> None:
        """文档不存在时返回 failed。"""
        from tasks.document_tasks import _process_document_async

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ):

            result = await _process_document_async(_TEST_UUID_NOT_FOUND)

        assert result["status"] == "failed"
        assert "error" in result


# ======================================================================
# 端到端分块策略验证
# ======================================================================


class TestChunkStrategyEndToEnd:
    """端到端验证不同文档类型走不同分块策略。"""

    def test_markdown_with_headers_uses_structural(self) -> None:
        """有标题的 Markdown 应走结构化分块。"""
        text = (
            "# 部署指南\n\n## 环境准备\n\n安装 Python 3.10+。\n\n"
            "## 配置\n\n编辑 config.yaml。\n\n"
            "## 启动\n\n运行 python main.py。\n\n"
        )
        result = _chunk_document(text, "md")
        strategies = {c.chunk_strategy for c in result}
        assert "structural" in strategies

    def test_faq_uses_qa_strategy(self) -> None:
        """FAQ 文档应走 Q&A 分块。"""
        text = (
            "## 问题：如何重置密码？\n\n"
            "## 回答：点击登录页的「忘记密码」链接。\n\n"
            "## 问题：如何修改头像？\n\n"
            "## 回答：在个人设置中上传新头像。\n\n"
        )
        result = _chunk_document(text, "md", content_type="faq")
        assert any(c.chunk_strategy == "qa" for c in result)

    def test_long_plain_text_uses_semantic_or_fallback(self) -> None:
        """无结构的纯文本应走语义分块或兜底。"""
        text = "这是一段没有任何结构的纯文本内容。" * 100
        result = _chunk_document(text, "txt")
        strategies = {c.chunk_strategy for c in result}
        assert strategies & {"semantic", "fallback"}

    def test_chunks_have_valid_content(self) -> None:
        """所有分块内容非空。"""
        text = "# 标题\n\n内容一\n\n# 标题二\n\n内容二"
        result = _chunk_document(text, "md")
        assert all(c.content.strip() for c in result)

    def test_chunks_have_unique_ids(self) -> None:
        """所有分块 ID 唯一。"""
        text = "# 标题一\n\n内容一\n\n# 标题二\n\n内容二\n\n# 标题三\n\n内容三"
        result = _chunk_document(text, "md")
        ids = [c.id for c in result]
        assert len(ids) == len(set(ids))  # 无重复
