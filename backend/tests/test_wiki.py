"""WikiService 单测 — P0-1 自动生成互链知识库。

测试策略：
    - 纯函数（_extract_links / _parse_title）直接验证；
    - generate：patch _list_docs / _list_pages / _generate_page_md / _rebuild_links，
      用 FakeDB 收集 WikiPage/WikiPageVersion 写入，验证幂等 upsert 与版本；
    - update / rollback：patch 数据访问与快照/链接重建，聚焦主流程；
    - graph：mock list_pages 与 execute，验证节点/边结构。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.wiki import WikiPage, WikiPageLink, WikiPageVersion
from app.services.wiki_service import WikiService


# ==================================================================
# 测试夹具
# ==================================================================

class _FakeDB:
    """最小 AsyncSession 替身 — add(同步)/flush(异步) 收集写入。"""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed: int = 0

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


def _make_doc(**kw) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(), kb_id=uuid.uuid4(), title="产品手册",
        content_text="# 产品手册\n这是产品介绍", doc_type="md",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_page(**kw) -> WikiPage:
    defaults = dict(
        id=uuid.uuid4(), kb_id=uuid.uuid4(), source_doc_id=None,
        title="产品手册", content_md="# 产品手册\n正文", status="draft",
        author_id=uuid.uuid4(), tenant_id=None, deleted_at=None,
    )
    defaults.update(kw)
    return WikiPage(**defaults)


def _make_version(**kw) -> WikiPageVersion:
    defaults = dict(
        id=uuid.uuid4(), page_id=uuid.uuid4(), version_seq=1,
        title="旧标题", content_md="旧正文", author_id=uuid.uuid4(), summary=None,
    )
    defaults.update(kw)
    return WikiPageVersion(**defaults)


class _FakeLLM:
    """假 LLM — chat 返回固定 Markdown。"""

    def __init__(self, output: str) -> None:
        self._output = output

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        del messages, kwargs
        yield self._output


# ==================================================================
# 纯函数
# ==================================================================

class TestWikiPure:
    def test_extract_links(self) -> None:
        md = "参见 [[产品手册]] 与 [[API 参考]]，重复 [[产品手册]]。"
        assert WikiService._extract_links(md) == ["产品手册", "API 参考"]

    def test_extract_links_empty(self) -> None:
        assert WikiService._extract_links("无链接正文") == []

    def test_parse_title_from_heading(self) -> None:
        md = "# 新页面标题\n\n## 章节\n正文"
        assert WikiService._parse_title(md, "fallback") == "新页面标题"

    def test_parse_title_fallback(self) -> None:
        md = "没有标题的正文"
        assert WikiService._parse_title(md, "默认标题") == "默认标题"


# ==================================================================
# generate
# ==================================================================

@pytest.mark.asyncio
async def test_generate_creates_pages_and_versions() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    kb_id = uuid.uuid4()
    doc1 = _make_doc(kb_id=kb_id, title="产品手册")
    doc2 = _make_doc(kb_id=kb_id, title="API 参考")
    service._list_docs = AsyncMock(return_value=[doc1, doc2])
    service._list_pages = AsyncMock(return_value=[])
    service._rebuild_links = AsyncMock()

    md1 = "# 产品手册\n\n介绍。相关：[[API 参考]]"
    md2 = "# API 参考\n\n接口列表。相关：[[产品手册]]"

    async def _side_effect(doc, linkable_titles):  # noqa: ANN001
        return md1 if doc.title == "产品手册" else md2

    service._generate_page_md = AsyncMock(side_effect=_side_effect)
    result = await service.generate(kb_id, author_id=uuid.uuid4())

    assert result["created"] == 2
    assert result["failed"] == 0
    pages = [o for o in db.added if isinstance(o, WikiPage)]
    assert len(pages) == 2
    assert {p.title for p in pages} == {"产品手册", "API 参考"}
    service._rebuild_links.assert_awaited_once_with(kb_id)


@pytest.mark.asyncio
async def test_generate_updates_existing_page_and_snapshots() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    kb_id = uuid.uuid4()
    doc = _make_doc(kb_id=kb_id, title="产品手册")
    existing = _make_page(kb_id=kb_id, source_doc_id=doc.id, title="产品手册")
    service._list_docs = AsyncMock(return_value=[doc])
    service._list_pages = AsyncMock(return_value=[existing])
    service._rebuild_links = AsyncMock()
    service._generate_page_md = AsyncMock(
        return_value="# 产品手册\n\n更新后的正文"
    )
    service._snapshot = AsyncMock(
        return_value=_make_version(page_id=existing.id, version_seq=1)
    )

    result = await service.generate(kb_id, author_id=uuid.uuid4())

    assert result["created"] == 0
    assert result["updated"] == 1
    assert existing.content_md == "# 产品手册\n\n更新后的正文"
    service._snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_failed_page_skipped() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    kb_id = uuid.uuid4()
    doc_ok = _make_doc(kb_id=kb_id, title="正常文档")
    doc_bad = _make_doc(kb_id=kb_id, title="坏文档")
    service._list_docs = AsyncMock(return_value=[doc_ok, doc_bad])
    service._list_pages = AsyncMock(return_value=[])
    service._rebuild_links = AsyncMock()

    async def _side(doc, linkable):  # noqa: ANN001
        if doc.title == "坏文档":
            raise RuntimeError("llm down")
        return "# 正常文档\n\nok"

    service._generate_page_md = AsyncMock(side_effect=_side)

    result = await service.generate(kb_id, author_id=uuid.uuid4())

    assert result["created"] == 1
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_generate_no_docs_raises() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    service._list_docs = AsyncMock(return_value=[])
    with pytest.raises(ValueError):
        await service.generate(uuid.uuid4())


# ==================================================================
# 编辑 / 回滚
# ==================================================================

@pytest.mark.asyncio
async def test_update_page_snapshots_and_rebuilds_links() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    page = _make_page()
    service.get_page = AsyncMock(return_value=page)
    service._snapshot = AsyncMock(return_value=_make_version(page_id=page.id))
    service._rebuild_page_links = AsyncMock()

    updated = await service.update_page(
        page.id, title="新标题", content_md="# 新标题\n内容", author_id=uuid.uuid4()
    )

    assert updated.title == "新标题"
    service._snapshot.assert_awaited_once()
    service._rebuild_page_links.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_update_page_missing_raises() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    service.get_page = AsyncMock(return_value=None)
    with pytest.raises(ValueError):
        await service.update_page(uuid.uuid4(), title="t", content_md="c")


@pytest.mark.asyncio
async def test_rollback_restores_version() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    page = _make_page(title="当前标题", content_md="当前正文")
    service.get_page = AsyncMock(return_value=page)
    version = _make_version(page_id=page.id, title="旧标题", content_md="旧正文")
    service._snapshot = AsyncMock(return_value=_make_version(page_id=page.id, version_seq=2))
    service._rebuild_page_links = AsyncMock()

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: version, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute

    updated = await service.rollback(page.id, version.id, author_id=uuid.uuid4())

    assert updated.title == "旧标题"
    assert updated.content_md == "旧正文"
    service._rebuild_page_links.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_list_versions_desc() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    v1 = _make_version(version_seq=1)
    v2 = _make_version(version_seq=2)

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: None, all=lambda: [v1, v2], __iter__=lambda self: iter([v1, v2])
            )
        )

    service.db.execute = fake_execute
    versions = await service.list_versions(v1.page_id)
    assert [v.version_seq for v in versions] == [2, 1]


# ==================================================================
# 图谱
# ==================================================================

@pytest.mark.asyncio
async def test_graph_returns_nodes_and_edges() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    p1 = _make_page(title="A")
    p2 = _make_page(title="B")
    service.list_pages = AsyncMock(return_value=[p1, p2])
    link = WikiPageLink(
        kb_id=p1.kb_id, from_page_id=p1.id, to_page_id=p2.id, link_text="B"
    )

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: None, all=lambda: [link], __iter__=lambda self: iter([link])
            )
        )

    service.db.execute = fake_execute

    data = await service.graph(p1.kb_id)

    assert len(data["nodes"]) == 2
    assert data["edges"] == [{"from": str(p1.id), "to": str(p2.id), "text": "B"}]


# ==================================================================
# 快照版本号
# ==================================================================

@pytest.mark.asyncio
async def test_snapshot_version_seq_increments() -> None:
    db = _FakeDB()
    service = WikiService(db=db, user=None)  # type: ignore[arg-type]
    page = _make_page()

    async def fake_execute(stmt):
        return SimpleNamespace(scalar=lambda: 3)

    service.db.execute = fake_execute
    version = await service._snapshot(page, author_id=uuid.uuid4(), summary="s")

    assert version.version_seq == 4
    assert any(isinstance(o, WikiPageVersion) for o in db.added)
