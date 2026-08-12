"""ExternalSyncService 单测 — P0 两阶段校验全路径覆盖。

测试策略：
    - 直接调用 ``_verify_and_refresh(session, doc_id, force=...)``，
      绕过 ``async_session_factory`` 真实 DB 连接。
    - mock service 的数据访问方法（_get_doc / _get_credentials /
      _update_last_checked / _update_after_sync / _trigger_reindex_async），
      聚焦校验逻辑本身。
    - mock adapter_registry.get 返回自定义 fake adapter，
      控制 get_revision / fetch 的返回值与异常。
    - ``filter_docs_to_verify`` 通过 patch ``app.database.async_session_factory``
      注入 mock session，验证批量过滤逻辑。

覆盖路径：
    - 跳过：doc_not_found / not_external / cache_hit / no_credentials / adapter_not_found
    - 新鲜：阶段 A 指纹一致 → verified_fresh
    - 更新：阶段 B hash 不一致 → updated_live
    - 新鲜：阶段 B hash 一致 → verified_fresh
    - 降级：get_revision 返回 None → 直接阶段 B
    - 失败：probe 超时 / fetch 超时 / fetch 异常 → verify_failed
    - 强制：force=True 忽略缓存
    - 工具：_is_cache_fresh / is_strong_freshness_category / filter_docs_to_verify
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.document.source_adapters.base import (
    AdapterError,
    DocumentSourceAdapter,
    FetchedDocument,
    RevisionInfo,
)
from app.services.external_sync_service import ExternalSyncService, RefreshResult
from app.utils.crypto import encrypt_secret
from app.utils.hash import compute_content_hash


# ==================================================================
# 测试夹具 / 辅助
# ==================================================================

def _make_doc(
    *,
    source: str | None = "feishu",
    source_doc_id: str | None = "doccnABC",
    source_revision: str | None = "42",
    content_hash: str | None = None,
    last_checked_at: datetime | None = None,
    category: str = "政策",
    source_url: str = "https://feishu.cn/docs/doccnABC",
    tenant_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """构造一个 Document-like 对象（仅需服务访问的属性）。"""
    return SimpleNamespace(
        id=uuid.uuid4(),
        source=source,
        source_doc_id=source_doc_id,
        source_revision=source_revision,
        content_hash=content_hash or compute_content_hash("old content"),
        last_checked_at=last_checked_at,
        category=category,
        source_url=source_url,
        tenant_id=tenant_id,
    )


class _FakeAdapter:
    """可控的假适配器 — get_revision/fetch 由测试注入返回值。

    不继承 DocumentSourceAdapter（ABC 会要求实现抽象方法）；
    service 仅调用 adapter.get_revision / adapter.fetch，鸭子类型足够。
    """

    adapter_id = "fake"
    display_name = "Fake"
    supported_formats = ("markdown",)

    def __init__(self) -> None:
        self.get_revision = AsyncMock()
        self.fetch = AsyncMock()


@pytest.fixture
def service() -> ExternalSyncService:
    """默认 service 实例 — 短 TTL 便于触发真实校验。"""
    return ExternalSyncService(check_cache_ttl=300, probe_timeout=3.0, fetch_timeout=5.0)


@pytest.fixture
def fake_adapter() -> _FakeAdapter:
    return _FakeAdapter()


def _wire_service(
    service: ExternalSyncService,
    *,
    doc: SimpleNamespace | None,
    credentials: dict[str, Any] | None = None,
    adapter: DocumentSourceAdapter | None = None,
) -> None:
    """注入 doc / credentials / adapter 到 service 的数据访问方法。"""
    service._get_doc = AsyncMock(return_value=doc)  # type: ignore[assignment]
    service._get_credentials = AsyncMock(return_value=credentials)  # type: ignore[assignment]
    # 避免真实 DB 写入
    service._update_last_checked = AsyncMock()  # type: ignore[assignment]
    service._update_after_sync = AsyncMock()  # type: ignore[assignment]
    service._trigger_reindex_async = AsyncMock()  # type: ignore[assignment]
    # patch registry
    if adapter is not None:
        # registry.get 在模块内被调用，patch 模块级引用
        patch(
            "app.services.external_sync_service.adapter_registry"
        ).start()
        from app.services.external_sync_service import adapter_registry
        adapter_registry.get = MagicMock(return_value=adapter)


# ==================================================================
# 纯函数：_is_cache_fresh / is_strong_freshness_category
# ==================================================================

class TestCacheFreshness:
    """_is_cache_fresh — 短窗口缓存判定。"""

    def test_none_is_not_fresh(self, service: ExternalSyncService) -> None:
        assert service._is_cache_fresh(None) is False

    def test_recent_is_fresh(self, service: ExternalSyncService) -> None:
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert service._is_cache_fresh(recent) is True

    def test_old_is_stale(self, service: ExternalSyncService) -> None:
        old = datetime.now(timezone.utc) - timedelta(seconds=600)
        assert service._is_cache_fresh(old) is False

    def test_naive_datetime_handled(self, service: ExternalSyncService) -> None:
        """DB 可能返回 naive datetime — 应按 UTC 处理。"""
        # 模拟 DB 返回的 naive datetime（无 tzinfo），按 UTC 解释
        recent_naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        assert service._is_cache_fresh(recent_naive) is True

    def test_custom_ttl(self) -> None:
        """构造函数可覆盖 TTL。"""
        svc = ExternalSyncService(check_cache_ttl=1)
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert svc._is_cache_fresh(recent) is False


class TestStrongFreshnessCategory:
    """is_strong_freshness_category — 强时效类判定。"""

    @pytest.mark.parametrize(
        "category,expected",
        [
            ("政策", True),
            ("SOP", True),
            ("合同模板", True),
            ("规范", True),
            ("制度", True),
            ("技术笔记", False),
            ("FAQ", False),
            ("", False),
            (None, False),
        ],
    )
    def test_categories(self, category: str | None, expected: bool) -> None:
        assert ExternalSyncService.is_strong_freshness_category(category) is expected


# ==================================================================
# _verify_and_refresh 主路径
# ==================================================================

class TestVerifySkipped:
    """跳过路径 — 返回 status=skipped。"""

    async def test_doc_not_found(self, service: ExternalSyncService) -> None:
        _wire_service(service, doc=None, adapter=_FakeAdapter())
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, uuid.uuid4(), force=False
        )

        assert result.status == "skipped"
        assert result.reason == "doc_not_found"

    async def test_not_external_skips(self, service: ExternalSyncService) -> None:
        """source 或 source_doc_id 为空 → 跳过。"""
        doc = _make_doc(source=None, source_doc_id=None)
        _wire_service(service, doc=doc, adapter=_FakeAdapter())
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )

        assert result.status == "skipped"
        assert result.reason == "not_external"
        assert result.sync_status == "trusted_local"

    async def test_cache_hit_skips(self, service: ExternalSyncService) -> None:
        """last_checked_at 在缓存窗口内 → 信任本地。"""
        doc = _make_doc(last_checked_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        _wire_service(service, doc=doc, adapter=_FakeAdapter())
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )

        assert result.status == "skipped"
        assert result.reason == "cache_hit"
        assert result.sync_status == "trusted_local"
        assert result.source_url == doc.source_url

    async def test_no_credentials_skips(self, service: ExternalSyncService) -> None:
        """无凭证 → 跳过，信任本地。"""
        doc = _make_doc(last_checked_at=None)  # cache 未命中
        _wire_service(service, doc=doc, credentials=None, adapter=_FakeAdapter())
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )

        assert result.status == "skipped"
        assert result.reason == "no_credentials"
        assert result.sync_status == "trusted_local"

    async def test_adapter_not_found_skips(self, service: ExternalSyncService) -> None:
        """registry 未注册该适配器 → 跳过。"""
        doc = _make_doc(last_checked_at=None)
        # 不注入 fake adapter → registry.get 返回 None
        service._get_doc = AsyncMock(return_value=doc)
        service._get_credentials = AsyncMock(return_value={"app_id": "x"})
        service._update_last_checked = AsyncMock()
        service._update_after_sync = AsyncMock()
        service._trigger_reindex_async = AsyncMock()
        with patch(
            "app.services.external_sync_service.adapter_registry"
        ) as reg_mock:
            reg_mock.get = MagicMock(return_value=None)
            session = MagicMock()

            result = await service._verify_and_refresh(
                session, doc.id, force=False
            )

        assert result.status == "skipped"
        assert result.reason == "adapter_not_found"


class TestVerifyFreshViaPhaseA:
    """阶段 A 指纹一致 → verified_fresh（不拉取全文）。"""

    async def test_revision_match_returns_fresh(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="42")
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )

        assert result.status == "fresh"
        assert result.sync_status == "verified_fresh"
        # 未调用 fetch（节省 API）
        fake_adapter.fetch.assert_not_awaited()
        # 更新了 last_checked_at
        service._update_last_checked.assert_awaited_once()


class TestVerifyUpdatedViaPhaseB:
    """阶段 B：指纹变化后拉取全文，hash 不一致 → updated_live。"""

    async def test_revision_changed_hash_mismatch_returns_updated(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        # 本地指纹=42，远端=99（变化）
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="99")
        # fetch 返回新内容（hash 与 doc.content_hash 不同）
        fake_adapter.fetch.return_value = FetchedDocument(
            source="feishu", title="t", content="brand new content",
            format="markdown", source_url=doc.source_url, doc_id="doccnABC",
        )
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()
        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )
        # 让 create_task 调度的 _trigger_reindex_async 任务跑完，避免 pending 警告
        await asyncio.sleep(0)

        assert result.status == "updated"
        assert result.sync_status == "updated_live"
        assert result.content == "brand new content"
        assert result.source_url == doc.source_url
        # 触发了重建索引（_trigger_reindex_async 被 await 调度）
        service._trigger_reindex_async.assert_awaited_once()
        # 更新了 content_hash / last_synced_at
        service._update_after_sync.assert_awaited_once()

    async def test_revision_changed_hash_match_returns_fresh(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """指纹变化但内容 hash 一致（如格式重排）→ 仍 fresh。"""
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="99")
        # fetch 内容与 doc.content_hash 对应的原文相同
        fake_adapter.fetch.return_value = FetchedDocument(
            source="feishu", title="t", content="old content",
            format="markdown", source_url=doc.source_url, doc_id="doccnABC",
        )
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()
        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )
        await asyncio.sleep(0)

        assert result.status == "fresh"
        assert result.sync_status == "verified_fresh"
        # 内容未变 → 不触发重建索引
        service._trigger_reindex_async.assert_not_awaited()


class TestVerifyDegradedPaths:
    """降级路径 — get_revision 不支持 / 超时 / fetch 失败。"""

    async def test_get_revision_none_falls_to_fetch(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """get_revision 返回 None（不支持轻量探测）→ 直接进入阶段 B。"""
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.return_value = None
        fake_adapter.fetch.return_value = FetchedDocument(
            source="feishu", title="t", content="old content",
            format="markdown", source_url=doc.source_url, doc_id="doccnABC",
        )
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=False
        )

        # fetch 被调用（降级路径）
        fake_adapter.fetch.assert_awaited_once()
        # 内容 hash 一致 → fresh
        assert result.status == "fresh"
        assert result.sync_status == "verified_fresh"

    async def test_probe_timeout_falls_to_fetch(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """阶段 A 超时 → 降级为阶段 B。"""
        # 用极短 probe_timeout 触发超时
        svc = ExternalSyncService(probe_timeout=0.01, fetch_timeout=5.0)
        doc = _make_doc(source_revision="42", last_checked_at=None)

        async def slow_probe(doc_id, credentials):  # noqa: ANN001
            await asyncio.sleep(1.0)
            return RevisionInfo(fingerprint="42")

        fake_adapter.get_revision = AsyncMock(side_effect=slow_probe)  # type: ignore[assignment]
        fake_adapter.fetch.return_value = FetchedDocument(
            source="feishu", title="t", content="old content",
            format="markdown", source_url=doc.source_url, doc_id="doccnABC",
        )
        _wire_service(svc, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await svc._verify_and_refresh(session, doc.id, force=False)

        # 超时降级 → 仍能完成 fetch 路径
        fake_adapter.fetch.assert_awaited_once()
        assert result.status == "fresh"

    async def test_probe_adapter_error_falls_to_fetch(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """阶段 A 抛 AdapterError → 降级为阶段 B（不直接失败）。"""
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.side_effect = AdapterError("feishu", "网络错误")
        fake_adapter.fetch.return_value = FetchedDocument(
            source="feishu", title="t", content="old content",
            format="markdown", source_url=doc.source_url, doc_id="doccnABC",
        )
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await service._verify_and_refresh(session, doc.id, force=False)

        fake_adapter.fetch.assert_awaited_once()
        assert result.status == "fresh"

    async def test_fetch_timeout_returns_failed(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """阶段 B fetch 超时 → status=failed, sync_status=verify_failed。"""
        svc = ExternalSyncService(probe_timeout=3.0, fetch_timeout=0.01)
        doc = _make_doc(source_revision="42", last_checked_at=None)
        # get_revision 返回变化指纹 → 触发 fetch
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="99")

        async def slow_fetch(doc_id, credentials):  # noqa: ANN001
            await asyncio.sleep(1.0)
            return FetchedDocument(
                source="feishu", title="t", content="x",
                format="markdown", source_url="", doc_id="",
            )

        fake_adapter.fetch = AsyncMock(side_effect=slow_fetch)  # type: ignore[assignment]
        _wire_service(svc, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await svc._verify_and_refresh(session, doc.id, force=False)

        assert result.status == "failed"
        assert result.sync_status == "verify_failed"
        assert result.reason == "fetch_timeout"

    async def test_fetch_exception_returns_failed(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """阶段 B fetch 抛异常 → failed/verify_failed。"""
        doc = _make_doc(source_revision="42", last_checked_at=None)
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="99")
        fake_adapter.fetch.side_effect = RuntimeError("connection reset")
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await service._verify_and_refresh(session, doc.id, force=False)

        assert result.status == "failed"
        assert result.sync_status == "verify_failed"
        assert "connection reset" in (result.reason or "")


# ==================================================================
# force=True 强制校验
# ==================================================================

class TestForceRefresh:
    """force=True 忽略短窗口缓存。"""

    async def test_force_ignores_cache(
        self, service: ExternalSyncService, fake_adapter: _FakeAdapter
    ) -> None:
        """即使缓存命中，force=True 仍执行真实校验。"""
        doc = _make_doc(
            source_revision="42",
            last_checked_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        fake_adapter.get_revision.return_value = RevisionInfo(fingerprint="42")
        _wire_service(service, doc=doc, credentials={"k": "v"}, adapter=fake_adapter)
        session = MagicMock()

        result = await service._verify_and_refresh(
            session, doc.id, force=True
        )

        # 强制校验 → 不走 cache_hit 跳过路径
        assert result.status == "fresh"
        assert result.sync_status == "verified_fresh"
        # get_revision 被调用
        fake_adapter.get_revision.assert_awaited_once()


# ==================================================================
# filter_docs_to_verify — 批量过滤
# ==================================================================

class TestFilterDocsToVerify:
    """filter_docs_to_verify — 按 source/category/cache 过滤。"""

    async def test_empty_input_returns_empty(self, service: ExternalSyncService) -> None:
        assert await service.filter_docs_to_verify([]) == []

    async def test_filters_correctly(self, service: ExternalSyncService) -> None:
        """混合文档 — 仅强时效类 + 外部来源 + 缓存过期者进入校验。"""
        now = datetime.now(timezone.utc)
        recent = now - timedelta(seconds=10)
        old = now - timedelta(seconds=600)

        # 4 个文档：
        # A：外部 + 政策 + 缓存过期 → 应校验
        # B：外部 + 政策 + 缓存未过期 → 跳过
        # C：外部 + 技术笔记（非强时效） → 跳过
        # D：非外部（source_doc_id=None） → 跳过
        rows = [
            SimpleNamespace(
                id=uuid.uuid4(), source="feishu", source_doc_id="A",
                category="政策", last_checked_at=old,
            ),
            SimpleNamespace(
                id=uuid.uuid4(), source="feishu", source_doc_id="B",
                category="SOP", last_checked_at=recent,
            ),
            SimpleNamespace(
                id=uuid.uuid4(), source="feishu", source_doc_id="C",
                category="技术笔记", last_checked_at=old,
            ),
            SimpleNamespace(
                id=uuid.uuid4(), source="feishu", source_doc_id=None,
                category="政策", last_checked_at=old,
            ),
        ]

        # mock async_session_factory
        mock_result = MagicMock()
        mock_result.all.return_value = rows
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_factory():
            yield mock_session

        with patch(
            "app.database.async_session_factory", fake_factory
        ):
            to_verify = await service.filter_docs_to_verify([r.id for r in rows])

        # 仅 A 通过过滤
        assert len(to_verify) == 1
        assert to_verify[0] == rows[0].id


# ==================================================================
# 凭证解密集成 — 确认 _get_credentials 端到端可用
# ==================================================================

class TestCredentialDecryption:
    """_get_credentials — 从 external_credentials 表解密凭证。"""

    async def test_decrypts_credentials(self, service: ExternalSyncService) -> None:
        """端到端：加密的 JSON → 解密 → dict。"""
        cred_json = '{"app_id": "cli_x", "app_secret": "secret"}'
        encrypted = encrypt_secret(cred_json)

        cred_record = SimpleNamespace(credentials_encrypted=encrypted)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cred_record
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        creds = await service._get_credentials(
            mock_session, tenant_id=None, adapter_id="feishu"
        )
        assert creds == {"app_id": "cli_x", "app_secret": "secret"}

    async def test_returns_none_on_decrypt_failure(
        self, service: ExternalSyncService
    ) -> None:
        """凭证 blob 损坏 → 解密失败 → 返回 None（降级信任本地）。"""
        cred_record = SimpleNamespace(credentials_encrypted=b"corrupted-short")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cred_record
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        creds = await service._get_credentials(
            mock_session, tenant_id=None, adapter_id="feishu"
        )
        assert creds is None

    async def test_returns_none_when_no_record(
        self, service: ExternalSyncService
    ) -> None:
        """无凭证记录 → None。"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        creds = await service._get_credentials(
            mock_session, tenant_id=None, adapter_id="feishu"
        )
        assert creds is None
