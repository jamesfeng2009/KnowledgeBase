"""
Notion 适配器 — 通过 Notion API 拉取页面内容并转为 Markdown。

API 流程：
    1. 获取页面元信息（GET /v1/pages/{page_id} → properties.title）
    2. 获取页面块（GET /v1/blocks/{block_id}/children?page_size=100，分页）
    3. 块类型 → Markdown 转换

输出格式：Markdown，由 MarkdownParser 后续解析，chunker._split_markdown 分块。

Notion 版本约束：API 版本通过 Notion-Version header 指定（默认 2022-06-28）。

凭证格式（credentials dict）::
    {"integration_token": "secret_xxx"}

    或 OAuth 模式::
    {"access_token": "oauth_xxx"}
"""
from __future__ import annotations

from typing import Any

from app.document.source_adapters.base import (
    AdapterError,
    DocumentSourceAdapter,
    FetchedDocument,
    RevisionInfo,
    SourceDocumentInfo,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# Notion API 基础 URL
_NOTION_BASE_URL = "https://api.notion.com/v1"
# Notion API 版本
_NOTION_API_VERSION = "2022-06-28"
# 请求超时（秒）
_REQUEST_TIMEOUT: int = 30
# 块列表分页大小
_BLOCK_PAGE_SIZE: int = 100


class NotionAdapter(DocumentSourceAdapter):
    """Notion 文档来源适配器 — blocks API → Markdown。

    使用方式::

        adapter = NotionAdapter()
        doc = await adapter.fetch(
            "page_id_here",
            credentials={"integration_token": "secret_xxx"},
        )
    """

    adapter_id = "notion"
    display_name = "Notion"
    supported_formats = ("markdown",)

    async def fetch(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> FetchedDocument:
        """拉取 Notion 页面 — blocks API → Markdown。

        Args:
            doc_url_or_id: 页面 ID（32 位十六进制字符串）或页面 URL。
                URL 形如 ``https://www.notion.so/Workspace/Page-Title-pageid``，
                会自动提取末尾的 page ID。
            credentials: 必须包含 ``integration_token`` 或 ``access_token``。

        Returns:
            FetchedDocument，format 为 ``"markdown"``。

        Raises:
            AdapterError: API 调用失败或凭证缺失。
        """
        token = credentials.get("integration_token") or credentials.get("access_token", "")
        if not token:
            raise AdapterError(self.adapter_id, "缺少 integration_token 或 access_token")

        page_id = self._extract_page_id(doc_url_or_id)
        headers = self._build_headers(token)

        # 获取页面元信息（标题）
        page_info = await self._get_page_info(page_id, headers)
        title = self._extract_page_title(page_info)

        # 获取所有块
        blocks = await self._get_all_blocks(page_id, headers)

        # 块 → Markdown
        content = self._blocks_to_markdown(blocks, title)

        log.info(
            "notion.fetched",
            page_id=page_id,
            title=title,
            blocks=len(blocks),
            chars=len(content),
        )

        return FetchedDocument(
            source=self.adapter_id,
            title=title,
            content=content,
            format="markdown",
            source_url=f"https://www.notion.so/{page_id.replace('-', '')}",
            doc_id=page_id,
            metadata={
                "block_count": len(blocks),
                "page_id": page_id,
            },
        )

    async def list_documents(
        self,
        space_or_root: str,
        credentials: dict[str, Any],
    ) -> list[SourceDocumentInfo]:
        """列出 Notion 数据库中的页面。

        通过 Notion database query API 获取数据库中的页面列表。

        Args:
            space_or_root: Notion 数据库 ID。
            credentials: 凭证。

        Returns:
            页面信息列表。

        Raises:
            AdapterError: API 调用失败。
        """
        token = credentials.get("integration_token") or credentials.get("access_token", "")
        if not token:
            raise AdapterError(self.adapter_id, "缺少 integration_token")

        database_id = space_or_root
        headers = self._build_headers(token)

        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        results: list[SourceDocumentInfo] = []
        start_cursor: str | None = None

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                while True:
                    body: dict[str, Any] = {"page_size": _BLOCK_PAGE_SIZE}
                    if start_cursor:
                        body["start_cursor"] = start_cursor

                    resp = await client.post(
                        f"{_NOTION_BASE_URL}/databases/{database_id}/query",
                        headers=headers,
                        json=body,
                    )
                    if resp.status_code == 404:
                        raise AdapterError(
                            self.adapter_id,
                            f"数据库 {database_id} 不存在或无权访问",
                            status_code=404,
                        )
                    resp.raise_for_status()
                    data = resp.json()

                    for page in data.get("results", []):
                        page_title = self._extract_page_title(page)
                        page_id = page.get("id", "")
                        last_edited = page.get("last_edited_time", "")
                        results.append(
                            SourceDocumentInfo(
                                doc_id=page_id,
                                title=page_title,
                                url=page.get("url", ""),
                                updated_at=last_edited,
                                author="",
                            )
                        )

                    if not data.get("has_more"):
                        break
                    start_cursor = data.get("next_cursor")
                    if not start_cursor:
                        break

        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"列举数据库页面失败: {exc}",
            ) from exc

        log.info("notion.listed", database_id=database_id, count=len(results))
        return results

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        """测试 Notion API 连接 — 调用 /v1/users/me 验证 token。"""
        token = credentials.get("integration_token") or credentials.get("access_token", "")
        if not token:
            return False
        headers = self._build_headers(token)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_NOTION_BASE_URL}/users/me",
                    headers=headers,
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def get_revision(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> RevisionInfo | None:
        """轻量查询 Notion 版本指纹 — 复用 _get_page_info 拿 last_edited_time。

        Notion API ``GET /v1/pages/{id}`` 返回 ``last_edited_time``
        （ISO 8601 字符串，每次编辑更新）。
        """
        token = credentials.get("integration_token") or credentials.get("access_token", "")
        if not token:
            raise AdapterError(self.adapter_id, "缺少 integration_token 或 access_token")

        page_id = self._extract_page_id(doc_url_or_id)
        headers = self._build_headers(token)
        page_info = await self._get_page_info(page_id, headers)

        last_edited = page_info.get("last_edited_time", "")
        if not last_edited:
            return None
        return RevisionInfo(fingerprint=last_edited, last_modified=last_edited)

    # ------------------------------------------------------------------
    # API 调用
    # ------------------------------------------------------------------

    async def _get_page_info(
        self, page_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        """获取页面元信息。"""
        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    f"{_NOTION_BASE_URL}/pages/{page_id}",
                    headers=headers,
                )
                if resp.status_code == 404:
                    raise AdapterError(
                        self.adapter_id,
                        f"页面 {page_id} 不存在或无权访问",
                        status_code=404,
                    )
                if resp.status_code == 401:
                    raise AdapterError(
                        self.adapter_id,
                        "认证失败，请检查 integration_token",
                        status_code=401,
                    )
                resp.raise_for_status()
                return resp.json()
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"获取页面信息失败: {exc}",
            ) from exc

    async def _get_all_blocks(
        self, block_id: str, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """获取块的所有子块（分页拉取）。"""
        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        all_blocks: list[dict[str, Any]] = []
        start_cursor: str | None = None

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                while True:
                    url = f"{_NOTION_BASE_URL}/blocks/{block_id}/children"
                    params: dict[str, str] = {"page_size": str(_BLOCK_PAGE_SIZE)}
                    if start_cursor:
                        params["start_cursor"] = start_cursor

                    resp = await client.get(url, headers=headers, params=params)
                    if resp.status_code == 404:
                        raise AdapterError(
                            self.adapter_id,
                            f"块 {block_id} 不存在或无权访问",
                            status_code=404,
                        )
                    resp.raise_for_status()
                    data = resp.json()

                    all_blocks.extend(data.get("results", []))

                    if not data.get("has_more"):
                        break
                    start_cursor = data.get("next_cursor")
                    if not start_cursor:
                        break

        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"获取块列表失败: {exc}",
            ) from exc

        return all_blocks

    # ------------------------------------------------------------------
    # 块 → Markdown 转换
    # ------------------------------------------------------------------

    def _blocks_to_markdown(
        self, blocks: list[dict[str, Any]], title: str
    ) -> str:
        """将 Notion 块列表转换为 Markdown。

        Args:
            blocks: 块列表（按文档顺序）。
            title: 页面标题。

        Returns:
            Markdown 字符串。
        """
        import re

        lines: list[str] = [f"# {title}", ""]

        for block in blocks:
            md = self._block_to_markdown(block)
            if md:
                lines.append(md)

        result = "\n".join(lines)
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        return result

    def _block_to_markdown(self, block: dict[str, Any]) -> str:
        """将单个 Notion 块转换为 Markdown 文本。

        Args:
            block: Notion 块数据。

        Returns:
            Markdown 文本行。
        """
        block_type = block.get("type", "")
        data = block.get(block_type, {})

        # 标题块
        if block_type in ("heading_1", "heading_2", "heading_3"):
            level = int(block_type.split("_")[1])
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return f"{'#' * level} {text}"

        # 普通段落
        if block_type == "paragraph":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return text
            return ""

        # 无序列表
        if block_type == "bulleted_list_item":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return f"- {text}"

        # 有序列表
        if block_type == "numbered_list_item":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return f"1. {text}"

        # 代码块
        if block_type == "code":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            lang = data.get("language", "")
            return f"```{lang}\n{text}\n```"

        # 引用块
        if block_type == "quote":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return f"> {text}"

        # 待办事项
        if block_type == "to_do":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            checked = data.get("checked", False)
            checkbox = "[x]" if checked else "[ ]"
            if text:
                return f"- {checkbox} {text}"

        # 分割线
        if block_type == "divider":
            return "---"

        # Callout（提示框）
        if block_type == "callout":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            icon = data.get("icon", {})
            emoji = icon.get("emoji", "") if isinstance(icon, dict) else ""
            if text:
                prefix = f"{emoji} " if emoji else ""
                return f"> {prefix}{text}"

        # 图片块
        if block_type == "image":
            image_type = data.get("type", "")
            if image_type == "file":
                url = data.get("file", {}).get("url", "")
            elif image_type == "external":
                url = data.get("external", {}).get("url", "")
            else:
                url = ""
            caption = self._rich_text_to_plain(data.get("caption", []))
            if url:
                return f"![{caption}]({url})"
            return f"[图片: {caption}]" if caption else "[图片]"

        # 表格块 — 递归获取 table_row 子块
        if block_type == "table":
            has_children = block.get("has_children", False)
            if has_children:
                # 表格子块需要单独 API 调用，这里用占位标记
                # 完整实现在异步流程中通过 _get_all_blocks 获取子块
                return "[表格内容需异步加载]"
            return ""

        # Toggle（折叠块）
        if block_type == "toggle":
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return f"**{text}**"

        # Embed
        if block_type == "embed":
            url = data.get("url", "")
            if url:
                return f"[嵌入内容]({url})"

        # Bookmark
        if block_type == "bookmark":
            url = data.get("url", "")
            caption = self._rich_text_to_plain(data.get("caption", []))
            if url:
                return f"[{caption or url}]({url})"

        # 未知块类型 — 尝试提取 rich_text
        if isinstance(data, dict) and "rich_text" in data:
            text = self._rich_text_to_plain(data.get("rich_text", []))
            if text:
                return text

        return ""

    @staticmethod
    def _rich_text_to_plain(rich_text: list[dict[str, Any]]) -> str:
        """将 Notion rich_text 数组转为纯文本。

        Notion 的 rich_text 是一个数组，每个元素有 plain_text 字段。
        某些元素类型（如 mention、equation）也有 plain_text。
        """
        if not rich_text:
            return ""
        parts: list[str] = []
        for item in rich_text:
            if isinstance(item, dict):
                plain = item.get("plain_text", "")
                if plain:
                    parts.append(plain)
        return "".join(parts)

    @staticmethod
    def _extract_page_title(page: dict[str, Any]) -> str:
        """从页面数据中提取标题。

        Notion 页面标题存在 properties.title.title 数组中（rich_text 格式）。
        """
        properties = page.get("properties", {})
        # 尝试常见的标题属性名
        for title_key in ("title", "Name", "名称", "名前"):
            title_prop = properties.get(title_key)
            if isinstance(title_prop, dict):
                title_data = title_prop.get("title", [])
                if title_data:
                    parts: list[str] = []
                    for item in title_data:
                        if isinstance(item, dict):
                            parts.append(item.get("plain_text", ""))
                    title = "".join(parts).strip()
                    if title:
                        return title

        # 回退：从页面 URL 推断
        url = page.get("url", "")
        if url:
            # https://www.notion.so/Workspace/Page-Title-pageid → Page Title
            import re
            slug = url.rstrip("/").split("/")[-1]
            # 去除末尾的 page ID（32 位十六进制）
            slug = re.sub(r"-[a-f0-9]{32}$", "", slug)
            slug = slug.replace("-", " ").strip()
            if slug:
                return slug

        return "Untitled"

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_page_id(doc_url_or_id: str) -> str:
        """从 URL 或纯 ID 中提取 Notion page ID。

        支持格式：
            - 纯 ID: ``"1234567890abcdef1234567890abcdef"``
            - 带连字符: ``"12345678-90ab-cdef-1234-567890abcdef"``
            - URL: ``https://www.notion.so/Workspace/Page-Title-1234567890abcdef1234567890abcdef``
        """
        import re

        # 纯 ID（32 位十六进制）
        stripped = doc_url_or_id.strip().replace("-", "")
        if re.match(r"^[a-f0-9]{32}$", stripped):
            return stripped

        # URL 末尾提取 32 位十六进制 ID
        match = re.search(r"([a-f0-9]{32})$", doc_url_or_id, re.IGNORECASE)
        if match:
            return match.group(1).lower()

        # 带 p= 参数的 URL
        match = re.search(r"[?&]p=([a-f0-9]+)", doc_url_or_id, re.IGNORECASE)
        if match:
            return match.group(1).lower()

        raise AdapterError(
            "notion",
            f"无法从输入提取 page ID: {doc_url_or_id[:100]}",
        )

    @staticmethod
    def _build_headers(token: str) -> dict[str, str]:
        """构建 Notion API 请求头。"""
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
