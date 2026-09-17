"""ChunkService 单测 — P0-2 分块编辑 + 版本历史。

测试策略：
    - 通过 patch 隔离数据访问（get_document / get_chunk / list_chunks /
      _snapshot / _reindex_document），聚焦业务逻辑；
    - init_chunks 通过 mock SemanticChunker 控制分块产出，
      用 FakeDB 收集 add/flush 的持久化对象；
    - diff 走真实 _make_diff（difflib），断言统一 diff 结构。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.chunk import ChunkVersion, DocumentChunk
from app.rag.chunker import Chunk
from app.services.chunk_service import ChunkService


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
        id=uuid.uuid4(), kb_id=uuid.uuid4(), title="测试文档",
        content_text="# 标题\n正文内容段落", doc_type="md",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_chunk(**kw) -> DocumentChunk:
    defaults = dict(
        id=uuid.uuid4(), doc_id=uuid.uuid4(), kb_id=uuid.uuid4(),
        chunk_index=0, section_path="标题", content_text="旧内容 v1",
        token_count=10, tenant_id=None, deleted_at=None,
    )
    defaults.update(kw)
    return DocumentChunk(**defaults)


def _make_version(**kw) -> ChunkVersion:
    defaults = dict(
        id=uuid.uuid4(), chunk_id=uuid.uuid4(), version_seq=1,
        content_text="旧内容 v1", author_id=uuid.uuid4(), summary=None,
    )
    defaults.update(kw)
    return ChunkVersion(**defaults)


# ==================================================================
# init_chunks
# ==================================================================

@pytest.mark.asyncio
async def test_init_chunks_persists_chunks() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    doc = _make_doc()
    service.get_document = AsyncMock(return_value=doc)
    service.list_chunks = AsyncMock(return_value=[])

    fake_chunks = [
        Chunk(id="c1", doc_id=str(doc.id), content="第一块", title_path="标题", token_count=4),
        Chunk(id="c2", doc_id=str(doc.id), content="第二块", title_path="", token_count=4),
    ]
    with patch("app.services.chunk_service.SemanticChunker") as mock_cls:
        mock_cls.return_value.chunk.return_value = fake_chunks
        rows = await service.init_chunks(doc.id)

    assert len(rows) == 2
    assert db.flushed >= 1
    persisted = [o for o in db.added if isinstance(o, DocumentChunk)]
    assert len(persisted) == 2
    assert persisted[0].chunk_index == 0
    assert persisted[0].content_text == "第一块"
    assert persisted[0].kb_id == doc.kb_id


@pytest.mark.asyncio
async def test_init_chunks_idempotent() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    doc = _make_doc()
    service.get_document = AsyncMock(return_value=doc)
    existing = [_make_chunk(doc_id=doc.id)]
    service.list_chunks = AsyncMock(return_value=existing)

    rows = await service.init_chunks(doc.id)

    assert rows == existing
    assert db.flushed == 0


@pytest.mark.asyncio
async def test_init_chunks_empty_content_raises() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    doc = _make_doc(content_text="   ")
    service.get_document = AsyncMock(return_value=doc)
    service.list_chunks = AsyncMock(return_value=[])

    with pytest.raises(ValueError):
        await service.init_chunks(doc.id)


# ==================================================================
# update_chunk
# ==================================================================

@pytest.mark.asyncio
async def test_update_chunk_snapshots_and_reindexes() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk()
    service.get_chunk = AsyncMock(return_value=chunk)
    snapshot = _make_version(chunk_id=chunk.id, version_seq=1)
    service._snapshot = AsyncMock(return_value=snapshot)
    service._reindex_document = AsyncMock()

    updated = await service.update_chunk(
        chunk.id, content="新内容 v2", author_id=uuid.uuid4(), summary="改表述"
    )

    assert updated.content_text == "新内容 v2"
    assert updated.token_count > 0
    service._snapshot.assert_awaited_once()
    service._reindex_document.assert_awaited_once_with(chunk.doc_id)


@pytest.mark.asyncio
async def test_update_chunk_missing_raises() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    service.get_chunk = AsyncMock(return_value=None)

    with pytest.raises(ValueError):
        await service.update_chunk(uuid.uuid4(), content="x")


# ==================================================================
# rollback
# ==================================================================

@pytest.mark.asyncio
async def test_rollback_restores_version_content() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk(content_text="当前内容 v3")
    service.get_chunk = AsyncMock(return_value=chunk)
    version = _make_version(chunk_id=chunk.id, version_seq=2, content_text="回滚目标 v2")
    service._snapshot = AsyncMock(return_value=_make_version(chunk_id=chunk.id, version_seq=3))

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: version, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute
    service._reindex_document = AsyncMock()

    updated = await service.rollback(chunk.id, version.id, author_id=uuid.uuid4())

    assert updated.content_text == "回滚目标 v2"
    service._reindex_document.assert_awaited_once_with(chunk.doc_id)


@pytest.mark.asyncio
async def test_rollback_missing_version_raises() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk()
    service.get_chunk = AsyncMock(return_value=chunk)

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: None, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute

    with pytest.raises(ValueError):
        await service.rollback(chunk.id, uuid.uuid4())


# ==================================================================
# 版本查询与 diff
# ==================================================================

@pytest.mark.asyncio
async def test_list_versions_desc_order() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    v1 = _make_version(version_seq=1)
    v2 = _make_version(version_seq=2)

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: None, all=lambda: [v1, v2], __iter__=lambda self: iter([v1, v2])
            )
        )

    service.db.execute = fake_execute
    versions = await service.list_versions(v1.chunk_id)
    assert [v.version_seq for v in versions] == [2, 1]


@pytest.mark.asyncio
async def test_get_version_returns_diff() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk(content_text="当前\n内容B")
    service.get_chunk = AsyncMock(return_value=chunk)
    version = _make_version(chunk_id=chunk.id, content_text="旧\n内容A")

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: version, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute
    got, diff_lines = await service.get_version(chunk.id, version.id)

    assert got.id == version.id
    assert diff_lines is not None
    assert any("内容A" in line for line in diff_lines)
    assert any("内容B" in line for line in diff_lines)


@pytest.mark.asyncio
async def test_get_version_no_diff_when_identical() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk(content_text="相同内容")
    service.get_chunk = AsyncMock(return_value=chunk)
    version = _make_version(chunk_id=chunk.id, content_text="相同内容")

    async def fake_execute(stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: version, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute
    _, diff_lines = await service.get_version(chunk.id, version.id)
    assert diff_lines is None


# ==================================================================
# _snapshot 版本号递增
# ==================================================================

@pytest.mark.asyncio
async def test_snapshot_version_seq_increments() -> None:
    db = _FakeDB()
    service = ChunkService(db=db, user=None)  # type: ignore[arg-type]
    chunk = _make_chunk(content_text="v1 内容")

    async def fake_execute(stmt):
        return SimpleNamespace(scalar=lambda: 5)

    service.db.execute = fake_execute
    version = await service._snapshot(chunk, author_id=uuid.uuid4(), summary="s")

    assert version.version_seq == 6
    assert version.content_text == "v1 内容"
    assert any(isinstance(o, ChunkVersion) for o in db.added)
