"""
文件夹服务 — 知识库文件夹树的创建/重命名/删除与文档归档（P0-3）。

设计约定：
    - path 为 '/'-分隔字符串（"产品/合规"），与 Document.path 共用同一约定；
    - 文件夹节点存 kb_folders 表（可为空文件夹），文档归属通过 Document.path 表达；
    - 树形视图由 folders + documents 聚合返回，不做多跳存储；
    - 重命名/删除采用"前缀批量更新"：只动匹配路径前缀的 folder 行与文档行，
      不遍历子树递归（路径即真相，无父子外键）。

遵循单一职责：本模块只做文件夹树与文档归档的路径运算与持久化，
权限校验由 API 层调用 PermissionService 完成。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folder import KbFolder
from app.models.knowledge import Document
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

_PATH_SEP = "/"


def split_path(path: str | None) -> list[str]:
    """将路径拆分为分段列表（"" / None → []）。"""
    if not path:
        return []
    return [seg for seg in path.split(_PATH_SEP) if seg]


def join_path(segments: list[str]) -> str | None:
    """将分段列表拼回路径（空列表 → None）。"""
    if not segments:
        return None
    return _PATH_SEP.join(segments)


def depth_of(path: str | None) -> int:
    """路径深度 = 分段数（文档/文件夹落在根时深度为 0）。"""
    return len(split_path(path))


class FolderService:
    """文件夹树服务 — 路径运算 + 前缀批量更新。"""

    def __init__(
        self, db: AsyncSession, user: Any, tenant_id: uuid.UUID | None = None
    ) -> None:
        """初始化文件夹服务。

        Args:
            db: 异步数据库会话。
            user: 当前请求的已认证用户（用于 owner 记录，可选）。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: Any = user
        self._tenant_id: uuid.UUID | None = tenant_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_tree(self, kb_id: uuid.UUID) -> dict[str, Any]:
        """返回知识库文件夹树（文件夹 + 文档聚合）。

        Returns:
            {"root": {...}} 根节点 children 按类型/排序排列。
        """
        folder_stmt = select(KbFolder).where(
            KbFolder.kb_id == kb_id,
            KbFolder.deleted_at.is_(None),
        )
        folder_stmt = apply_tenant_filter(folder_stmt, KbFolder, self._tenant_id)
        folder_result = await self.db.execute(folder_stmt)
        folders = list(folder_result.scalars().all())

        doc_stmt = select(Document).where(
            Document.kb_id == kb_id,
            Document.deleted_at.is_(None),
        )
        doc_stmt = apply_tenant_filter(doc_stmt, Document, self._tenant_id)
        doc_result = await self.db.execute(doc_stmt)
        documents = list(doc_result.scalars().all())

        return {"root": self._build_tree(folders, documents)}

    async def get_folder(
        self, folder_id: uuid.UUID, kb_id: uuid.UUID
    ) -> KbFolder | None:
        """按 ID 查询未删除文件夹（限定 kb_id 防越权）。"""
        stmt = select(KbFolder).where(
            KbFolder.id == folder_id,
            KbFolder.kb_id == kb_id,
            KbFolder.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, KbFolder, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    async def create(
        self,
        kb_id: uuid.UUID,
        name: str,
        parent_path: str | None = None,
        sort_order: int = 0,
    ) -> KbFolder:
        """创建文件夹（同名同路径查重）。

        Args:
            kb_id: 知识库 ID。
            name: 文件夹名（单段，不允许包含 '/'）。
            parent_path: 父路径（None = 根级）。
            sort_order: 同级排序。

        Returns:
            新建的 KbFolder。

        Raises:
            ValueError: 名称含 '/' 或 (kb_id, path) 已存在（未删除）。
        """
        name = name.strip().strip(_PATH_SEP)
        if not name:
            raise ValueError("folder_name_empty")
        if _PATH_SEP in name:
            raise ValueError("folder_name_contains_separator")

        parent_segments = split_path(parent_path)
        path = join_path([*parent_segments, name])
        assert path is not None

        dup = await self._find_by_path(kb_id, path)
        if dup is not None:
            log.info("folder.duplicate", kb_id=str(kb_id), path=path)
            raise ValueError("folder_path_exists")

        folder = KbFolder(
            kb_id=kb_id,
            name=name,
            path=path,
            parent_path=join_path(parent_segments),
            depth=len(parent_segments) + 1,
            sort_order=sort_order,
            tenant_id=self._tenant_id,
        )
        self.db.add(folder)
        await self.db.flush()
        log.info("folder.created", folder_id=str(folder.id), path=path, kb_id=str(kb_id))
        return folder

    async def rename(self, folder_id: uuid.UUID, new_name: str) -> KbFolder:
        """重命名文件夹 — 级联更新子文件夹与文档的 path 前缀。

        Args:
            folder_id: 文件夹 ID。
            new_name: 新名称（单段）。

        Returns:
            更新后的 KbFolder。

        Raises:
            ValueError: 文件夹不存在或新名非法/与同级冲突。
        """
        new_name = new_name.strip().strip(_PATH_SEP)
        if not new_name or _PATH_SEP in new_name:
            raise ValueError("folder_name_invalid")

        folder = await self._get_by_id(folder_id)
        if folder is None:
            raise ValueError("folder_not_found")

        old_path = folder.path
        parent_segments = split_path(folder.parent_path)
        new_path = join_path([*parent_segments, new_name])
        assert new_path is not None

        if new_path == old_path:
            return folder
        # 同级查重（排除自身）
        conflict = await self._find_by_path(folder.kb_id, new_path)
        if conflict is not None and conflict.id != folder.id:
            raise ValueError("folder_path_exists")

        prefix = old_path + _PATH_SEP
        # 1) 自身
        folder.name = new_name
        folder.path = new_path
        # 2) 子文件夹（path 以 old_path/ 开头）— 逐条重算 path/parent_path，
        #    避免依赖后端 concat 方言
        sub_folders = await self._query_subfolders(folder.kb_id, old_path)
        old_seg_count = len(split_path(old_path))
        for sub in sub_folders:
            sub_segs = split_path(sub.path)
            sub.path = join_path([new_path, *sub_segs[old_seg_count:]])
            parent_segs = split_path(sub.parent_path)
            sub.parent_path = join_path([new_path, *parent_segs[old_seg_count:]])
        # 3) 文档（path 以 old_path/ 开头 或 == old_path）— 逐条重算
        affected = await self._docs_with_path(folder.kb_id, old_path)
        for doc in affected:
            segs = split_path(doc.path)
            doc.path = join_path([new_path, *segs[old_seg_count:]])
            doc.depth = depth_of(doc.path)
        if sub_folders or affected:
            await self.db.flush()
        log.info("folder.renamed", folder_id=str(folder.id), old_path=old_path, new_path=new_path)
        return folder

    async def delete(self, folder_id: uuid.UUID) -> None:
        """删除文件夹 — 级联软删子文件夹；其中文档移动到父级（移除该段）。

        Args:
            folder_id: 文件夹 ID。

        Raises:
            ValueError: 文件夹不存在。
        """
        folder = await self._get_by_id(folder_id)
        if folder is None:
            raise ValueError("folder_not_found")

        old_path = folder.path
        parent_segments = split_path(folder.parent_path)

        # 1) 文档：path == old_path → 移到根（None）；path 以 old_path/ 开头 → 截掉该段
        affected = await self._docs_with_path(folder.kb_id, old_path)
        for doc in affected:
            segs = split_path(doc.path)
            doc.path = join_path([*parent_segments, *segs[len(split_path(old_path)) :]])
            doc.depth = depth_of(doc.path)
        if affected:
            await self.db.flush()

        # 2) 级联软删自身 + 子文件夹
        prefix = old_path + _PATH_SEP
        stmt = (
            update(KbFolder)
            .where(
                KbFolder.kb_id == folder.kb_id,
                KbFolder.deleted_at.is_(None),
                (KbFolder.path == old_path) | (KbFolder.path.like(prefix + "%")),
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await self.db.execute(stmt)
        log.info("folder.deleted", folder_id=str(folder.id), path=old_path)

    async def move_document(
        self, doc_id: uuid.UUID, path: str | None, sort_order: int | None = None
    ) -> Document:
        """把文档移动到指定文件夹（path=None 表示移到根）。

        Args:
            doc_id: 文档 ID。
            path: 目标文件夹路径（None / "" = 根级）。
            sort_order: 可选，更新同级排序。

        Returns:
            更新后的 Document。

        Raises:
            ValueError: 文档不存在。
        """
        stmt = select(Document).where(
            Document.id == doc_id,
            Document.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        doc = result.scalars().first()
        if doc is None:
            raise ValueError("document_not_found")

        doc.path = join_path(split_path(path)) if path else None
        doc.depth = depth_of(doc.path)
        if sort_order is not None:
            doc.sort_order = sort_order
        await self.db.flush()
        log.info("folder.document_moved", doc_id=str(doc_id), path=doc.path)
        return doc

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get_by_id(self, folder_id: uuid.UUID) -> KbFolder | None:
        stmt = select(KbFolder).where(
            KbFolder.id == folder_id,
            KbFolder.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, KbFolder, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _find_by_path(self, kb_id: uuid.UUID, path: str) -> KbFolder | None:
        stmt = select(KbFolder).where(
            KbFolder.kb_id == kb_id,
            KbFolder.path == path,
            KbFolder.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, KbFolder, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _docs_with_path(self, kb_id: uuid.UUID, path: str) -> list[Document]:
        prefix = path + _PATH_SEP
        stmt = select(Document).where(
            Document.kb_id == kb_id,
            Document.deleted_at.is_(None),
            (Document.path == path) | (Document.path.like(prefix + "%")),
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _query_subfolders(
        self, kb_id: uuid.UUID, parent_path: str
    ) -> list[KbFolder]:
        """查询指定路径下的直接或间接子文件夹（path 以 parent_path/ 开头）。"""
        prefix = parent_path + _PATH_SEP
        stmt = select(KbFolder).where(
            KbFolder.kb_id == kb_id,
            KbFolder.deleted_at.is_(None),
            KbFolder.path.like(prefix + "%"),
        )
        stmt = apply_tenant_filter(stmt, KbFolder, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 树构建（纯函数，便于单测）
    # ------------------------------------------------------------------

    def _build_tree(
        self,
        folders: list[KbFolder],
        documents: list[Document],
    ) -> dict[str, Any]:
        """构建统一树 — 根节点 children 含文件夹与文档节点。

        节点结构::

            {"type": "folder"|"doc", "id": str|None, "name": str,
             "path": str|None, "depth": int, "children": [...]}

        排序：先文件夹后文档，同级按 (sort_order, name) 稳定排序。
        """
        root: dict[str, Any] = {
            "type": "root",
            "id": None,
            "name": "/",
            "path": None,
            "depth": 0,
            "children": [],
        }
        folder_nodes: dict[str, dict[str, Any]] = {}

        for folder in sorted(folders, key=lambda f: (f.depth, f.sort_order, f.name)):
            node: dict[str, Any] = {
                "type": "folder",
                "id": str(folder.id),
                "name": folder.name,
                "path": folder.path,
                "depth": folder.depth,
                "sort_order": folder.sort_order,
                "children": [],
            }
            folder_nodes[folder.path] = node

        doc_nodes_by_path: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for doc in sorted(documents, key=lambda d: (d.sort_order, d.title or "")):
            node: dict[str, Any] = {
                "type": "doc",
                "id": str(doc.id),
                "name": doc.title or "",
                "path": doc.path,
                "depth": doc.depth,
                "sort_order": doc.sort_order,
                "doc_type": doc.doc_type,
            }
            doc_nodes_by_path[doc.path].append(node)

        # 挂文件夹：深度升序保证父先于子；父不存在时挂到根
        for folder in sorted(folders, key=lambda f: (f.depth, f.sort_order)):
            node = folder_nodes[folder.path]
            parent_path = folder.parent_path
            if parent_path and parent_path in folder_nodes:
                folder_nodes[parent_path]["children"].append(node)
            else:
                root["children"].append(node)

        # 挂文档：所属文件夹不存在时挂到根
        for path_key, nodes in doc_nodes_by_path.items():
            if path_key and path_key in folder_nodes:
                folder_nodes[path_key]["children"].extend(nodes)
            else:
                root["children"].extend(nodes)

        def _sort(node: dict[str, Any]) -> None:
            node["children"].sort(
                key=lambda c: (
                    0 if c["type"] == "folder" else 1,
                    c.get("sort_order", 0),
                    c["name"],
                )
            )
            for child in node["children"]:
                if child["type"] == "folder":
                    _sort(child)

        _sort(root)
        return root
