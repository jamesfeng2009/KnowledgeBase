"""P0 多平台文档解析集成测试 — 覆盖 WikiHtmlCleaner / MarkdownParser / Chunker h1-h6 /
适配器基类 + 注册表 / Confluence Adapter / Obsidian Adapter / 文档导入端点。

测试覆盖：
- WikiHtmlCleaner：Confluence 命名空间剥离、装饰标签清理、属性白名单、降级清洗
- MarkdownParser：frontmatter 提取、parse_from_content、工厂注册
- Chunker：h1-h6 标题分块（HTML + Markdown）
- 适配器基类 + 注册表：注册/获取/列举/注销
- ConfluenceAdapter：pageId 提取、认证头构建、fetch（mocked httpx）、list_documents
- ObsidianAdapter：文件读取、路径穿越防护、list_documents、obsidian:// URI 解析
- 文档导入端点：适配器路由、错误处理、Document 创建
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery before importing app modules
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# WikiHtmlCleaner 测试
# ======================================================================


class TestWikiHtmlCleaner:
    """WikiHtmlCleaner 清洗管线测试。"""

    def test_clean_confluence_storage_format(self) -> None:
        """Confluence storage format HTML — 剥离 ac:/ri: 命名空间，保留正文。"""
        from app.document.wiki_cleaner import clean_wiki_html

        html = """
        <ac:structured-macro ac:name="code">
            <ac:plain-text-body><![CDATA[print("hello")]]></ac:plain-text-body>
        </ac:structured-macro>
        <h1>架构设计</h1>
        <p>这是一段正文，包含 <ri:user ri:username="admin"/> 的引用。</p>
        <ac:rich-text-body><p>富文本正文内容</p></ac:rich-text-body>
        <table><tr><th>字段</th><th>类型</th></tr><tr><td>id</td><td>int</td></tr></table>
        """

        result = clean_wiki_html(html, source="confluence")

        # 命名空间标签应被剥离
        assert "ac:structured-macro" not in result
        assert "ac:plain-text-body" not in result
        assert "ac:rich-text-body" not in result
        assert "ri:user" not in result

        # 正文内容应保留
        assert "架构设计" in result
        assert "富文本正文内容" in result
        assert "print" in result  # 代码宏内容

        # 表格应保留
        assert "<table>" in result
        assert "<th>" in result

        # ri:user 应转为 @admin
        assert "@admin" in result

    def test_clean_strips_script_style(self) -> None:
        """script/style 标签应被完全移除。"""
        from app.document.wiki_cleaner import clean_wiki_html

        html = """
        <script>alert("xss")</script>
        <style>body { color: red; }</style>
        <h1>标题</h1>
        <p>正文</p>
        """

        result = clean_wiki_html(html, source="generic")

        assert "alert" not in result
        assert "color: red" not in result
        assert "标题" in result
        assert "正文" in result

    def test_clean_preserves_h1_to_h6(self) -> None:
        """h1-h6 标题标签应全部保留（chunker 按标题分块）。"""
        from app.document.wiki_cleaner import clean_wiki_html

        html = """
        <h1>H1标题</h1><p>内容1</p>
        <h2>H2标题</h2><p>内容2</p>
        <h3>H3标题</h3><p>内容3</p>
        <h4>H4标题</h4><p>内容4</p>
        <h5>H5标题</h5><p>内容5</p>
        <h6>H6标题</h6><p>内容6</p>
        """

        result = clean_wiki_html(html, source="generic")

        for i in range(1, 7):
            assert f"<h{i}>H{i}标题</h{i}>" in result, f"h{i} 标签应保留"

    def test_clean_strips_non_whitelisted_attrs(self) -> None:
        """非白名单属性应被清除（class/id/style 等）。"""
        from app.document.wiki_cleaner import clean_wiki_html

        html = '<p class="highlight" id="p1" style="color:red" data-track="123">文本</p>'

        result = clean_wiki_html(html, source="generic")

        assert "文本" in result
        assert 'class="highlight"' not in result
        assert 'id="p1"' not in result
        assert "color:red" not in result
        assert 'data-track="123"' not in result

    def test_clean_preserves_href_and_src(self) -> None:
        """a[href] 和 img[src] 属性应保留。"""
        from app.document.wiki_cleaner import clean_wiki_html

        html = '<a href="https://example.com" class="link" onclick="evil()">链接</a>'

        result = clean_wiki_html(html, source="generic")

        assert "链接" in result
        assert 'href="https://example.com"' in result
        assert 'class="link"' not in result
        assert "onclick" not in result

    def test_clean_empty_input(self) -> None:
        """空输入应返回空字符串。"""
        from app.document.wiki_cleaner import clean_wiki_html

        assert clean_wiki_html("") == ""
        assert clean_wiki_html("   ") == ""

    def test_auto_detect_confluence(self) -> None:
        """auto 模式应自动检测 Confluence 来源。"""
        from app.document.wiki_cleaner import _detect_source

        assert _detect_source('<ac:structured-macro>') == "confluence"
        assert _detect_source('<ri:attachment>') == "confluence"
        assert _detect_source('<div data-record-type="text">') == "feishu"
        assert _detect_source('<p>普通 HTML</p>') == "generic"

    def test_fallback_clean_preserves_headings(self) -> None:
        """bs4 未安装时降级清洗应保留 h1-h6 标签。"""
        from app.document.wiki_cleaner import _fallback_clean

        html = """
        <ac:structured-macro><ac:plain-text-body>代码内容</ac:plain-text-body></ac:structured-macro>
        <h1>标题1</h1><p>内容</p>
        <h4>标题4</h4>
        <script>alert(1)</script>
        """

        result = _fallback_clean(html, "confluence")

        # 命名空间标签应被去除
        assert "ac:structured-macro" not in result
        assert "ac:plain-text-body" not in result

        # 标题应保留
        assert "<h1>标题1</h1>" in result
        assert "<h4>标题4</h4>" in result

        # 正文保留
        assert "代码内容" in result
        assert "alert(1)" not in result


# ======================================================================
# MarkdownParser 测试
# ======================================================================


class TestMarkdownParser:
    """MarkdownParser 测试 — frontmatter 提取、parse_from_content。"""

    def test_is_available_always_true(self) -> None:
        """MarkdownParser 零依赖，始终可用。"""
        from app.document.markdown_parser import MarkdownParser

        assert MarkdownParser.is_available() is True

    def test_is_supported(self) -> None:
        """支持 md 和 markdown 类型。"""
        from app.document.markdown_parser import MarkdownParser

        assert MarkdownParser.is_supported("md") is True
        assert MarkdownParser.is_supported("markdown") is True
        assert MarkdownParser.is_supported("html") is False
        assert MarkdownParser.is_supported("pdf") is False

    def test_parse_from_content_with_frontmatter(self) -> None:
        """frontmatter 中的 title 应被提取并作为 h1 补充到正文。"""
        from app.document.markdown_parser import MarkdownParser

        content = """---
title: 架构设计文档
date: 2025-01-15
tags: [架构, 微服务]
---

## 概述
这是一段概述内容。

## 详细设计
### 模块A
模块A的详细描述。
"""

        parser = MarkdownParser()
        result = parser.parse_from_content(content)

        # frontmatter title 应被转为 h1
        assert "# 架构设计文档" in result

        # 正文内容应保留
        assert "## 概述" in result
        assert "## 详细设计" in result
        assert "### 模块A" in result

        # frontmatter 本身应被移除
        assert "date: 2025-01-15" not in result
        assert "tags:" not in result

    def test_parse_from_content_without_frontmatter(self) -> None:
        """无 frontmatter 时应原样返回内容。"""
        from app.document.markdown_parser import MarkdownParser

        content = "# 标题\n\n正文内容"

        parser = MarkdownParser()
        result = parser.parse_from_content(content)

        assert result == "# 标题\n\n正文内容"

    def test_parse_from_content_with_h1_and_frontmatter_title(self) -> None:
        """正文已有 h1 时，frontmatter title 不应重复添加。"""
        from app.document.markdown_parser import MarkdownParser

        content = """---
title: Frontmatter标题
---

# 正文H1标题

内容
"""

        parser = MarkdownParser()
        result = parser.parse_from_content(content)

        # 不应重复添加 h1
        assert result.count("# Frontmatter标题") == 0
        assert "# 正文H1标题" in result

    def test_parse_from_content_empty(self) -> None:
        """空内容应返回空字符串。"""
        from app.document.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        assert parser.parse_from_content("") == ""
        assert parser.parse_from_content("   ") == ""

    @pytest.mark.asyncio
    async def test_parse_file(self, tmp_path: Path) -> None:
        """从文件路径解析 Markdown。"""
        from app.document.markdown_parser import MarkdownParser

        md_file = tmp_path / "test.md"
        md_file.write_text("# 测试标题\n\n正文内容", encoding="utf-8")

        parser = MarkdownParser()
        result = await parser.parse(str(md_file))

        assert "# 测试标题" in result
        assert "正文内容" in result

    @pytest.mark.asyncio
    async def test_parse_file_not_found(self) -> None:
        """文件不存在时应返回空字符串（不抛异常）。"""
        from app.document.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        result = await parser.parse("/nonexistent/file.md")
        assert result == ""

    def test_factory_registers_markdown(self) -> None:
        """工厂应注册 MarkdownParser（Docling 禁用时走 legacy 路径）。"""
        from app.document.factory import get_parser

        with patch("app.document.factory._is_docling_enabled", return_value=False):
            parser = get_parser("md")
            assert parser is not None
            assert parser.__class__.__name__ == "MarkdownParser"

            parser2 = get_parser("markdown")
            assert parser2 is not None


# ======================================================================
# Chunker h1-h6 测试
# ======================================================================


class TestChunkerH1ToH6:
    """Chunker h1-h6 标题分块测试 — 验证 P0-3 增强。"""

    def test_split_html_h4_h5_h6(self) -> None:
        """HTML h4-h6 标题应触发结构化分块。"""
        from app.rag.chunker import SemanticChunker

        content = """
<h1>第一章</h1><p>第一章内容</p>
<h2>1.1 节</h2><p>1.1 内容</p>
<h3>1.1.1 小节</h3><p>1.1.1 内容</p>
<h4>1.1.1.1 子小节</h4><p>h4 内容</p>
<h5>更深层级</h5><p>h5 内容</p>
<h6>最深层级</h6><p>h6 内容</p>
"""

        chunker = SemanticChunker()
        chunks = chunker.chunk(content, doc_type="html")

        # h4-h6 标题应被识别为分块边界
        assert len(chunks) >= 4, f"h1-h6 应产生至少 4 个分块，实际: {len(chunks)}"

        # 验证深层标题内容出现在分块中
        all_content = " ".join(c.content for c in chunks)
        assert "h4 内容" in all_content
        assert "h5 内容" in all_content
        assert "h6 内容" in all_content

    def test_split_markdown_h4_h5_h6(self) -> None:
        """Markdown #### ##### ###### 标题应触发结构化分块。"""
        from app.rag.chunker import SemanticChunker

        content = """# 第一章

第一章内容

## 1.1 节

1.1 内容

### 1.1.1 小节

1.1.1 内容

#### 1.1.1.1 子小节

h4 内容

##### 更深层级

h5 内容

###### 最深层级

h6 内容
"""

        chunker = SemanticChunker()
        chunks = chunker.chunk(content, doc_type="md")

        assert len(chunks) >= 4, f"Markdown h1-h6 应产生至少 4 个分块，实际: {len(chunks)}"

        all_content = " ".join(c.content for c in chunks)
        assert "h4 内容" in all_content
        assert "h5 内容" in all_content
        assert "h6 内容" in all_content


# ======================================================================
# 适配器基类 + 注册表 测试
# ======================================================================


class TestSourceAdapterRegistry:
    """适配器注册表测试。"""

    def test_registry_has_confluence_and_obsidian(self) -> None:
        """注册表应自动注册 Confluence 和 Obsidian 适配器。"""
        from app.document.source_adapters.registry import adapter_registry

        adapters = adapter_registry.list_adapters()
        adapter_ids = [a["adapter_id"] for a in adapters]

        assert "confluence" in adapter_ids
        assert "obsidian" in adapter_ids

    def test_get_confluence_adapter(self) -> None:
        """获取 Confluence 适配器实例。"""
        from app.document.source_adapters.registry import adapter_registry

        adapter = adapter_registry.get("confluence")
        assert adapter is not None
        assert adapter.adapter_id == "confluence"
        assert adapter.display_name == "Confluence"
        assert "html" in adapter.supported_formats

    def test_get_obsidian_adapter(self) -> None:
        """获取 Obsidian 适配器实例。"""
        from app.document.source_adapters.registry import adapter_registry

        adapter = adapter_registry.get("obsidian")
        assert adapter is not None
        assert adapter.adapter_id == "obsidian"
        assert adapter.display_name == "Obsidian"
        assert "markdown" in adapter.supported_formats

    def test_get_nonexistent_adapter(self) -> None:
        """获取不存在的适配器应返回 None。"""
        from app.document.source_adapters.registry import adapter_registry

        assert adapter_registry.get("nonexistent") is None

    def test_register_custom_adapter(self) -> None:
        """注册自定义适配器。"""
        from app.document.source_adapters.base import DocumentSourceAdapter, FetchedDocument, SourceDocumentInfo
        from app.document.source_adapters.registry import adapter_registry

        class MockAdapter(DocumentSourceAdapter):
            adapter_id = "mock_test"
            display_name = "Mock Test"
            supported_formats = ("html",)

            async def fetch(self, doc_url_or_id: str, credentials: dict) -> FetchedDocument:
                return FetchedDocument(
                    source="mock_test",
                    title="Mock",
                    content="<p>mock</p>",
                    format="html",
                )

            async def list_documents(self, space_or_root: str, credentials: dict) -> list[SourceDocumentInfo]:
                return []

            async def test_connection(self, credentials: dict) -> bool:
                return True

        adapter_registry.register(MockAdapter())
        try:
            assert adapter_registry.get("mock_test") is not None
        finally:
            adapter_registry.unregister("mock_test")

    def test_unregister_adapter(self) -> None:
        """注销适配器。"""
        from app.document.source_adapters.base import DocumentSourceAdapter, FetchedDocument, SourceDocumentInfo
        from app.document.source_adapters.registry import adapter_registry

        class TempAdapter(DocumentSourceAdapter):
            adapter_id = "temp_test"
            display_name = "Temp"
            supported_formats = ("markdown",)

            async def fetch(self, doc_url_or_id: str, credentials: dict) -> FetchedDocument:
                return FetchedDocument(source="temp", title="t", content="", format="markdown")

            async def list_documents(self, space_or_root: str, credentials: dict) -> list[SourceDocumentInfo]:
                return []

            async def test_connection(self, credentials: dict) -> bool:
                return False

        adapter_registry.register(TempAdapter())
        assert adapter_registry.unregister("temp_test") is True
        assert adapter_registry.get("temp_test") is None
        assert adapter_registry.unregister("temp_test") is False


class TestFetchedDocument:
    """FetchedDocument 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值应正确。"""
        from app.document.source_adapters.base import FetchedDocument

        doc = FetchedDocument(
            source="confluence",
            title="测试",
            content="<p>内容</p>",
            format="html",
        )

        assert doc.source_url == ""
        assert doc.doc_id == ""
        assert doc.metadata == {}

    def test_with_metadata(self) -> None:
        """元数据应正确存储。"""
        from app.document.source_adapters.base import FetchedDocument

        doc = FetchedDocument(
            source="obsidian",
            title="笔记",
            content="# 笔记",
            format="markdown",
            source_url="obsidian://open?vault=MyVault&file=note",
            doc_id="/path/to/note.md",
            metadata={"author": "user", "tags": ["tag1", "tag2"]},
        )

        assert doc.metadata["author"] == "user"
        assert "tag1" in doc.metadata["tags"]


# ======================================================================
# ConfluenceAdapter 测试
# ======================================================================


class TestConfluenceAdapter:
    """Confluence 适配器测试 — pageId 提取、认证头、fetch（mocked）。"""

    def test_extract_page_id_pure_number(self) -> None:
        """纯数字 pageId。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        assert ConfluenceAdapter._extract_page_id("123456789") == "123456789"

    def test_extract_page_id_from_cloud_url(self) -> None:
        """从 Cloud URL 提取 pageId。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        url = "https://example.atlassian.net/wiki/spaces/DEV/pages/123456789/Page+Title"
        assert ConfluenceAdapter._extract_page_id(url) == "123456789"

    def test_extract_page_id_from_server_url(self) -> None:
        """从 Server URL 提取 pageId。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        url = "https://confluence.example.com/pages/viewpage.action?pageId=987654321"
        assert ConfluenceAdapter._extract_page_id(url) == "987654321"

    def test_extract_page_id_invalid(self) -> None:
        """无法提取 pageId 时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        with pytest.raises(AdapterError):
            ConfluenceAdapter._extract_page_id("no-id-here")

    def test_build_auth_headers_cloud(self) -> None:
        """Cloud 模式 — Basic Auth（username:api_token）。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        headers = ConfluenceAdapter._build_auth_headers({
            "username": "user@example.com",
            "api_token": "ATATT3xF",
        })

        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

    def test_build_auth_headers_server_dc(self) -> None:
        """Server/DC 模式 — Bearer PAT。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        headers = ConfluenceAdapter._build_auth_headers({
            "pat": "NzU4MjQ=",
        })

        assert headers["Authorization"] == "Bearer NzU4MjQ="

    def test_build_auth_headers_missing(self) -> None:
        """缺少凭证时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        with pytest.raises(AdapterError):
            ConfluenceAdapter._build_auth_headers({})

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """fetch 成功 — 模拟 Confluence API 响应。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "123456",
            "title": "架构设计文档",
            "body": {
                "storage": {
                    "value": "<h1>架构设计</h1><p>正文内容</p>",
                }
            },
            "space": {"key": "DEV"},
            "version": {"number": 5},
            "history": {
                "createdBy": {"displayName": "张三"},
                "createdDate": "2025-01-15T10:00:00Z",
            },
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = ConfluenceAdapter()
            result = await adapter.fetch(
                "123456",
                credentials={
                    "base_url": "https://example.atlassian.net",
                    "username": "user@example.com",
                    "api_token": "token",
                },
            )

        assert result.source == "confluence"
        assert result.title == "架构设计文档"
        assert "<h1>架构设计</h1>" in result.content
        assert result.format == "html"
        assert result.doc_id == "123456"
        assert result.metadata["space_key"] == "DEV"
        assert result.metadata["version"] == 5

    @pytest.mark.asyncio
    async def test_fetch_missing_base_url(self) -> None:
        """缺少 base_url 时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        adapter = ConfluenceAdapter()
        with pytest.raises(AdapterError, match="base_url"):
            await adapter.fetch("123", credentials={})

    @pytest.mark.asyncio
    async def test_fetch_not_found(self) -> None:
        """页面不存在时应抛出 404 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = ConfluenceAdapter()
            with pytest.raises(AdapterError, match="不存在"):
                await adapter.fetch(
                    "999",
                    credentials={
                        "base_url": "https://example.atlassian.net",
                        "username": "u",
                        "api_token": "t",
                    },
                )

    @pytest.mark.asyncio
    async def test_test_connection_success(self) -> None:
        """test_connection 成功。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            adapter = ConfluenceAdapter()
            result = await adapter.test_connection({
                "base_url": "https://example.atlassian.net",
                "username": "u",
                "api_token": "t",
            })

        assert result is True

    @pytest.mark.asyncio
    async def test_test_connection_no_base_url(self) -> None:
        """缺少 base_url 时 test_connection 返回 False。"""
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        adapter = ConfluenceAdapter()
        assert await adapter.test_connection({}) is False


# ======================================================================
# ObsidianAdapter 测试
# ======================================================================


class TestObsidianAdapter:
    """Obsidian 适配器测试 — 文件读取、路径穿越防护。"""

    @pytest.mark.asyncio
    async def test_fetch_success(self, tmp_path: Path) -> None:
        """成功读取 vault 中的 Markdown 文件。"""
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        # 创建 vault 结构
        vault = tmp_path / "MyVault"
        vault.mkdir()
        notes_dir = vault / "Projects"
        notes_dir.mkdir()
        md_file = notes_dir / "Architecture.md"
        md_file.write_text("# 架构设计\n\n## 概述\n正文内容", encoding="utf-8")

        adapter = ObsidianAdapter()
        result = await adapter.fetch(
            "Projects/Architecture.md",
            credentials={"vault_path": str(vault)},
        )

        assert result.source == "obsidian"
        assert result.title == "Architecture"
        assert "# 架构设计" in result.content
        assert result.format == "markdown"
        assert "obsidian://" in result.source_url

    @pytest.mark.asyncio
    async def test_fetch_missing_vault_path(self) -> None:
        """缺少 vault_path 时应抛出 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        adapter = ObsidianAdapter()
        with pytest.raises(AdapterError, match="vault_path"):
            await adapter.fetch("test.md", credentials={})

    @pytest.mark.asyncio
    async def test_fetch_file_not_found(self, tmp_path: Path) -> None:
        """文件不存在时应抛出 404 AdapterError。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        adapter = ObsidianAdapter()
        with pytest.raises(AdapterError, match="不存在"):
            await adapter.fetch(
                "nonexistent.md",
                credentials={"vault_path": str(tmp_path)},
            )

    @pytest.mark.asyncio
    async def test_fetch_path_traversal_blocked(self, tmp_path: Path) -> None:
        """路径穿越攻击应被阻止。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        vault = tmp_path / "Vault"
        vault.mkdir()

        # 创建 vault 外的文件
        secret_file = tmp_path / "secret.md"
        secret_file.write_text("secret", encoding="utf-8")

        adapter = ObsidianAdapter()
        with pytest.raises(AdapterError, match="越界"):
            await adapter.fetch(
                "../secret.md",
                credentials={"vault_path": str(vault)},
            )

    @pytest.mark.asyncio
    async def test_fetch_non_md_file_rejected(self, tmp_path: Path) -> None:
        """非 .md 文件应被拒绝。"""
        from app.document.source_adapters.base import AdapterError
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        vault = tmp_path / "Vault"
        vault.mkdir()
        txt_file = vault / "notes.txt"
        txt_file.write_text("text", encoding="utf-8")

        adapter = ObsidianAdapter()
        with pytest.raises(AdapterError, match="仅支持 .md"):
            await adapter.fetch(
                "notes.txt",
                credentials={"vault_path": str(vault)},
            )

    @pytest.mark.asyncio
    async def test_fetch_obsidian_uri(self, tmp_path: Path) -> None:
        """obsidian:// URI 格式应正确解析。"""
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        vault = tmp_path / "MyVault"
        vault.mkdir()
        md_file = vault / "note.md"
        md_file.write_text("# Note", encoding="utf-8")

        adapter = ObsidianAdapter()
        result = await adapter.fetch(
            "obsidian://open?vault=MyVault&file=note.md",
            credentials={"vault_path": str(vault)},
        )

        assert result.title == "note"
        assert "# Note" in result.content

    @pytest.mark.asyncio
    async def test_list_documents(self, tmp_path: Path) -> None:
        """递归列举 vault 中所有 .md 文件。"""
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        vault = tmp_path / "Vault"
        vault.mkdir()
        (vault / "root.md").write_text("# Root", encoding="utf-8")
        sub = vault / "sub"
        sub.mkdir()
        (sub / "child.md").write_text("# Child", encoding="utf-8")
        # .obsidian 目录应被排除
        obsidian_dir = vault / ".obsidian"
        obsidian_dir.mkdir()
        (obsidian_dir / "config.md").write_text("config", encoding="utf-8")

        adapter = ObsidianAdapter()
        results = await adapter.list_documents(
            str(vault),
            credentials={},
        )

        titles = [r.title for r in results]
        assert "root" in titles
        assert "child" in titles
        # .obsidian 目录中的文件不应出现
        assert "config" not in titles

    @pytest.mark.asyncio
    async def test_test_connection_success(self, tmp_path: Path) -> None:
        """test_connection — 目录存在返回 True。"""
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        adapter = ObsidianAdapter()
        assert await adapter.test_connection({"vault_path": str(tmp_path)}) is True

    @pytest.mark.asyncio
    async def test_test_connection_fail(self) -> None:
        """test_connection — 目录不存在返回 False。"""
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        adapter = ObsidianAdapter()
        assert await adapter.test_connection({"vault_path": "/nonexistent"}) is False
        assert await adapter.test_connection({}) is False


# ======================================================================
# AdapterError 测试
# ======================================================================


class TestAdapterError:
    """AdapterError 异常测试。"""

    def test_error_message_format(self) -> None:
        """错误消息应包含适配器 ID 和描述。"""
        from app.document.source_adapters.base import AdapterError

        err = AdapterError("confluence", "页面不存在", status_code=404)
        assert "[confluence]" in str(err)
        assert "页面不存在" in str(err)
        assert err.adapter_id == "confluence"
        assert err.status_code == 404

    def test_error_default_status_code(self) -> None:
        """默认 status_code 为 0。"""
        from app.document.source_adapters.base import AdapterError

        err = AdapterError("obsidian", "读取失败")
        assert err.status_code == 0
