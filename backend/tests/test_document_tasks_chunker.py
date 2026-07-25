"""document_tasks.py 分块策略接入测试 — 验证 SemanticChunker 四级分块策略接入。

覆盖：
- _chunk_document() 函数：正确调用 SemanticChunker 并返回 Chunk 对象
- _chunk_text() 向后兼容：Deprecated 函数仍可调用
- _build_indexes / _build_opensearch_index / _build_milvus_index 接收 Chunk 元数据
- _process_document_async 集成：完整流程使用 SemanticChunker
- 回归测试：确保流程不因重构而中断
"""
from __future__ import annotations

import json
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

from app.rag.chunker import Chunk
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


def _parse_bulk_body(body: str) -> list[dict[str, Any]]:
    """解析 OpenSearch bulk NDJSON 请求体（action 行与 source 行交替）。"""
    return [json.loads(line) for line in body.strip().split("\n") if line.strip()]


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
            mock_os.assert_called_once_with("doc-001", chunk_objects, kb_id=None)
            mock_vec.assert_called_once_with("doc-001", chunk_objects, embeddings, kb_id=None)

    @pytest.mark.asyncio
    async def test_build_opensearch_index_with_chunk_metadata(self) -> None:
        """_build_opensearch_index 存储 Chunk 元数据。"""
        chunk_objects = self._make_chunk_objects(2)

        # Mock OpenSearch 客户端
        mock_client = AsyncMock()
        mock_client.indices.exists = AsyncMock(return_value=True)
        mock_client.bulk = AsyncMock(return_value={"errors": False, "items": []})
        mock_client.close = AsyncMock()

        with patch("opensearchpy.AsyncOpenSearch", return_value=mock_client):
            await _build_opensearch_index("doc-001", chunk_objects)

        # 验证所有 chunk 通过单次 bulk 请求索引且包含元数据
        assert mock_client.bulk.call_count == 1
        lines = _parse_bulk_body(mock_client.bulk.call_args.kwargs.get("body", ""))
        actions, sources = lines[0::2], lines[1::2]
        assert len(actions) == len(sources) == 2
        for i, (action, source) in enumerate(zip(actions, sources, strict=True)):
            assert action["index"]["_id"] == f"chunk-{i}"
            assert source["doc_id"] == "doc-001"
            assert source["chunk_id"] == f"chunk-{i}"
            assert "content" in source
            assert "title_path" in source
            assert "content_type" in source
            assert "chunk_strategy" in source
            assert source["content_type"] == "tutorial"
            assert source["chunk_strategy"] == "structural"

    @pytest.mark.asyncio
    async def test_build_opensearch_index_creates_index_with_metadata_fields(self) -> None:
        """OpenSearch 索引创建时应包含元数据字段映射。"""
        chunk_objects = self._make_chunk_objects(1)

        mock_client = AsyncMock()
        mock_client.indices.exists = AsyncMock(return_value=False)
        mock_client.indices.create = AsyncMock()
        mock_client.bulk = AsyncMock(return_value={"errors": False, "items": []})
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
        mock_store.upsert.assert_called_once_with("doc-001", chunk_objects, embeddings, kb_id=None)

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


# ======================================================================
# 并行编排 + 知识图谱构建（方向一 + 方向二）
# ======================================================================


class TestParallelPipelineAndGraphBuild:
    """并行编排 + 知识图谱构建测试。

    验证：
    - 方向一：分块完成后并行执行"向量化+索引"和"知识图谱构建"两条支线
    - 方向二：知识图谱构建复用 chunk_objects，避免重复分块
    - 降级：knowledge_graph 模块未启用时不执行图谱构建
    """

    def _make_mock_doc(self, classification: str = "internal"):
        """构造 mock Document 对象。"""
        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = "# 标题\n\n这是文档内容。" * 20
        mock_doc.content_html = None
        mock_doc.doc_type = "md"
        mock_doc.status = "draft"
        mock_doc.file_path = None
        mock_doc.classification = classification
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)
        mock_doc.tenant_id = _uuid.UUID(_TEST_UUID)
        return mock_doc

    def _make_mock_session(self, mock_doc):
        """构造 mock 数据库会话。"""
        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)
        return mock_session_cm, mock_repo

    @pytest.mark.asyncio
    async def test_graph_disabled_skips_graph_build(self) -> None:
        """knowledge_graph 模块未启用时，不调用 _build_knowledge_graph。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc()
        mock_session_cm, mock_repo = self._make_mock_session(mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._build_knowledge_graph", new_callable=AsyncMock) as mock_graph, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        mock_index.assert_called_once()
        # knowledge_graph 模块未启用（TenantService 检查失败），不应调用图谱构建
        mock_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_enabled_triggers_graph_build(self) -> None:
        """knowledge_graph 模块启用时，调用 _build_knowledge_graph。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc()
        mock_session_cm, mock_repo = self._make_mock_session(mock_doc)

        # mock TenantService.is_module_enabled 返回 True
        mock_tenant_svc = AsyncMock()
        mock_tenant_svc.is_module_enabled = AsyncMock(return_value=True)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._build_knowledge_graph", new_callable=AsyncMock) as mock_graph, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"), \
             patch(
                 "app.services.tenant_service.TenantService",
                 return_value=mock_tenant_svc,
             ):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        mock_index.assert_called_once()
        # knowledge_graph 模块启用，应调用图谱构建
        mock_graph.assert_called_once()

    @pytest.mark.asyncio
    async def test_graph_build_failure_does_not_break_pipeline(self) -> None:
        """知识图谱构建失败不影响主流程（向量化+索引）。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc()
        mock_session_cm, mock_repo = self._make_mock_session(mock_doc)

        mock_tenant_svc = AsyncMock()
        mock_tenant_svc.is_module_enabled = AsyncMock(return_value=True)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch(
                 "tasks.document_tasks._build_knowledge_graph",
                 new_callable=AsyncMock,
                 side_effect=Exception("Neo4j 连接失败"),
             ) as mock_graph, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"), \
             patch(
                 "app.services.tenant_service.TenantService",
                 return_value=mock_tenant_svc,
             ):

            result = await _process_document_async(_TEST_UUID)

        # 主流程仍应成功
        assert result["status"] == "success"
        mock_index.assert_called_once()
        mock_graph.assert_called_once()
        # 警告中应包含图谱构建失败信息
        assert any("知识图谱构建失败" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_graph_build_receives_chunk_objects(self) -> None:
        """_build_knowledge_graph 接收的 chunk_objects 应与 _build_indexes 相同（计算复用）。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc()
        mock_session_cm, mock_repo = self._make_mock_session(mock_doc)

        mock_tenant_svc = AsyncMock()
        mock_tenant_svc.is_module_enabled = AsyncMock(return_value=True)

        # 捕获两个函数接收的 chunk_objects
        index_chunks: list = []
        graph_chunks: list = []

        async def capture_index(doc_id, chunk_objects, chunks, embeddings, **kwargs):
            index_chunks.extend(chunk_objects)

        async def capture_graph(doc_id, chunk_objects, doc, **kwargs):
            graph_chunks.extend(chunk_objects)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch(
                 "app.repositories.knowledge_repository.DocumentRepository",
                 return_value=mock_repo,
             ), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", side_effect=capture_index), \
             patch("tasks.document_tasks._build_knowledge_graph", side_effect=capture_graph), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"), \
             patch(
                 "app.services.tenant_service.TenantService",
                 return_value=mock_tenant_svc,
             ):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        # 两个支线接收的 chunk_objects 数量应相同（计算复用）
        assert len(index_chunks) > 0
        assert len(index_chunks) == len(graph_chunks)


# ======================================================================
# _build_knowledge_graph 辅助函数测试
# ======================================================================


class TestBuildKnowledgeGraphFunction:
    """_build_knowledge_graph 辅助函数测试。"""

    @pytest.mark.asyncio
    async def test_build_knowledge_graph_returns_triples_count(self) -> None:
        """_build_knowledge_graph 返回三元组数量。"""
        from tasks.document_tasks import _build_knowledge_graph

        mock_chunks = [MagicMock(content="微服务属于架构模式", title_path="")]
        mock_doc = MagicMock()

        mock_service = AsyncMock()
        mock_service.extract_triples_from_chunks = AsyncMock(
            return_value=[("微服务", "属于", "架构模式")]
        )
        mock_service.invalidate_recommend_cache = AsyncMock()

        with patch(
            "app.services.graph_service.get_graph_service", return_value=mock_service
        ), \
             patch("app.llm.factory.get_llm_provider", side_effect=Exception("no LLM")):

            count = await _build_knowledge_graph(_TEST_UUID, mock_chunks, mock_doc)

        assert count == 1
        mock_service.extract_triples_from_chunks.assert_called_once()
        mock_service.invalidate_recommend_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_knowledge_graph_no_triples_returns_zero(self) -> None:
        """无三元组时返回 0。"""
        from tasks.document_tasks import _build_knowledge_graph

        mock_chunks = [MagicMock(content="今天天气很好", title_path="")]
        mock_doc = MagicMock()

        mock_service = AsyncMock()
        mock_service.extract_triples_from_chunks = AsyncMock(return_value=[])
        mock_service.invalidate_recommend_cache = AsyncMock()

        with patch(
            "app.services.graph_service.get_graph_service", return_value=mock_service
        ), \
             patch("app.llm.factory.get_llm_provider", side_effect=Exception("no LLM")):

            count = await _build_knowledge_graph(_TEST_UUID, mock_chunks, mock_doc)

        assert count == 0

    @pytest.mark.asyncio
    async def test_build_knowledge_graph_service_unavailable(self) -> None:
        """GraphService 不可用时返回 0，不抛异常。"""
        from tasks.document_tasks import _build_knowledge_graph

        mock_chunks = [MagicMock(content="内容", title_path="")]
        mock_doc = MagicMock()

        with patch(
            "app.services.graph_service.get_graph_service",
            side_effect=RuntimeError("Neo4j 未配置"),
        ):
            count = await _build_knowledge_graph(_TEST_UUID, mock_chunks, mock_doc)

        assert count == 0


# ======================================================================
# chord 拆分测试 — Task 拓扑 + chunk 持久化 + 降级
# ======================================================================


class TestChordPipelineSplit:
    """chord 拆分测试 — 4 个独立 task + chunk Redis 持久化 + 降级。

    验证：
    - _build_index_async / _build_graph_async 独立执行
    - _finalize_document_async 合并 warnings
    - _parse_and_chunk_async 正确返回 chunk_objects + graph_enabled
    - chunk_objects 通过 Redis 跨进程共享

    测试策略：Celery 在测试环境被 mock，直接测试底层异步函数。
    """

    @pytest.mark.asyncio
    async def test_build_index_async_calls_build_indexes(self) -> None:
        """_build_index_async 调用 _generate_embeddings + _build_indexes。"""
        from tasks.document_tasks import _build_index_async

        mock_chunks = [MagicMock(content="测试内容", id="c1")]
        chunks_text = ["测试内容"]

        with patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[[0.1]]) as mock_emb, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_idx:

            result = await _build_index_async(_TEST_UUID, mock_chunks, chunks_text)

        assert result["status"] == "done"
        mock_emb.assert_called_once()
        mock_idx.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_index_async_embedding_failure_degraded(self) -> None:
        """向量化失败时降级为空向量，仍记录 warning。"""
        from tasks.document_tasks import _build_index_async

        mock_chunks = [MagicMock(content="内容", id="c1")]

        with patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, side_effect=Exception("API 不可用")), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock):

            result = await _build_index_async(_TEST_UUID, mock_chunks, ["内容"])

        assert result["status"] == "done"
        assert any("向量化失败" in w for w in result["index_warnings"])

    @pytest.mark.asyncio
    async def test_build_graph_async_calls_build_knowledge_graph(self) -> None:
        """_build_graph_async 调用 _build_knowledge_graph。"""
        from tasks.document_tasks import _build_graph_async

        mock_chunks = [MagicMock(content="微服务属于架构模式", id="c1")]
        mock_doc = MagicMock()
        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)
        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._build_knowledge_graph", new_callable=AsyncMock) as mock_graph:

            result = await _build_graph_async(_TEST_UUID, mock_chunks)

        assert result["status"] == "done"
        mock_graph.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_graph_async_failure_records_warning(self) -> None:
        """图谱构建失败时记录 warning，不抛异常。"""
        from tasks.document_tasks import _build_graph_async

        mock_chunks = [MagicMock(content="内容", id="c1")]
        mock_doc = MagicMock()
        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._build_knowledge_graph", new_callable=AsyncMock, side_effect=Exception("Neo4j 断开")):

            result = await _build_graph_async(_TEST_UUID, mock_chunks)

        assert result["status"] == "done"
        assert any("知识图谱构建失败" in w for w in result["graph_warnings"])

    @pytest.mark.asyncio
    async def test_finalize_document_async_publishes_internal(self) -> None:
        """_finalize_document_async 对 internal 密级直接发布。"""
        from tasks.document_tasks import _finalize_document_async

        results = [
            {"doc_id": _TEST_UUID, "status": "done", "index_warnings": []},
            {"doc_id": _TEST_UUID, "status": "done", "graph_warnings": []},
        ]

        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = "内容"
        mock_doc.classification = "internal"
        mock_doc.parse_warnings = None
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)
        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _finalize_document_async(_TEST_UUID, results)

        assert result["status"] == "success"
        assert result["final_status"] == "published"

    @pytest.mark.asyncio
    async def test_finalize_document_async_submits_confidential_for_audit(self) -> None:
        """_finalize_document_async 对 confidential 密级提交审核。"""
        from tasks.document_tasks import _finalize_document_async

        results = []

        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = "机密内容"
        mock_doc.classification = "confidential"
        mock_doc.parse_warnings = None
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _finalize_document_async(_TEST_UUID, results)

        assert result["final_status"] == "pending_review"
        mock_audit.assert_called_once()

    @pytest.mark.asyncio
    async def test_finalize_document_async_merges_subtask_warnings(self) -> None:
        """_finalize_document_async 合并子 task 的 warnings。"""
        from tasks.document_tasks import _finalize_document_async

        results = [
            {"doc_id": _TEST_UUID, "index_warnings": ["索引降级"]},
            {"doc_id": _TEST_UUID, "graph_warnings": ["图谱失败"]},
        ]

        mock_doc = MagicMock()
        mock_doc.content_text = "内容"
        mock_doc.classification = "internal"
        mock_doc.parse_warnings = ["解析警告"]
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.async_session_factory", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _finalize_document_async(_TEST_UUID, results)

        assert "解析警告" in result["warnings"]
        assert "索引降级" in result["warnings"]
        assert "图谱失败" in result["warnings"]


class TestChunkRedisPersistence:
    """chunk_objects Redis 持久化测试。"""

    def test_save_and_load_chunks_roundtrip(self) -> None:
        """chunk_objects 序列化到 Redis 再反序列化，数据完整。"""
        import types

        # 确保 redis 模块存在
        if "redis" not in sys.modules:
            mock_redis_mod = types.ModuleType("redis")
            mock_redis_mod.from_url = MagicMock()
            sys.modules["redis"] = mock_redis_mod

        from tasks.document_tasks import _save_chunks_to_redis, _load_chunks_from_redis
        from app.rag.chunker import Chunk

        chunks = [
            Chunk(
                id="c1", doc_id=_TEST_UUID, content="测试内容1",
                parent_id=None, title_path="标题1",
                content_type="plain", chunk_strategy="semantic",
            ),
            Chunk(
                id="c2", doc_id=_TEST_UUID, content="测试内容2",
                parent_id="c1", title_path="标题1 > 子标题",
                content_type="plain", chunk_strategy="structural",
            ),
        ]

        saved_payload = []

        mock_client = MagicMock()
        def mock_setex(key, ttl, value):
            saved_payload.append(value)
        def mock_get(key):
            return saved_payload[0] if saved_payload else None
        mock_client.setex = mock_setex
        mock_client.get = mock_get

        with patch("redis.from_url", return_value=mock_client):
            result = _save_chunks_to_redis(_TEST_UUID, chunks)
            assert result is True
            loaded = _load_chunks_from_redis(_TEST_UUID)

        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0].id == "c1"
        assert loaded[0].content == "测试内容1"
        assert loaded[0].title_path == "标题1"
        assert loaded[1].parent_id == "c1"
        assert loaded[1].chunk_strategy == "structural"

    def test_save_chunks_redis_unavailable_returns_false(self) -> None:
        """Redis 不可用时 _save_chunks_to_redis 返回 False。"""
        import types

        if "redis" not in sys.modules:
            mock_redis_mod = types.ModuleType("redis")
            mock_redis_mod.from_url = MagicMock()
            sys.modules["redis"] = mock_redis_mod

        from tasks.document_tasks import _save_chunks_to_redis
        from app.rag.chunker import Chunk

        chunks = [Chunk(id="c1", doc_id=_TEST_UUID, content="内容")]

        with patch("redis.from_url", side_effect=Exception("Connection refused")):
            result = _save_chunks_to_redis(_TEST_UUID, chunks)

        assert result is False

    def test_load_chunks_not_found_returns_none(self) -> None:
        """Redis 中无数据时 _load_chunks_from_redis 返回 None。"""
        import types

        if "redis" not in sys.modules:
            mock_redis_mod = types.ModuleType("redis")
            mock_redis_mod.from_url = MagicMock()
            sys.modules["redis"] = mock_redis_mod

        from tasks.document_tasks import _load_chunks_from_redis

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=None)

        with patch("redis.from_url", return_value=mock_client):
            result = _load_chunks_from_redis(_TEST_UUID)

        assert result is None


# ======================================================================
# 修复回归测试 — chunk 序列化字段补全 / finalize 短路+幂等 / OpenSearch _id
# ======================================================================


class TestChunkRedisRoundTripFields:
    """修复 1：chunk 经 Redis 序列化传递时补全 token_count/start_pos/end_pos。"""

    def _ensure_redis_mock(self) -> None:
        import types

        if "redis" not in sys.modules:
            mock_redis_mod = types.ModuleType("redis")
            mock_redis_mod.from_url = MagicMock()
            sys.modules["redis"] = mock_redis_mod

    def test_roundtrip_preserves_token_and_position_fields(self) -> None:
        """round-trip 后 token_count/start_pos/end_pos 完整恢复。"""
        self._ensure_redis_mock()

        from tasks.document_tasks import _load_chunks_from_redis, _save_chunks_to_redis

        chunks = [
            Chunk(
                id="c1", doc_id=_TEST_UUID, content="第一段内容",
                parent_id=None, start_pos=0, end_pos=120, token_count=42,
                title_path="标题", content_type="plain", chunk_strategy="structural",
            ),
            Chunk(
                id="c2", doc_id=_TEST_UUID, content="第二段内容",
                parent_id="c1", start_pos=120, end_pos=260, token_count=57,
                title_path="标题 > 子标题", content_type="plain", chunk_strategy="semantic",
            ),
        ]

        saved_payload: list[str] = []
        mock_client = MagicMock()
        mock_client.setex = lambda key, ttl, value: saved_payload.append(value)
        mock_client.get = lambda key: saved_payload[0] if saved_payload else None

        with patch("redis.from_url", return_value=mock_client):
            assert _save_chunks_to_redis(_TEST_UUID, chunks) is True
            loaded = _load_chunks_from_redis(_TEST_UUID)

        assert loaded is not None
        assert len(loaded) == 2
        # 关键断言：位置与 token 字段不丢失
        assert loaded[0].start_pos == 0
        assert loaded[0].end_pos == 120
        assert loaded[0].token_count == 42
        assert loaded[1].start_pos == 120
        assert loaded[1].end_pos == 260
        assert loaded[1].token_count == 57
        # 原有字段不受影响
        assert loaded[1].parent_id == "c1"
        assert loaded[1].chunk_strategy == "semantic"

    def test_roundtrip_legacy_payload_defaults_to_zero(self) -> None:
        """旧格式数据（无位置/token 字段）反序列化向后兼容，默认 0。"""
        self._ensure_redis_mock()

        import json as _json

        from tasks.document_tasks import _load_chunks_from_redis

        legacy_payload = _json.dumps(
            [
                {
                    "id": "c1",
                    "doc_id": _TEST_UUID,
                    "content": "旧格式内容",
                    "parent_id": None,
                    "title_path": "",
                    "content_type": "plain",
                    "chunk_strategy": "fallback",
                }
            ],
            ensure_ascii=False,
        )

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=legacy_payload)

        with patch("redis.from_url", return_value=mock_client):
            loaded = _load_chunks_from_redis(_TEST_UUID)

        assert loaded is not None
        assert loaded[0].start_pos == 0
        assert loaded[0].end_pos == 0
        assert loaded[0].token_count == 0


class TestFinalizeFailedShortCircuit:
    """修复 2：finalize 标记 failed 后短路发布流程，重复执行幂等。"""

    def _make_doc(
        self,
        content_text: str = "",
        status: str = "draft",
        classification: str = "internal",
    ):
        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = content_text
        mock_doc.classification = classification
        mock_doc.parse_warnings = None
        mock_doc.status = status
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)
        return mock_doc

    def _make_db_mocks(self, mock_doc):
        """构造 task_db_session + DocumentRepository 的 mock。"""
        from contextlib import asynccontextmanager

        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_task_db_session():
            yield mock_session

        return mock_task_db_session, mock_repo, mock_session

    @pytest.mark.asyncio
    async def test_failed_parse_short_circuits_publish(self) -> None:
        """无正文内容（parse_status=failed）时不发布、不审核、不触发智能处理。"""
        from tasks.document_tasks import _finalize_document_async

        mock_doc = self._make_doc(content_text="")
        mock_task_db_session, mock_repo, mock_session = self._make_db_mocks(mock_doc)

        with patch("app.database.task_db_session", mock_task_db_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress") as mock_progress, \
             patch("tasks.document_tasks._cleanup_chunks_redis"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay") as mock_intel:

            result = await _finalize_document_async(_TEST_UUID, [])

        assert result["status"] == "failed"
        assert "error" in result
        # 状态一致：parse_status 与 status 均为 failed
        assert mock_doc.parse_status == "failed"
        assert mock_doc.status == "failed"
        # 短路：不提交审核、不触发智能处理
        mock_audit.assert_not_called()
        mock_intel.assert_not_called()
        # 进度标记为 failed 而非 done
        assert any(
            call.kwargs.get("stage") == "failed" for call in mock_progress.call_args_list
        )

    @pytest.mark.asyncio
    async def test_failed_parse_repeat_finalize_is_idempotent(self) -> None:
        """重复 finalize 失败文档：状态保持 failed，无重复发布副作用（幂等）。"""
        from tasks.document_tasks import _finalize_document_async

        mock_doc = self._make_doc(content_text="")
        mock_task_db_session, mock_repo, mock_session = self._make_db_mocks(mock_doc)

        with patch("app.database.task_db_session", mock_task_db_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._cleanup_chunks_redis"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay") as mock_intel:

            first = await _finalize_document_async(_TEST_UUID, [])
            # 模拟 chord 重放：第二次 finalize（doc.status 已是 failed）
            second = await _finalize_document_async(_TEST_UUID, [])

        assert first["status"] == "failed"
        assert second["status"] == "failed"
        assert mock_doc.status == "failed"
        # 两次执行均无审核/智能处理副作用
        mock_audit.assert_not_called()
        mock_intel.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeat_finalize_published_is_idempotent(self) -> None:
        """文档已 published 时重复 finalize：不重复发布、不重复触发智能处理。"""
        from tasks.document_tasks import _finalize_document_async

        mock_doc = self._make_doc(content_text="正文内容", status="published")
        mock_task_db_session, mock_repo, mock_session = self._make_db_mocks(mock_doc)

        with patch("app.database.task_db_session", mock_task_db_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._cleanup_chunks_redis"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay") as mock_intel:

            result = await _finalize_document_async(_TEST_UUID, [])

        assert result["status"] == "success"
        assert result["final_status"] == "published"
        assert result.get("idempotent") is True
        # 状态不被翻转，无重复发布副作用
        assert mock_doc.status == "published"
        mock_audit.assert_not_called()
        mock_intel.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeat_finalize_pending_review_skips_audit(self) -> None:
        """文档已 pending_review 时重复 finalize：不重复提交审核流程。"""
        from tasks.document_tasks import _finalize_document_async

        mock_doc = self._make_doc(
            content_text="机密内容", status="pending_review", classification="confidential"
        )
        mock_task_db_session, mock_repo, mock_session = self._make_db_mocks(mock_doc)

        with patch("app.database.task_db_session", mock_task_db_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._cleanup_chunks_redis"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay") as mock_intel:

            result = await _finalize_document_async(_TEST_UUID, [])

        assert result["status"] == "success"
        assert result["final_status"] == "pending_review"
        # 不重复提交审核、不重复触发智能处理
        mock_audit.assert_not_called()
        mock_intel.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_finalize_still_publishes_normally(self) -> None:
        """回归：首次 finalize（draft 状态）仍正常发布并触发智能处理。"""
        from tasks.document_tasks import _finalize_document_async

        mock_doc = self._make_doc(content_text="正文内容", status="draft")
        mock_task_db_session, mock_repo, mock_session = self._make_db_mocks(mock_doc)

        with patch("app.database.task_db_session", mock_task_db_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._update_parse_progress"), \
             patch("tasks.document_tasks._cleanup_chunks_redis"), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence.delay") as mock_intel:

            result = await _finalize_document_async(_TEST_UUID, [])

        assert result["status"] == "success"
        assert result["final_status"] == "published"
        assert mock_doc.status == "published"
        mock_intel.assert_called_once()


class TestOpenSearchDeterministicId:
    """修复 3：OpenSearch 写入以确定性 chunk_id 作 _id，重复执行 upsert 幂等。"""

    def _make_chunk_objects(self, count: int = 3) -> list[Chunk]:
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

    def _make_mock_client(self) -> AsyncMock:
        mock_client = AsyncMock()
        mock_client.indices.exists = AsyncMock(return_value=True)
        mock_client.bulk = AsyncMock(return_value={"errors": False, "items": []})
        mock_client.close = AsyncMock()
        return mock_client

    def _extract_bulk_ids(self, mock_client: AsyncMock) -> list[str]:
        """从 bulk NDJSON 请求体中提取所有 action 行的 _id。"""
        body = mock_client.bulk.call_args.kwargs.get("body", "")
        return [line["index"]["_id"] for line in _parse_bulk_body(body)[0::2]]

    @pytest.mark.asyncio
    async def test_index_uses_chunk_id_as_document_id(self) -> None:
        """每个 chunk 的写入都指定 _id=chunk.id（确定性）。"""
        chunk_objects = self._make_chunk_objects(2)
        mock_client = self._make_mock_client()

        with patch("opensearchpy.AsyncOpenSearch", return_value=mock_client):
            await _build_opensearch_index("doc-001", chunk_objects)

        assert mock_client.bulk.call_count == 1
        lines = _parse_bulk_body(mock_client.bulk.call_args.kwargs.get("body", ""))
        actions, sources = lines[0::2], lines[1::2]
        for i, (action, source) in enumerate(zip(actions, sources, strict=True)):
            assert action["index"]["_id"] == f"chunk-{i}"
            assert source["chunk_id"] == f"chunk-{i}"

    @pytest.mark.asyncio
    async def test_repeat_indexing_upserts_same_ids(self) -> None:
        """重复任务执行：两次写入使用相同 _id 集合（upsert 语义，不产生重复文档）。"""
        chunk_objects = self._make_chunk_objects(3)

        first_client = self._make_mock_client()
        second_client = self._make_mock_client()

        with patch("opensearchpy.AsyncOpenSearch", return_value=first_client):
            await _build_opensearch_index("doc-001", chunk_objects)
        with patch("opensearchpy.AsyncOpenSearch", return_value=second_client):
            await _build_opensearch_index("doc-001", chunk_objects)

        first_ids = self._extract_bulk_ids(first_client)
        second_ids = self._extract_bulk_ids(second_client)
        # 幂等：重复执行写入相同数量的文档，且 _id 完全一致（覆盖而非新增）
        assert len(first_ids) == len(second_ids) == 3
        assert first_ids == second_ids == ["chunk-0", "chunk-1", "chunk-2"]
        # 所有 _id 均显式指定（无 None → 不会依赖 OpenSearch 自动生成）
        assert all(fid is not None for fid in first_ids + second_ids)
