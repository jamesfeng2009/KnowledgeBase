"""外部文档实时同步服务 — P0 两阶段校验 + P1 强制同步入口。

P0 核心机制（消除窗口期）：
    阶段 A（轻量探测）：adapter.get_revision() 拿版本指纹，对比 Document.source_revision
    阶段 B（按需拉取）：仅指纹变化才 fetch 全文 + compute_content_hash 对比
    短窗口缓存：last_checked_at 在阈值内不重复探测，直接信任本地

调用方：
    - engine.py 的 _retrieve 节点 → verify_and_refresh() 检索时校验
    - webhooks API（P1）→ force_refresh() 收到飞书/Confluence 事件后调用

数据流::

    用户提问 → RAG._retrieve 命中外部文档
        ↓
    verify_and_refresh(doc_id)
        ↓
    ┌─ 短窗口缓存命中？→ 信任本地（sync_status=trusted_local）
    └─ 否 → 阶段 A：adapter.get_revision()
            ↓
        ┌─ 指纹一致？→ 信任本地（sync_status=verified_fresh）
        └─ 指纹变化或不支持探测 → 阶段 B：adapter.fetch() 全文
                ↓
            compute_content_hash 对比
            ┌─ hash 一致？→ 信任本地（sync_status=verified_fresh）
            └─ hash 不同 → 用最新内容回答（sync_status=updated_live）
                          + 异步 _trigger_reindex 重建向量
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.document.source_adapters.base import (
    AdapterError,
    DocumentSourceAdapter,
    RevisionInfo,
)
from app.document.source_adapters.registry import adapter_registry
from app.models.knowledge import Document, ExternalCredential
from app.utils.crypto import decrypt_secret
from app.utils.hash import compute_content_hash
from app.utils.logger import get_logger

log = get_logger(__name__)

# === 默认配置常量 ===
# 短窗口缓存：last_checked_at 在此窗口内不重复探测（秒）
_CHECK_CACHE_TTL_SECONDS: int = 300  # 5 分钟
# 阶段 A 探测超时（秒）
_PROBE_TIMEOUT: float = 3.0
# 阶段 B 拉取全文超时（秒）
_FETCH_TIMEOUT: float = 5.0
# 强时效文档类别（仅这些类别触发回源校验）
_STRONG_FRESHNESS_CATEGORIES: frozenset[str] = frozenset({
    "政策", "SOP", "合同模板", "规范", "制度",
})


@dataclass
class RefreshResult:
    """回源校验结果。

    Attributes:
        status: 校验结果状态：
            - "skipped"：跳过（非外部文档/非强时效/缓存命中/无凭证）
            - "fresh"：已校验，内容未变
            - "updated"：内容已更新，本次回答使用 content 字段
            - "failed"：校验失败，降级信任本地
        content: 当 status="updated" 时为最新内容文本；其他状态为 None
        source_url: 原始文档 URL（用于 P3 prompt 时效声明）
        sync_status: 内部状态标签，用于 P3 prompt 显示：
            - "trusted_local"：信任本地缓存（未校验）
            - "verified_fresh"：已校验，最新
            - "updated_live"：已校验，实时拉取了最新内容
            - "verify_failed"：校验失败，降级
        reason: 失败/跳过原因（用于日志/调试）
        new_revision: 探测到的新版本指纹（用于日志）
    """

    status: str
    content: str | None = None
    source_url: str | None = None
    sync_status: str = "trusted_local"
    reason: str | None = None
    new_revision: str | None = None


class ExternalSyncService:
    """外部文档实时同步服务 — P0 两阶段校验 + P1 强制同步。

    无状态服务：每次调用内部管理 session 生命周期（async_session_factory）。
    配置参数可通过构造函数覆盖（用于测试）。
    """

    def __init__(
        self,
        *,
        check_cache_ttl: int = _CHECK_CACHE_TTL_SECONDS,
        probe_timeout: float = _PROBE_TIMEOUT,
        fetch_timeout: float = _FETCH_TIMEOUT,
    ) -> None:
        self._check_cache_ttl = check_cache_ttl
        self._probe_timeout = probe_timeout
        self._fetch_timeout = fetch_timeout

    # ------------------------------------------------------------------
    # P0：检索时回源校验主入口
    # ------------------------------------------------------------------

    async def verify_and_refresh(
        self,
        doc_id: uuid.UUID,
        *,
        force: bool = False,
    ) -> RefreshResult:
        """检索时回源校验 — 两阶段：先轻量探测，再按需拉取全文。

        Args:
            doc_id: 文档 ID。
            force: 强制校验（忽略短窗口缓存），P1 Webhook 调用时为 True。

        Returns:
            RefreshResult — 调用方根据 status 决定是否用最新内容覆盖本次回答。
        """
        from app.database import async_session_factory

        async with async_session_factory() as session:
            return await self._verify_and_refresh(session, doc_id, force=force)

    async def filter_docs_to_verify(
        self,
        doc_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """批量查询，返回需要回源校验的文档 ID 列表（engine.py 检索节点用）。

        一次 DB 查询批量获取所有文档的元数据，在内存中按条件过滤，
        避免对每个文档单独查询。过滤条件：
            - source != NULL（外部来源）
            - source_doc_id != NULL
            - category ∈ 强时效类（政策/SOP/合同模板等）
            - last_checked_at 未在短窗口缓存内

        Args:
            doc_ids: 检索召回的文档 ID 列表。

        Returns:
            需要校验的文档 ID 列表（已过滤，数量 <= len(doc_ids)）。
        """
        if not doc_ids:
            return []
        from app.database import async_session_factory

        async with async_session_factory() as session:
            result = await session.execute(
                select(
                    Document.id,
                    Document.source,
                    Document.source_doc_id,
                    Document.category,
                    Document.last_checked_at,
                ).where(Document.id.in_(doc_ids))
            )
            rows = result.all()

        to_verify: list[uuid.UUID] = []
        for row in rows:
            # source 和 source_doc_id 不为空
            if not row.source or not row.source_doc_id:
                continue
            # 强时效类
            if not self.is_strong_freshness_category(row.category):
                continue
            # 缓存未过期
            if self._is_cache_fresh(row.last_checked_at):
                continue
            to_verify.append(row.id)
        return to_verify

    async def _verify_and_refresh(
        self,
        session: AsyncSession,
        doc_id: uuid.UUID,
        *,
        force: bool,
    ) -> RefreshResult:
        """实际校验逻辑 — 在传入的 session 中执行。"""
        # 1. 查文档元数据
        doc = await self._get_doc(session, doc_id)
        if doc is None:
            return RefreshResult(status="skipped", reason="doc_not_found")

        # 非外部来源 → 跳过
        if not doc.source or not doc.source_doc_id:
            return RefreshResult(status="skipped", reason="not_external")

        # 短窗口缓存：未过期且非强制 → 信任本地
        if not force and self._is_cache_fresh(doc.last_checked_at):
            return RefreshResult(
                status="skipped",
                source_url=doc.source_url,
                sync_status="trusted_local",
                reason="cache_hit",
            )

        # 2. 获取凭证
        credentials = await self._get_credentials(session, doc.tenant_id, doc.source)
        if credentials is None:
            return RefreshResult(
                status="skipped",
                source_url=doc.source_url,
                sync_status="trusted_local",
                reason="no_credentials",
            )

        # 3. 获取适配器
        adapter = adapter_registry.get(doc.source)
        if adapter is None:
            return RefreshResult(
                status="skipped",
                source_url=doc.source_url,
                sync_status="trusted_local",
                reason="adapter_not_found",
            )

        # 4. 阶段 A：轻量探测（带超时）
        rev_info = await self._probe_revision(adapter, doc, credentials)

        if rev_info is not None:
            # 指纹对比
            await self._update_last_checked(
                session, doc_id, rev_info.fingerprint
            )
            if rev_info.fingerprint == doc.source_revision:
                # 指纹一致 → 信任本地（内容未变）
                return RefreshResult(
                    status="fresh",
                    source_url=doc.source_url,
                    sync_status="verified_fresh",
                )
            # 指纹变化 → 进入阶段 B
            log.info(
                "external_sync.revision_changed",
                doc_id=str(doc_id),
                old_revision=doc.source_revision,
                new_revision=rev_info.fingerprint,
            )
        # else: 不支持轻量探测（get_revision 返回 None）→ 直接进入阶段 B

        # 5. 阶段 B：拉取全文 + hash 对比
        return await self._fetch_and_compare(session, adapter, doc, credentials)

    # ------------------------------------------------------------------
    # P1：Webhook 强制同步入口
    # ------------------------------------------------------------------

    async def force_refresh(
        self,
        adapter_id: str,
        source_doc_id: str,
        tenant_id: uuid.UUID | None = None,
    ) -> RefreshResult:
        """Webhook 触发的强制同步 — 按 adapter_id + source_doc_id 反查文档并刷新。

        P1 阶段使用：飞书/Confluence Webhook 收到 document.updated 事件后调用。
        """
        from app.database import async_session_factory

        async with async_session_factory() as session:
            doc = await self._get_doc_by_external_id(
                session, adapter_id, source_doc_id, tenant_id
            )
            if doc is None:
                return RefreshResult(
                    status="skipped",
                    reason="doc_not_found_by_external_id",
                )
            # 强制校验（忽略缓存）
            return await self._verify_and_refresh(
                session, doc.id, force=True
            )

    # ------------------------------------------------------------------
    # P2：定时巡检兜底
    # ------------------------------------------------------------------

    async def get_stale_external_docs(
        self,
        max_age_hours: int = 24,
        batch_size: int = 50,
    ) -> list[Document]:
        """查询过期未校验的外部文档 — P2 巡检入口。

        方案 A：无类别过滤，所有外部文档统一阈值。
        last_checked_at 天然限流 — P0/P1 已校验的文档不会入选。

        Args:
            max_age_hours: 最大滞后阈值（h），超过即视为过期。
            batch_size: 单批返回上限，避免单次任务过长。

        Returns:
            待巡检的 Document 列表（按 last_checked_at 升序，最旧的优先）。
        """
        from app.database import async_session_factory

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        async with async_session_factory() as session:
            stmt = (
                select(Document)
                .where(
                    Document.source.is_not(None),
                    Document.source_doc_id.is_not(None),
                )
                .where(
                    (Document.last_checked_at.is_(None))
                    | (Document.last_checked_at < cutoff)
                )
                .order_by(Document.last_checked_at.asc().nulls_first())
                .limit(batch_size)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def patrol(
        self,
        max_age_hours: int = 24,
        batch_size: int = 50,
        concurrency: int = 2,
    ) -> dict[str, Any]:
        """批量巡检过期外部文档 — P2 兜底安全网。

        捕获 P0（检索时校验）+ P1（webhook）可能遗漏的更新：
            - 从未被检索到的文档
            - webhook 投递失败/未配置的文档

        流程：
            1. 查询 last_checked_at 过期的外部文档（无类别过滤）
            2. asyncio.Semaphore 限流并发校验
            3. 单文档失败不中断整批，记录 failed_count
            4. 失败率 > 50% 记 error 告警

        Args:
            max_age_hours: 最大滞后阈值（h）。
            batch_size: 单批巡检文档数上限。
            concurrency: 并发上限（防 IP 封禁）。

        Returns:
            巡检摘要 dict：total / fresh / updated / failed / skipped。
        """
        stale_docs = await self.get_stale_external_docs(max_age_hours, batch_size)
        total = len(stale_docs)
        if total == 0:
            log.info("external_sync.patrol_no_stale_docs", max_age_hours=max_age_hours)
            return {
                "total": 0, "fresh": 0, "updated": 0,
                "failed": 0, "skipped": 0,
            }

        log.info(
            "external_sync.patrol_started",
            total=total,
            max_age_hours=max_age_hours,
            concurrency=concurrency,
        )

        semaphore = asyncio.Semaphore(concurrency)
        counts = {"fresh": 0, "updated": 0, "failed": 0, "skipped": 0}

        async def _check_one(doc: Document) -> None:
            async with semaphore:
                try:
                    result = await self.verify_and_refresh(doc.id, force=True)
                    if result.status == "fresh":
                        counts["fresh"] += 1
                    elif result.status == "updated":
                        counts["updated"] += 1
                    else:
                        counts["skipped"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    log.warning(
                        "external_sync.patrol_doc_failed",
                        doc_id=str(doc.id),
                        source=doc.source,
                        error=str(exc)[:200],
                    )

        # 并发巡检
        await asyncio.gather(*[_check_one(d) for d in stale_docs])

        failed_rate = counts["failed"] / total if total > 0 else 0
        log_level = "error" if failed_rate > 0.5 else "info"
        log_message = (
            "external_sync.patrol_completed",
            {
                "total": total,
                "fresh": counts["fresh"],
                "updated": counts["updated"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "failed_rate": round(failed_rate, 2),
            },
        )
        if log_level == "error":
            log.error(*log_message)
        else:
            log.info(*log_message)

        return {
            "total": total,
            "fresh": counts["fresh"],
            "updated": counts["updated"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
        }

    # ------------------------------------------------------------------
    # 阶段 A：轻量探测
    # ------------------------------------------------------------------

    async def _probe_revision(
        self,
        adapter: DocumentSourceAdapter,
        doc: Document,
        credentials: dict[str, Any],
    ) -> RevisionInfo | None:
        """阶段 A：轻量探测版本指纹（带超时，失败降级为 None）。"""
        try:
            return await asyncio.wait_for(
                adapter.get_revision(doc.source_doc_id or "", credentials),
                timeout=self._probe_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "external_sync.probe_timeout",
                doc_id=str(doc.id),
                adapter=doc.source,
                timeout=self._probe_timeout,
            )
            return None
        except AdapterError as exc:
            log.warning(
                "external_sync.probe_failed",
                doc_id=str(doc.id),
                adapter=doc.source,
                error=str(exc)[:200],
            )
            return None
        except Exception as exc:
            log.warning(
                "external_sync.probe_error",
                doc_id=str(doc.id),
                adapter=doc.source,
                error=str(exc)[:200],
            )
            return None

    # ------------------------------------------------------------------
    # 阶段 B：拉取全文 + hash 对比
    # ------------------------------------------------------------------

    async def _fetch_and_compare(
        self,
        session: AsyncSession,
        adapter: DocumentSourceAdapter,
        doc: Document,
        credentials: dict[str, Any],
    ) -> RefreshResult:
        """阶段 B：拉取全文 + compute_content_hash 对比。"""
        try:
            fetched = await asyncio.wait_for(
                adapter.fetch(doc.source_doc_id or "", credentials),
                timeout=self._fetch_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "external_sync.fetch_timeout",
                doc_id=str(doc.id),
                adapter=doc.source,
                timeout=self._fetch_timeout,
            )
            return RefreshResult(
                status="failed",
                source_url=doc.source_url,
                sync_status="verify_failed",
                reason="fetch_timeout",
            )
        except Exception as exc:
            log.warning(
                "external_sync.fetch_failed",
                doc_id=str(doc.id),
                adapter=doc.source,
                error=str(exc)[:200],
            )
            return RefreshResult(
                status="failed",
                source_url=doc.source_url,
                sync_status="verify_failed",
                reason=str(exc)[:200],
            )

        # hash 对比
        new_hash = compute_content_hash(fetched.content)

        # 更新时间戳 + content_hash + source_url
        await self._update_after_sync(
            session, doc.id, new_hash, fetched.source_url or doc.source_url
        )

        if new_hash == doc.content_hash:
            # hash 一致 → 内容未变（即使指纹变了，实质内容相同）
            return RefreshResult(
                status="fresh",
                source_url=doc.source_url,
                sync_status="verified_fresh",
            )

        # hash 不同 → 内容已更新
        log.info(
            "external_sync.doc_updated",
            doc_id=str(doc.id),
            old_hash=(doc.content_hash or "")[:16],
            new_hash=new_hash[:16],
            adapter=doc.source,
        )

        # 异步触发重建索引（不阻塞当前检索）
        # 注意：不传 session，避免在 session 关闭后访问
        asyncio.create_task(self._trigger_reindex_async(str(doc.id)))

        return RefreshResult(
            status="updated",
            content=fetched.content,
            source_url=doc.source_url,
            sync_status="updated_live",
        )

    # ------------------------------------------------------------------
    # 数据访问
    # ------------------------------------------------------------------

    async def _get_doc(
        self, session: AsyncSession, doc_id: uuid.UUID
    ) -> Document | None:
        """按 ID 查文档。"""
        result = await session.execute(
            select(Document).where(Document.id == doc_id)
        )
        return result.scalar_one_or_none()

    async def _get_doc_by_external_id(
        self,
        session: AsyncSession,
        adapter_id: str,
        source_doc_id: str,
        tenant_id: uuid.UUID | None,
    ) -> Document | None:
        """按外部来源 ID 反查文档（P1 Webhook 用）。"""
        stmt = select(Document).where(
            Document.source == adapter_id,
            Document.source_doc_id == source_doc_id,
        )
        if tenant_id is not None:
            stmt = stmt.where(Document.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_credentials(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID | None,
        adapter_id: str,
    ) -> dict[str, Any] | None:
        """从 external_credentials 表读取并解密凭证。"""
        stmt = select(ExternalCredential).where(
            ExternalCredential.adapter_id == adapter_id,
            ExternalCredential.is_active.is_(True),
        )
        if tenant_id is not None:
            stmt = stmt.where(ExternalCredential.tenant_id == tenant_id)
        else:
            stmt = stmt.where(ExternalCredential.tenant_id.is_(None))
        result = await session.execute(stmt)
        cred = result.scalar_one_or_none()
        if cred is None:
            return None
        try:
            plaintext = decrypt_secret(cred.credentials_encrypted)
            return json.loads(plaintext)
        except Exception as exc:
            log.warning(
                "external_sync.credential_decrypt_failed",
                adapter_id=adapter_id,
                error=str(exc)[:200],
            )
            return None

    # ------------------------------------------------------------------
    # 时间戳更新
    # ------------------------------------------------------------------

    async def _update_last_checked(
        self,
        session: AsyncSession,
        doc_id: uuid.UUID,
        revision: str | None,
    ) -> None:
        """更新 last_checked_at + source_revision。"""
        values: dict[str, Any] = {
            "last_checked_at": datetime.now(timezone.utc),
        }
        if revision is not None:
            values["source_revision"] = revision
        try:
            await session.execute(
                update(Document).where(Document.id == doc_id).values(**values)
            )
            await session.commit()
        except Exception as exc:
            log.warning(
                "external_sync.update_checked_failed",
                doc_id=str(doc_id),
                error=str(exc)[:200],
            )
            await session.rollback()

    async def _update_after_sync(
        self,
        session: AsyncSession,
        doc_id: uuid.UUID,
        new_hash: str | None,
        source_url: str | None,
    ) -> None:
        """阶段 B 后更新 last_synced_at + content_hash + last_checked_at。"""
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "last_synced_at": now,
            "last_checked_at": now,
        }
        if new_hash is not None:
            values["content_hash"] = new_hash
        if source_url:
            values["source_url"] = source_url
        try:
            await session.execute(
                update(Document).where(Document.id == doc_id).values(**values)
            )
            await session.commit()
        except Exception as exc:
            log.warning(
                "external_sync.update_synced_failed",
                doc_id=str(doc_id),
                error=str(exc)[:200],
            )
            await session.rollback()

    # ------------------------------------------------------------------
    # 异步触发重建索引（复用 knowledge_service._trigger_reindex 逻辑）
    # ------------------------------------------------------------------

    async def _trigger_reindex_async(self, doc_id: str) -> None:
        """异步触发文档重建索引 — 先删旧向量，再异步触发 process_document。

        优雅降级：Celery/向量存储不可用时仅记录日志。
        与 knowledge_service._trigger_reindex 逻辑一致，但不依赖 service 实例。
        """
        try:
            # ① 先删除旧向量 — 防止旧 chunk 残留
            from app.rag.vector_store import get_vector_store

            store = get_vector_store()
            await store.delete(doc_id)
            log.info("external_sync.reindex_deleted", doc_id=doc_id)

            # ② 异步触发重建（Celery 任务）
            from tasks.document_tasks import process_document

            process_document.delay(doc_id)
            log.info("external_sync.reindex_triggered", doc_id=doc_id)
        except Exception as exc:
            log.warning(
                "external_sync.reindex_failed",
                doc_id=doc_id,
                error=str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _is_cache_fresh(self, last_checked: datetime | None) -> bool:
        """判断短窗口缓存是否有效。"""
        if last_checked is None:
            return False
        now = datetime.now(timezone.utc)
        # 处理 naive datetime（DB 可能返回 naive）
        checked = last_checked
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return (now - checked) < timedelta(seconds=self._check_cache_ttl)

    @staticmethod
    def is_strong_freshness_category(category: str | None) -> bool:
        """判断文档类别是否属于强时效类（需要回源校验）。

        用于 engine.py 检索节点决定是否触发回源校验。
        """
        if not category:
            return False
        return category in _STRONG_FRESHNESS_CATEGORIES


# ------------------------------------------------------------------
# 单例工厂
# ------------------------------------------------------------------

_service_instance: ExternalSyncService | None = None


def get_external_sync_service() -> ExternalSyncService:
    """获取 ExternalSyncService 单例。

    无状态服务，全局单例安全。配置通过环境变量覆盖时需重启服务。
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = ExternalSyncService()
    return _service_instance
