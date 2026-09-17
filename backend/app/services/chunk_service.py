"""
分块服务 — 检索分块的持久化、编辑快照、diff/回滚与索引重建（P0-2）。

核心流程：
    - init_chunks：把文档正文分块并落库（幂等，已存在则返回现有）；
    - update_chunk / rollback：编辑前把当前正文快照进 chunk_versions，
      更新正文后重建整篇文档的向量索引（upsert 覆盖同 doc_id）；
    - get_version：返回版本快照与相对当前正文的 unified diff。

设计约定：
    - 正文编辑以 document_chunks 为事实来源，重索引以 DB 分块为准，
      不依赖内存 Chunk 对象；
    - 重索引失败时向上抛错（编辑成功但索引失败属一致性问题，需告知调用方），
      测试通过 mock _reindex_document 隔离。

遵循单一职责：本模块只做分块 CRUD + 版本 + 索引重建，
权限校验由 API 层调用 PermissionService 完成。
"""

from __future__ import annotations

import difflib
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import ChunkVersion, DocumentChunk
from app.models.knowledge import Document
from app.rag.chunker import SemanticChunker, estimate_tokens
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

_MAX_DIFF_LINES = 200


class ChunkService:
    """分块服务 — 分块 CRUD + 版本历史 + 索引重建。"""

    def __init__(
        self, db: AsyncSession, user: Any, tenant_id: uuid.UUID | None = None
    ) -> None:
        """初始化分块服务。

        Args:
            db: 异步数据库会话。
            user: 当前请求的已认证用户（用于 author_id 记录，可选）。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: Any = user
        self._tenant_id: uuid.UUID | None = tenant_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_document(self, doc_id: uuid.UUID) -> Document | None:
        """按 ID 查询未删除文档。"""
        stmt = select(Document).where(Document.id == doc_id, Document.deleted_at.is_(None))
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def list_chunks(self, doc_id: uuid.UUID) -> list[DocumentChunk]:
        """列出文档的持久化分块（按 chunk_index 升序）。"""
        stmt = select(DocumentChunk).where(
            DocumentChunk.doc_id == doc_id,
            DocumentChunk.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, DocumentChunk, self._tenant_id)
        result = await self.db.execute(stmt)
        chunks = list(result.scalars().all())
        return sorted(chunks, key=lambda c: c.chunk_index)

    async def get_chunk(self, chunk_id: uuid.UUID) -> DocumentChunk | None:
        """按 ID 查询未删除分块。"""
        stmt = select(DocumentChunk).where(
            DocumentChunk.id == chunk_id, DocumentChunk.deleted_at.is_(None)
        )
        stmt = apply_tenant_filter(stmt, DocumentChunk, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # 初始化与编辑
    # ------------------------------------------------------------------

    async def init_chunks(self, doc_id: uuid.UUID) -> list[DocumentChunk]:
        """初始化文档分块 — 幂等：已有分块直接返回，否则分块并落库。

        Args:
            doc_id: 文档 ID。

        Returns:
            持久化分块列表（按 chunk_index 升序）。

        Raises:
            ValueError: 文档不存在或文档无正文可分块。
        """
        doc = await self.get_document(doc_id)
        if doc is None:
            raise ValueError("document_not_found")

        existing = await self.list_chunks(doc_id)
        if existing:
            log.info("chunk.init_skipped", doc_id=str(doc_id), count=len(existing))
            return existing

        content = doc.content_text or ""
        if not content.strip():
            raise ValueError("document_empty_content")

        chunks = SemanticChunker().chunk(
            content=content,
            doc_type=doc.doc_type or "md",
            content_type="auto",
            doc_id=str(doc_id),
        )
        if not chunks:
            raise ValueError("document_chunk_failed")

        rows: list[DocumentChunk] = []
        for index, chunk in enumerate(chunks):
            row = DocumentChunk(
                doc_id=doc.id,
                kb_id=doc.kb_id,
                chunk_index=index,
                section_path=chunk.title_path or None,
                content_text=chunk.content,
                token_count=chunk.token_count or estimate_tokens(chunk.content),
                tenant_id=self._tenant_id,
            )
            self.db.add(row)
            rows.append(row)
        await self.db.flush()
        log.info("chunk.initialized", doc_id=str(doc_id), count=len(rows))
        return rows

    async def update_chunk(
        self,
        chunk_id: uuid.UUID,
        content: str,
        author_id: uuid.UUID | None = None,
        summary: str | None = None,
    ) -> DocumentChunk:
        """编辑分块正文 — 快照旧内容 + 更新正文 + 重建文档索引。

        Args:
            chunk_id: 分块 ID。
            content: 新正文。
            author_id: 操作者 ID（默认取当前用户）。
            summary: 编辑摘要。

        Returns:
            更新后的 DocumentChunk。

        Raises:
            ValueError: 分块不存在或新正文为空。
        """
        chunk = await self.get_chunk(chunk_id)
        if chunk is None:
            raise ValueError("chunk_not_found")
        content = content.strip()
        if not content:
            raise ValueError("chunk_content_empty")

        await self._snapshot(chunk, author_id=author_id, summary=summary)
        chunk.content_text = content
        chunk.token_count = estimate_tokens(content)
        await self.db.flush()
        await self._reindex_document(chunk.doc_id)
        log.info(
            "chunk.updated", chunk_id=str(chunk_id), doc_id=str(chunk.doc_id)
        )
        return chunk

    async def rollback(
        self,
        chunk_id: uuid.UUID,
        version_id: uuid.UUID,
        author_id: uuid.UUID | None = None,
    ) -> DocumentChunk:
        """回滚分块到指定版本 — 快照当前 + 写回版本内容 + 重建索引。

        Args:
            chunk_id: 分块 ID。
            version_id: 目标版本 ID。
            author_id: 操作者 ID（默认取当前用户）。

        Returns:
            更新后的 DocumentChunk。

        Raises:
            ValueError: 分块或版本不存在。
        """
        chunk = await self.get_chunk(chunk_id)
        if chunk is None:
            raise ValueError("chunk_not_found")
        stmt = select(ChunkVersion).where(
            ChunkVersion.id == version_id, ChunkVersion.chunk_id == chunk.id
        )
        result = await self.db.execute(stmt)
        version = result.scalars().first()
        if version is None:
            raise ValueError("chunk_version_not_found")

        # 快照当前内容（新版本），再写回目标版本内容
        await self._snapshot(chunk, author_id=author_id, summary=f"rollback to v{version.version_seq}")
        chunk.content_text = version.content_text
        chunk.token_count = estimate_tokens(version.content_text)
        await self.db.flush()
        await self._reindex_document(chunk.doc_id)
        log.info(
            "chunk.rolled_back", chunk_id=str(chunk_id), version_seq=version.version_seq
        )
        return chunk

    # ------------------------------------------------------------------
    # 版本历史
    # ------------------------------------------------------------------

    async def list_versions(self, chunk_id: uuid.UUID) -> list[ChunkVersion]:
        """列出分块版本历史（版本号降序）。"""
        stmt = select(ChunkVersion).where(ChunkVersion.chunk_id == chunk_id)
        result = await self.db.execute(stmt)
        versions = list(result.scalars().all())
        return sorted(versions, key=lambda v: v.version_seq, reverse=True)

    async def get_version(
        self, chunk_id: uuid.UUID, version_id: uuid.UUID
    ) -> tuple[ChunkVersion, list[str] | None]:
        """返回指定版本与相对当前正文的 unified diff。

        Returns:
            (version, diff_lines)；diff 为 None 表示版本内容与当前一致。

        Raises:
            ValueError: 分块或版本不存在。
        """
        chunk = await self.get_chunk(chunk_id)
        if chunk is None:
            raise ValueError("chunk_not_found")
        stmt = select(ChunkVersion).where(
            ChunkVersion.id == version_id, ChunkVersion.chunk_id == chunk.id
        )
        result = await self.db.execute(stmt)
        version = result.scalars().first()
        if version is None:
            raise ValueError("chunk_version_not_found")

        diff_lines = self._make_diff(version.content_text, chunk.content_text)
        return version, diff_lines

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _snapshot(
        self,
        chunk: DocumentChunk,
        author_id: uuid.UUID | None,
        summary: str | None,
    ) -> ChunkVersion:
        """把分块当前正文快照为新版本（version_seq = 文档内 max+1）。"""
        stmt = select(func.max(ChunkVersion.version_seq)).where(
            ChunkVersion.chunk_id == chunk.id
        )
        result = await self.db.execute(stmt)
        current_max = result.scalar() or 0
        version = ChunkVersion(
            chunk_id=chunk.id,
            version_seq=current_max + 1,
            content_text=chunk.content_text,
            author_id=author_id or getattr(self.user, "id", uuid.uuid4()),
            summary=summary,
        )
        self.db.add(version)
        return version

    def _make_diff(self, old_text: str, new_text: str) -> list[str] | None:
        """生成 unified diff（截断上限，避免超大 diff 撑爆响应）。"""
        if old_text == new_text:
            return None
        diff = list(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="version",
                tofile="current",
            )
        )
        return diff[:_MAX_DIFF_LINES]

    async def _reindex_document(self, doc_id: uuid.UUID) -> None:
        """重建文档的向量索引 — 以 DB 分块为事实来源整篇覆盖 upsert。

        延迟导入工厂，避免模块导入期加载向量后端；
        重索引失败抛错（编辑已提交，需由调用方决定补偿策略）。
        """
        from app.llm.embedder import get_embedder
        from app.rag.vector_store.factory import get_vector_store
        from app.rag.chunker import Chunk

        doc = await self.get_document(doc_id)
        if doc is None:
            raise ValueError("document_not_found")
        chunks = await self.list_chunks(doc_id)
        if not chunks:
            return

        embedder = get_embedder()
        embeddings = await embedder.embed([c.content_text for c in chunks])
        store = get_vector_store()
        chunk_objs = [
            Chunk(
                id=str(c.id),
                doc_id=str(doc.id),
                content=c.content_text,
                token_count=c.token_count,
                title_path=c.section_path or "",
            )
            for c in chunks
        ]
        await store.upsert(
            doc_id=str(doc.id),
            chunks=chunk_objs,
            embeddings=embeddings,
            kb_id=str(doc.kb_id) if doc.kb_id else None,
        )
        log.info("chunk.reindexed", doc_id=str(doc_id), count=len(chunks))
