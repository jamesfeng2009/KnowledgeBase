"""E4 验收闸门 — 任务级解析开关透传（优先级 任务>租户>全局）。

契约（task.md E4 默认值清单）：
    - 上传端点可选 parse_config Form（JSON），白名单校验失败 400（写操作前）；
    - celery process_document → _parse_and_chunk_async / 降级
      _process_document_async 三级透传；
    - 逐键合并 {**tenant_cfg, **task_cfg}（不整体替换）；
    - 任务入口再校验，非法 fail-closed（不进解析）；
    - 一次性语义：覆盖不落 Tenant.settings；
    - 无任务参数时租户/全局行为不回归（正向证人）。

管线闸门走真实 PG（conftest ekb_test），_parse_document 打替身仅用于
捕获上下文内 get_parse_settings() 实效值（断言侧＝生产 overlay 机制，
与输入配置不同源）；API 闸门复用 test_upload_summary 的 auth_client 基座。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import sqlalchemy as sa

from app.config import get_settings
from tests.test_d7_tenant_parse_config import _seed_tenant
from tests.test_pdf_pipeline_hardening import _seed_document
from tests.test_upload_summary import _make_mock_doc, auth_client  # noqa: F401


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


_KEY = "PDF_IMAGE_EXTRACTION_ENABLED"
_KEY2 = "MINERU_ENABLED"


def _capture_parse(monkeypatch, *, empty: bool = False) -> dict:
    """替身 _parse_document — 记录上下文内生产 get_parse_settings 实效值。

    empty=True 返回空文本让流水线在解析后早退（降级入口闸门只验合并
    接线，不跑 embedding/索引）。
    """
    import tasks.document_tasks as dt
    from app.core.parse_config import get_parse_settings

    captured: dict = {}

    async def fake_parse(doc, warnings=None, **kw):
        s = get_parse_settings()
        captured["values"] = {k: getattr(s, k) for k in (_KEY, _KEY2)}
        captured["is_global"] = s is get_settings()
        captured["called"] = True
        return ("" if empty else "E4 测试正文内容。" * 30, [])

    monkeypatch.setattr(dt, "_parse_document", fake_parse)
    return captured


async def _seed(db_session, tmp_path, tenant_cfg: dict | None):
    tid = uuid.uuid4()
    settings = {"parse_config": tenant_cfg} if tenant_cfg else None
    await _seed_tenant(db_session, tid, settings=settings)
    doc = await _seed_document(db_session, str(tmp_path / "e4.pdf"), tid)
    return tid, doc


# ==================================================================
# 管线合并语义 — _parse_and_chunk_async（真实 PG）
# ==================================================================


class TestE4Merge:
    async def test_task_overrides_tenant(self, db_session, tmp_path, monkeypatch):
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch)
        _, doc = await _seed(db_session, tmp_path, {_KEY: True})
        await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(doc.tenant_id), parse_config={_KEY: False}
        )
        assert captured["called"]
        assert captured["values"][_KEY] is False, "任务级必须压过租户级"

    async def test_per_key_merge_not_replace(self, db_session, tmp_path, monkeypatch):
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch)
        _, doc = await _seed(db_session, tmp_path, {_KEY: True, _KEY2: True})
        await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(doc.tenant_id), parse_config={_KEY: False}
        )
        assert captured["values"][_KEY] is False
        assert captured["values"][_KEY2] is True, "未覆盖键必须保留租户值（逐键合并）"

    async def test_no_task_cfg_tenant_witness(self, db_session, tmp_path, monkeypatch):
        """正向证人：无任务参数，D7 租户级行为不回归。"""
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch)
        _, doc = await _seed(db_session, tmp_path, {_KEY: False})
        await dt._parse_and_chunk_async(str(doc.id), tenant_id=str(doc.tenant_id))
        assert captured["values"][_KEY] is False

    async def test_no_cfg_global_witness(self, db_session, tmp_path, monkeypatch):
        """正向证人：租户/任务均未配置 → 全局 Settings 本体。"""
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch)
        _, doc = await _seed(db_session, tmp_path, None)
        await dt._parse_and_chunk_async(str(doc.id), tenant_id=str(doc.tenant_id))
        assert captured["is_global"] is True
        assert captured["values"][_KEY] == getattr(get_settings(), _KEY)

    async def test_invalid_task_cfg_fail_closed(
        self, db_session, tmp_path, monkeypatch
    ):
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch)
        _, doc = await _seed(db_session, tmp_path, None)
        result = await dt._parse_and_chunk_async(
            str(doc.id),
            tenant_id=str(doc.tenant_id),
            parse_config={"DOCLING_ENABLED": False},
        )
        assert result["status"] == "failed"
        assert "parse_config" in result["error"]
        assert not captured.get("called"), "非法配置不得进入解析"

    async def test_task_cfg_not_persisted(self, db_session, tmp_path, monkeypatch):
        import tasks.document_tasks as dt

        _capture_parse(monkeypatch)
        tid, doc = await _seed(db_session, tmp_path, {_KEY: True})
        await dt._parse_and_chunk_async(
            str(doc.id), tenant_id=str(tid), parse_config={_KEY: False}
        )
        # 裸 SQL 读回 — 不走 ORM 身份映射（断言两侧不同源）
        row = (
            await db_session.execute(
                sa.text("SELECT settings FROM tenants WHERE id = :id"),
                {"id": str(tid)},
            )
        ).one()
        assert row[0]["parse_config"] == {_KEY: True}, "任务级覆盖不得落租户配置"

    async def test_fallback_entry_passthrough(self, db_session, tmp_path, monkeypatch):
        """降级入口 _process_document_async 同样合并任务级覆盖。"""
        import tasks.document_tasks as dt

        captured = _capture_parse(monkeypatch, empty=True)
        _, doc = await _seed(db_session, tmp_path, {_KEY: True})
        await dt._process_document_async(str(doc.id), parse_config={_KEY: False})
        assert captured["called"]
        assert captured["values"][_KEY] is False


# ==================================================================
# celery 任务接线 — process_document 三级透传（同步直调 .run）
# ==================================================================


def _stub_task_env(monkeypatch, *, chunks_ok: bool):
    import tasks.document_tasks as dt

    monkeypatch.setattr(dt, "_update_parse_progress", lambda *a, **kw: None)
    monkeypatch.setattr(dt, "_should_use_multipart_pipeline", lambda doc_id: False)
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=SimpleNamespace(acquired=True))
    lock.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.utils.task_lock.TaskLockContext", lambda *a: lock)
    parse_mock = AsyncMock(
        return_value=(
            {"chunk_objects": [], "graph_enabled": False}
            if chunks_ok
            else {"status": "failed", "error": "stop"}
        )
    )
    monkeypatch.setattr(dt, "_parse_and_chunk_async", parse_mock)
    return parse_mock


class TestE4CeleryWiring:
    def test_task_passes_parse_config(self, monkeypatch):
        import tasks.document_tasks as dt

        parse_mock = _stub_task_env(monkeypatch, chunks_ok=False)
        cfg = {_KEY: False}
        dt.process_document.run(str(uuid.uuid4()), parse_config=cfg)
        parse_mock.assert_awaited_once()
        assert parse_mock.await_args.kwargs.get("parse_config") == cfg

    def test_redis_fallback_passes_parse_config(self, monkeypatch):
        import tasks.document_tasks as dt

        _stub_task_env(monkeypatch, chunks_ok=True)
        monkeypatch.setattr(dt, "_save_chunks_to_redis", lambda *a: False)
        fallback = AsyncMock(return_value={"status": "success"})
        monkeypatch.setattr(dt, "_process_document_async", fallback)
        cfg = {_KEY: False}
        dt.process_document.run(str(uuid.uuid4()), parse_config=cfg)
        fallback.assert_awaited_once()
        assert fallback.await_args.kwargs.get("parse_config") == cfg


# ==================================================================
# API 边界 — 上传端点 parse_config Form 字段
# ==================================================================


def _upload_mocks():
    # page_count/char_count 必须给整型 — DocResponse pydantic 拒 None
    # （既有 _make_mock_doc 默认 None，test_upload_summary 只断言 ≠413 未暴露）
    mock_doc = _make_mock_doc(
        content_text="内容", status="draft", page_count=1, char_count=2
    )
    service_patch = patch("app.api.v1.documents.KnowledgeService")
    repo_patch = patch("app.repositories.knowledge_repository.DocumentRepository")
    minio_patch = patch("app.utils.minio_client.upload_file", new_callable=AsyncMock)
    task_patch = patch("tasks.document_tasks.process_document")
    return mock_doc, service_patch, repo_patch, minio_patch, task_patch


class TestE4API:
    async def test_valid_config_reaches_delay(self, auth_client: httpx.AsyncClient):
        mock_doc, sp, rp, mp, tp = _upload_mocks()
        with sp as cls, rp as rcls, mp as upload, tp as task:
            cls.return_value.upload_document = AsyncMock(return_value=mock_doc)
            rcls.return_value.update = AsyncMock(return_value=mock_doc)
            upload.return_value = "minio://ekb-documents/e4"
            resp = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid.uuid4()), "title": "E4"},
                files={"file": ("e4.md", b"# E4", "text/markdown")},
                data={"parse_config": json.dumps({_KEY: False})},
            )
        assert resp.status_code == 201, resp.text
        task.delay.assert_called_once()
        assert task.delay.call_args.kwargs.get("parse_config") == {_KEY: False}

    async def test_invalid_json_400_before_writes(self, auth_client: httpx.AsyncClient):
        mock_doc, sp, rp, mp, tp = _upload_mocks()
        with sp as cls, rp, mp as upload, tp as task:
            cls.return_value.upload_document = AsyncMock(return_value=mock_doc)
            resp = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid.uuid4()), "title": "E4"},
                files={"file": ("e4.md", b"# E4", "text/markdown")},
                data={"parse_config": "{not json"},
            )
        assert resp.status_code == 400
        upload.assert_not_awaited()
        cls.return_value.upload_document.assert_not_awaited()
        task.delay.assert_not_called()

    async def test_non_whitelist_key_400(self, auth_client: httpx.AsyncClient):
        mock_doc, sp, rp, mp, tp = _upload_mocks()
        with sp as cls, rp, mp as upload, tp as task:
            cls.return_value.upload_document = AsyncMock(return_value=mock_doc)
            resp = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid.uuid4()), "title": "E4"},
                files={"file": ("e4.md", b"# E4", "text/markdown")},
                data={"parse_config": json.dumps({"DOCLING_ENABLED": False})},
            )
        assert resp.status_code == 400
        task.delay.assert_not_called()

    async def test_absent_config_witness(self, auth_client: httpx.AsyncClient):
        """正向证人：不带 parse_config 上传形状不变（None 透传）。"""
        mock_doc, sp, rp, mp, tp = _upload_mocks()
        with sp as cls, rp as rcls, mp as upload, tp as task:
            cls.return_value.upload_document = AsyncMock(return_value=mock_doc)
            rcls.return_value.update = AsyncMock(return_value=mock_doc)
            upload.return_value = "minio://ekb-documents/e4"
            resp = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid.uuid4()), "title": "E4"},
                files={"file": ("e4.md", b"# E4", "text/markdown")},
            )
        assert resp.status_code == 201, resp.text
        task.delay.assert_called_once()
        assert task.delay.call_args.kwargs.get("parse_config") is None
