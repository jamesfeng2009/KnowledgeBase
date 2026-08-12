"""4 个适配器 get_revision 单测 — 覆盖飞书 / Confluence / Notion / Obsidian。

测试策略：
    - 飞书 / Confluence / Notion：mock httpx（或适配器内部 HTTP 辅助方法），
      验证 RevisionInfo.fingerprint 正确提取、降级路径、缺凭证错误。
    - Obsidian：本地文件系统，用 tmp_path 构造真实 vault 文件，
      验证 mtime_ns 指纹、路径穿越防护、缺 vault_path 错误。

不涉及真实网络请求，全部通过 mock / 本地文件隔离。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.document.source_adapters.base import AdapterError, RevisionInfo
from app.document.source_adapters.confluence_adapter import ConfluenceAdapter
from app.document.source_adapters.feishu_adapter import FeishuAdapter
from app.document.source_adapters.notion_adapter import NotionAdapter
from app.document.source_adapters.obsidian_adapter import ObsidianAdapter


# ------------------------------------------------------------------
# 通用 httpx mock 工具
# ------------------------------------------------------------------

def _build_httpx_mock(json_data: Any, status_code: int = 200) -> MagicMock:
    """构造 httpx.AsyncClient 的 mock — 支持异步上下文管理器。

    用法::

        with patch("httpx.AsyncClient", return_value=_build_httpx_mock({"a": 1})):
            ...
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)

    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=client)
    async_cm.__aexit__ = AsyncMock(return_value=None)
    return async_cm


# ==================================================================
# 飞书适配器
# ==================================================================

class TestFeishuGetRevision:
    """飞书 get_revision — 复用 _get_tenant_token + _get_document_info。"""

    @pytest.fixture
    def adapter(self) -> FeishuAdapter:
        return FeishuAdapter()

    async def test_returns_revision_id(self, adapter: FeishuAdapter) -> None:
        """API 返回 revision_id=42 → RevisionInfo.fingerprint == "42"。"""
        with (
            patch.object(
                adapter, "_get_tenant_token", new=AsyncMock(return_value="tenant-token-xxx")
            ),
            patch.object(
                adapter,
                "_get_document_info",
                new=AsyncMock(return_value={"title": "报销政策", "revision_id": 42}),
            ),
        ):
            rev = await adapter.get_revision(
                "doccnXXXX", credentials={"app_id": "cli_x", "app_secret": "s"}
            )
        assert rev is not None
        assert rev.fingerprint == "42"

    async def test_missing_revision_returns_none(self, adapter: FeishuAdapter) -> None:
        """API 未返回 revision_id → None（降级 fetch 全文）。"""
        with (
            patch.object(
                adapter, "_get_tenant_token", new=AsyncMock(return_value="t")
            ),
            patch.object(
                adapter,
                "_get_document_info",
                new=AsyncMock(return_value={"title": "doc"}),
            ),
        ):
            rev = await adapter.get_revision(
                "doccnX", credentials={"app_id": "a", "app_secret": "b"}
            )
        assert rev is None

    async def test_missing_credentials_raises(self, adapter: FeishuAdapter) -> None:
        """缺 app_id/app_secret → AdapterError。"""
        with pytest.raises(AdapterError, match="app_id"):
            await adapter.get_revision("doccnX", credentials={})

    async def test_doc_token_extracted_from_url(self, adapter: FeishuAdapter) -> None:
        """URL 形式的 doc_url_or_id 应自动提取 token。"""
        captured: dict[str, Any] = {}

        async def fake_get_doc_info(doc_token: str, token: str) -> dict[str, Any]:
            captured["doc_token"] = doc_token
            return {"revision_id": 7}

        with (
            patch.object(adapter, "_get_tenant_token", new=AsyncMock(return_value="t")),
            patch.object(adapter, "_get_document_info", new=fake_get_doc_info),
        ):
            rev = await adapter.get_revision(
                "https://example.feishu.cn/docs/doccnABCD1234",
                credentials={"app_id": "a", "app_secret": "b"},
            )
        assert rev is not None
        assert rev.fingerprint == "7"
        assert captured["doc_token"] == "doccnABCD1234"


# ==================================================================
# Confluence 适配器
# ==================================================================

class TestConfluenceGetRevision:
    """Confluence get_revision — expand=version 轻量查询。"""

    @pytest.fixture
    def adapter(self) -> ConfluenceAdapter:
        return ConfluenceAdapter()

    async def test_returns_version_number(self, adapter: ConfluenceAdapter) -> None:
        """API 返回 version.number=5 → RevisionInfo.fingerprint == "5"。"""
        api_data = {"version": {"number": 5, "when": "2026-08-12T10:00:00.000Z"}}
        with patch("httpx.AsyncClient", return_value=_build_httpx_mock(api_data)):
            rev = await adapter.get_revision(
                "123456789",
                credentials={
                    "base_url": "https://example.atlassian.net",
                    "username": "u@example.com",
                    "api_token": "ATATT...",
                },
            )
        assert rev is not None
        assert rev.fingerprint == "5"
        assert rev.last_modified == "2026-08-12T10:00:00.000Z"

    async def test_missing_version_returns_none(self, adapter: ConfluenceAdapter) -> None:
        """API 返回无 version.number → None。"""
        api_data = {"version": {"when": "2026-08-12"}}
        with patch("httpx.AsyncClient", return_value=_build_httpx_mock(api_data)):
            rev = await adapter.get_revision(
                "123",
                credentials={
                    "base_url": "https://example.atlassian.net",
                    "username": "u",
                    "api_token": "t",
                },
            )
        assert rev is None

    async def test_missing_base_url_raises(self, adapter: ConfluenceAdapter) -> None:
        """缺 base_url → AdapterError。"""
        with pytest.raises(AdapterError, match="base_url"):
            await adapter.get_revision("123", credentials={"username": "u", "api_token": "t"})

    async def test_404_raises_adapter_error(self, adapter: ConfluenceAdapter) -> None:
        """页面不存在（404）→ AdapterError with status_code=404。"""
        with patch("httpx.AsyncClient", return_value=_build_httpx_mock({}, status_code=404)):
            with pytest.raises(AdapterError) as exc_info:
                await adapter.get_revision(
                    "999",
                    credentials={
                        "base_url": "https://example.atlassian.net",
                        "username": "u",
                        "api_token": "t",
                    },
                )
        assert exc_info.value.status_code == 404


# ==================================================================
# Notion 适配器
# ==================================================================

class TestNotionGetRevision:
    """Notion get_revision — 复用 _get_page_info 拿 last_edited_time。"""

    @pytest.fixture
    def adapter(self) -> NotionAdapter:
        return NotionAdapter()

    async def test_returns_last_edited_time(self, adapter: NotionAdapter) -> None:
        """API 返回 last_edited_time → RevisionInfo.fingerprint == 该时间字符串。"""
        page_info = {"last_edited_time": "2026-08-12T10:00:00.000Z"}
        with patch.object(adapter, "_get_page_info", new=AsyncMock(return_value=page_info)):
            rev = await adapter.get_revision(
                "1234567890abcdef1234567890abcdef",
                credentials={"integration_token": "secret_xxx"},
            )
        assert rev is not None
        assert rev.fingerprint == "2026-08-12T10:00:00.000Z"
        assert rev.last_modified == "2026-08-12T10:00:00.000Z"

    async def test_missing_last_edited_returns_none(self, adapter: NotionAdapter) -> None:
        """API 未返回 last_edited_time → None。"""
        with patch.object(adapter, "_get_page_info", new=AsyncMock(return_value={})):
            rev = await adapter.get_revision(
                "1234567890abcdef1234567890abcdef",
                credentials={"integration_token": "t"},
            )
        assert rev is None

    async def test_missing_token_raises(self, adapter: NotionAdapter) -> None:
        """缺 integration_token/access_token → AdapterError。"""
        with pytest.raises(AdapterError, match="integration_token"):
            await adapter.get_revision(
                "1234567890abcdef1234567890abcdef", credentials={}
            )

    async def test_access_token_alias_accepted(self, adapter: NotionAdapter) -> None:
        """access_token 字段作为 integration_token 的别名。"""
        page_info = {"last_edited_time": "2026-01-01T00:00:00.000Z"}
        with patch.object(adapter, "_get_page_info", new=AsyncMock(return_value=page_info)):
            rev = await adapter.get_revision(
                "1234567890abcdef1234567890abcdef",
                credentials={"access_token": "alt-token"},
            )
        assert rev is not None
        assert rev.fingerprint == "2026-01-01T00:00:00.000Z"


# ==================================================================
# Obsidian 适配器（本地文件系统）
# ==================================================================

class TestObsidianGetRevision:
    """Obsidian get_revision — os.stat mtime_ns，零网络成本。"""

    @pytest.fixture
    def adapter(self) -> ObsidianAdapter:
        return ObsidianAdapter()

    async def test_returns_mtime_ns(self, adapter: ObsidianAdapter, tmp_path) -> None:
        """返回 st_mtime_ns 字符串作为指纹。"""
        vault = tmp_path / "vault"
        vault.mkdir()
        md_file = vault / "Note.md"
        md_file.write_text("# Hello", encoding="utf-8")

        rev = await adapter.get_revision(
            "Note.md",
            credentials={"vault_path": str(vault)},
        )
        assert rev is not None
        # fingerprint 是纳秒时间戳字符串，纯数字
        assert rev.fingerprint.isdigit()
        assert int(rev.fingerprint) > 0

    async def test_fingerprint_changes_on_edit(self, adapter: ObsidianAdapter, tmp_path) -> None:
        """文件修改后 mtime_ns 变化 → fingerprint 变化。"""
        vault = tmp_path / "vault"
        vault.mkdir()
        md_file = vault / "Note.md"
        md_file.write_text("v1", encoding="utf-8")

        rev1 = await adapter.get_revision(
            "Note.md", credentials={"vault_path": str(vault)}
        )
        # 修改文件 + 显式调整 mtime（确保纳秒级变化）
        md_file.write_text("v2 content longer", encoding="utf-8")
        import os, time
        new_ts = time.time() + 10
        os.utime(md_file, (new_ts, new_ts))

        rev2 = await adapter.get_revision(
            "Note.md", credentials={"vault_path": str(vault)}
        )
        assert rev1 is not None and rev2 is not None
        assert rev1.fingerprint != rev2.fingerprint

    async def test_missing_vault_path_raises(self, adapter: ObsidianAdapter) -> None:
        """缺 vault_path → AdapterError。"""
        with pytest.raises(AdapterError, match="vault_path"):
            await adapter.get_revision("Note.md", credentials={})

    async def test_nonexistent_vault_raises(self, adapter: ObsidianAdapter, tmp_path) -> None:
        """vault 目录不存在 → AdapterError。"""
        with pytest.raises(AdapterError, match="vault 目录不存在"):
            await adapter.get_revision(
                "Note.md",
                credentials={"vault_path": str(tmp_path / "no-such-vault")},
            )

    async def test_nonexistent_file_raises(self, adapter: ObsidianAdapter, tmp_path) -> None:
        """文件不存在 → AdapterError with status_code=404。"""
        vault = tmp_path / "vault"
        vault.mkdir()
        with pytest.raises(AdapterError) as exc_info:
            await adapter.get_revision(
                "Missing.md",
                credentials={"vault_path": str(vault)},
            )
        assert exc_info.value.status_code == 404

    async def test_path_traversal_blocked(self, adapter: ObsidianAdapter, tmp_path) -> None:
        """路径穿越（../../etc/passwd）→ AdapterError。"""
        vault = tmp_path / "vault"
        vault.mkdir()
        # 在 vault 父目录放一个文件，尝试通过相对路径访问
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")

        with pytest.raises(AdapterError, match="路径越界"):
            await adapter.get_revision(
                "../secret.txt",
                credentials={"vault_path": str(vault)},
            )


# ==================================================================
# 基类默认实现
# ==================================================================

class TestBaseAdapterDefaultRevision:
    """DocumentSourceAdapter 基类 get_revision 默认返回 None（不支持轻量探测）。"""

    async def test_base_returns_none(self) -> None:
        from app.document.source_adapters.base import DocumentSourceAdapter

        # DocumentSourceAdapter 是 ABC，需实现抽象方法；构造一个最小子类
        class _Stub(DocumentSourceAdapter):
            adapter_id = "stub"

            async def fetch(self, doc_url_or_id, credentials):
                ...

            async def list_documents(self, space_or_root, credentials):
                ...

            async def test_connection(self, credentials) -> bool:
                return True

        stub = _Stub()
        rev = await stub.get_revision("any", {})
        assert rev is None
