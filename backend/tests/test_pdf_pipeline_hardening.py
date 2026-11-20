"""PDF 解析链路加固测试 — task.md A1~A9 的验收闸门。

原则：PDF 一律用 fitz 真实生成（不 mock fitz），DB 走真实 PostgreSQL
（conftest db_session），只 mock MinIO / VLM 等外部服务边界。
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fitz
import pytest

from app.config import get_settings
from app.document.pdf_parser import PDFParser

# ----------------------------------------------------------------------
# 真实 PDF 构造工具
# ----------------------------------------------------------------------


def _make_text_pdf(path: str, text: str = "你好世界，这是真实 PDF 文本。") -> str:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontname="china-s")
    doc.save(path)
    doc.close()
    return path


def _make_empty_pdf(path: str) -> str:
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    return path


def _make_encrypted_pdf(path: str) -> str:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "机密内容", fontname="china-s")
    doc.save(
        path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    doc.close()
    return path


def _png_bytes(size: int = 120) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size))
    pix.clear_with(128)
    return pix.tobytes("png")


def _make_image_pdf(path: str, with_text: str = "") -> str:
    doc = fitz.open()
    page = doc.new_page()
    if with_text:
        page.insert_text((72, 50), with_text, fontname="china-s")
    page.insert_image(fitz.Rect(72, 100, 400, 420), stream=_png_bytes())
    doc.save(path)
    doc.close()
    return path


def _make_scan_pdf(path: str, pages: int) -> str:
    doc = fitz.open()
    img = _png_bytes()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=img)
    doc.save(path)
    doc.close()
    return path


# ======================================================================
# A3. PDFParser 真实文档解析（close 后不得再访问 doc）
# ======================================================================


class TestA3RealPdfParse:
    @pytest.mark.asyncio
    async def test_parse_real_pdf_returns_text(self, tmp_path):
        path = _make_text_pdf(str(tmp_path / "real.pdf"))
        result = await PDFParser().parse(path)
        assert "你好世界" in result

    @pytest.mark.asyncio
    async def test_parse_real_multipage_pdf(self, tmp_path):
        path = str(tmp_path / "multi.pdf")
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page()
            page.insert_text((72, 100), f"第{i}页内容", fontname="china-s")
        doc.save(path)
        doc.close()
        result = await PDFParser().parse(path)
        for i in range(3):
            assert f"第{i}页内容" in result


# ======================================================================
# A4. 加密 PDF 必须显式失败，不得静默返回空
# ======================================================================


class TestA4EncryptedPdf:
    @pytest.mark.asyncio
    async def test_parser_raises_on_encrypted(self, tmp_path):
        path = _make_encrypted_pdf(str(tmp_path / "enc.pdf"))
        from app.document.pdf_parser import EncryptedPDFError

        with pytest.raises(EncryptedPDFError):
            await PDFParser().parse(path)

    @pytest.mark.asyncio
    async def test_parse_pdf_pipeline_raises_not_silent_empty(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_pdf

        path = _make_encrypted_pdf(str(tmp_path / "enc.pdf"))
        doc = SimpleNamespace(id=uuid.uuid4(), file_path=path, content_text=None)
        from app.document.pdf_parser import EncryptedPDFError

        with pytest.raises(EncryptedPDFError):
            await _parse_pdf(doc)


# ======================================================================
# A5. 栏检测：单栏缩进不得误判新栏；多栏保持左→右
# ======================================================================


class TestA5ColumnDetection:
    def test_indented_block_stays_in_single_column(self):
        from app.document.layout_analyzer import TextBlock, sort_reading_order

        blocks = [
            TextBlock(bbox=(72, 100, 280, 115), text="p1"),
            TextBlock(bbox=(72, 120, 280, 135), text="p2"),
            TextBlock(bbox=(102, 140, 270, 155), text="quote"),
            TextBlock(bbox=(72, 160, 280, 175), text="p3"),
            TextBlock(bbox=(72, 180, 280, 195), text="p4"),
        ]
        ordered = sort_reading_order(blocks, 595.0)
        assert [b.text for b in ordered] == ["p1", "p2", "quote", "p3", "p4"]

    def test_real_single_column_indented_pdf_order(self, tmp_path):
        from app.document.layout_analyzer import extract_page_text_ordered

        path = str(tmp_path / "indent.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(72, 100, 500, 118), "第一段正文内容", fontname="china-s"
        )
        page.insert_textbox(
            fitz.Rect(72, 120, 500, 138), "第二段正文内容", fontname="china-s"
        )
        page.insert_textbox(
            fitz.Rect(110, 140, 500, 158), "缩进引用块内容", fontname="china-s"
        )
        page.insert_textbox(
            fitz.Rect(72, 160, 500, 178), "第三段正文内容", fontname="china-s"
        )
        page.insert_textbox(
            fitz.Rect(72, 180, 500, 198), "第四段正文内容", fontname="china-s"
        )
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            text = extract_page_text_ordered(doc[0].get_text("dict"), doc[0].rect.width)
        finally:
            doc.close()
        assert (
            text.index("第一段")
            < text.index("第二段")
            < text.index("缩进引用")
            < text.index("第三段")
            < text.index("第四段")
        )

    def test_real_two_column_pdf_left_before_right(self, tmp_path):
        from app.document.layout_analyzer import extract_page_text_ordered

        path = str(tmp_path / "twocol.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(72, 100, 280, 400),
            "左栏第一行\n左栏第二行\n左栏第三行",
            fontname="china-s",
        )
        page.insert_textbox(
            fitz.Rect(330, 100, 540, 400),
            "右栏第一行\n右栏第二行\n右栏第三行",
            fontname="china-s",
        )
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            text = extract_page_text_ordered(doc[0].get_text("dict"), doc[0].rect.width)
        finally:
            doc.close()
        assert text.index("左栏第三行") < text.index("右栏第一行")


# ======================================================================
# A6. chunker：前导内容保留 + 表格原子性
# ======================================================================


class TestA6Chunker:
    def test_markdown_preamble_preserved(self):
        from app.rag.chunker import SemanticChunker

        content = (
            "这是首个标题之前的前导说明文字。\n\n# 标题一\n内容A\n\n## 标题二\n内容B"
        )
        chunks = SemanticChunker._split_markdown(content, "doc-1")
        assert chunks, "结构化分块不应为空"
        assert any("前导说明文字" in c.content for c in chunks)

    def test_html_preamble_preserved(self):
        from app.rag.chunker import SemanticChunker

        content = "<p>首个标题之前的前导说明。</p><h1>标题一</h1><p>内容A</p><h2>标题二</h2><p>内容B</p>"
        chunks = SemanticChunker._split_html(content, "doc-1")
        assert chunks
        assert any("前导说明" in c.content for c in chunks)

    def test_oversized_table_not_cut_mid_table(self):
        from app.rag.chunker import SemanticChunker

        rows = ["<tr><th>列A</th><th>列B</th></tr>"]
        for i in range(300):
            rows.append(f"<tr><td>数据行{i}甲</td><td>数据行{i}乙</td></tr>")
        table = "<table>\n" + "\n".join(rows) + "\n</table>"
        text = f"表格前的说明文字。\n{table}\n表格后的说明文字。"
        parts = SemanticChunker._split_by_tokens(text, 800)
        assert len(parts) > 1, "超长文本应被切分"
        for p in parts:
            opens = p.count("<table")
            closes = p.count("</table>")
            assert opens == closes, f"表格被拦腰切断: {p[:80]}..."
            if opens:
                assert "<th>列A</th>" in p, "切分后的表格片段必须保留表头"
        joined = "".join(parts)
        assert "数据行299甲" in joined and "表格后的说明文字" in joined

    def test_small_table_stays_atomic(self):
        from app.rag.chunker import SemanticChunker

        table = "<table><tr><th>H</th></tr><tr><td>V</td></tr></table>"
        text = "前文。" * 400 + "\n" + table + "\n" + "后文。" * 400
        parts = SemanticChunker._split_by_tokens(text, 800)
        table_parts = [p for p in parts if "<table" in p]
        assert len(table_parts) == 1
        assert table_parts[0].count("<table") == table_parts[0].count("</table>") == 1
        assert "<td>V</td>" in table_parts[0]


# ======================================================================
# A7. 图片命名空间：doc_id 贯穿，不同文档 key 不冲突
# ======================================================================


class TestA7ImageNamespace:
    @pytest.mark.asyncio
    async def test_two_docs_image_keys_do_not_collide(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "PDF_IMAGE_UPLOAD_ENABLED", True, raising=False)
        monkeypatch.setattr(
            settings, "PDF_IMAGE_EXTRACTION_ENABLED", True, raising=False
        )
        monkeypatch.setattr(PDFParser, "_vlm_describe", AsyncMock(return_value=""))

        captured: list[str] = []

        async def fake_upload(bucket, object_name, data, content_type):
            captured.append(object_name)
            return f"minio://{bucket}/{object_name}"

        monkeypatch.setattr("app.utils.minio_client.upload_file", fake_upload)

        path = _make_image_pdf(str(tmp_path / "img.pdf"))
        await PDFParser().parse(path, doc_id="doc-a")
        await PDFParser().parse(path, doc_id="doc-b")

        assert len(captured) == 2
        assert captured[0].startswith("doc-a/")
        assert captured[1].startswith("doc-b/")
        assert captured[0] != captured[1]

    @pytest.mark.asyncio
    async def test_pipeline_passes_real_doc_id(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "DOCLING_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "PDF_IMAGE_UPLOAD_ENABLED", True, raising=False)
        monkeypatch.setattr(PDFParser, "_vlm_describe", AsyncMock(return_value=""))

        captured: list[str] = []

        async def fake_upload(bucket, object_name, data, content_type):
            captured.append(object_name)
            return f"minio://{bucket}/{object_name}"

        monkeypatch.setattr("app.utils.minio_client.upload_file", fake_upload)

        from tasks.document_tasks import _parse_pdf

        doc_id = uuid.uuid4()
        path = _make_image_pdf(str(tmp_path / "img.pdf"))
        doc = SimpleNamespace(id=doc_id, file_path=path, content_text=None)
        await _parse_pdf(doc)
        assert captured and captured[0].startswith(f"{doc_id}/")


# ======================================================================
# A8. 混合扫描页触发 OCR + 超限 warning
# ======================================================================


class TestA8ScanOcr:
    @pytest.mark.asyncio
    async def test_mixed_page_triggers_ocr(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "PDF_SCAN_OCR_ENABLED", True, raising=False)
        ocr_mock = AsyncMock(return_value="整页 OCR 全文")
        monkeypatch.setattr(PDFParser, "_scan_page_ocr", ocr_mock)

        path = _make_image_pdf(str(tmp_path / "mixed.pdf"), with_text="封面")
        parser = PDFParser()
        result = await parser.parse(path)
        assert ocr_mock.await_count >= 1, "含少量文本层的混合扫描页应触发 OCR"
        assert "整页 OCR 全文" in result

    @pytest.mark.asyncio
    async def test_over_limit_pages_produce_warning(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "PDF_SCAN_OCR_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "PDF_SCAN_OCR_MAX_PAGES", 20, raising=False)
        ocr_mock = AsyncMock(return_value="")
        monkeypatch.setattr(PDFParser, "_scan_page_ocr", ocr_mock)

        path = _make_scan_pdf(str(tmp_path / "scan21.pdf"), pages=21)
        parser = PDFParser()
        await parser.parse(path)
        warnings = parser.last_parse_warnings
        assert any(
            "21" in w and "OCR" in w for w in warnings
        ), f"超出 OCR 上限的第 21 页必须产生 warning，实际: {warnings}"
        assert ocr_mock.await_count == 20, "超限页不应再渲染/调用 OCR"

    @pytest.mark.asyncio
    async def test_pipeline_propagates_scan_warnings(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "DOCLING_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "PDF_SCAN_OCR_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "PDF_SCAN_OCR_MAX_PAGES", 20, raising=False)
        monkeypatch.setattr(PDFParser, "_scan_page_ocr", AsyncMock(return_value=""))

        from tasks.document_tasks import _parse_pdf

        path = _make_scan_pdf(str(tmp_path / "scan21.pdf"), pages=21)
        doc = SimpleNamespace(id=uuid.uuid4(), file_path=path, content_text=None)
        warnings: list[str] = []
        await _parse_pdf(doc, warnings=warnings)
        assert any("OCR" in w for w in warnings)


# ======================================================================
# A9. Docling 同步 convert 必须离开事件循环线程
# ======================================================================


class TestA9DoclingThread:
    @pytest.mark.asyncio
    async def test_convert_runs_off_event_loop(self, monkeypatch):
        from app.document.docling_parser import DoclingParser

        loop_thread = threading.get_ident()
        seen: list[int] = []

        class FakeResult:
            class document:
                @staticmethod
                def export_to_html():
                    return "<h1>标题</h1><p>内容</p>"

        class FakeConverter:
            def convert(self, path):
                seen.append(threading.get_ident())
                return FakeResult()

        parser = DoclingParser()
        monkeypatch.setattr(parser, "_get_converter", lambda: FakeConverter())
        monkeypatch.setattr(parser, "_should_use_mineru", lambda path: False)

        html = await parser.parse("dummy.pdf")
        assert "<h1>标题</h1>" in html
        assert seen and seen[0] != loop_thread, "converter.convert 阻塞了事件循环线程"

    @pytest.mark.asyncio
    async def test_parse_raw_runs_off_event_loop(self, monkeypatch):
        from app.document.docling_parser import DoclingParser

        loop_thread = threading.get_ident()
        seen: list[int] = []

        class FakeConverter:
            def convert(self, path):
                seen.append(threading.get_ident())
                return object()

        parser = DoclingParser()
        monkeypatch.setattr(parser, "_get_converter", lambda: FakeConverter())
        result = await parser._parse_raw("dummy.pdf")
        assert result is not None
        assert seen and seen[0] != loop_thread


# ======================================================================
# A1 / A2. chord 管线集成（真实 PostgreSQL）
# ======================================================================


async def _seed_document(db_session, file_path: str, tenant_id, content_text=None):
    from app.models.knowledge import Document, KnowledgeBase
    from app.models.user import User

    user = User(
        email=f"pdf-test-{uuid.uuid4()}@ekb.local",
        hashed_password="x",
        name="PDF测试",
    )
    db_session.add(user)
    await db_session.flush()
    kb = KnowledgeBase(name="PDF测试库", owner_id=user.id, tenant_id=tenant_id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(
        kb_id=kb.id,
        title="测试PDF",
        owner_id=user.id,
        doc_type="pdf",
        file_path=file_path,
        tenant_id=tenant_id,
        content_text=content_text,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


class TestA1A2PipelineIntegration:
    @pytest.mark.asyncio
    async def test_parse_and_chunk_with_explicit_tenant(
        self, db_session, tmp_path, monkeypatch
    ):
        """A1：真实调用 _parse_and_chunk_async（带 tenant_id），不得 NameError。"""
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_and_chunk_async

        tenant_id = uuid.uuid4()
        path = _make_text_pdf(str(tmp_path / "ok.pdf"), "真实管线解析文本")
        doc = await _seed_document(db_session, path, tenant_id)

        result = await _parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        assert result.get("status") != "failed", result
        assert result["chunk_objects"], "应产出分块"
        joined = "".join(c.content for c in result["chunk_objects"])
        assert "真实管线解析文本" in joined

    @pytest.mark.asyncio
    async def test_parse_and_chunk_tenant_lookup_fallback(
        self, db_session, tmp_path, monkeypatch
    ):
        """A1：历史队列消息不带 tenant_id 时按 doc_id 反查也能跑通。"""
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_and_chunk_async

        tenant_id = uuid.uuid4()
        path = _make_text_pdf(str(tmp_path / "ok.pdf"), "反查租户路径文本")
        doc = await _seed_document(db_session, path, tenant_id)

        result = await _parse_and_chunk_async(str(doc.id))
        assert result.get("status") != "failed", result
        assert result["chunk_objects"]

    @pytest.mark.asyncio
    async def test_minio_path_downloaded_before_parse(
        self, db_session, tmp_path, monkeypatch
    ):
        """A2：minio:// 路径必须先下载再解析，产出非空文本。"""
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_and_chunk_async

        pdf_bytes = fitz.open()
        page = pdf_bytes.new_page()
        page.insert_text((72, 100), "MinIO下载后解析的文本", fontname="china-s")
        raw = pdf_bytes.tobytes()
        pdf_bytes.close()

        async def fake_download(bucket, object_name, dest_path):
            assert bucket == "ekb-documents"
            with open(dest_path, "wb") as f:
                f.write(raw)

        monkeypatch.setattr(
            "app.utils.minio_client.download_file_to_path", fake_download
        )

        tenant_id = uuid.uuid4()
        doc = await _seed_document(
            db_session, "minio://ekb-documents/kb/test.pdf", tenant_id
        )
        result = await _parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        assert result.get("status") != "failed", result
        joined = "".join(c.content for c in result["chunk_objects"])
        assert "MinIO下载后解析的文本" in joined

        await db_session.refresh(doc)
        assert doc.parse_status in ("parsed", "partial")
        assert (
            doc.file_path == "minio://ekb-documents/kb/test.pdf"
        ), "临时路径不得写回 DB"

    @pytest.mark.asyncio
    async def test_empty_content_must_not_be_parsed(
        self, db_session, tmp_path, monkeypatch
    ):
        """A2 反向闸门：解析结果为空时 parse_status 不得为 parsed。"""
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        monkeypatch.setattr(PDFParser, "_scan_page_ocr", AsyncMock(return_value=""))
        from tasks.document_tasks import _parse_and_chunk_async

        tenant_id = uuid.uuid4()
        path = _make_empty_pdf(str(tmp_path / "empty.pdf"))
        doc = await _seed_document(db_session, path, tenant_id)

        result = await _parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        await db_session.refresh(doc)
        assert doc.parse_status != "parsed", "空内容不得标 parsed"
        assert doc.parse_status == "failed"
        assert result.get("status") == "failed"
        assert doc.parse_warnings, "必须带用户可见 warning"

    @pytest.mark.asyncio
    async def test_encrypted_pdf_pipeline_failed_with_warning(
        self, db_session, tmp_path, monkeypatch
    ):
        """A4 管线级：加密 PDF → failed + 用户可见「加密」warning。"""
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_and_chunk_async

        tenant_id = uuid.uuid4()
        path = _make_encrypted_pdf(str(tmp_path / "enc.pdf"))
        doc = await _seed_document(db_session, path, tenant_id)

        result = await _parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        await db_session.refresh(doc)
        assert result.get("status") == "failed"
        assert doc.parse_status == "failed"
        assert any("加密" in w for w in (doc.parse_warnings or []))
