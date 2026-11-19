"""D7 解析开关租户级覆盖验收闸门 — task.md D7。

原则与 A/D 组一致：PDF 一律 fitz 真实生成，DB 走真实 PostgreSQL，
只 mock MinIO / VLM / Docling converter 边界。断言两侧不同源：
租户配置经生产 TenantService 写入 JSONB，读回用裸 SQL（不走 ORM
身份映射）；Docling 断言侧用真实 docling.datamodel pydantic 模型。

契约（app/core/parse_config.py）：
    - validate_parse_config — 白名单＋类型校验；
    - parse_overrides(ctx) / get_parse_settings() — 上下文覆盖，
      未配置回落全局 env（全局降为缺省值）；
    - TenantService.get_parse_config / update_parse_config —
      复用 modules 的 Tenant.settings JSONB 模式；
    - 管线入口按 doc.tenant_id 加载覆盖，解析器消费点改读 overlay。
"""

from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock

import fitz
import pytest
import sqlalchemy as sa

from app.config import get_settings
from app.document.pdf_parser import PDFParser

from tests.test_d6_asset_chunks import (
    _VLM_DESC,
    _make_asset_pdf,
    _mock_image_boundaries,
)
from tests.test_pdf_pipeline_hardening import _seed_document


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（既有环境勘误桶）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


async def _seed_tenant(db_session, tenant_id, settings=None):
    from app.models import Tenant

    tenant = Tenant(id=tenant_id, name=f"D7租户-{tenant_id}", settings=settings)
    db_session.add(tenant)
    await db_session.commit()
    return tenant


def _make_scanned_pdf(path: str) -> str:
    """纯扫描页：整页图片、零文本层。"""
    from tests.test_pdf_pipeline_hardening import _png_bytes

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 612, 792), stream=_png_bytes(size=200))
    doc.save(path)
    doc.close()
    return path


# ======================================================================
# 1. 白名单校验
# ======================================================================


class TestD7Validation:
    def test_unknown_key_rejected(self):
        from app.core.parse_config import validate_parse_config

        with pytest.raises(ValueError):
            validate_parse_config({"DOCLING_ENABLED": False})

    def test_wrong_type_rejected(self):
        from app.core.parse_config import validate_parse_config

        with pytest.raises(ValueError):
            validate_parse_config({"PDF_SCAN_OCR_ENABLED": "yes"})
        with pytest.raises(ValueError):
            validate_parse_config({"DOCLING_TABLE_MODE": "TURBO"})

    def test_valid_config_normalized(self):
        from app.core.parse_config import validate_parse_config

        out = validate_parse_config(
            {"PDF_SCAN_OCR_ENABLED": False, "DOCLING_TABLE_MODE": "accurate"}
        )
        assert out == {"PDF_SCAN_OCR_ENABLED": False, "DOCLING_TABLE_MODE": "ACCURATE"}


# ======================================================================
# 2. TenantService JSONB 读写（复用 modules 模式）
# ======================================================================


class TestD7TenantService:
    async def test_update_persists_jsonb(self, db_session):
        from app.services.tenant_service import TenantService

        tid = uuid.uuid4()
        await _seed_tenant(db_session, tid, settings={"custom_key": "保留"})
        svc = TenantService(db_session)
        await svc.update_parse_config({"MINERU_ENABLED": True}, tenant_id=tid)
        await db_session.commit()

        # 裸 SQL 读回 — 不走 ORM 身份映射（断言两侧不同源）
        row = (
            await db_session.execute(
                sa.text("SELECT settings FROM tenants WHERE id = :id"),
                {"id": str(tid)},
            )
        ).one()
        assert row[0]["parse_config"] == {"MINERU_ENABLED": True}
        assert row[0]["custom_key"] == "保留", "不得覆盖 settings 其他键"
        assert await svc.get_parse_config(tenant_id=tid) == {"MINERU_ENABLED": True}

    async def test_unset_tenant_returns_empty(self, db_session):
        """正向证人：未配置租户回落空覆盖（全局 env 即缺省值）。"""
        from app.services.tenant_service import TenantService

        tid = uuid.uuid4()
        await _seed_tenant(db_session, tid)
        assert await TenantService(db_session).get_parse_config(tenant_id=tid) == {}


# ======================================================================
# 3. 上下文 overlay
# ======================================================================


class TestD7Overlay:
    def test_no_context_returns_global(self):
        from app.core.parse_config import get_parse_settings

        assert get_parse_settings() is get_settings()

    def test_override_wins_and_resets(self):
        from app.core.parse_config import get_parse_settings, parse_overrides

        with parse_overrides({"PDF_SCAN_OCR_ENABLED": False}):
            s = get_parse_settings()
            assert s.PDF_SCAN_OCR_ENABLED is False
            # 非覆盖键回落全局
            assert s.PDF_IMAGE_MAX_PER_DOC == get_settings().PDF_IMAGE_MAX_PER_DOC
        assert get_parse_settings() is get_settings(), "上下文退出后不得泄漏覆盖"


# ======================================================================
# 4. 同文档解析行为分叉（集成，真实 PG + fitz）
# ======================================================================


class TestD7ParseFork:
    async def test_vlm_description_fork(self, db_session, tmp_path, monkeypatch):
        """验收主闸：租户 A 关 VLM 描述、租户 B 开 → 同文档行为分叉。"""
        import tasks.document_tasks as dt
        from app.services.tenant_service import TenantService

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        _mock_image_boundaries(monkeypatch)

        ta, tb = uuid.uuid4(), uuid.uuid4()
        await _seed_tenant(db_session, ta)
        await _seed_tenant(db_session, tb)
        await TenantService(db_session).update_parse_config(
            {"PDF_IMAGE_EXTRACTION_ENABLED": False}, tenant_id=ta
        )
        await TenantService(db_session).update_parse_config(
            {"PDF_IMAGE_EXTRACTION_ENABLED": True}, tenant_id=tb
        )
        await db_session.commit()

        path = _make_asset_pdf(str(tmp_path / "fork.pdf"))
        doc_a = await _seed_document(db_session, path, ta)
        doc_b = await _seed_document(db_session, path, tb)

        ra = await dt._parse_and_chunk_async(str(doc_a.id), tenant_id=str(ta))
        rb = await dt._parse_and_chunk_async(str(doc_b.id), tenant_id=str(tb))
        assert ra.get("status") != "failed" and rb.get("status") != "failed"

        await db_session.refresh(doc_a)
        await db_session.refresh(doc_b)
        assert _VLM_DESC not in doc_a.content_text, "A 关 VLM 不得产出图片描述"
        assert "minio://" not in doc_a.content_text
        assert _VLM_DESC in doc_b.content_text, "B 开 VLM 必须产出图片描述"
        assert "minio://" in doc_b.content_text
        # 表格提取不受图片开关影响（两侧都保留）
        assert "指标甲" in doc_a.content_text and "指标甲" in doc_b.content_text

    async def test_unconfigured_tenant_falls_back_global(self, db_session, tmp_path, monkeypatch):
        """正向证人：未配置租户行为等于全局默认（图片描述开）。"""
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        _mock_image_boundaries(monkeypatch)

        tid = uuid.uuid4()
        await _seed_tenant(db_session, tid)
        path = _make_asset_pdf(str(tmp_path / "default.pdf"))
        doc = await _seed_document(db_session, path, tid)

        result = await dt._parse_and_chunk_async(str(doc.id), tenant_id=str(tid))
        assert result.get("status") != "failed", result
        await db_session.refresh(doc)
        assert _VLM_DESC in doc.content_text, "未配置租户应回落全局默认（开）"

    async def test_scan_ocr_fork(self, db_session, tmp_path, monkeypatch):
        """OCR 组：租户 A 关扫描 OCR、租户 B 开 → 纯扫描页行为分叉。"""
        import tasks.document_tasks as dt
        from app.services.tenant_service import TenantService

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        monkeypatch.setattr(
            get_settings(), "PDF_IMAGE_EXTRACTION_ENABLED", False, raising=False
        )
        monkeypatch.setattr(
            PDFParser, "_scan_page_ocr", AsyncMock(return_value="扫描识别出的文字")
        )

        ta, tb = uuid.uuid4(), uuid.uuid4()
        await _seed_tenant(db_session, ta)
        await _seed_tenant(db_session, tb)
        await TenantService(db_session).update_parse_config(
            {"PDF_SCAN_OCR_ENABLED": False}, tenant_id=ta
        )
        await db_session.commit()

        path = _make_scanned_pdf(str(tmp_path / "scan.pdf"))
        doc_a = await _seed_document(db_session, path, ta)
        doc_b = await _seed_document(db_session, path, tb)

        await dt._parse_and_chunk_async(str(doc_a.id), tenant_id=str(ta))
        await dt._parse_and_chunk_async(str(doc_b.id), tenant_id=str(tb))
        await db_session.refresh(doc_a)
        await db_session.refresh(doc_b)
        assert "扫描识别出的文字" not in (doc_a.content_text or "")
        assert "[扫描页 OCR]" not in (doc_a.content_text or "")
        assert "扫描识别出的文字" in doc_b.content_text

    async def test_no_cross_tenant_leak(self, db_session, tmp_path, monkeypatch):
        """先解析关闭租户的文档，再解析未配置租户的文档 — 覆盖不得泄漏。"""
        import tasks.document_tasks as dt
        from app.services.tenant_service import TenantService

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        _mock_image_boundaries(monkeypatch)

        ta, tc = uuid.uuid4(), uuid.uuid4()
        await _seed_tenant(db_session, ta)
        await _seed_tenant(db_session, tc)
        await TenantService(db_session).update_parse_config(
            {"PDF_IMAGE_EXTRACTION_ENABLED": False}, tenant_id=ta
        )
        await db_session.commit()

        path = _make_asset_pdf(str(tmp_path / "leak.pdf"))
        doc_a = await _seed_document(db_session, path, ta)
        doc_c = await _seed_document(db_session, path, tc)

        await dt._parse_and_chunk_async(str(doc_a.id), tenant_id=str(ta))
        await dt._parse_and_chunk_async(str(doc_c.id), tenant_id=str(tc))
        await db_session.refresh(doc_c)
        assert _VLM_DESC in doc_c.content_text, "前一租户的关闭覆盖泄漏到了后续解析"


# ======================================================================
# 5. Docling / MinerU 开关分叉（converter 边界替身，断言侧真实模型）
# ======================================================================


@pytest.fixture
def captured_converter(monkeypatch):
    captured: dict = {}

    class _StubFormatOption:
        def __init__(self, pipeline_options=None, **kwargs):
            self.pipeline_options = pipeline_options

    class _StubConverter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    stub = types.ModuleType("docling.document_converter")
    stub.DocumentConverter = _StubConverter
    stub.PdfFormatOption = _StubFormatOption
    monkeypatch.setitem(sys.modules, "docling.document_converter", stub)
    return captured


def _pdf_options(captured):
    from docling.datamodel.base_models import InputFormat

    fmt_opts = captured.get("format_options") or {}
    assert InputFormat.PDF in fmt_opts
    return fmt_opts[InputFormat.PDF].pipeline_options


class TestD7DoclingMineru:
    def test_table_mode_fork(self, captured_converter):
        from docling.datamodel.pipeline_options import TableFormerMode

        from app.core.parse_config import parse_overrides
        from app.document.docling_parser import DoclingParser

        with parse_overrides({"DOCLING_TABLE_MODE": "ACCURATE"}):
            DoclingParser()._get_converter()
        opts_b = _pdf_options(captured_converter)
        assert opts_b.table_structure_options.mode == TableFormerMode.ACCURATE

        with parse_overrides({"DOCLING_TABLE_MODE": "FAST"}):
            DoclingParser()._get_converter()
        opts_a = _pdf_options(captured_converter)
        assert opts_a.table_structure_options.mode == TableFormerMode.FAST

    def test_do_ocr_fork(self, captured_converter):
        from app.core.parse_config import parse_overrides
        from app.document.docling_parser import DoclingParser

        with parse_overrides({"DOCLING_DO_OCR": False}):
            DoclingParser()._get_converter()
        assert _pdf_options(captured_converter).do_ocr is False

        with parse_overrides({}):
            DoclingParser()._get_converter()
        assert _pdf_options(captured_converter).do_ocr is get_settings().DOCLING_DO_OCR

    def test_mineru_fork(self, tmp_path, monkeypatch):
        from app.core.parse_config import parse_overrides
        from app.document.docling_parser import DoclingParser

        monkeypatch.setattr(
            get_settings(), "MINERU_PYTHON", "/fake/mineru/venv/bin/python", raising=False
        )
        path = _make_scanned_pdf(str(tmp_path / "mineru.pdf"))
        parser = DoclingParser()

        with parse_overrides({"MINERU_ENABLED": True}):
            assert parser._should_use_mineru(path) is True
        with parse_overrides({"MINERU_ENABLED": False}):
            assert parser._should_use_mineru(path) is False
        # 正向证人：无覆盖时回落全局默认（关）
        assert parser._should_use_mineru(path) is False


# ======================================================================
# 6. 管理端点（复用 modules 端点模式）
# ======================================================================


class TestD7API:
    async def test_parse_config_endpoints(self, db_session):
        import httpx

        from app.models import User
        from app.database import get_db_session
        from app.deps import get_current_active_user
        from app.main import app

        tid = uuid.uuid4()
        await _seed_tenant(db_session, tid)
        admin = User(
            email=f"d7-admin-{uuid.uuid4()}@ekb.local",
            hashed_password="x",
            name="D7管理员",
            role="admin",
            tenant_id=tid,
        )
        editor = User(
            email=f"d7-editor-{uuid.uuid4()}@ekb.local",
            hashed_password="x",
            name="D7编辑",
            role="editor",
            tenant_id=tid,
        )
        db_session.add_all([admin, editor])
        await db_session.commit()

        async def override_db():
            yield db_session

        app.dependency_overrides[get_db_session] = override_db
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://d7-test",
                trust_env=False,
            ) as client:
                app.dependency_overrides[get_current_active_user] = lambda: editor
                resp = await client.put(
                    "/api/v1/tenants/parse-config",
                    json={"config": {"PDF_SCAN_OCR_ENABLED": False}},
                )
                assert resp.status_code == 403, "非管理员不得修改解析配置"

                app.dependency_overrides[get_current_active_user] = lambda: admin
                resp = await client.put(
                    "/api/v1/tenants/parse-config",
                    json={"config": {"PDF_SCAN_OCR_ENABLED": False}},
                )
                assert resp.status_code == 200, resp.text
                resp = await client.get("/api/v1/tenants/parse-config")
                assert resp.status_code == 200, resp.text
                assert resp.json()["data"]["parse_config"] == {
                    "PDF_SCAN_OCR_ENABLED": False
                }

                resp = await client.put(
                    "/api/v1/tenants/parse-config",
                    json={"config": {"NOT_A_KEY": True}},
                )
                assert resp.status_code == 400, "白名单外键必须 400"
        finally:
            app.dependency_overrides.clear()