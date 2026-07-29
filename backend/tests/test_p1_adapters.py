"""P1 多平台适配器测试 — 飞书 Adapter + Notion Adapter。

测试覆盖：
- 飞书 Adapter：doc_token 提取、认证 token 获取、blocks → Markdown 转换、fetch（mocked）
- Notion Adapter：page ID 提取、标题提取、rich_text 解析、blocks → Markdown 转换、fetch（mocked）
- 注册表：飞书和 Notion 适配器自动注册
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# 飞书 Adapter 测试
# ======================================================================


class TestFeishuAdapter:
    """飞书适配器测试。"""

    def test_extract_doc_token_pure(self) -> None:
        """纯 token 提取。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        assert FeishuAdapter._extract_doc_token("doccnXXXX1234") == "doccnXXXX1234"

    def test_extract_doc_token_from_docs_url(self) -> None:
        """从 /docs/ URL 提取 token。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        url = "https://example.feishu.cn/docs/doccnXXXX1234abcd"
        assert FeishuAdapter._extract_doc_token(url) == "doccnXXXX1234abcd"

    def test_extract_doc_token_from_wiki_url(self) -> None:
        """从 /wiki/ URL 提取 token。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        url = "https://example.feishu.cn/wiki/wikcnYYYY5678efgh"
        assert FeishuAdapter._extract_doc_token(url) == "wikcnYYYY5678efgh"

    def test_extract_doc_token_from_lark_url(self) -> None:
        """从 larksuite URL 提取 token。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        url = "https://example.larksuite.com/docs/doccnZZZZ9012ijkl"
        assert FeishuAdapter._extract_doc_token(url) == "doccnZZZZ9012ijkl"

    def test_extract_doc_token_invalid(self) -> None:
        """无效输入应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        with pytest.raises(AdapterError):
            FeishuAdapter._extract_doc_token("no-token-here")

    def test_blocks_to_markdown_basic(self) -> None:
        """块 → Markdown 基本转换。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter = FeishuAdapter()
        blocks = [
            {"block_id": "page1", "block_type": 1, "children": ["b1", "b2", "b3", "b4"]},
            {
                "block_id": "b1",
                "block_type": 3,  # heading1
                "heading1": {
                    "elements": [{"text_run": {"content": "架构设计"}}]
                },
            },
            {
                "block_id": "b2",
                "block_type": 2,  # text
                "text": {
                    "elements": [{"text_run": {"content": "这是一段正文。"}}]
                },
            },
            {
                "block_id": "b3",
                "block_type": 12,  # bullet
                "bullet": {
                    "elements": [{"text_run": {"content": "列表项1"}}]
                },
            },
            {
                "block_id": "b4",
                "block_type": 14,  # code
                "code": {
                    "style": {"language": "python"},
                    "elements": [{"text_run": {"content": "print('hello')"}}]
                },
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "文档标题")

        assert "# 文档标题" in result
        assert "# 架构设计" in result
        assert "这是一段正文。" in result
        assert "- 列表项1" in result
        assert "```python" in result
        assert "print('hello')" in result

    def test_blocks_to_markdown_headings(self) -> None:
        """h1-h6 标题转换。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter = FeishuAdapter()
        blocks = [
            {"block_id": "page1", "block_type": 1, "children": [f"h{i}" for i in range(1, 7)]},
        ]
        for i in range(1, 7):
            field = f"heading{i}"
            blocks.append({
                "block_id": f"h{i}",
                "block_type": 2 + i,  # heading1=3, heading2=4, ...
                field: {"elements": [{"text_run": {"content": f"标题{i}"}}]},
            })

        result = adapter._blocks_to_markdown(blocks, "测试")

        for i in range(1, 7):
            assert f"{'#' * i} 标题{i}" in result

    def test_blocks_to_markdown_quote_divider(self) -> None:
        """引用和分割线转换。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter = FeishuAdapter()
        blocks = [
            {"block_id": "page1", "block_type": 1, "children": ["q1", "d1"]},
            {
                "block_id": "q1",
                "block_type": 15,  # quote
                "quote": {"elements": [{"text_run": {"content": "引用内容"}}]},
            },
            {"block_id": "d1", "block_type": 19},  # divider
        ]

        result = adapter._blocks_to_markdown(blocks, "测试")

        assert "> 引用内容" in result
        assert "---" in result

    @pytest.mark.asyncio
    async def test_fetch_missing_credentials(self) -> None:
        """缺少凭证时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter = FeishuAdapter()
        with pytest.raises(AdapterError, match="app_id"):
            await adapter.fetch("doccnXXXX", credentials={})

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """fetch 成功 — 模拟飞书 API 响应。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        # Mock token 响应
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "code": 0,
            "tenant_access_token": "t-xxx",
        }
        token_resp.raise_for_status = MagicMock()

        # Mock 文档信息响应
        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "code": 0,
            "data": {
                "document": {
                    "document_id": "doccnXXXX",
                    "title": "架构设计文档",
                }
            },
        }
        doc_resp.raise_for_status = MagicMock()

        # Mock 块列表响应
        blocks_resp = MagicMock()
        blocks_resp.status_code = 200
        blocks_resp.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {"block_id": "page1", "block_type": 1, "children": ["b1"]},
                    {
                        "block_id": "b1",
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": "正文内容"}}]},
                    },
                ],
                "has_more": False,
            },
        }
        blocks_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=token_resp)
        mock_client.get = AsyncMock(
            side_effect=[doc_resp, blocks_resp]  # 第一次 GET 文档信息，第二次 GET 块列表
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = FeishuAdapter()
            result = await adapter.fetch(
                "doccnXXXX",
                credentials={"app_id": "cli_xxx", "app_secret": "secret"},
            )

        assert result.source == "feishu"
        assert result.title == "架构设计文档"
        assert "正文内容" in result.content
        assert result.format == "markdown"
        assert result.doc_id == "doccnXXXX"

    @pytest.mark.asyncio
    async def test_test_connection_success(self) -> None:
        """test_connection 成功。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"code": 0, "tenant_access_token": "t-xxx"}
        token_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=token_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = FeishuAdapter()
            assert await adapter.test_connection({
                "app_id": "cli_xxx",
                "app_secret": "secret",
            }) is True

    @pytest.mark.asyncio
    async def test_test_connection_missing_creds(self) -> None:
        """缺少凭证时 test_connection 返回 False。"""
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter = FeishuAdapter()
        assert await adapter.test_connection({}) is False


# ======================================================================
# Notion Adapter 测试
# ======================================================================


class TestNotionAdapter:
    """Notion 适配器测试。"""

    def test_extract_page_id_pure(self) -> None:
        """纯 32 位十六进制 ID。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        assert NotionAdapter._extract_page_id("1234567890abcdef1234567890abcdef") == "1234567890abcdef1234567890abcdef"

    def test_extract_page_id_with_hyphens(self) -> None:
        """带连字符的 UUID 格式。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        result = NotionAdapter._extract_page_id("12345678-90ab-cdef-1234-567890abcdef")
        assert result == "1234567890abcdef1234567890abcdef"

    def test_extract_page_id_from_url(self) -> None:
        """从 URL 提取 page ID。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        url = "https://www.notion.so/Workspace/Architecture-Design-1234567890abcdef1234567890abcdef"
        result = NotionAdapter._extract_page_id(url)
        assert result == "1234567890abcdef1234567890abcdef"

    def test_extract_page_id_invalid(self) -> None:
        """无效输入应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.notion_adapter import NotionAdapter

        with pytest.raises(AdapterError):
            NotionAdapter._extract_page_id("not-a-valid-id")

    def test_build_headers(self) -> None:
        """请求头构建 — 包含 Authorization 和 Notion-Version。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        headers = NotionAdapter._build_headers("secret_xxx")

        assert headers["Authorization"] == "Bearer secret_xxx"
        assert "Notion-Version" in headers
        assert headers["Notion-Version"] == "2022-06-28"

    def test_rich_text_to_plain(self) -> None:
        """rich_text 数组 → 纯文本。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        rich_text = [
            {"plain_text": "Hello "},
            {"plain_text": "World"},
            {"plain_text": "!"},
        ]
        result = NotionAdapter._rich_text_to_plain(rich_text)
        assert result == "Hello World!"

    def test_rich_text_to_plain_empty(self) -> None:
        """空 rich_text 返回空字符串。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        assert NotionAdapter._rich_text_to_plain([]) == ""
        assert NotionAdapter._rich_text_to_plain([{}]) == ""

    def test_extract_page_title_from_properties(self) -> None:
        """从页面 properties 提取标题。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        page = {
            "properties": {
                "title": {
                    "title": [
                        {"plain_text": "架构设计"},
                        {"plain_text": "文档"},
                    ]
                }
            }
        }
        assert NotionAdapter._extract_page_title(page) == "架构设计文档"

    def test_extract_page_title_from_name_property(self) -> None:
        """从 Name 属性提取标题（数据库页面）。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        page = {
            "properties": {
                "Name": {
                    "title": [
                        {"plain_text": "需求文档"},
                    ]
                }
            }
        }
        assert NotionAdapter._extract_page_title(page) == "需求文档"

    def test_extract_page_title_from_url(self) -> None:
        """从 URL 回退提取标题。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        page = {
            "properties": {},
            "url": "https://www.notion.so/Workspace/Architecture-Design-1234567890abcdef1234567890abcdef",
        }
        title = NotionAdapter._extract_page_title(page)
        assert "Architecture Design" in title

    def test_blocks_to_markdown_basic(self) -> None:
        """块 → Markdown 基本转换。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        blocks = [
            {
                "type": "heading_1",
                "heading_1": {"rich_text": [{"plain_text": "第一章"}]},
            },
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "正文内容"}]},
            },
            {
                "type": "heading_2",
                "heading_2": {"rich_text": [{"plain_text": "1.1 节"}]},
            },
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": "列表项"}]},
            },
            {
                "type": "code",
                "code": {
                    "rich_text": [{"plain_text": "print('hello')"}],
                    "language": "python",
                },
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "文档标题")

        assert "# 文档标题" in result
        assert "# 第一章" in result
        assert "正文内容" in result
        assert "## 1.1 节" in result
        assert "- 列表项" in result
        assert "```python" in result
        assert "print('hello')" in result

    def test_blocks_to_markdown_todo_divider_quote(self) -> None:
        """待办/分割线/引用转换。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        blocks = [
            {
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"plain_text": "已完成任务"}],
                    "checked": True,
                },
            },
            {
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"plain_text": "未完成任务"}],
                    "checked": False,
                },
            },
            {"type": "divider", "divider": {}},
            {
                "type": "quote",
                "quote": {"rich_text": [{"plain_text": "引用内容"}]},
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "测试")

        assert "- [x] 已完成任务" in result
        assert "- [ ] 未完成任务" in result
        assert "---" in result
        assert "> 引用内容" in result

    def test_blocks_to_markdown_callout(self) -> None:
        """Callout 块转换。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        blocks = [
            {
                "type": "callout",
                "callout": {
                    "rich_text": [{"plain_text": "注意：这是一个提示"}],
                    "icon": {"emoji": "💡"},
                },
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "测试")

        assert "> 💡 注意：这是一个提示" in result

    def test_blocks_to_markdown_image(self) -> None:
        """图片块转换。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": "https://example.com/image.png"},
                    "caption": [{"plain_text": "架构图"}],
                },
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "测试")

        assert "![架构图](https://example.com/image.png)" in result

    def test_blocks_to_markdown_numbered_list(self) -> None:
        """有序列表转换。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        blocks = [
            {
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"plain_text": "第一步"}]},
            },
            {
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"plain_text": "第二步"}]},
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "测试")

        assert "1. 第一步" in result
        assert "1. 第二步" in result

    @pytest.mark.asyncio
    async def test_fetch_missing_token(self) -> None:
        """缺少 token 时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        with pytest.raises(AdapterError, match="integration_token"):
            await adapter.fetch("1234567890abcdef1234567890abcdef", credentials={})

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """fetch 成功 — 模拟 Notion API 响应。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        # Mock 页面信息响应
        page_resp = MagicMock()
        page_resp.status_code = 200
        page_resp.json.return_value = {
            "id": "1234567890abcdef1234567890abcdef",
            "url": "https://www.notion.so/Workspace/Test-1234567890abcdef1234567890abcdef",
            "properties": {
                "title": {
                    "title": [{"plain_text": "架构设计文档"}]
                }
            },
        }
        page_resp.raise_for_status = MagicMock()

        # Mock 块列表响应
        blocks_resp = MagicMock()
        blocks_resp.status_code = 200
        blocks_resp.json.return_value = {
            "results": [
                {
                    "type": "heading_1",
                    "heading_1": {"rich_text": [{"plain_text": "概述"}]},
                },
                {
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "这是概述内容。"}]},
                },
            ],
            "has_more": False,
        }
        blocks_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[page_resp, blocks_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = NotionAdapter()
            result = await adapter.fetch(
                "1234567890abcdef1234567890abcdef",
                credentials={"integration_token": "secret_xxx"},
            )

        assert result.source == "notion"
        assert result.title == "架构设计文档"
        assert "# 概述" in result.content
        assert "这是概述内容。" in result.content
        assert result.format == "markdown"

    @pytest.mark.asyncio
    async def test_test_connection_success(self) -> None:
        """test_connection 成功。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        resp = MagicMock()
        resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = NotionAdapter()
            assert await adapter.test_connection({
                "integration_token": "secret_xxx",
            }) is True

    @pytest.mark.asyncio
    async def test_test_connection_missing_token(self) -> None:
        """缺少 token 时 test_connection 返回 False。"""
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter = NotionAdapter()
        assert await adapter.test_connection({}) is False


# ======================================================================
# 注册表 P1 适配器测试
# ======================================================================


class TestP1AdapterRegistry:
    """P1 适配器注册表测试。"""

    def test_registry_has_feishu(self) -> None:
        """注册表应注册飞书适配器。"""
        from app.document.source_adapters.registry import adapter_registry

        adapter = adapter_registry.get("feishu")
        assert adapter is not None
        assert adapter.adapter_id == "feishu"
        assert adapter.display_name == "飞书文档"
        assert "markdown" in adapter.supported_formats

    def test_registry_has_notion(self) -> None:
        """注册表应注册 Notion 适配器。"""
        from app.document.source_adapters.registry import adapter_registry

        adapter = adapter_registry.get("notion")
        assert adapter is not None
        assert adapter.adapter_id == "notion"
        assert adapter.display_name == "Notion"
        assert "markdown" in adapter.supported_formats

    def test_registry_has_all_four_adapters(self) -> None:
        """注册表应注册全部四个适配器。"""
        from app.document.source_adapters.registry import adapter_registry

        adapters = adapter_registry.list_adapters()
        adapter_ids = [a["adapter_id"] for a in adapters]

        assert "confluence" in adapter_ids
        assert "obsidian" in adapter_ids
        assert "feishu" in adapter_ids
        assert "notion" in adapter_ids
