"""PDF 解析链路 P2 加固测试 — task.md A10~A17 的验收闸门。

原则与 A1~A9 闸门一致：PDF 一律用 fitz 真实生成（不 mock fitz），
DB 走真实 PostgreSQL（conftest db_session），只 mock MinIO / VLM 边界。
A15 的闸门在 tests/test_sniff.py::test_pdf_tolerates_leading_garbage。
"""

from __future__ import annotations

import glob
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fitz
import httpx
import pytest

from app.config import get_settings
from app.document.pdf_parser import PDFParser

from tests.test_pdf_pipeline_hardening import (
    _make_image_pdf,
    _make_text_pdf,
    _png_bytes,
    _seed_document,
)


# ----------------------------------------------------------------------
# P2 专用真实 PDF 构造工具
# ----------------------------------------------------------------------


def _make_hf_pdf(path: str, pages: int = 4) -> str:
    """每页恒定页眉（顶部）+ 页脚（底部）+ 中部重复正文 + 唯一正文。"""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()  # 默认 612x792
        page.insert_text((72, 30), "机密文件禁止外传", fontname="china-s")
        page.insert_text((72, 770), "内部资料注意保存", fontname="china-s")
        page.insert_text((72, 400), "每页重复的中部正文段落", fontname="china-s")
        page.insert_text((72, 450), f"第{i}页唯一正文内容", fontname="china-s")
    doc.save(path)
    doc.close()
    return path


def _make_table_pdf(path: str) -> str:
    """画线 2x2 表格 + 单元格文字 + 表格外正文。"""
    doc = fitz.open()
    page = doc.new_page()
    for y in (100, 150, 200):
        page.draw_line(fitz.Point(72, y), fitz.Point(372, y), width=1)
    for x in (72, 222, 372):
        page.draw_line(fitz.Point(x, 100), fitz.Point(x, 200), width=1)
    page.insert_text((80, 130), "单元格甲", fontname="china-s")
    page.insert_text((230, 130), "单元格乙", fontname="china-s")
    page.insert_text((80, 180), "单元格丙", fontname="china-s")
    page.insert_text((230, 180), "单元格丁", fontname="china-s")
    page.insert_text((72, 400), "表格外正文必须保留", fontname="china-s")
    doc.save(path)
    doc.close()
    return path


def _make_multipage_pdf(path: str, pages: int) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"第{i}页正文", fontname="china-s")
    doc.save(path)
    doc.close()
    return path


# ======================================================================
# A10. 页眉页脚跨页重复块剔除
# ======================================================================


class TestA10HeaderFooterRemoval:
    @pytest.mark.asyncio
    async def test_repeated_header_footer_removed(self, tmp_path):
        path = _make_hf_pdf(str(tmp_path / "hf.pdf"), pages=4)
        result = await PDFParser().parse(path)
        assert "机密文件禁止外传" not in result, "跨页页眉应被剔除"
        assert "内部资料注意保存" not in result, "跨页页脚应被剔除"
        for i in range(4):
            assert f"第{i}页唯一正文内容" in result

    @pytest.mark.asyncio
    async def test_repeated_midpage_text_kept(self, tmp_path):
        """正向证人：中部重复文本不是页眉页脚，不得误删。"""
        path = _make_hf_pdf(str(tmp_path / "hf.pdf"), pages=4)
        result = await PDFParser().parse(path)
        assert "每页重复的中部正文段落" in result

    @pytest.mark.asyncio
    async def test_below_threshold_pages_kept(self, tmp_path):
        """正向证人：页数 < 阈值（3）时不启用剔除，页眉应保留。"""
        path = _make_hf_pdf(str(tmp_path / "hf2.pdf"), pages=2)
        result = await PDFParser().parse(path)
        assert "机密文件禁止外传" in result

    @pytest.mark.asyncio
    async def test_disabled_by_config(self, tmp_path, monkeypatch):
        """正向证人：开关关闭时页眉应保留。"""
        monkeypatch.setattr(
            get_settings(), "PDF_HEADER_FOOTER_REMOVAL_ENABLED", False, raising=False
        )
        path = _make_hf_pdf(str(tmp_path / "hf.pdf"), pages=4)
        result = await PDFParser().parse(path)
        assert "机密文件禁止外传" in result

    def test_detect_keys_unit(self, tmp_path):
        from app.document.layout_analyzer import detect_header_footer_keys

        path = _make_hf_pdf(str(tmp_path / "hf.pdf"), pages=4)
        with fitz.open(path) as doc:
            page_dicts = [doc[i].get_text("dict") for i in range(len(doc))]
        keys = detect_header_footer_keys(page_dicts, min_pages=3)
        key_texts = {k[0] for k in keys}
        assert "机密文件禁止外传" in key_texts
        assert "内部资料注意保存" in key_texts
        assert "每页重复的中部正文段落" not in key_texts
        # 页数不足 / 阈值非法 → 空集
        assert detect_header_footer_keys(page_dicts[:2], min_pages=3) == set()
        assert detect_header_footer_keys(page_dicts, min_pages=1) == set()


# ======================================================================
# A13. 表格区域文本按 bbox 去重
# ======================================================================


class TestA13TableTextDedup:
    @pytest.mark.asyncio
    async def test_cell_text_appears_exactly_once(self, tmp_path):
        path = _make_table_pdf(str(tmp_path / "table.pdf"))
        result = await PDFParser().parse(path)
        assert "<table>" in result, "表格应以 HTML 结构输出"
        for cell in ("单元格甲", "单元格乙", "单元格丙", "单元格丁"):
            assert result.count(cell) == 1, f"{cell} 应只出现一次（表格内），实际 {result.count(cell)}"

    @pytest.mark.asyncio
    async def test_text_outside_table_kept(self, tmp_path):
        """正向证人：表格外正文不得被 bbox 剔除误伤。"""
        path = _make_table_pdf(str(tmp_path / "table.pdf"))
        result = await PDFParser().parse(path)
        assert "表格外正文必须保留" in result


# ======================================================================
# A12. 真实页数元数据落库
# ======================================================================


class TestA12RealPageCount:
    @pytest.mark.asyncio
    async def test_parser_exposes_last_page_count(self, tmp_path):
        path = _make_multipage_pdf(str(tmp_path / "p3.pdf"), pages=3)
        parser = PDFParser()
        await parser.parse(path)
        assert parser.last_page_count == 3

    @pytest.mark.asyncio
    async def test_pipeline_persists_real_page_count(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        from tasks.document_tasks import _parse_and_chunk_async

        tenant_id = uuid.uuid4()
        path = _make_multipage_pdf(str(tmp_path / "p3.pdf"), pages=3)
        doc = await _seed_document(db_session, path, tenant_id)

        result = await _parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        assert result.get("status") != "failed", result
        await db_session.refresh(doc)
        assert doc.page_count == 3, "page_count 应为真实页数而非 <h2> 数推断"


# ======================================================================
# A14. 重索引按源文件哈希判定重解析
# ======================================================================


class TestA14ReindexHash:
    @pytest.mark.asyncio
    async def test_changed_file_reparsed_and_hash_updated(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        import tasks.document_tasks as dt

        tenant_id = uuid.uuid4()
        path = _make_text_pdf(str(tmp_path / "v1.pdf"), "第一版文件内容")
        doc = await _seed_document(db_session, path, tenant_id, content_text="旧文本")

        calls = {"n": 0}
        orig_dispatch = dt._parse_document_dispatch

        async def spy_dispatch(d, warnings=None):
            calls["n"] += 1
            return await orig_dispatch(d, warnings)

        monkeypatch.setattr(dt, "_parse_document_dispatch", spy_dispatch)

        # 首次：有旧文本但无哈希 → 必须重解析
        text1, _ = await dt._parse_document(doc)
        assert "第一版文件内容" in text1
        assert calls["n"] == 1
        assert doc.file_hash == dt._sha256_file(path)

        # 文件未变 → 早退，不再解析
        text2, _ = await dt._parse_document(doc)
        assert calls["n"] == 1, "文件未变时重索引不得重解析"
        assert text2 == doc.content_text

        # 换文件 → 哈希不一致，必须重解析且哈希更新
        old_hash = doc.file_hash
        _make_text_pdf(path, "第二版全新文件内容")
        text3, _ = await dt._parse_document(doc)
        assert calls["n"] == 2, "文件已变时必须重解析"
        assert "第二版全新文件内容" in text3
        assert doc.file_hash != old_hash
        assert doc.file_hash == dt._sha256_file(path)


# ======================================================================
# A16. fitz 兜底句柄泄漏（独立验收）
# ======================================================================


class TestA16FitzFallbackClose:
    @pytest.mark.asyncio
    async def test_fallback_closes_handle_on_page_error(self, tmp_path, monkeypatch):
        """兜底路径中 get_text 抛异常，finally 仍必须 close。"""
        import tasks.document_tasks as dt

        path = _make_multipage_pdf(str(tmp_path / "fb.pdf"), pages=2)
        doc = SimpleNamespace(file_path=path, content_text="", page_count=0)

        monkeypatch.setattr("app.document.get_parser", lambda _t: None)

        closed = {"n": 0}
        orig_close = fitz.Document.close

        def spy_close(self):
            closed["n"] += 1
            return orig_close(self)

        monkeypatch.setattr(fitz.Document, "close", spy_close)

        def boom(_self):
            raise RuntimeError("模拟页读取失败")

        monkeypatch.setattr(fitz.Page, "get_text", boom)

        await dt._parse_pdf(doc)
        # 正向证人：兜底确实进入（页数在异常前已写入），close 必然被调
        assert doc.page_count == 2, "fitz 兜底未实际执行"
        assert closed["n"] >= 1, "异常路径下 fitz 句柄未关闭"


# ======================================================================
# A11. 图片链路：presigned URL + legacy 跨模态 + 去二次解析
# ======================================================================


class TestA11ImagePipeline:
    def test_extract_pdf_images_real(self, tmp_path):
        from app.document.image_storage import extract_pdf_images

        path = _make_image_pdf(str(tmp_path / "img.pdf"), with_text="含图文档")
        images = extract_pdf_images(path)
        assert len(images) == 1
        assert images[0][:8] == b"\x89PNG\r\n\x1a\n"

    def test_extract_pdf_images_small_filtered(self, tmp_path):
        from app.document.image_storage import extract_pdf_images

        path = str(tmp_path / "small.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_image(fitz.Rect(72, 100, 82, 110), stream=_png_bytes(size=10))
        doc.save(path)
        doc.close()
        assert extract_pdf_images(path) == [], "10px 小图应被过滤"

    @pytest.mark.asyncio
    async def test_cross_modal_no_second_full_parse(self, tmp_path, monkeypatch):
        """legacy PDF 路径图片接入跨模态，且不得再调 _parse_raw 二次全量解析。"""
        import tasks.document_tasks as dt
        from app.document.docling_parser import DoclingParser

        monkeypatch.setattr(get_settings(), "CROSS_MODAL_ENABLED", True, raising=False)
        monkeypatch.setattr(
            DoclingParser,
            "_parse_raw",
            AsyncMock(side_effect=AssertionError("不得对 PDF 二次全量解析")),
        )
        monkeypatch.setattr(
            "app.vlm.provider.get_vision_provider",
            MagicMock(side_effect=ImportError("VLM 不可用")),
        )

        path = _make_image_pdf(str(tmp_path / "img.pdf"), with_text="含图文档")
        doc = SimpleNamespace(file_path=path, id="doc-x")
        images = await dt._extract_images_for_cross_modal(doc, "pdf")
        assert len(images) == 1
        img_bytes, desc = images[0]
        assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert desc == "[图片内容]", "VLM 不可用时应有占位描述"

    @pytest.mark.asyncio
    async def test_resolve_minio_urls(self, monkeypatch):
        from app.document.image_storage import resolve_minio_image_urls

        sign = AsyncMock(return_value="https://signed.example/x")
        monkeypatch.setattr("app.utils.minio_client.get_presigned_url", sign)
        text = (
            '前缀 <img src="minio://bkt/a/page1_img0.png"/> '
            "minio://bkt/a/page1_img0.png 尾部 minio://bkt/a/page2_img1.jpg"
        )
        out = await resolve_minio_image_urls(text)
        assert "minio://" not in out
        assert out.count("https://signed.example/x") == 3
        assert sign.await_count == 2, "相同地址应去重只签一次"
        assert "前缀" in out and "尾部" in out

    @pytest.mark.asyncio
    async def test_resolve_minio_urls_failure_keeps_original(self, monkeypatch):
        """正向证人：签名失败保留原地址，不阻断响应。"""
        from app.document.image_storage import resolve_minio_image_urls

        monkeypatch.setattr(
            "app.utils.minio_client.get_presigned_url",
            AsyncMock(side_effect=RuntimeError("minio  down")),
        )
        text = "minio://bkt/a/x.png"
        assert await resolve_minio_image_urls(text) == text

    @pytest.mark.asyncio
    async def test_get_presigned_url_params(self, monkeypatch):
        from app.utils import minio_client

        client = MagicMock()
        client.generate_presigned_url.return_value = "https://signed"
        monkeypatch.setattr(minio_client, "_get_client", lambda: client)
        url = await minio_client.get_presigned_url("bkt", "a/x.png", expires_in=120)
        assert url == "https://signed"
        client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "bkt", "Key": "a/x.png"},
            ExpiresIn=120,
        )

    @pytest.mark.asyncio
    async def test_api_converts_minio_urls_on_output(self, db_session, monkeypatch):
        """GET /documents/{id} 输出前 minio:// → presigned；存储层保留原址。"""
        from app.models.knowledge import Document, KnowledgeBase
        from app.models.user import User

        owner = User(
            email=f"p2-api-{uuid.uuid4()}@ekb.local",
            hashed_password="x",
            name="P2",
            role="editor",
            clearance_level="secret",
        )
        db_session.add(owner)
        await db_session.flush()
        kb = KnowledgeBase(name="P2库", visibility="public", owner_id=owner.id)
        db_session.add(kb)
        await db_session.flush()
        raw = '正文 <img src="minio://bkt/d/page1_img0.png"/>'
        doc = Document(
            kb_id=kb.id,
            title="P2图片文档",
            content_text=raw,
            content_html=raw,
            doc_type="md",
            status="draft",
            owner_id=owner.id,
            classification="internal",
        )
        db_session.add(doc)
        await db_session.commit()

        monkeypatch.setattr(
            "app.utils.minio_client.get_presigned_url",
            AsyncMock(return_value="https://signed.example/img"),
        )

        from app.database import get_db_session
        from app.deps import get_current_active_user
        from app.main import app

        async def override_user():
            return owner

        async def override_db():
            yield db_session

        app.dependency_overrides[get_current_active_user] = override_user
        app.dependency_overrides[get_db_session] = override_db
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://p2-test",
            ) as client:
                resp = await client.get(f"/api/v1/documents/{doc.id}")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert "minio://" not in data["content_text"]
        assert "https://signed.example/img" in data["content_text"]
        assert "minio://" not in data["content_html"]
        # 存储层不被污染
        await db_session.refresh(doc)
        assert doc.content_text == raw


# ======================================================================
# A17. 存量真实 PDF 样例接入测试（test_documents/pdf/ 20 个）
# ======================================================================

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_PDFS = sorted(glob.glob(str(_REPO_ROOT / "test_documents" / "pdf" / "*.pdf")))


class TestA17RealSamples:
    def test_sample_dir_present(self):
        assert len(_REAL_PDFS) == 20, f"期望 20 个真实样例，实际 {len(_REAL_PDFS)}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pdf_path", _REAL_PDFS, ids=lambda p: os.path.basename(p))
    async def test_real_sample_parses(self, pdf_path):
        """实测基线（2026-11-18）：20 个样例均 4 页、正文 886~1217 字、零告警。"""
        parser = PDFParser()
        result = await parser.parse(pdf_path)
        assert len(result) > 200, f"{pdf_path} 解析文本过短: {len(result)}"
        assert parser.last_page_count == 4
        assert parser.last_parse_warnings == []