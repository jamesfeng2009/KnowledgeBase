"""文档更新契约回归测试 — 固化 P0 修复（重复路由遮蔽 + 版本快照语义）。

背景（P0）：
    ``GET/PUT /documents/{doc_id}`` 曾在 ``api/v1/knowledge.py`` 与
    ``api/v1/documents.py`` 各定义一次。``api/v1/__init__.py`` 中
    knowledge_router 先于 documents_router 注册，FastAPI 只取先注册者，
    documents.py 的实现永不执行，导致两个用户可感知的故障：

    1. PUT 请求里的 ``title/doc_type/status/classification`` 被静默丢弃
       —— 前端标题自动保存返回 200，但库里没改；
    2. documents.py 中的「更新前版本快照」逻辑一起失效，
       ``document_versions`` 恒为空，版本回溯形同虚设。

本文件把修复后的契约固化为测试：
    - 全站 v1 路由不得出现 (method, path) 重复；
    - PUT 能真正落库元数据字段；
    - 内容变化时快照保存的是「更新前」内容
      （同一 session 下 ORM identity map 会把旧对象一起改写，顺序错了就会存成新内容）；
    - 内容未变、或仅改标题时不产生垃圾版本；
    - 版本恢复需要写权限（只读协作者不得覆盖文档内容）。
"""

from __future__ import annotations

import collections
import importlib
import pkgutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

# ======================================================================
# 路由唯一性
# ======================================================================


def test_no_duplicate_v1_routes() -> None:
    """所有 v1 子路由的 (method, path) 必须唯一。

    重复注册不会报错，只会让后注册的实现静默失效 —— 这类问题在运行时
    完全无声，只能靠静态断言兜住。
    """
    import app.api.v1 as v1_pkg

    seen: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for module_info in pkgutil.iter_modules(v1_pkg.__path__):
        module = importlib.import_module(f"app.api.v1.{module_info.name}")
        router = getattr(module, "router", None)
        if router is None:
            continue
        for route in router.routes:
            methods = getattr(route, "methods", None) or set()
            for method in methods:
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    seen[(method, route.path)].append(f"app.api.v1.{module_info.name}")

    duplicates = {key: mods for key, mods in seen.items() if len(mods) > 1}
    assert not duplicates, (
        "检测到重复注册的路由（后注册者会被静默遮蔽，不报错）：\n"
        + "\n".join(
            f"  {method} {path} <- {mods}"
            for (method, path), mods in duplicates.items()
        )
    )


# ======================================================================
# 夹具
# ======================================================================


@pytest_asyncio.fixture
async def seeded(db_session):
    """建一套最小可用的 用户 / 知识库 / 文档 / 只读成员 数据。"""
    from app.models.knowledge import Document, KnowledgeBase
    from app.models.user import KbMember, User

    owner = User(
        email="owner@contract.local",
        hashed_password="not-used",
        name="Owner",
        role="editor",
        clearance_level="secret",
    )
    viewer = User(
        email="viewer@contract.local",
        hashed_password="not-used",
        name="Viewer",
        role="viewer",
        clearance_level="secret",
    )
    db_session.add_all([owner, viewer])
    await db_session.flush()

    kb = KnowledgeBase(name="契约测试库", visibility="public", owner_id=owner.id)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(
        kb_id=kb.id,
        title="原标题",
        content_html="<p>旧内容</p>",
        content_json={"type": "doc", "content": []},
        content_text="旧内容",
        doc_type="md",
        status="draft",
        owner_id=owner.id,
        classification="internal",
    )
    db_session.add(doc)
    # viewer 是知识库成员但角色为 viewer（可读不可写）
    db_session.add(KbMember(kb_id=kb.id, user_id=viewer.id, role="viewer"))
    await db_session.commit()

    return SimpleNamespace(owner=owner, viewer=viewer, kb=kb, doc=doc)


@asynccontextmanager
async def _client_as(session, user):
    """以指定用户身份调用真实 FastAPI 应用（DB 走真实 PG session）。"""
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
            base_url="http://contract-test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _snapshot_count(session, doc_id: UUID) -> int:
    from app.models.knowledge import DocumentVersion

    result = await session.execute(
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.doc_id == doc_id)
    )
    return int(result.scalar_one())


# ======================================================================
# 元数据落库
# ======================================================================


@pytest.mark.asyncio
async def test_put_persists_metadata_fields(db_session, seeded) -> None:
    """PUT 必须真正写入 title / status / doc_type / classification。

    修复前：请求返回 200，四个字段全被丢弃（被 knowledge.py 中被遮蔽的实现接管）。
    """
    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.put(
            f"/api/v1/documents/{seeded.doc.id}",
            json={
                "title": "改过的标题",
                "status": "published",
                "doc_type": "html",
                "classification": "confidential",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["title"] == "改过的标题"
    await db_session.commit()

    await db_session.refresh(seeded.doc)
    assert seeded.doc.title == "改过的标题"
    assert seeded.doc.status == "published"
    assert seeded.doc.doc_type == "html"
    assert seeded.doc.classification == "confidential"


@pytest.mark.asyncio
async def test_put_title_only_creates_no_version(db_session, seeded) -> None:
    """仅改标题不应产生版本快照（编辑器标题防抖保存不能污染版本历史）。"""
    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.put(
            f"/api/v1/documents/{seeded.doc.id}", json={"title": "只改标题"}
        )
    assert resp.status_code == 200, resp.text
    await db_session.commit()
    assert await _snapshot_count(db_session, seeded.doc.id) == 0


# ======================================================================
# 版本快照
# ======================================================================


@pytest.mark.asyncio
async def test_put_content_snapshots_previous_content(db_session, seeded) -> None:
    """内容变化时，快照必须是「更新前」的内容。

    这是最容易写错的一处：service.update_document 与路由共用同一个 session，
    ORM identity map 返回的是同一个对象，更新后再读 doc.content_html
    拿到的已经是新值 —— 快照顺序错一步，版本历史就全是废物。
    """
    from app.models.knowledge import DocumentVersion

    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.put(
            f"/api/v1/documents/{seeded.doc.id}",
            json={"content_html": "<p>新内容</p>", "content_text": "新内容"},
        )
    assert resp.status_code == 200, resp.text
    await db_session.commit()

    # 文档本体已更新
    await db_session.refresh(seeded.doc)
    assert seeded.doc.content_html == "<p>新内容</p>"

    # 快照记录的是旧内容
    result = await db_session.execute(
        select(DocumentVersion).where(DocumentVersion.doc_id == seeded.doc.id)
    )
    versions = list(result.scalars().all())
    assert len(versions) == 1, f"应恰好 1 条快照，实际 {len(versions)}"
    assert versions[0].content_html == "<p>旧内容</p>"
    assert versions[0].author_id == seeded.owner.id
    assert "原标题" in (versions[0].summary or "")


@pytest.mark.asyncio
async def test_put_same_content_creates_no_duplicate_version(
    db_session, seeded
) -> None:
    """重复提交相同内容不应反复插快照（幂等保存不能写爆版本表）。"""
    payload = {"content_html": "<p>旧内容</p>"}  # 与库中现值完全一致

    async with _client_as(db_session, seeded.owner) as client:
        resp = await client.put(f"/api/v1/documents/{seeded.doc.id}", json=payload)
    assert resp.status_code == 200, resp.text
    await db_session.commit()
    assert await _snapshot_count(db_session, seeded.doc.id) == 0


# ======================================================================
# 权限
# ======================================================================


@pytest.mark.asyncio
async def test_put_denied_for_readonly_member(db_session, seeded) -> None:
    """只读成员（KbMember.role=viewer）不得通过 PUT 修改文档。"""
    async with _client_as(db_session, seeded.viewer) as client:
        resp = await client.put(
            f"/api/v1/documents/{seeded.doc.id}", json={"content_html": "<p>越权</p>"}
        )
    assert resp.status_code == 403, resp.text
    await db_session.rollback()

    await db_session.refresh(seeded.doc)
    assert seeded.doc.content_html == "<p>旧内容</p>"
    assert await _snapshot_count(db_session, seeded.doc.id) == 0


@pytest.mark.asyncio
async def test_restore_denied_for_readonly_member(db_session, seeded) -> None:
    """只读成员不得通过 /versions/{id}/restore 覆盖文档内容。

    修复前该端点只做 get_document（读权限），没有 check_write，
    任何能读文档的人都能把内容改回历史版本。
    """
    from app.models.knowledge import DocumentVersion

    version = DocumentVersion(
        doc_id=seeded.doc.id,
        content_html="<p>更早的内容</p>",
        content_json=None,
        author_id=seeded.owner.id,
        summary="历史版本",
    )
    db_session.add(version)
    await db_session.commit()

    async with _client_as(db_session, seeded.viewer) as client:
        resp = await client.post(
            f"/api/v1/documents/{seeded.doc.id}/versions/{version.id}/restore"
        )
    assert resp.status_code == 403, resp.text
    await db_session.rollback()

    await db_session.refresh(seeded.doc)
    assert seeded.doc.content_html == "<p>旧内容</p>"
