"""D6 图片/表格资产化验收闸门 — task.md D6。

原则与 A 组/D 组一致：PDF 一律 fitz 真实生成，DB 走真实 PostgreSQL，
迁移在真实 PG 上执行生产 upgrade/downgrade，只 mock MinIO / VLM / 向量库 HTTP 边界。
断言两侧不同源：chunker 闸门的输入来自真实 PDFParser 输出（生产序列化器），
不手写镜像 HTML。
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import fitz
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.document.pdf_parser import PDFParser
from app.rag.chunker import Chunk, SemanticChunker, estimate_tokens
from tests.test_pdf_pipeline_hardening import (
    _make_text_pdf,
    _png_bytes,
    _seed_document,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
_VLM_DESC = "三层架构示意图：接入层、服务层、存储层"


# ----------------------------------------------------------------------
# 真实 PDF 构造：表格 + 图片 + 正文
# ----------------------------------------------------------------------


def _make_asset_pdf(path: str) -> str:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 50), "资产化测试文档正文", fontname="china-s")
    for y in (100, 150, 200):
        page.draw_line(fitz.Point(72, y), fitz.Point(372, y), width=1)
    for x in (72, 222, 372):
        page.draw_line(fitz.Point(x, 100), fitz.Point(x, 200), width=1)
    page.insert_text((80, 130), "指标甲", fontname="china-s")
    page.insert_text((230, 130), "指标乙", fontname="china-s")
    page.insert_text((80, 180), "数值丙", fontname="china-s")
    page.insert_text((230, 180), "数值丁", fontname="china-s")
    page.insert_image(fitz.Rect(72, 300, 400, 620), stream=_png_bytes())
    page.insert_text((72, 700), "表格与图片之外的结尾正文", fontname="china-s")
    doc.save(path)
    doc.close()
    return path


def _mock_image_boundaries(monkeypatch, desc: str = _VLM_DESC):
    """只 mock MinIO 上传与 VLM 描述两个外部边界。"""

    async def fake_upload(bucket, object_name, data, content_type):
        return f"minio://{bucket}/{object_name}"

    monkeypatch.setattr("app.utils.minio_client.upload_file", fake_upload)
    monkeypatch.setattr(PDFParser, "_vlm_describe", AsyncMock(return_value=desc))


async def _parse_asset_pdf(tmp_path, monkeypatch, desc: str = _VLM_DESC) -> str:
    _mock_image_boundaries(monkeypatch, desc)
    path = _make_asset_pdf(str(tmp_path / "asset.pdf"))
    return await PDFParser().parse(path, doc_id="d6-doc")


# ======================================================================
# 1. 图片上传默认常开
# ======================================================================


class TestD6UploadDefaults:
    def test_upload_defaults_true(self):
        settings = get_settings()
        assert settings.PDF_IMAGE_UPLOAD_ENABLED is True
        assert settings.DOCX_IMAGE_UPLOAD_ENABLED is True
        assert settings.PPTX_IMAGE_UPLOAD_ENABLED is True


# ======================================================================
# 2. DocumentChunk 资产字段
# ======================================================================


class TestD6ChunkModel:
    def test_chunk_model_asset_columns(self):
        from app.models.chunk import DocumentChunk

        cols = DocumentChunk.__table__.columns
        assert "chunk_type" in cols
        assert "asset_ref" in cols
        assert "parent_chunk_id" in cols
        assert cols["chunk_type"].nullable is False
        assert cols["asset_ref"].nullable is True
        assert cols["parent_chunk_id"].nullable is True


# ======================================================================
# 3. 迁移真实 upgrade / downgrade
# ======================================================================


def _load_migration(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def migration():
    hits = sorted(
        (BACKEND_DIR / "alembic" / "versions").glob("*add_chunk_asset_fields*.py")
    )
    assert hits, "缺少 D6 迁移（*add_chunk_asset_fields*.py）"
    return _load_migration("d6_chunk_asset_migration", hits[-1])


def _run_ops(sync_conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    ops = Operations(ctx)
    ops._install_proxy()
    try:
        fn()
    finally:
        ops._remove_proxy()


async def _seed_legacy_chunk_row(db_session, tmp_path) -> str:
    """模拟存量旧 schema：删掉资产列后插入一行旧分块。"""
    doc = await _seed_document(db_session, str(tmp_path / "legacy.pdf"), uuid.uuid4())
    for col in ("chunk_type", "asset_ref", "parent_chunk_id"):
        await db_session.execute(
            sa.text(f"ALTER TABLE document_chunks DROP COLUMN IF EXISTS {col}")
        )
    chunk_id = str(uuid.uuid4())
    await db_session.execute(
        sa.text(
            "INSERT INTO document_chunks (id, doc_id, kb_id, chunk_index, content_text, token_count) "
            "VALUES (:id, :doc, :kb, 0, '存量旧分块', 3)"
        ),
        {"id": chunk_id, "doc": str(doc.id), "kb": str(doc.kb_id)},
    )
    await db_session.commit()
    return chunk_id


class TestD6Migration:
    async def test_upgrade_adds_columns_and_backfills(
        self, db_session, tmp_path, migration
    ):
        chunk_id = await _seed_legacy_chunk_row(db_session, tmp_path)

        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: _run_ops(c, migration.upgrade))

            async with engine.begin() as conn:
                types = {
                    r[0]: r[1]
                    for r in (
                        await conn.execute(
                            sa.text(
                                "SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = 'document_chunks' "
                                "AND column_name IN ('chunk_type','asset_ref','parent_chunk_id')"
                            )
                        )
                    ).all()
                }
                assert types.get("chunk_type") == "character varying", types
                assert types.get("asset_ref") == "text", types
                assert types.get("parent_chunk_id") == "uuid", types

                row = (
                    await conn.execute(
                        sa.text(
                            "SELECT chunk_type, asset_ref, parent_chunk_id "
                            "FROM document_chunks WHERE id = :id"
                        ),
                        {"id": chunk_id},
                    )
                ).one()
                assert row[0] == "text", "存量行应回填 chunk_type='text'"
                assert row[1] is None and row[2] is None

                fk = (
                    await conn.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM pg_constraint "
                            "WHERE conrelid = 'document_chunks'::regclass "
                            "AND contype = 'f' AND conname = 'fk_document_chunks_parent'"
                        )
                    )
                ).scalar()
                assert fk == 1, "parent_chunk_id 自引用外键缺失"
        finally:
            await engine.dispose()

    async def test_downgrade_removes_columns(self, db_session, tmp_path, migration):
        await _seed_legacy_chunk_row(db_session, tmp_path)
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: _run_ops(c, migration.upgrade))
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: _run_ops(c, migration.downgrade))
            async with engine.begin() as conn:
                remaining = (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'document_chunks' "
                            "AND column_name IN ('chunk_type','asset_ref','parent_chunk_id')"
                        )
                    )
                ).all()
                assert remaining == [], f"downgrade 未删净资产列: {remaining}"
        finally:
            await engine.dispose()


# ======================================================================
# 4. chunker 资产抽取（输入 = 真实 PDFParser 输出）
# ======================================================================


class TestD6ChunkerAssets:
    async def test_table_asset_extracted(self, tmp_path, monkeypatch):
        text = await _parse_asset_pdf(tmp_path, monkeypatch)
        assert "<table" in text, "前置：真实解析应产出 HTML 表格"
        chunker = SemanticChunker()
        chunks = chunker.extract_asset_chunks(
            chunker.chunk(text, doc_type="pdf", doc_id="d6-doc"), "d6-doc"
        )
        tables = [c for c in chunks if c.chunk_type == "table"]
        assert (
            len(tables) == 1
        ), f"应恰有 1 个表格资产块: {[c.chunk_type for c in chunks]}"
        t = tables[0]
        assert t.asset_ref and "<table" in t.asset_ref
        assert "指标甲" in t.asset_ref and "数值丁" in t.asset_ref
        parent = next(c for c in chunks if c.id == t.parent_id)
        assert parent.chunk_type == "text"
        assert "<table" not in parent.content, "正文块不得重复含整表"
        assert "指标甲" not in parent.content

    async def test_image_asset_extracted(self, tmp_path, monkeypatch):
        text = await _parse_asset_pdf(tmp_path, monkeypatch)
        assert "minio://" in text, "前置：上传常开后正文应含 minio 图片地址"
        chunker = SemanticChunker()
        chunks = chunker.extract_asset_chunks(
            chunker.chunk(text, doc_type="pdf", doc_id="d6-doc"), "d6-doc"
        )
        images = [c for c in chunks if c.chunk_type == "image"]
        assert len(images) == 1
        img = images[0]
        assert img.asset_ref and img.asset_ref.startswith("minio://")
        assert img.content == _VLM_DESC, "图片块文本应为 VLM 描述（可单独命中）"
        parent = next(c for c in chunks if c.id == img.parent_id)
        assert parent.chunk_type == "text"
        assert "<img" not in parent.content
        assert _VLM_DESC not in parent.content, "正文块不得重复含整段描述"
        assert "资产化测试文档正文" in parent.content
        assert "表格与图片之外的结尾正文" in parent.content

    async def test_text_only_unchanged_witness(self, tmp_path):
        path = _make_text_pdf(
            str(tmp_path / "plain.pdf"), "纯文本文档，没有任何表格和图片。"
        )
        text = await PDFParser().parse(path)
        chunker = SemanticChunker()
        original = chunker.chunk(text, doc_type="pdf", doc_id="d6-plain")
        extracted = chunker.extract_asset_chunks(original, "d6-plain")
        assert [(c.id, c.content) for c in extracted] == [
            (c.id, c.content) for c in original
        ], "无资产文档不得被改动（正向证人）"
        assert all(c.chunk_type == "text" for c in extracted)

    async def test_idempotent_ids(self, tmp_path, monkeypatch):
        text = await _parse_asset_pdf(tmp_path, monkeypatch)
        chunker = SemanticChunker()
        out1 = chunker.extract_asset_chunks(
            chunker.chunk(text, doc_type="pdf", doc_id="d6-doc"), "d6-doc"
        )
        out2 = chunker.extract_asset_chunks(
            chunker.chunk(text, doc_type="pdf", doc_id="d6-doc"), "d6-doc"
        )
        assert [c.id for c in out1] == [
            c.id for c in out2
        ], "同内容重复抽取 ID 必须一致"
        out3 = chunker.extract_asset_chunks(out1, "d6-doc")
        assert [(c.id, c.content) for c in out3] == [
            (c.id, c.content) for c in out1
        ], "对已抽取结果再抽取必须幂等"

    async def test_parent_child_duplicate_asset_dedup(self):
        table = "<table><tr><th>列H</th></tr><tr><td>值V</td></tr></table>"
        content = "语义段落文字。" * 300 + "\n" + table
        chunker = SemanticChunker()
        parent = Chunk(
            id=str(uuid.uuid4()),
            doc_id="d6-dup",
            content=content,
            token_count=estimate_tokens(content),
        )
        family = chunker._parent_child_index(content, [parent], "d6-dup")
        assert sum("<table" in c.content for c in family) >= 2, "前置：父与子都含整表"
        out = chunker.extract_asset_chunks(family, "d6-dup")
        tables = [c for c in out if c.chunk_type == "table"]
        assert len(tables) == 1, "父子重复内容中的同一表格只产出一个资产块"
        assert all("<table" not in c.content for c in out if c.chunk_type == "text")

    async def test_image_without_desc_placeholder(self, tmp_path, monkeypatch):
        text = await _parse_asset_pdf(tmp_path, monkeypatch, desc="")
        chunker = SemanticChunker()
        chunks = chunker.extract_asset_chunks(
            chunker.chunk(text, doc_type="pdf", doc_id="d6-doc"), "d6-doc"
        )
        images = [c for c in chunks if c.chunk_type == "image"]
        assert len(images) == 1
        assert images[0].content.strip(), "无 VLM 描述时图片块文本不得为空"
        assert images[0].asset_ref.startswith("minio://")


# ======================================================================
# 5. ChunkService 落库资产字段
# ======================================================================


class TestD6ChunkService:
    async def test_init_chunks_persists_asset_fields(
        self, db_session, tmp_path, monkeypatch
    ):
        from app.services.chunk_service import ChunkService

        text = await _parse_asset_pdf(tmp_path, monkeypatch)
        doc = await _seed_document(
            db_session, str(tmp_path / "asset.pdf"), uuid.uuid4(), content_text=text
        )
        rows = await ChunkService(db_session, user=None).init_chunks(doc.id)
        by_type = {
            t: [r for r in rows if r.chunk_type == t]
            for t in ("text", "image", "table")
        }
        assert len(by_type["image"]) == 1
        assert len(by_type["table"]) == 1
        img = by_type["image"][0]
        assert img.asset_ref.startswith("minio://")
        assert img.content_text == _VLM_DESC
        tbl = by_type["table"][0]
        assert "<table" in tbl.asset_ref
        ids = {r.id for r in rows}
        for asset in (img, tbl):
            assert (
                asset.parent_chunk_id in ids
            ), "parent_chunk_id 必须回链到真实存在的正文块"
            parent = next(r for r in rows if r.id == asset.parent_chunk_id)
            assert parent.chunk_type == "text"

    async def test_init_chunks_text_only_witness(self, db_session, tmp_path):
        from app.services.chunk_service import ChunkService

        path = _make_text_pdf(str(tmp_path / "plain.pdf"), "纯文本存量文档内容。")
        text = await PDFParser().parse(path)
        doc = await _seed_document(db_session, path, uuid.uuid4(), content_text=text)
        rows = await ChunkService(db_session, user=None).init_chunks(doc.id)
        assert rows
        assert all(r.chunk_type == "text" for r in rows)
        assert all(r.asset_ref is None for r in rows)
        assert all(r.parent_chunk_id is None for r in rows)


# ======================================================================
# 6. 端到端管线：资产 chunk 进入索引链路
# ======================================================================


class TestD6Pipeline:
    async def test_pipeline_upserts_asset_chunks(
        self, db_session, tmp_path, monkeypatch
    ):
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        _mock_image_boundaries(monkeypatch)

        tenant_id = uuid.uuid4()
        path = _make_asset_pdf(str(tmp_path / "pipe.pdf"))
        doc = await _seed_document(db_session, path, tenant_id)
        # 实测校准（2026-11-19）：_parse_and_chunk_async 返回 chunk_objects，
        # 向量化/索引在外层调用方执行 — 断言返回的 chunk 对象即管线产物。
        result = await dt._parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        objs = result.get("chunk_objects") or []
        assert objs, f"管线未产出分块: {result}"
        types = [c.chunk_type for c in objs]
        assert "image" in types, f"管线未产出图片资产块: {types}"
        assert "table" in types, f"管线未产出表格资产块: {types}"
        img = next(c for c in objs if c.chunk_type == "image")
        assert img.asset_ref.startswith("minio://")
        assert img.content == _VLM_DESC
        assert result.get("parse_warnings") == []


# ======================================================================
# 7. 检索类型过滤
# ======================================================================


class TestD6ChunkTypeFilter:
    def test_os_chunk_type_clause(self):
        from app.rag.filter_builder import (
            build_opensearch_filter_clauses,
            validate_filters,
        )

        assert build_opensearch_filter_clauses({"chunk_type": "image"}) == [
            {"term": {"chunk_type": "image"}}
        ]
        assert build_opensearch_filter_clauses({"chunk_type": ["image", "table"]}) == [
            {"terms": {"chunk_type": ["image", "table"]}}
        ]
        assert validate_filters({"chunk_type": "image"}) == []

    def test_milvus_chunk_type_expr(self):
        from app.rag.filter_builder import build_milvus_expr

        assert (
            build_milvus_expr(None, {"chunk_type": "table"}) == "chunk_type == 'table'"
        )
        assert (
            build_milvus_expr(None, {"chunk_type": ["image", "table"]})
            == "chunk_type in ['image', 'table']"
        )


# ======================================================================
# 8. 向量库写入/读出携带资产字段
# ======================================================================


class TestD6VectorStore:
    async def test_os_upsert_body_carries_asset_fields(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        http = MagicMock()
        http.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
        store = OpenSearchVectorStore(http_client=http)
        store._index_ready = True
        store._available = True

        chunk = Chunk(
            id="c-img",
            doc_id="d6",
            content=_VLM_DESC,
            chunk_type="image",
            asset_ref="minio://bkt/d6/page1_img0.png",
        )
        count = await store.upsert("d6", [chunk], [[0.0]])
        assert count == 1
        body = http.post.await_args.kwargs["content"]
        doc_body = json.loads(body.strip().split("\n")[1])
        assert doc_body["chunk_type"] == "image"
        assert doc_body["asset_ref"] == "minio://bkt/d6/page1_img0.png"

    def test_os_search_result_carries_asset_fields(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        data = {
            "hits": {
                "hits": [
                    {
                        "_id": "c-img",
                        "_score": 0.9,
                        "_source": {
                            "doc_id": "d6",
                            "chunk_id": "c-img",
                            "content": _VLM_DESC,
                            "chunk_type": "image",
                            "asset_ref": "minio://bkt/d6/page1_img0.png",
                        },
                    }
                ]
            }
        }
        results = OpenSearchVectorStore._parse_results(data, 10)
        assert results[0]["chunk_type"] == "image"
        assert results[0]["asset_ref"] == "minio://bkt/d6/page1_img0.png"

    async def test_os_fetch_by_ids_carries_asset_fields(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "docs": [
                {
                    "found": True,
                    "_id": "c-tbl",
                    "_source": {
                        "chunk_id": "c-tbl",
                        "content": "<table><tr><td>V</td></tr></table>",
                        "chunk_type": "table",
                        "asset_ref": "<table><tr><td>V</td></tr></table>",
                    },
                }
            ]
        }
        http = MagicMock()
        http.post = AsyncMock(return_value=resp)
        store = OpenSearchVectorStore(http_client=http)
        out = await store.fetch_by_ids(["c-tbl"])
        assert out["c-tbl"]["chunk_type"] == "table"
        assert "<table" in out["c-tbl"]["asset_ref"]

    def test_qdrant_payload_carries_asset_fields(self):
        from app.rag.vector_store.qdrant_store import QdrantVectorStore

        store = QdrantVectorStore.__new__(QdrantVectorStore)
        chunk = Chunk(
            id="c-img",
            doc_id="d6",
            content=_VLM_DESC,
            chunk_type="image",
            asset_ref="minio://bkt/d6/x.png",
        )
        payload = store._payload_for_chunk("d6", chunk, None, None, None, None, None)
        assert payload["chunk_type"] == "image"
        assert payload["asset_ref"] == "minio://bkt/d6/x.png"
