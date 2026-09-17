"""FolderService 单测 — P0-3 文件夹树的路径运算与持久化逻辑。

测试策略：
    - 纯函数（split_path / join_path / depth_of / _build_tree）直接验证；
    - Service 方法通过 mock AsyncSession（add/flush/execute）与 mock
      数据访问方法（_find_by_path / _get_by_id / _docs_with_path）聚焦逻辑；
    - 重命名/删除的级联更新只断言受影响对象的 path/depth 重算结果。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.folder import KbFolder
from app.services.folder_service import (
    FolderService,
    depth_of,
    join_path,
    split_path,
)


# ==================================================================
# 纯函数
# ==================================================================

class TestPathUtils:
    def test_split_path(self) -> None:
        assert split_path("产品/合规/数据安全") == ["产品", "合规", "数据安全"]
        assert split_path("") == []
        assert split_path(None) == []
        assert split_path("产品") == ["产品"]

    def test_join_path(self) -> None:
        assert join_path(["产品", "合规"]) == "产品/合规"
        assert join_path([]) is None
        assert join_path(["产品"]) == "产品"

    def test_depth_of(self) -> None:
        assert depth_of("产品/合规") == 2
        assert depth_of("产品") == 1
        assert depth_of(None) == 0
        assert depth_of("") == 0


class TestBuildTree:
    def _folder(self, path: str, depth: int, sort_order: int = 0) -> SimpleNamespace:
        segs = split_path(path)
        return SimpleNamespace(
            id=uuid.uuid4(), name=segs[-1], path=path,
            parent_path=join_path(segs[:-1]), depth=depth, sort_order=sort_order,
        )

    def _doc(self, path: str | None, sort_order: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(), title=f"doc-{path or 'root'}", path=path,
            depth=depth_of(path), sort_order=sort_order, doc_type="md",
        )

    def test_tree_with_folder_hierarchy(self) -> None:
        service = FolderService(db=None, user=None)  # type: ignore[arg-type]
        folders = [
            self._folder("产品", 1),
            self._folder("产品/合规", 2),
            self._folder("研发", 1),
        ]
        documents = [
            self._doc("产品/合规", 0),
            self._doc(None, 0),
            self._doc("研发", 0),
        ]
        root = service._build_tree(folders, documents)
        children = root["children"]
        # 顶层：文件夹（产品、研发）+ 根文档，文件夹在前
        assert [c["type"] for c in children] == ["folder", "folder", "doc"]
        assert [c["name"] for c in children] == ["产品", "研发", "doc-root"]
        # 产品 → 合规 → 文档
        prod = children[0]
        assert [c["name"] for c in prod["children"]] == ["合规"]
        hegui = prod["children"][0]
        assert [c["name"] for c in hegui["children"]] == ["doc-产品/合规"]

    def test_doc_without_folder_goes_to_root(self) -> None:
        service = FolderService(db=None, user=None)  # type: ignore[arg-type]
        root = service._build_tree(
            [self._folder("产品", 1)],
            [self._doc("产品", 0), self._doc("其他/不存在", 0)],
        )
        # "产品" 文件夹在；"其他/不存在" 无文件夹节点 → 挂根
        assert [c["name"] for c in root["children"]] == ["产品", "doc-其他/不存在"]

    def test_sort_folder_before_doc_and_by_sort_order(self) -> None:
        service = FolderService(db=None, user=None)  # type: ignore[arg-type]
        root = service._build_tree(
            [self._folder("B", 1, sort_order=1), self._folder("A", 1, sort_order=0)],
            [self._doc(None, sort_order=9)],
        )
        assert [c["name"] for c in root["children"]] == ["A", "B", "doc-root"]


# ==================================================================
# Service 方法
# ==================================================================

class _FakeSession:
    """最小 AsyncSession 替身 — 只支持 add(同步)/flush(异步)/execute(返回空)。"""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed: int = 0

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, stmt):  # noqa: ANN001
        return SimpleNamespace(scalars=lambda: SimpleNamespace(
            first=lambda: None, all=lambda: [], __iter__=lambda self: iter([])
        ))


def _make_folder(**kw) -> KbFolder:
    defaults = dict(
        id=uuid.uuid4(), kb_id=uuid.uuid4(), name="产品", path="产品",
        parent_path=None, depth=1, sort_order=0, tenant_id=None,
    )
    defaults.update(kw)
    return KbFolder(**defaults)


def _make_doc(**kw) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(), kb_id=uuid.uuid4(), title="文档", path=None,
        depth=0, sort_order=0, deleted_at=None, doc_type="md",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_create_folder_basic() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    kb_id = uuid.uuid4()
    # mock 查重为空
    service._find_by_path = _async_none

    folder = await service.create(kb_id, name="合规", parent_path="产品")

    assert folder.path == "产品/合规"
    assert folder.depth == 2
    assert folder.parent_path == "产品"
    assert db.added and db.flushed >= 1


@pytest.mark.asyncio
async def test_create_folder_duplicate_raises() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    existing = _make_folder(path="产品/合规")
    service._find_by_path = _async_value(existing)

    with pytest.raises(ValueError):
        await service.create(uuid.uuid4(), name="合规", parent_path="产品")


@pytest.mark.asyncio
async def test_create_folder_name_contains_separator_raises() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await service.create(uuid.uuid4(), name="合规/数据")


@pytest.mark.asyncio
async def test_rename_folder_cascades_children_and_docs() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    folder = _make_folder(path="产品", parent_path=None, depth=1)
    service._get_by_id = _async_value(folder)
    service._find_by_path = _async_none  # 同级无冲突

    # 子文件夹与文档
    sub = _make_folder(path="产品/合规", parent_path="产品", depth=2)
    doc = _make_doc(path="产品/合规/手册", depth=3)
    service._docs_with_path = _async_value([doc])

    # 子文件夹查询：返回 mock 的 execute 结果 —— 直接替换 _query_subfolders
    async def _fake_subquery(*_a, **_kw):
        return [sub]

    service._query_subfolders = _fake_subquery

    updated = await service.rename(folder.id, "产品线")

    assert updated.path == "产品线"
    assert sub.path == "产品线/合规"
    assert sub.parent_path == "产品线"
    assert doc.path == "产品线/合规/手册"
    assert doc.depth == 3


@pytest.mark.asyncio
async def test_delete_folder_moves_docs_to_parent() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    folder = _make_folder(path="产品/合规", parent_path="产品", depth=2)
    service._get_by_id = _async_value(folder)
    doc_same = _make_doc(path="产品/合规", depth=2)
    doc_child = _make_doc(path="产品/合规/手册", depth=3)
    service._docs_with_path = _async_value([doc_same, doc_child])
    service._query_subfolders = _async_value([])

    await service.delete(folder.id)

    # 文档重算：同路径文档移到父级（父 = "产品"）；子文档截掉 "合规" 段
    assert doc_same.path == "产品"
    assert doc_same.depth == 1
    assert doc_child.path == "产品/手册"
    assert doc_child.depth == 2


@pytest.mark.asyncio
async def test_move_document_to_folder() -> None:
    db = _FakeSession()
    service = FolderService(db=db, user=None)  # type: ignore[arg-type]
    doc = _make_doc(path=None, depth=0)
    service.db = _FakeSession()

    async def fake_execute(stmt):
        # 捕获 select(Document) 并返回 doc
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: doc, all=lambda: [], __iter__=lambda self: iter([])
            )
        )

    service.db.execute = fake_execute
    updated = await service.move_document(doc.id, "产品/合规")

    assert updated.path == "产品/合规"
    assert updated.depth == 2


# ==================================================================
# 辅助 mock
# ==================================================================

async def _async_none(*_a, **_kw):
    return None


def _async_value(value):
    async def _inner(*_a, **_kw):
        return value

    return _inner
