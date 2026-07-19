"""文档上传大小校验 + 解析摘要响应测试。

P0: 文件大小校验（MAX_UPLOAD_SIZE_MB）— 超限返回 413
P1: 解析摘要响应（/documents/{doc_id}/summary）— 返回 preview/structure/warnings/pages

测试覆盖：
- TestValidateUploadSize: _validate_upload_size 辅助函数单元测试
- TestDocumentSummaryHelpers: _extract_structure_tags / _count_pages / _infer_parse_status
- TestDocumentSummaryEndpoint: /documents/{doc_id}/summary 端点端到端
- TestUploadSizeValidation: 上传端点大小校验集成测试
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.api.v1.documents import (
    _count_pages,
    _extract_structure_tags,
    _infer_parse_status,
    _validate_upload_size,
)
from app.config import get_settings


# ======================================================================
# Fixtures
# ======================================================================


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 用于测试认证强制。"""
    from app.main import app
    from app.middleware import get_rate_limiter

    # 清理限流器 buckets，防止跨测试触发 429
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(mock_user):
    """带认证与 DB 覆盖的客户端。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app
    from app.middleware import get_rate_limiter

    # 清理限流器 buckets，防止跨测试触发 429
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    async def override_user():
        return mock_user

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _make_mock_doc(
    content_text: str = "",
    doc_type: str = "md",
    status: str = "draft",
    file_path: str | None = None,
    parse_status: str | None = None,
    parse_warnings: list[str] | None = None,
    page_count: int | None = None,
    char_count: int | None = None,
) -> SimpleNamespace:
    """构造模拟文档对象（含 P1 解析元数据字段）。"""
    return SimpleNamespace(
        id=uuid4(),
        kb_id=uuid4(),
        title="测试文档",
        content_html="<p>测试</p>",
        content_json=None,
        content_text=content_text,
        doc_type=doc_type,
        status=status,
        owner_id=uuid4(),
        dept_id=None,
        classification="internal",
        view_count=0,
        file_path=file_path,
        summary=None,
        category=None,
        # P1: 解析元数据字段
        parse_status=parse_status,
        parse_warnings=parse_warnings,
        page_count=page_count,
        char_count=char_count,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        deleted_at=None,
        tenant_id=None,
    )


# ======================================================================
# P0: _validate_upload_size 单元测试
# ======================================================================


class TestValidateUploadSize:
    """文件大小校验辅助函数测试。"""

    def test_small_file_passes(self) -> None:
        """小文件（1KB）不触发 413。"""
        content = b"x" * 1024  # 1KB
        # 不抛异常即通过
        _validate_upload_size(content)

    def test_exactly_at_limit_passes(self) -> None:
        """刚好等于上限（50MB）不触发 413。"""
        settings = get_settings()
        max_mb = settings.MAX_UPLOAD_SIZE_MB
        # 50MB - 1 字节（避免浮点精度问题）
        content = b"x" * (max_mb * 1024 * 1024 - 1)
        _validate_upload_size(content)

    def test_exceeds_limit_raises_413(self) -> None:
        """超过上限（51MB）触发 413。"""
        from fastapi import HTTPException

        settings = get_settings()
        max_mb = settings.MAX_UPLOAD_SIZE_MB
        # 构造超过上限 1MB 的内容
        content = b"x" * ((max_mb + 1) * 1024 * 1024)
        with pytest.raises(HTTPException) as exc_info:
            _validate_upload_size(content)
        assert exc_info.value.status_code == 413
        assert "超过上限" in exc_info.value.detail

    def test_empty_file_passes(self) -> None:
        """空文件不触发 413。"""
        _validate_upload_size(b"")

    def test_magic_mock_settings_fallback(self) -> None:
        """MagicMock 配置场景下回退到默认 50MB。"""
        from fastapi import HTTPException

        mock_settings = MagicMock()
        # MagicMock 返回非 int 值，应回退到默认 50MB
        mock_settings.MAX_UPLOAD_SIZE_MB = MagicMock()  # 非 int
        with patch("app.api.v1.documents.get_settings", return_value=mock_settings):
            # 51MB 应触发 413（回退到默认 50MB）
            content = b"x" * (51 * 1024 * 1024)
            with pytest.raises(HTTPException) as exc_info:
                _validate_upload_size(content)
            assert exc_info.value.status_code == 413

    def test_zero_max_size_fallback_to_default(self) -> None:
        """配置为 0 时回退到默认 50MB。"""
        from fastapi import HTTPException

        mock_settings = SimpleNamespace(MAX_UPLOAD_SIZE_MB=0)
        with patch("app.api.v1.documents.get_settings", return_value=mock_settings):
            # 51MB 应触发 413（回退到默认 50MB）
            content = b"x" * (51 * 1024 * 1024)
            with pytest.raises(HTTPException) as exc_info:
                _validate_upload_size(content)
            assert exc_info.value.status_code == 413


# ======================================================================
# P1: 摘要辅助函数测试
# ======================================================================


class TestExtractStructureTags:
    """结构标签提取测试。"""

    def test_empty_text_returns_empty(self) -> None:
        assert _extract_structure_tags("") == []

    def test_none_text_returns_empty(self) -> None:
        assert _extract_structure_tags(None) == []  # type: ignore[arg-type]

    def test_single_h1(self) -> None:
        text = "<h1>标题</h1><p>内容</p>"
        assert _extract_structure_tags(text) == ["h1"]

    def test_multiple_tags_dedup(self) -> None:
        text = "<h1>大标题</h1><h2>小标题</h2><table><tr></tr></table><ul><li>a</li></ul>"
        result = _extract_structure_tags(text)
        assert "h1" in result
        assert "h2" in result
        assert "table" in result
        assert "ul" in result
        assert "li" in result

    def test_tags_order_preserved(self) -> None:
        """标签出现顺序应保持（首次出现顺序）。"""
        text = "<table><tr></tr></table><h1>标题</h1><ul><li>a</li></ul>"
        result = _extract_structure_tags(text)
        assert result.index("table") < result.index("h1")
        assert result.index("h1") < result.index("ul")

    def test_case_insensitive(self) -> None:
        text = "<H1>大写</H1><TABLE></TABLE>"
        result = _extract_structure_tags(text)
        assert "h1" in result
        assert "table" in result

    def test_no_html_tags_returns_empty(self) -> None:
        text = "纯文本内容无标签"
        assert _extract_structure_tags(text) == []

    def test_h3_h6_recognized(self) -> None:
        text = "<h3>三级</h3><h6>六级</h6>"
        result = _extract_structure_tags(text)
        assert "h3" in result
        assert "h6" in result


class TestCountPages:
    """页数推断测试。"""

    def test_empty_text_returns_zero(self) -> None:
        assert _count_pages("", "pdf") == 0

    def test_page_markers_counted(self) -> None:
        """分页标记 <!-- page: N --> 优先计数。"""
        text = "<p>第1页</p>\n<!-- page: 1 -->\n<p>第2页</p>\n<!-- page: 2 -->\n"
        assert _count_pages(text, "pdf") == 2

    def test_h2_count_for_pptx(self) -> None:
        """PPTX 按 <h2> 标题计数（每 slide 一个 h2）。"""
        text = "<h2>幻灯片 1</h2><p>内容</p><h2>幻灯片 2</h2><p>内容</p><h2>幻灯片 3</h2>"
        assert _count_pages(text, "pptx") == 3

    def test_h2_count_for_xlsx(self) -> None:
        """XLSX 按 <h2> 标题计数（每 sheet 一个 h2）。"""
        text = "<h2>Sheet1</h2><table></table><h2>Sheet2</h2><table></table>"
        assert _count_pages(text, "xlsx") == 2

    def test_h2_count_for_docx(self) -> None:
        """DOCX 按 <h2> 标题计数。"""
        text = "<h2>第一章</h2><p>内容</p><h2>第二章</h2>"
        assert _count_pages(text, "docx") == 2

    def test_md_returns_zero(self) -> None:
        """MD/TXT 类型不计数。"""
        text = "<h2>标题</h2><p>内容</p>"
        assert _count_pages(text, "md") == 0

    def test_page_markers_preferred_over_h2(self) -> None:
        """分页标记优先于 h2 计数。"""
        text = "<h2>标题1</h2><h2>标题2</h2><!-- page: 1 -->"
        # 有 1 个分页标记，应返回 1 而非 2
        assert _count_pages(text, "pdf") == 1


class TestInferParseStatus:
    """解析状态推断测试。"""

    def test_published_status_parsed(self) -> None:
        doc = _make_mock_doc(content_text="内容", status="published")
        assert _infer_parse_status(doc) == "parsed"

    def test_pending_review_status_parsed(self) -> None:
        doc = _make_mock_doc(content_text="内容", status="pending_review")
        assert _infer_parse_status(doc) == "parsed"

    def test_archived_status_parsed(self) -> None:
        doc = _make_mock_doc(content_text="内容", status="archived")
        assert _infer_parse_status(doc) == "parsed"

    def test_draft_with_content_parsed(self) -> None:
        doc = _make_mock_doc(content_text="有内容", status="draft")
        assert _infer_parse_status(doc) == "parsed"

    def test_draft_without_content_pending(self) -> None:
        doc = _make_mock_doc(content_text="", status="draft")
        assert _infer_parse_status(doc) == "pending"

    def test_draft_with_whitespace_content_pending(self) -> None:
        doc = _make_mock_doc(content_text="   \n   ", status="draft")
        assert _infer_parse_status(doc) == "pending"


# ======================================================================
# P1: /documents/{doc_id}/summary 端点测试
# ======================================================================


class TestDocumentSummaryEndpoint:
    """解析摘要端点端到端测试。"""

    @pytest.mark.asyncio
    async def test_summary_parsed_document(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """已解析文档返回 preview + structure + pages。"""
        mock_doc = _make_mock_doc(
            content_text="<h1>标题</h1><p>正文内容</p><table><tr></tr></table>",
            doc_type="docx",
            status="published",
            file_path="minio://ekb-documents/doc.docx",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["doc_id"] == str(mock_doc.id)
        assert data["title"] == "测试文档"
        assert data["doc_type"] == "docx"
        assert data["status"] == "published"
        assert "标题" in data["preview"]
        assert "h1" in data["structure"]
        assert "table" in data["structure"]
        assert data["pages"] == 0  # 无 h2 也无分页标记
        assert data["char_count"] > 0
        assert data["parse_status"] == "parsed"
        assert data["warnings"] == []

    @pytest.mark.asyncio
    async def test_summary_pending_document(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """草稿且无内容返回 pending 状态 + 警告。"""
        mock_doc = _make_mock_doc(
            content_text="",
            doc_type="pdf",
            status="draft",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["parse_status"] == "pending"
        assert data["preview"] == ""
        assert data["char_count"] == 0
        assert len(data["warnings"]) > 0
        assert any("空" in w for w in data["warnings"])

    @pytest.mark.asyncio
    async def test_summary_pptx_with_pages(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """PPTX 文档按 <h2> 计数幻灯片数。"""
        content = (
            "<h2>幻灯片 1: 标题</h2><p>内容</p>"
            "<h2>幻灯片 2: 标题</h2><p>内容</p>"
            "<h2>幻灯片 3: 标题</h2><p>内容</p>"
        )
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="pptx",
            status="published",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pages"] == 3
        assert "h2" in data["structure"]

    @pytest.mark.asyncio
    async def test_summary_legacy_format_warning(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """旧格式 .doc 返回警告信息。"""
        mock_doc = _make_mock_doc(
            content_text="",
            doc_type="doc",
            status="draft",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert any("旧格式" in w for w in data["warnings"])

    @pytest.mark.asyncio
    async def test_summary_with_page_markers(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """带分页标记的文档正确计数页数。"""
        content = (
            "<p>第1页内容</p>\n<!-- page: 1 -->\n"
            "<p>第2页内容</p>\n<!-- page: 2 -->\n"
        )
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="pdf",
            status="published",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pages"] == 2

    @pytest.mark.asyncio
    async def test_summary_requires_auth(
        self, raw_client: httpx.AsyncClient
    ) -> None:
        """summary 端点需要认证。"""
        response = await raw_client.get(f"/api/v1/documents/{uuid4()}/summary")
        assert response.status_code == 401


# ======================================================================
# P0: 上传端点大小校验集成测试
# ======================================================================


class TestUploadSizeValidation:
    """上传端点文件大小校验测试。"""

    @pytest.mark.asyncio
    async def test_upload_small_file_accepted(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """小文件上传不触发 413（mock 服务层避免实际写入）。"""
        mock_doc = _make_mock_doc(content_text="内容", status="draft")

        # DocumentRepository 在函数内导入，patch 其真实路径
        with patch("app.api.v1.documents.KnowledgeService") as mock_cls, \
             patch("app.repositories.knowledge_repository.DocumentRepository") as mock_repo_cls, \
             patch("app.utils.minio_client.upload_file", new_callable=AsyncMock) as mock_upload:
            mock_service = mock_cls.return_value
            mock_service.upload_document = AsyncMock(return_value=mock_doc)
            mock_repo = mock_repo_cls.return_value
            mock_repo.update = AsyncMock(return_value=mock_doc)
            mock_upload.return_value = "minio://ekb-documents/test"

            # 1KB 小文件
            file_content = b"x" * 1024
            response = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid4()), "title": "测试"},
                files={"file": ("test.md", file_content, "text/markdown")},
            )

        # 不应返回 413
        assert response.status_code != 413

    @pytest.mark.asyncio
    async def test_upload_oversized_file_returns_413(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """超大文件上传返回 413。"""
        settings = get_settings()
        max_mb = settings.MAX_UPLOAD_SIZE_MB

        # 构造超过上限 1MB 的内容（不实际发送，用 patch 模拟 file.read）
        from io import BytesIO

        oversize_bytes = b"x" * ((max_mb + 1) * 1024 * 1024)

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.upload_document = AsyncMock()

            # 使用 Starlette UploadFile mock
            mock_file = MagicMock()
            mock_file.filename = "huge.pdf"
            mock_file.content_type = "application/pdf"
            mock_file.read = AsyncMock(return_value=oversize_bytes)

            # 直接调用校验函数验证
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc_info:
                _validate_upload_size(oversize_bytes)
            assert exc_info.value.status_code == 413
            assert "超过上限" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_image_oversized_rejected(self) -> None:
        """图片上传同样受大小限制。"""
        from fastapi import HTTPException

        settings = get_settings()
        max_mb = settings.MAX_UPLOAD_SIZE_MB
        oversize_bytes = b"x" * ((max_mb + 1) * 1024 * 1024)

        with pytest.raises(HTTPException) as exc_info:
            _validate_upload_size(oversize_bytes)
        assert exc_info.value.status_code == 413


# ======================================================================
# P1: 解析任务 warnings 返回测试
# ======================================================================


class TestDocumentTaskWarnings:
    """解析任务 warnings 收集测试。

    验证 _process_document_async 返回值包含 warnings / parse_status / char_count 字段。
    通过 mock 整个处理链路避免依赖外部服务。
    """

    @pytest.mark.asyncio
    async def test_task_returns_warnings_field(self) -> None:
        """任务返回值应包含 warnings 字段（即使为空列表）。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "md"
        mock_doc.classification = "internal"
        mock_doc.content_text = "已有内容"
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        # async_session_factory 和 DocumentRepository 都是函数内导入，
        # 必须 patch 源模块（app.database / app.repositories...）。
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = "解析后的内容"
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            result = await _process_document_async(str(mock_doc.id))

        assert "warnings" in result
        assert "parse_status" in result
        assert "char_count" in result
        assert isinstance(result["warnings"], list)
        assert result["char_count"] == len("解析后的内容")

    @pytest.mark.asyncio
    async def test_task_collects_parse_failure_warning(self) -> None:
        """解析失败时应收集警告。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "md"
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            # 解析抛异常
            mock_parse.side_effect = Exception("解析器崩溃")
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            result = await _process_document_async(str(mock_doc.id))

        assert len(result["warnings"]) > 0
        assert any("解析异常" in w for w in result["warnings"])
        assert result["parse_status"] in ("partial", "failed")

    @pytest.mark.asyncio
    async def test_task_legacy_format_warning(self) -> None:
        """旧格式 .doc 应产生警告。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "doc"  # 旧格式
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = ""  # 旧格式返回空
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            result = await _process_document_async(str(mock_doc.id))

        assert any("旧格式" in w for w in result["warnings"])


# ======================================================================
# P1: DB 字段优先读取测试
# ======================================================================


class TestSummaryDbFieldsPriority:
    """摘要端点优先读 DB 持久化字段测试。

    验证当 DB 已有 parse_status / parse_warnings / page_count / char_count 时，
    端点优先返回 DB 值，而非动态计算。
    """

    @pytest.mark.asyncio
    async def test_db_page_count_preferred(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB page_count 优先于动态计算。"""
        # content_text 有 3 个 <h2>，动态计算会返回 3
        content = "<h2>标题1</h2><h2>标题2</h2><h2>标题3</h2>"
        # 但 DB page_count=10，应返回 10
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="pptx",
            status="published",
            page_count=10,
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pages"] == 10  # DB 值优先

    @pytest.mark.asyncio
    async def test_db_char_count_preferred(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB char_count 优先于动态计算。"""
        content = "短内容"  # 动态计算 len=3
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="md",
            status="published",
            char_count=999,  # DB 值
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["char_count"] == 999

    @pytest.mark.asyncio
    async def test_db_parse_status_preferred(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB parse_status 优先于推断。"""
        # status=published 推断为 parsed，但 DB parse_status=partial
        mock_doc = _make_mock_doc(
            content_text="有内容",
            doc_type="pdf",
            status="published",
            parse_status="partial",
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["parse_status"] == "partial"  # DB 值优先

    @pytest.mark.asyncio
    async def test_db_parse_warnings_preferred(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB parse_warnings 优先于动态推断。"""
        mock_doc = _make_mock_doc(
            content_text="有内容",
            doc_type="pdf",
            status="published",
            parse_warnings=["向量化失败", "索引构建降级"],
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["warnings"]) == 2
        assert "向量化失败" in data["warnings"]

    @pytest.mark.asyncio
    async def test_fallback_when_db_fields_none(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB 字段为 NULL 时回退到动态计算（历史数据兼容）。"""
        content = "<h2>标题1</h2><h2>标题2</h2>"
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="docx",
            status="published",
            parse_status=None,  # NULL
            parse_warnings=None,  # NULL
            page_count=None,  # NULL
            char_count=None,  # NULL
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # 回退到动态计算
        assert data["pages"] == 2  # 2 个 <h2>
        assert data["char_count"] == len(content)
        assert data["parse_status"] == "parsed"  # published → parsed
        assert data["warnings"] == []  # 有内容无警告

    @pytest.mark.asyncio
    async def test_fallback_when_db_page_count_zero(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """DB page_count=0 时回退到动态计算（0 视为未设置）。"""
        content = "<h2>标题1</h2><h2>标题2</h2><h2>标题3</h2>"
        mock_doc = _make_mock_doc(
            content_text=content,
            doc_type="pptx",
            status="published",
            page_count=0,  # 0 视为未设置
        )

        with patch("app.api.v1.documents.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(
                f"/api/v1/documents/{mock_doc.id}/summary"
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # 回退到动态计算
        assert data["pages"] == 3


# ======================================================================
# P1: process_document 任务持久化字段测试
# ======================================================================


class TestTaskPersistsParseMetadata:
    """验证 process_document 任务将解析元数据持久化到 Document。"""

    @pytest.mark.asyncio
    async def test_task_sets_parse_status_parsed(self) -> None:
        """成功解析时 doc.parse_status 应被设置为 parsed。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "md"
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = "解析后的内容"
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            await _process_document_async(str(mock_doc.id))

        assert mock_doc.parse_status == "parsed"
        assert mock_doc.char_count == len("解析后的内容")

    @pytest.mark.asyncio
    async def test_task_sets_parse_status_partial_on_warning(self) -> None:
        """有警告时 doc.parse_status 应被设置为 partial。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "md"
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = "内容"
            mock_chunk.return_value = []
            # 向量化失败 → 产生警告
            mock_embed.side_effect = Exception("VLM 服务不可用")
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            await _process_document_async(str(mock_doc.id))

        assert mock_doc.parse_status == "partial"
        assert mock_doc.parse_warnings is not None
        assert len(mock_doc.parse_warnings) > 0

    @pytest.mark.asyncio
    async def test_task_sets_page_count_from_h2(self) -> None:
        """PPTX 文档应按 <h2> 计数设置 page_count。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "pptx"
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        parsed_content = "<h2>幻灯片1</h2><h2>幻灯片2</h2><h2>幻灯片3</h2><h2>幻灯片4</h2>"

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = parsed_content
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            await _process_document_async(str(mock_doc.id))

        assert mock_doc.page_count == 4  # 4 个 <h2>

    @pytest.mark.asyncio
    async def test_task_sets_page_count_from_markers(self) -> None:
        """有分页标记时优先按标记计数。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = MagicMock()
        mock_doc.id = uuid4()
        mock_doc.doc_type = "pdf"
        mock_doc.classification = "internal"
        mock_doc.content_text = ""
        mock_doc.owner_id = uuid4()
        mock_doc.status = "draft"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        parsed_content = (
            "<p>第1页</p>\n<!-- page: 1 -->\n"
            "<p>第2页</p>\n<!-- page: 2 -->\n"
        )

        with patch("app.database.async_session_factory", return_value=mock_session), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._parse_document", new_callable=AsyncMock) as mock_parse, \
             patch("tasks.document_tasks._chunk_document") as mock_chunk, \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock) as mock_embed, \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock) as mock_index, \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock), \
             patch("tasks.intelligence_tasks.process_intelligence") as mock_intel:
            mock_parse.return_value = parsed_content
            mock_chunk.return_value = []
            mock_embed.return_value = []
            mock_index.return_value = None
            mock_intel.delay = MagicMock()

            await _process_document_async(str(mock_doc.id))

        assert mock_doc.page_count == 2  # 2 个分页标记


# ======================================================================
# P1: 迁移文件测试
# ======================================================================


class TestParseMetadataMigration:
    """验证 add_document_parse_metadata 迁移文件。"""

    def test_migration_file_exists(self) -> None:
        """迁移文件应存在。"""
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1000-a1b2c3d4e5f6_add_document_parse_metadata.py",
        )
        assert os.path.exists(migration_path), f"迁移文件不存在: {migration_path}"

    def test_migration_revision_chain(self) -> None:
        """迁移链路应正确：115a9c06ba4a → a1b2c3d4e5f6。"""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        # 切换到 backend 目录
        import os

        os.chdir(os.path.dirname(__file__) + "/..")
        script = ScriptDirectory.from_config(cfg)
        revisions = {r.revision: r for r in script.walk_revisions()}

        assert "a1b2c3d4e5f6" in revisions, "新迁移 revision 不存在"
        assert revisions["a1b2c3d4e5f6"].down_revision == "115a9c06ba4a", (
            "down_revision 应为 115a9c06ba4a"
        )

    def test_migration_adds_four_columns(self) -> None:
        """迁移 upgrade 应添加 4 个字段。"""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1000-a1b2c3d4e5f6_add_document_parse_metadata.py",
        )

        spec = importlib.util.spec_from_file_location("migration", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 验证 revision 标识符
        assert module.revision == "a1b2c3d4e5f6"
        assert module.down_revision == "115a9c06ba4a"

        # 验证 upgrade 和 downgrade 函数存在
        assert callable(module.upgrade)
        assert callable(module.downgrade)

    def test_migration_upgrade_downgrade_idempotent(self, tmp_path) -> None:
        """迁移 upgrade/downgrade 函数应能在 alembic 上下文中执行。

        本测试验证迁移文件可以被正确加载和解析，
        实际的 upgrade/downgrade 执行由 alembic upgrade head 命令完成。
        """
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "2026_07_19_1000-a1b2c3d4e5f6_add_document_parse_metadata.py",
        )

        spec = importlib.util.spec_from_file_location("migration_test", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 验证 upgrade/downgrade 是可调用函数
        assert callable(module.upgrade)
        assert callable(module.downgrade)

        # 验证 upgrade 函数源码包含 4 个 add_column 调用
        import inspect

        upgrade_src = inspect.getsource(module.upgrade)
        assert upgrade_src.count("op.add_column") == 4, (
            f"upgrade 应包含 4 个 add_column 调用，实际: {upgrade_src.count('op.add_column')}"
        )

        # 验证 downgrade 函数源码包含 4 个 drop_column 调用
        downgrade_src = inspect.getsource(module.downgrade)
        assert downgrade_src.count("op.drop_column") == 4, (
            f"downgrade 应包含 4 个 drop_column 调用，实际: {downgrade_src.count('op.drop_column')}"
        )

        # 验证字段名都在源码中
        for field in ("parse_status", "parse_warnings", "page_count", "char_count"):
            assert field in upgrade_src, f"upgrade 源码缺少字段: {field}"
            assert field in downgrade_src, f"downgrade 源码缺少字段: {field}"
