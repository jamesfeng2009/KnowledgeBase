"""
Wiki 服务 — Agent 自动生成互链 Markdown 知识库的生成/编辑/回滚/图谱（P0-1）。

核心流程：
    - generate：读 KB 全部文档 → LLM 逐篇生成 Markdown Wiki 页 →
      幂等 upsert（同 source_doc_id 更新 + 追加版本）→ 重建全部互链；
    - update_page / rollback：编辑前快照进 wiki_page_versions，
      更新后重建该页互链；
    - graph：由 wiki_page_links 输出节点/边，支撑可视化知识图谱。

互链约定：正文中用 [[页面标题]] 标注内部链接；链接仅指向已存在页面标题，
未命中的 [[...]] 保留原文（作为待建链接占位）。

设计约定：
    - 生成失败只跳过单篇并记录 warning（部分成功），不中断整库；
    - 链接重建为整库重扫（页面数级小，避免增量维护复杂度）；
    - 版本号页内递增（max+1），回滚 = 快照当前 + 写回版本内容。

遵循单一职责：本模块只做 Wiki 页面与链接的生成/CRUD/版本，
权限校验由 API 层调用 PermissionService 完成。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document
from app.models.wiki import WikiPage, WikiPageLink, WikiPageVersion
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_MAX_INPUT_CHARS = 8000

_GENERATE_SYSTEM_PROMPT = """你是企业知识库的 Wiki 编辑。请把给定的原始文档改写为结构化的 Markdown Wiki 页面。

要求：
1. 第一行必须是页面标题，格式：# 标题
2. 正文按章节组织（使用 ## 二级标题），保留关键事实、数字、日期和结论，不编造内容
3. 如果正文提到本知识库中的其他主题，请在"可链接标题"清单中选择对应标题，用 [[标题]] 语法标注（可多次出现）
4. 只输出 Markdown 正文本身，不要输出任何解释、前言或后记
5. 若原始文档过短，直接输出简短概述页

可链接标题：
{linkable_titles}"""


class WikiService:
    """Wiki 服务 — 页面生成、编辑、版本与图谱。"""

    def __init__(
        self, db: AsyncSession, user: Any, tenant_id: uuid.UUID | None = None
    ) -> None:
        """初始化 Wiki 服务。

        Args:
            db: 异步数据库会话。
            user: 当前请求的已认证用户（用于 author_id 记录）。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: Any = user
        self._tenant_id: uuid.UUID | None = tenant_id

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    async def generate(
        self, kb_id: uuid.UUID, author_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """为知识库全部文档生成/更新 Wiki 页面并重建互链。

        Args:
            kb_id: 知识库 ID。
            author_id: 操作者 ID（默认取当前用户）。

        Returns:
            {"created": int, "updated": int, "failed": int, "pages": int}。

        Raises:
            ValueError: 知识库内没有可生成的文档。
        """
        author_id = author_id or getattr(self.user, "id", uuid.uuid4())
        docs = await self._list_docs(kb_id)
        if not docs:
            raise ValueError("kb_no_documents")

        # 现有页面标题 → id（用于链接匹配）
        existing_pages = await self._list_pages(kb_id)
        pages_by_title: dict[str, WikiPage] = {p.title: p for p in existing_pages}

        created = 0
        updated = 0
        failed = 0

        for doc in docs:
            try:
                content_md = await self._generate_page_md(doc, [p.title for p in pages_by_title])
                title = self._parse_title(content_md, doc.title or "未命名")
                page = pages_by_title.get(title)
                if page is None:
                    # 按 source_doc_id 再匹配一次（标题可能变化）
                    page = next(
                        (p for p in pages_by_title.values() if p.source_doc_id == doc.id),
                        None,
                    )
                if page is None:
                    page = WikiPage(
                        kb_id=kb_id,
                        source_doc_id=doc.id,
                        title=title,
                        content_md=content_md,
                        status="draft",
                        author_id=author_id,
                        tenant_id=self._tenant_id,
                    )
                    self.db.add(page)
                    created += 1
                else:
                    await self._snapshot(page, author_id=author_id, summary="auto-generate")
                    page.title = title
                    page.content_md = content_md
                    updated += 1
                pages_by_title[title] = page
            except Exception as exc:  # noqa: BLE001 — 单篇失败跳过
                failed += 1
                log.warning(
                    "wiki.generate_page_failed",
                    doc_id=str(doc.id), error=str(exc),
                )
        await self.db.flush()
        await self._rebuild_links(kb_id)
        await self.db.flush()
        log.info(
            "wiki.generated", kb_id=str(kb_id), created=created, updated=updated, failed=failed
        )
        return {"created": created, "updated": updated, "failed": failed, "pages": len(pages_by_title)}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_pages(self, kb_id: uuid.UUID) -> list[WikiPage]:
        """列出知识库 Wiki 页面（标题升序）。"""
        stmt = select(WikiPage).where(
            WikiPage.kb_id == kb_id, WikiPage.deleted_at.is_(None)
        )
        stmt = apply_tenant_filter(stmt, WikiPage, self._tenant_id)
        result = await self.db.execute(stmt)
        pages = list(result.scalars().all())
        return sorted(pages, key=lambda p: p.title)

    async def get_page(self, page_id: uuid.UUID) -> WikiPage | None:
        """按 ID 查询未删除页面。"""
        stmt = select(WikiPage).where(
            WikiPage.id == page_id, WikiPage.deleted_at.is_(None)
        )
        stmt = apply_tenant_filter(stmt, WikiPage, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def get_outbound_links(self, page_id: uuid.UUID) -> list[WikiPageLink]:
        """查询页面出链。"""
        stmt = select(WikiPageLink).where(WikiPageLink.from_page_id == page_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def graph(self, kb_id: uuid.UUID) -> dict[str, Any]:
        """返回知识库 Wiki 图谱（节点 + 边）。"""
        pages = await self.list_pages(kb_id)
        stmt = select(WikiPageLink).where(WikiPageLink.kb_id == kb_id)
        result = await self.db.execute(stmt)
        links = list(result.scalars().all())
        return {
            "nodes": [
                {
                    "id": str(p.id),
                    "title": p.title,
                    "source_doc_id": str(p.source_doc_id) if p.source_doc_id else None,
                }
                for p in pages
            ],
            "edges": [
                {
                    "from": str(link.from_page_id),
                    "to": str(link.to_page_id),
                    "text": link.link_text,
                }
                for link in links
            ],
        }

    # ------------------------------------------------------------------
    # 编辑 / 版本
    # ------------------------------------------------------------------

    async def update_page(
        self,
        page_id: uuid.UUID,
        title: str,
        content_md: str,
        author_id: uuid.UUID | None = None,
        summary: str | None = None,
    ) -> WikiPage:
        """编辑 Wiki 页面 — 快照 + 更新 + 重建链接。

        Raises:
            ValueError: 页面不存在或正文为空。
        """
        page = await self.get_page(page_id)
        if page is None:
            raise ValueError("wiki_page_not_found")
        title = title.strip()
        content_md = content_md.strip()
        if not title or not content_md:
            raise ValueError("wiki_page_empty")

        await self._snapshot(page, author_id=author_id, summary=summary)
        page.title = title
        page.content_md = content_md
        await self.db.flush()
        await self._rebuild_page_links(page)
        await self.db.flush()
        log.info("wiki.page_updated", page_id=str(page_id))
        return page

    async def list_versions(self, page_id: uuid.UUID) -> list[WikiPageVersion]:
        """列出页面版本（版本号降序）。"""
        stmt = select(WikiPageVersion).where(WikiPageVersion.page_id == page_id)
        result = await self.db.execute(stmt)
        versions = list(result.scalars().all())
        return sorted(versions, key=lambda v: v.version_seq, reverse=True)

    async def rollback(
        self,
        page_id: uuid.UUID,
        version_id: uuid.UUID,
        author_id: uuid.UUID | None = None,
    ) -> WikiPage:
        """回滚页面到指定版本 — 快照当前 + 写回 + 重建链接。

        Raises:
            ValueError: 页面或版本不存在。
        """
        page = await self.get_page(page_id)
        if page is None:
            raise ValueError("wiki_page_not_found")
        stmt = select(WikiPageVersion).where(
            WikiPageVersion.id == version_id, WikiPageVersion.page_id == page.id
        )
        result = await self.db.execute(stmt)
        version = result.scalars().first()
        if version is None:
            raise ValueError("wiki_version_not_found")

        await self._snapshot(page, author_id=author_id, summary=f"rollback to v{version.version_seq}")
        page.title = version.title
        page.content_md = version.content_md
        await self.db.flush()
        await self._rebuild_page_links(page)
        await self.db.flush()
        log.info("wiki.page_rolled_back", page_id=str(page_id), version_seq=version.version_seq)
        return page

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _list_docs(self, kb_id: uuid.UUID) -> list[Document]:
        stmt = select(Document).where(
            Document.kb_id == kb_id, Document.deleted_at.is_(None)
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        docs = list(result.scalars().all())
        return [d for d in docs if (d.content_text or "").strip()]

    async def _list_pages(self, kb_id: uuid.UUID) -> list[WikiPage]:
        return await self.list_pages(kb_id)

    async def _generate_page_md(self, doc: Document, linkable_titles: list[str]) -> str:
        """调用 LLM 生成单页 Markdown（非流式）。

        Raises:
            RuntimeError: LLM 未配置或输出为空。
        """
        from app.llm.factory import get_llm_provider

        provider = get_llm_provider()
        content = (doc.content_text or "")[:_MAX_INPUT_CHARS]
        messages = [
            {
                "role": "system",
                "content": _GENERATE_SYSTEM_PROMPT.format(
                    linkable_titles="\n".join(f"- {t}" for t in linkable_titles) or "-（无）"
                ),
            },
            {
                "role": "user",
                "content": f"原始文档标题：{doc.title or ''}\n原始文档内容：\n{content}",
            },
        ]
        text_parts: list[str] = []
        async for chunk in provider.chat(messages, stream=False):
            if isinstance(chunk, str):
                text_parts.append(chunk)
        output = "".join(text_parts).strip()
        if not output:
            raise RuntimeError("wiki_llm_empty_output")
        return output

    @staticmethod
    def _parse_title(content_md: str, fallback: str) -> str:
        """从 Markdown 中解析页面标题：取第一个 '# ' 行。"""
        for line in content_md.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                title = stripped[2:].strip()
                if title:
                    return title[:255]
        return fallback[:255]

    @staticmethod
    def _extract_links(content_md: str) -> list[str]:
        """提取正文中的 [[标题]] 链接文本（去重保序）。"""
        seen: set[str] = set()
        result: list[str] = []
        for match in _LINK_RE.finditer(content_md):
            title = match.group(1).strip()
            if title and title not in seen:
                seen.add(title)
                result.append(title)
        return result

    async def _rebuild_page_links(self, page: WikiPage) -> None:
        """重建单个页面的出链。"""
        stmt = delete(WikiPageLink).where(WikiPageLink.from_page_id == page.id)
        await self.db.execute(stmt)
        await self._add_links(page.kb_id, page, self._extract_links(page.content_md))

    async def _rebuild_links(self, kb_id: uuid.UUID) -> None:
        """重建知识库全部互链 — 先清空再全量重扫。"""
        stmt = delete(WikiPageLink).where(WikiPageLink.kb_id == kb_id)
        await self.db.execute(stmt)
        pages = await self.list_pages(kb_id)
        by_title: dict[str, WikiPage] = {}
        for page in pages:
            by_title[page.title] = page
        for page in pages:
            links = self._extract_links(page.content_md)
            targets = [by_title[t] for t in links if t in by_title and by_title[t].id != page.id]
            for target in targets:
                self.db.add(
                    WikiPageLink(
                        kb_id=kb_id,
                        from_page_id=page.id,
                        to_page_id=target.id,
                        link_text=target.title,
                        tenant_id=None,
                    )
                )

    async def _add_links(
        self, kb_id: uuid.UUID, page: WikiPage, link_texts: list[str]
    ) -> None:
        """为页面建立到目标页面的链接（目标需已存在，跳过自身）。"""
        pages = await self.list_pages(kb_id)
        by_title = {p.title: p for p in pages}
        for text in link_texts:
            target = by_title.get(text)
            if target is None or target.id == page.id:
                continue
            self.db.add(
                WikiPageLink(
                    kb_id=kb_id,
                    from_page_id=page.id,
                    to_page_id=target.id,
                    link_text=text,
                )
            )

    async def _snapshot(
        self,
        page: WikiPage,
        author_id: uuid.UUID | None,
        summary: str | None,
    ) -> WikiPageVersion:
        """把页面当前内容快照为新版本（version_seq = 页内 max+1）。"""
        stmt = select(func.max(WikiPageVersion.version_seq)).where(
            WikiPageVersion.page_id == page.id
        )
        result = await self.db.execute(stmt)
        current_max = result.scalar() or 0
        version = WikiPageVersion(
            page_id=page.id,
            version_seq=current_max + 1,
            title=page.title,
            content_md=page.content_md,
            author_id=author_id or getattr(self.user, "id", uuid.uuid4()),
            summary=summary,
        )
        self.db.add(version)
        return version
