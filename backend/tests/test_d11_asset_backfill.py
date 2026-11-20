"""D11 存量资产回填命令验收闸门 — task.md D11。

原则与 A/D 组一致：PDF 一律 fitz 真实生成，DB 走真实 PostgreSQL
（conftest db_session），只 mock MinIO / VLM / 向量化 / 索引 HTTP 边界。
断言两侧不同源：存量文档的 content_text 是手写旧文本（≠ 真实 PDF 解析
产物），force 重解析必须产出真实解析内容才算绕过哈希早退。

回填命令契约（scripts/backfill_asset_chunks.py）：
    - load_targets(kb_id, tenant_id, doc_id) — PG 权威源选取目标文档；
    - backfill_one(doc_id, tenant_id, dry_run) — 强制重解析＋重建 chunk，
      先按 doc_id 删除三个索引的旧条目（text/knn/cross-modal），再重建；
    - backfill_documents(targets, dry_run) — 串行执行，单文档失败隔离。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import get_settings
from tests.test_d6_asset_chunks import _make_asset_pdf, _mock_image_boundaries
from tests.test_pdf_pipeline_hardening import _seed_document

# ----------------------------------------------------------------------
# 边界 mock：向量化 / 跨模态提取 / 索引构建 / 索引删除 HTTP
# ----------------------------------------------------------------------


def _setup_backfill_boundaries(monkeypatch, events: list) -> dict:
    """mock 外部边界并记录 (delete/build) 事件顺序。

    Returns:
        {"builds": [(doc_id, chunk_objects), ...]} — _build_indexes 捕获。
    """
    import tasks.document_tasks as dt

    # 环境校准（2026-11-19）：本机 NO_PROXY 含 ::1/[::1] 条目，httpx
    # trust_env 解析代理时抛 InvalidURL（既有环境勘误桶，非 D11 缺陷）。
    # 生产 helper _delete_document_from_indexes 用默认 trust_env 建 client，
    # 闸门内移除该缺陷变量保证边界 mock 可达。
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
    _mock_image_boundaries(monkeypatch)

    captured: dict = {"builds": []}

    async def fake_embeddings(chunks):
        return [[0.0] for _ in chunks]

    async def fake_cross_modal_extract(doc, doc_type):
        return []

    async def spy_build_indexes(doc_id, chunk_objects, chunks, embeddings, **kwargs):
        events.append(("build", doc_id))
        captured["builds"].append((doc_id, list(chunk_objects)))

    monkeypatch.setattr(dt, "_generate_embeddings", fake_embeddings)
    monkeypatch.setattr(dt, "_extract_images_for_cross_modal", fake_cross_modal_extract)
    monkeypatch.setattr(dt, "_build_indexes", spy_build_indexes)

    async def recorder_post(self, url, **kwargs):
        events.append(("delete", url))
        resp = MagicMock()
        resp.json.return_value = {"deleted": 1}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", recorder_post)
    return captured


async def _seed_legacy_doc(db_session, tmp_path, tenant_id, name="legacy.pdf"):
    """存量文档：旧纯文本 + 与源文件一致的哈希（A14 早退条件成立）。"""
    import tasks.document_tasks as dt

    path = _make_asset_pdf(str(tmp_path / name))
    doc = await _seed_document(
        db_session, path, tenant_id, content_text="存量旧文本，没有任何资产块"
    )
    doc.file_hash = dt._sha256_file(path)
    await db_session.commit()
    return doc


# ======================================================================
# 1. force_reparse 生产接线（保留 A14 日常语义）
# ======================================================================


class TestD11ForceReparse:
    @pytest.mark.asyncio
    async def test_force_bypasses_hash_early_exit(
        self, db_session, tmp_path, monkeypatch
    ):
        """哈希一致时 force_reparse=True 仍必须重解析并产出资产块。"""
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        _mock_image_boundaries(monkeypatch)

        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)

        calls = {"n": 0}
        orig_dispatch = dt._parse_document_dispatch

        async def spy_dispatch(d, warnings=None, meta=None):
            calls["n"] += 1
            return await orig_dispatch(d, warnings, meta=meta)

        monkeypatch.setattr(dt, "_parse_document_dispatch", spy_dispatch)

        result = await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(tenant_id), force_reparse=True
        )
        assert result.get("status") != "failed", result
        assert calls["n"] == 1, "force_reparse 未绕过哈希早退"
        objs = result.get("chunk_objects") or []
        types = [c.chunk_type for c in objs]
        assert "image" in types and "table" in types, f"重解析未产出资产块: {types}"
        await db_session.refresh(doc)
        assert "存量旧文本" not in doc.content_text
        assert "指标甲" in doc.content_text, "content_text 应为真实解析产物"

    @pytest.mark.asyncio
    async def test_default_keeps_a14_early_exit(
        self, db_session, tmp_path, monkeypatch
    ):
        """正向证人：不传 force 时 A14 哈希早退语义不变。"""
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)

        calls = {"n": 0}
        orig_dispatch = dt._parse_document_dispatch

        async def spy_dispatch(d, warnings=None, meta=None):
            calls["n"] += 1
            return await orig_dispatch(d, warnings, meta=meta)

        monkeypatch.setattr(dt, "_parse_document_dispatch", spy_dispatch)

        result = await dt._parse_and_chunk_async(str(doc.id), tenant_id=str(tenant_id))
        assert result.get("status") != "failed", result
        assert calls["n"] == 0, "默认路径不得重解析（A14 日常语义）"
        await db_session.refresh(doc)
        assert doc.content_text == "存量旧文本，没有任何资产块"

    @pytest.mark.asyncio
    async def test_force_no_file_returns_edited_content(
        self, db_session, tmp_path, monkeypatch
    ):
        """正向证人：无源文件的编辑型文档，force 也不得崩溃/清空内容。"""
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)
        doc.file_path = None
        await db_session.commit()

        result = await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(tenant_id), force_reparse=True
        )
        assert result.get("status") != "failed", result
        await db_session.refresh(doc)
        assert doc.content_text == "存量旧文本，没有任何资产块"

    @pytest.mark.asyncio
    async def test_force_missing_file_returns_old_content(
        self, db_session, tmp_path, monkeypatch
    ):
        """正向证人：源文件丢失（不可哈希）时 force 维持已有内容。"""
        import tasks.document_tasks as dt

        monkeypatch.setattr(get_settings(), "DOCLING_ENABLED", False, raising=False)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)
        doc.file_path = str(tmp_path / "已丢失.pdf")
        await db_session.commit()

        result = await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(tenant_id), force_reparse=True
        )
        assert result.get("status") != "failed", result
        await db_session.refresh(doc)
        assert doc.content_text == "存量旧文本，没有任何资产块"


# ======================================================================
# 2. 回填命令：资产生成 + 旧条目清理 + 幂等
# ======================================================================


class TestD11BackfillCommand:
    @pytest.mark.asyncio
    async def test_backfill_generates_assets_and_cleans_old(
        self, db_session, tmp_path, monkeypatch
    ):
        """回填产出资产块；重建前必须按 doc_id 删除三个索引的旧条目。"""
        import scripts.backfill_asset_chunks as bf

        events: list = []
        captured = _setup_backfill_boundaries(monkeypatch, events)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)

        result = await bf.backfill_one(str(doc.id), str(tenant_id))
        assert result["status"] == "done", result
        assert result["assets"] >= 2, f"应至少产出 1 图片 + 1 表格资产块: {result}"

        assert len(captured["builds"]) == 1
        _, objs = captured["builds"][0]
        types = [c.chunk_type for c in objs]
        assert "image" in types and "table" in types
        assert all(
            "<table" not in c.content for c in objs if c.chunk_type == "text"
        ), "整表不得残留在正文块中"

        # 三个索引的 delete_by_query 都必须在 build 之前，且按 doc_id 精确匹配
        deletes = [url for kind, url in events if kind == "delete"]
        build_idx = next(i for i, (kind, _) in enumerate(events) if kind == "build")
        assert all(
            i < build_idx for i, (kind, _) in enumerate(events) if kind == "delete"
        ), f"旧条目删除必须先于重建: {events}"
        for index in (
            "ekb_documents",
            "ekb_knn_vectors",
            get_settings().OPENSEARCH_CROSS_MODAL_INDEX,
        ):
            assert any(
                f"/{index}/_delete_by_query" in url for url in deletes
            ), f"缺少索引 {index} 的旧条目清理: {deletes}"

        await db_session.refresh(doc)
        assert "指标甲" in doc.content_text, "回填应刷新 content_text 为真实解析产物"

    @pytest.mark.asyncio
    async def test_backfill_idempotent(self, db_session, tmp_path, monkeypatch):
        """重复执行：确定性 chunk ID 不变，单次构建内资产块不重复。"""
        import scripts.backfill_asset_chunks as bf

        events: list = []
        captured = _setup_backfill_boundaries(monkeypatch, events)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)

        r1 = await bf.backfill_one(str(doc.id), str(tenant_id))
        r2 = await bf.backfill_one(str(doc.id), str(tenant_id))
        assert r1["status"] == r2["status"] == "done"

        assert len(captured["builds"]) == 2
        ids1 = [c.id for c in captured["builds"][0][1]]
        ids2 = [c.id for c in captured["builds"][1][1]]
        assert ids1 == ids2, "重复回填必须产出相同的确定性 chunk ID"
        for _, objs in captured["builds"]:
            assert sum(c.chunk_type == "image" for c in objs) == 1
            assert sum(c.chunk_type == "table" for c in objs) == 1
        # 第二轮重建前同样先清理（防止 cross-modal 随机 ID 条目累积）
        second_build_idx = [i for i, (k, _) in enumerate(events) if k == "build"][1]
        assert any(
            k == "delete" for k, _ in events[second_build_idx - 3 : second_build_idx]
        )

    @pytest.mark.asyncio
    async def test_dry_run_no_writes(self, db_session, tmp_path, monkeypatch):
        """dry-run 只统计：不解析、不删除、不重建、DB 不变。"""
        import scripts.backfill_asset_chunks as bf

        events: list = []
        captured = _setup_backfill_boundaries(monkeypatch, events)
        tenant_id = uuid.uuid4()
        doc = await _seed_legacy_doc(db_session, tmp_path, tenant_id)
        old_hash = doc.file_hash

        summary = await bf.backfill_documents(
            [{"doc_id": str(doc.id), "tenant_id": str(tenant_id)}], dry_run=True
        )
        assert summary["dry_run"] == 1
        assert events == [], "dry-run 不得触发删除/重建"
        assert captured["builds"] == []
        await db_session.refresh(doc)
        assert doc.content_text == "存量旧文本，没有任何资产块"
        assert doc.file_hash == old_hash


# ======================================================================
# 3. 目标选取：知识库/租户粒度 + 排除项
# ======================================================================


class TestD11TargetSelection:
    @pytest.mark.asyncio
    async def test_scope_kb_filter(self, db_session, tmp_path, monkeypatch):
        import scripts.backfill_asset_chunks as bf

        t = uuid.uuid4()
        doc1 = await _seed_legacy_doc(db_session, tmp_path, t, name="a.pdf")
        doc2 = await _seed_legacy_doc(db_session, tmp_path, t, name="b.pdf")
        assert doc1.kb_id != doc2.kb_id

        targets = await bf.load_targets(kb_id=str(doc1.kb_id))
        ids = {row["doc_id"] for row in targets}
        assert str(doc1.id) in ids
        assert str(doc2.id) not in ids

    @pytest.mark.asyncio
    async def test_scope_tenant_filter(self, db_session, tmp_path, monkeypatch):
        import scripts.backfill_asset_chunks as bf

        t1, t2 = uuid.uuid4(), uuid.uuid4()
        doc1 = await _seed_legacy_doc(db_session, tmp_path, t1, name="a.pdf")
        doc2 = await _seed_legacy_doc(db_session, tmp_path, t2, name="b.pdf")

        targets = await bf.load_targets(tenant_id=str(t1))
        ids = {row["doc_id"] for row in targets}
        assert str(doc1.id) in ids
        assert str(doc2.id) not in ids

    @pytest.mark.asyncio
    async def test_scope_excludes_unsupported_and_deleted(
        self, db_session, tmp_path, monkeypatch
    ):
        """视频/音频/.doc/.ppt 与软删除文档不得进入回填目标。"""
        import scripts.backfill_asset_chunks as bf

        t = uuid.uuid4()
        keep = await _seed_legacy_doc(db_session, tmp_path, t, name="keep.pdf")
        video = await _seed_legacy_doc(db_session, tmp_path, t, name="v.pdf")
        video.doc_type = "mp4"
        legacy_doc = await _seed_legacy_doc(db_session, tmp_path, t, name="old.pdf")
        legacy_doc.doc_type = "doc"
        deleted = await _seed_legacy_doc(db_session, tmp_path, t, name="del.pdf")
        deleted.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        targets = await bf.load_targets(tenant_id=str(t))
        ids = {row["doc_id"] for row in targets}
        assert ids == {str(keep.id)}, f"目标选取排除项失效: {ids}"


# ======================================================================
# 4. 失败隔离 + 未回填文档不变（正向证人）
# ======================================================================


class TestD11Isolation:
    @pytest.mark.asyncio
    async def test_failed_doc_isolated(self, db_session, tmp_path, monkeypatch):
        """单文档解析失败不得中断批次，其余文档照常回填。"""
        import scripts.backfill_asset_chunks as bf

        events: list = []
        _setup_backfill_boundaries(monkeypatch, events)
        t = uuid.uuid4()
        bad = await _seed_document(
            db_session, str(tmp_path / "不存在.pdf"), t, content_text=None
        )
        good = await _seed_legacy_doc(db_session, tmp_path, t, name="good.pdf")

        summary = await bf.backfill_documents(
            [
                {"doc_id": str(bad.id), "tenant_id": str(t)},
                {"doc_id": str(good.id), "tenant_id": str(t)},
            ]
        )
        assert summary["failed"] == 1, summary
        assert summary["done"] == 1, summary
        assert any(
            kind == "build" and payload == str(good.id) for kind, payload in events
        )

    @pytest.mark.asyncio
    async def test_unbackfilled_doc_unchanged(self, db_session, tmp_path, monkeypatch):
        """正向证人：只对 A 回填时，B 的内容/哈希/状态与索引事件均不受影响。"""
        import scripts.backfill_asset_chunks as bf

        events: list = []
        _setup_backfill_boundaries(monkeypatch, events)
        t = uuid.uuid4()
        doc_a = await _seed_legacy_doc(db_session, tmp_path, t, name="a.pdf")
        doc_b = await _seed_legacy_doc(db_session, tmp_path, t, name="b.pdf")
        before = (doc_b.content_text, doc_b.file_hash, doc_b.parse_status)

        result = await bf.backfill_one(str(doc_a.id), str(t))
        assert result["status"] == "done"

        await db_session.refresh(doc_b)
        assert (doc_b.content_text, doc_b.file_hash, doc_b.parse_status) == before
        assert all(
            str(doc_b.id) not in payload for _, payload in events
        ), "未回填文档不得出现在删除/重建事件中"
