"""``documents.file_size`` 写入链路回归测试（任务 #13）。

背景：``filesize`` 列自 c9d0e1f2a3b4 迁移引入后**全仓只读不写**，导致两个真实故障：

1. ``BillingService.get_storage_usage`` 的 ``SUM(Document.file_size)`` 恒为 0，
   配额检查永远退回"content_text 长度 + 每文档 500KB"估算，与真实占用脱钩；
2. ``task/document_tasks._should_use_multipart_pipeline`` 靠 ``file_size`` 判断
   是否走 GB 视频多任务管线，列恒为 NULL → 大视频永远走单任务普通管线（超时）。

本文件锁住所有落库入口都必须写真实字节数，并用 AST 扫描防止将来新增入口时漏写。
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"


# ======================================================================
# 夹具（沿用 test_documents_update_contract 的真实 PG + ASGI 客户端模式）
# ======================================================================


@pytest_asyncio.fixture
async def seeded(db_session):
    from app.models.knowledge import KnowledgeBase
    from app.models.user import User

    owner = User(
        email="size-owner@contract.local",
        hashed_password="not-used",
        name="Owner",
        role="editor",
        clearance_level="secret",
    )
    db_session.add(owner)
    await db_session.flush()

    kb = KnowledgeBase(name="体积统计库", visibility="public", owner_id=owner.id)
    db_session.add(kb)
    await db_session.commit()
    return SimpleNamespace(owner=owner, kb=kb)


@asynccontextmanager
async def _client_as(session, user):
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app

    async def override_user():
        return user

    async def override_db():
        yield session

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://size-test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _load_doc(session, doc_id):
    from app.models.knowledge import Document

    result = await session.execute(select(Document).where(Document.id == doc_id))
    return result.scalar_one()


# ======================================================================
# 入口 1：文件上传
# ======================================================================


@pytest.mark.asyncio
async def test_file_upload_records_real_byte_size(db_session, seeded) -> None:
    """POST /documents/upload 必须把文件真实字节数写进 file_size。"""
    payload = ("# 标题\n\n" + "内容" * 500).encode("utf-8")

    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.post(
            "/api/v1/documents/upload",
            params={"kb_id": str(seeded.kb.id), "title": "体积测试.md"},
            files={"file": ("体积测试.md", payload, "text/markdown")},
        )
    assert resp.status_code == 201, resp.text

    doc_id = resp.json()["data"]["id"]
    await db_session.commit()
    doc = await _load_doc(db_session, doc_id)
    assert doc.file_size == len(payload), (
        f"file_size 应为文件字节数 {len(payload)}，实际 {doc.file_size}"
    )


# ======================================================================
# 入口 2：纯文本创建
# ======================================================================


@pytest.mark.asyncio
async def test_text_create_records_utf8_size(db_session, seeded) -> None:
    """POST /knowledge/{kb_id}/documents 按 UTF-8 字节数写入 file_size。"""
    content = "中文内容" * 100  # 多字节字符，验证不是按字符数而是按字节数

    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.post(
            f"/api/v1/knowledge/{seeded.kb.id}/documents",
            json={
                "kb_id": str(seeded.kb.id),
                "title": "纯文本",
                "content_text": content,
                "doc_type": "md",
            },
        )
    assert resp.status_code == 201, resp.text

    doc_id = resp.json()["data"]["id"]
    await db_session.commit()
    doc = await _load_doc(db_session, doc_id)
    assert doc.file_size == len(content.encode("utf-8"))


# ======================================================================
# 入口 3：网页剪藏
# ======================================================================


@pytest.mark.asyncio
async def test_clip_records_size(db_session, seeded) -> None:
    """POST /documents/clip 与配额预检的 additional_bytes 口径一致。"""
    content = "剪藏正文" * 50

    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.post(
            "/api/v1/documents/clip",
            json={
                "kb_id": str(seeded.kb.id),
                "title": "剪藏页",
                "content": content,
                "source_url": "https://example.com/a",
            },
        )
    assert resp.status_code == 201, resp.text

    doc_id = resp.json()["data"]["id"]
    await db_session.commit()
    doc = await _load_doc(db_session, doc_id)
    assert doc.file_size == len(content.encode("utf-8"))


# ======================================================================
# 配额统计联动
# ======================================================================


@pytest.mark.asyncio
async def test_storage_usage_sums_file_size(db_session, seeded) -> None:
    """带租户的文档按 file_size 求和，且不再叠加 500KB/篇的估算。"""
    import uuid

    from app.models.knowledge import Document
    from app.services.billing_service import BillingService

    tenant_id = uuid.uuid4()
    docs = [
        Document(
            kb_id=seeded.kb.id,
            title=f"doc-{i}",
            content_text="x" * 10_000,  # 估算口径会算成 10000+500KB，差异明显
            doc_type="md",
            status="draft",
            owner_id=seeded.owner.id,
            classification="internal",
            tenant_id=tenant_id,
            file_size=size,
        )
        for i, size in enumerate((1_000, 2_000, 3_000))
    ]
    db_session.add_all(docs)
    await db_session.commit()

    billing = BillingService(db_session)
    used = await billing.get_storage_usage(tenant_id)
    assert used == 6_000, f"应为 file_size 之和 6000，实际 {used}"


@pytest.mark.asyncio
async def test_storage_usage_treats_legacy_zero_as_unknown(db_session, seeded) -> None:
    """历史文档的 ``file_size=0``（迁移 8c93453eea3d 回填的 NULL）必须按未知估算。

    否则这批文档全被算成 0 字节，租户可以无限超配额而系统毫无察觉。
    """
    import uuid

    from app.models.knowledge import Document
    from app.services.billing_service import BillingService

    tenant_id = uuid.uuid4()
    db_session.add(
        Document(
            kb_id=seeded.kb.id,
            title="历史文档",
            content_text="y" * 1_000,
            doc_type="md",
            status="draft",
            owner_id=seeded.owner.id,
            classification="internal",
            tenant_id=tenant_id,
            file_size=0,
        )
    )
    await db_session.commit()

    billing = BillingService(db_session)
    used = await billing.get_storage_usage(tenant_id)
    assert used == 1_000 + 500 * 1024


# ======================================================================
# 大文件分流联动
# ======================================================================


@pytest.mark.asyncio
async def test_multipart_pipeline_triggers_on_large_video(db_session, seeded) -> None:
    """file_size 写入后，>50MB 视频必须命中多任务管线判断。

    这条断言正是 file_size 恒为 NULL 时失效的那个判断：``(doc.file_size or 0)``
    除以 1MB 恒为 0，永远不 > 50，GB 级视频会被塞进单任务普通管线等到超时。
    """
    import asyncio

    from app.models.knowledge import Document
    from tasks.document_tasks import _should_use_multipart_pipeline

    def _make(title: str, doc_type: str, size: int | None) -> Document:
        doc = Document(
            kb_id=seeded.kb.id,
            title=title,
            content_text="",
            doc_type=doc_type,
            status="draft",
            owner_id=seeded.owner.id,
            classification="internal",
            file_size=size,
        )
        db_session.add(doc)
        return doc

    small = _make("小视频.mp4", "mp4", 10 * 1024 * 1024)
    big = _make("大视频.mp4", "mp4", 80 * 1024 * 1024)
    nofile = _make("无体积.mp4", "mp4", None)
    await db_session.commit()

    # _should_use_multipart_pipeline 内部用 asyncio.run()，必须在无事件循环的线程里跑
    assert await asyncio.to_thread(_should_use_multipart_pipeline, str(small.id)) is False
    assert await asyncio.to_thread(_should_use_multipart_pipeline, str(big.id)) is True
    assert await asyncio.to_thread(_should_use_multipart_pipeline, str(nofile.id)) is False


# ======================================================================
# 静态扫描：新增落库入口不许漏写 file_size
# ======================================================================


def _iter_app_modules() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _document_aliases(tree: ast.Module) -> set[str]:
    """找出本模块里指向 ``app.models.knowledge.Document`` 的本地名字。

    同名类有好几个（``llama_index.core.schema.Document``、``docx.Document``），
    只有 ORM 的那个才需要 file_size，靠 import 来源区分。
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.models.knowledge":
            for alias in node.names:
                if alias.name == "Document":
                    aliases.add(alias.asname or alias.name)
    return aliases


def test_every_document_create_path_passes_file_size() -> None:
    """扫全 app/：``upload_document(...)`` 与 ORM ``Document(...)`` 必须带 file_size。

    这是"防回流"断言 —— 新增一个上传入口忘了写 file_size，配额与大文件分流
    就会重新变成摆设，而且不会有任何报错。静态扫描是唯一能提前发现的办法。
    """
    offenders: list[str] = []

    for path in _iter_app_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc_aliases = _document_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
                is_orm_document = False
            elif isinstance(func, ast.Name):
                name = func.id
                is_orm_document = name in doc_aliases
            else:
                continue
            if name != "upload_document" and not is_orm_document:
                continue
            # app/cli/client.py 是 HTTP 客户端封装，不落库
            if "cli" in path.parts:
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "file_size" not in kwargs:
                offenders.append(f"{path.relative_to(BACKEND_DIR)}:{node.lineno} {name}(...)")

    assert not offenders, (
        "以下创建 Document 的调用未传 file_size（会让存储配额与大文件分流失效）：\n  "
        + "\n  ".join(offenders)
    )


def test_knowledge_service_signature_accepts_file_size() -> None:
    """service 层必须暴露 file_size 形参，否则上层想写也写不进去。"""
    import inspect

    from app.services.knowledge_service import KnowledgeService

    params = inspect.signature(KnowledgeService.upload_document).parameters
    assert "file_size" in params


def test_file_size_is_bigint_in_model() -> None:
    """列类型必须是 BIGINT：INTEGER 上限 2.1GB，GB 级视频会 overflow。"""
    from sqlalchemy import BigInteger

    from app.models.knowledge import Document

    assert isinstance(Document.__table__.c.file_size.type, BigInteger)
