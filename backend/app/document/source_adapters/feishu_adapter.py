"""
飞书文档适配器 — 通过飞书 OpenAPI 拉取文档内容并转为 Markdown。

API 流程：
    1. 获取 tenant_access_token（app_id + app_secret → POST /open-apis/auth/v3/tenant_access_token/internal）
    2. 获取文档元信息（GET /open-apis/docx/v1/documents/{document_id}）
    3. 获取文档块（GET /open-apis/docx/v1/documents/{document_id}/blocks，分页）
    4. 块类型 → Markdown 转换

输出格式：Markdown，由 MarkdownParser 后续解析，chunker._split_markdown 分块。

支持文档类型：飞书新版文档（docx），不支持旧版 doc（已废弃）。
Wiki 页面通过 doc_token 拉取，与普通文档共用同一 API。

凭证格式（credentials dict）::
    {"app_id": "cli_xxx", "app_secret": "xxx"}
"""
from __future__ import annotations

from typing import Any

from app.document.source_adapters.base import (
    AdapterError,
    DocumentSourceAdapter,
    FetchedDocument,
    SourceDocumentInfo,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# 飞书 API 基础 URL
_FEISHU_BASE_URL = "https://open.feishu.cn"
# 请求超时（秒）
_REQUEST_TIMEOUT: int = 30
# 块列表分页大小
_BLOCK_PAGE_SIZE: int = 500

# 飞书 docx block_type 枚举 → Markdown 映射
# 参考: https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/list
_BLOCK_TYPE_PAGE = 1
_BLOCK_TYPE_TEXT = 2
_BLOCK_TYPE_HEADING_START = 3  # heading1=3, heading2=4, ... heading9=11
_BLOCK_TYPE_BULLET = 12
_BLOCK_TYPE_ORDERED = 13
_BLOCK_TYPE_CODE = 14
_BLOCK_TYPE_QUOTE = 15
_BLOCK_TYPE_TODO = 17
_BLOCK_TYPE_DIVIDER = 19
_BLOCK_TYPE_IMAGE = 27
_BLOCK_TYPE_TABLE = 31


class FeishuAdapter(DocumentSourceAdapter):
    """飞书文档来源适配器 — OpenAPI blocks → Markdown。

    使用方式::

        adapter = FeishuAdapter()
        doc = await adapter.fetch(
            "doccnXXXXXX",
            credentials={
                "app_id": "cli_xxx",
                "app_secret": "xxx",
            },
        )
    """

    adapter_id = "feishu"
    display_name = "飞书文档"
    supported_formats = ("markdown",)

    async def fetch(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> FetchedDocument:
        """拉取飞书文档 — blocks API → Markdown。

        Args:
            doc_url_or_id: 文档 token（如 ``"doccnXXXXXX"``）或文档 URL。
                URL 形式如 ``https://xxx.feishu.cn/docs/doccnXXXXXX`` 或
                ``https://xxx.feishu.cn/wiki/doccnXXXXXX``，会自动提取 token。
            credentials: 必须包含 ``app_id`` 和 ``app_secret``。

        Returns:
            FetchedDocument，format 为 ``"markdown"``。

        Raises:
            AdapterError: API 调用失败或凭证缺失。
        """
        app_id = credentials.get("app_id", "")
        app_secret = credentials.get("app_secret", "")
        if not app_id or not app_secret:
            raise AdapterError(self.adapter_id, "缺少 app_id 或 app_secret")

        doc_token = self._extract_doc_token(doc_url_or_id)

        # 获取 tenant_access_token
        token = await self._get_tenant_token(app_id, app_secret)

        # 获取文档元信息
        doc_info = await self._get_document_info(doc_token, token)
        title = doc_info.get("title", f"飞书文档 {doc_token}")

        # 获取所有块
        blocks = await self._get_all_blocks(doc_token, token)

        # 块 → Markdown
        content = self._blocks_to_markdown(blocks, title)

        log.info(
            "feishu.fetched",
            doc_token=doc_token,
            title=title,
            blocks=len(blocks),
            chars=len(content),
        )

        return FetchedDocument(
            source=self.adapter_id,
            title=title,
            content=content,
            format="markdown",
            source_url=f"https://feishu.cn/docs/{doc_token}",
            doc_id=doc_token,
            metadata={
                "app_id": app_id,
                "block_count": len(blocks),
            },
        )

    async def list_documents(
        self,
        space_or_root: str,
        credentials: dict[str, Any],
    ) -> list[SourceDocumentInfo]:
        """列出飞书知识空间下的文档。

        飞书 API 需要指定 space_id，通过 drive API 获取文件列表。
        此方法需要额外权限，暂返回空列表（后续按需实现）。

        Args:
            space_or_root: 知识空间 ID。
            credentials: 凭证。
        """
        # 飞书知识空间文件列表 API 需要额外权限和分页处理
        # 暂返回空列表，后续按需实现
        log.info("feishu.list_not_implemented", space_id=space_or_root)
        return []

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        """测试飞书 API 连接 — 获取 tenant_access_token 验证凭证。"""
        app_id = credentials.get("app_id", "")
        app_secret = credentials.get("app_secret", "")
        if not app_id or not app_secret:
            return False
        try:
            token = await self._get_tenant_token(app_id, app_secret)
            return bool(token)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # API 调用
    # ------------------------------------------------------------------

    async def _get_tenant_token(self, app_id: str, app_secret: str) -> str:
        """获取 tenant_access_token。"""
        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{_FEISHU_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code", -1) != 0:
                    raise AdapterError(
                        self.adapter_id,
                        f"获取 token 失败: {data.get('msg', 'unknown')}",
                    )
                return data["tenant_access_token"]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"获取 token 请求失败: {exc}",
            ) from exc

    async def _get_document_info(
        self, doc_token: str, token: str
    ) -> dict[str, Any]:
        """获取文档元信息。"""
        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    f"{_FEISHU_BASE_URL}/open-apis/docx/v1/documents/{doc_token}",
                    headers=headers,
                )
                if resp.status_code == 404:
                    raise AdapterError(
                        self.adapter_id,
                        f"文档 {doc_token} 不存在或无权访问",
                        status_code=404,
                    )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code", -1) != 0:
                    raise AdapterError(
                        self.adapter_id,
                        f"获取文档信息失败: {data.get('msg', 'unknown')}",
                    )
                return data.get("data", {}).get("document", {})
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"获取文档信息失败: {exc}",
            ) from exc

    async def _get_all_blocks(
        self, doc_token: str, token: str
    ) -> list[dict[str, Any]]:
        """获取文档所有块（分页拉取）。"""
        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        headers = {"Authorization": f"Bearer {token}"}
        all_blocks: list[dict[str, Any]] = []
        page_token = ""

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                while True:
                    url = (
                        f"{_FEISHU_BASE_URL}/open-apis/docx/v1/documents/{doc_token}/blocks"
                    )
                    params: dict[str, Any] = {"page_size": _BLOCK_PAGE_SIZE}
                    if page_token:
                        params["page_token"] = page_token

                    resp = await client.get(url, headers=headers, params=params)
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("code", -1) != 0:
                        raise AdapterError(
                            self.adapter_id,
                            f"获取块列表失败: {data.get('msg', 'unknown')}",
                        )

                    items = data.get("data", {}).get("items", [])
                    all_blocks.extend(items)

                    if not data.get("data", {}).get("has_more"):
                        break
                    page_token = data.get("data", {}).get("page_token", "")
                    if not page_token:
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
        """将飞书文档块列表转换为 Markdown。

        Args:
            blocks: 块列表（按文档顺序）。
            title: 文档标题。

        Returns:
            Markdown 字符串。
        """
        # 构建块 ID → 块 的映射（用于父子关系查找）
        block_map: dict[str, dict[str, Any]] = {}
        for block in blocks:
            block_id = block.get("block_id", "")
            if block_id:
                block_map[block_id] = block

        # 找到根块（page 类型）的子块
        root_block_id = ""
        for block in blocks:
            if block.get("block_type") == _BLOCK_TYPE_PAGE:
                root_block_id = block.get("block_id", "")
                break

        # 获取根块的直接子块 ID 列表
        root_children: list[str] = []
        if root_block_id and root_block_id in block_map:
            root_children = block_map[root_block_id].get("children", [])

        # 如果没有找到根块，按顺序处理所有块
        if not root_children:
            root_children = [b.get("block_id", "") for b in blocks if b.get("block_id")]

        # 递归生成 Markdown
        lines: list[str] = [f"# {title}", ""]

        for child_id in root_children:
            if child_id in block_map:
                md = self._block_to_markdown(block_map[child_id], block_map, depth=0)
                if md:
                    lines.append(md)

        result = "\n".join(lines)
        # 压缩连续空行
        import re
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        return result

    def _block_to_markdown(
        self,
        block: dict[str, Any],
        block_map: dict[str, dict[str, Any]],
        depth: int = 0,
    ) -> str:
        """将单个块转换为 Markdown 文本。

        Args:
            block: 块数据。
            block_map: 所有块的映射（用于查找子块）。
            depth: 嵌套深度（用于缩进）。

        Returns:
            Markdown 文本行。
        """
        block_type = block.get("block_type", 0)
        indent = "  " * depth

        # 标题块: heading1=3 ... heading9=11
        if _BLOCK_TYPE_HEADING_START <= block_type <= _BLOCK_TYPE_HEADING_START + 8:
            level = block_type - _BLOCK_TYPE_HEADING_START + 1
            text = self._extract_text(block)
            if text:
                return f"{'#' * min(level, 6)} {text}"

        # 普通文本块
        if block_type == _BLOCK_TYPE_TEXT:
            text = self._extract_text(block)
            if text:
                return f"{indent}{text}"

        # 无序列表
        if block_type == _BLOCK_TYPE_BULLET:
            text = self._extract_text(block)
            if text:
                return f"{indent}- {text}"

        # 有序列表
        if block_type == _BLOCK_TYPE_ORDERED:
            text = self._extract_text(block)
            if text:
                return f"{indent}1. {text}"

        # 代码块
        if block_type == _BLOCK_TYPE_CODE:
            text = self._extract_text(block)
            # 飞书代码块的 style.language 存语言名
            lang = ""
            code_data = block.get("code", {})
            if isinstance(code_data, dict):
                lang = str(code_data.get("style", {}).get("language", ""))
            return f"```{lang}\n{text}\n```"

        # 引用块
        if block_type == _BLOCK_TYPE_QUOTE:
            text = self._extract_text(block)
            if text:
                return f"> {text}"

        # 待办事项
        if block_type == _BLOCK_TYPE_TODO:
            text = self._extract_text(block)
            todo_data = block.get("todo", {})
            done = False
            if isinstance(todo_data, dict):
                done = todo_data.get("style", {}).get("done", False) if isinstance(todo_data.get("style"), dict) else False
            checkbox = "[x]" if done else "[ ]"
            if text:
                return f"- {checkbox} {text}"

        # 分割线
        if block_type == _BLOCK_TYPE_DIVIDER:
            return "---"

        # 图片块
        if block_type == _BLOCK_TYPE_IMAGE:
            image_data = block.get("image", {})
            token = ""
            if isinstance(image_data, dict):
                token = str(image_data.get("token", ""))
            if token:
                return f"![图片]({token})"
            return "[图片]"

        # 表格块 — 递归处理子块（表格单元格也是块）
        if block_type == _BLOCK_TYPE_TABLE:
            return self._table_to_markdown(block, block_map)

        # 页面块 — 跳过（已在顶部处理为标题）
        if block_type == _BLOCK_TYPE_PAGE:
            return ""

        # 未知块类型 — 尝试提取文本
        text = self._extract_text(block)
        if text:
            return f"{indent}{text}"
        return ""

    def _extract_text(self, block: dict[str, Any]) -> str:
        """从块中提取纯文本内容。

        飞书块结构中，不同块类型的文本字段名不同
        （text/heading1/bullet/ordered/code/quote/todo），字段值为
        ``{"elements": [...]}``。elements 每项的真实结构为::

            {"text_run": {"content": "实际文本", "text_element_style": {...}}}

        旧实现误读 ``elem["content"]``（该字段在真实 API 响应中不存在），
        导致飞书文档同步后全文为空。此处优先按真实结构 text_run.content
        解析，并兼容平铺的 elem["content"]（防御性回退）；mention_doc
        元素提取文档标题，避免提及链接丢失语义。
        """
        # 尝试所有可能的文本字段
        for field in (
            "text", "heading1", "heading2", "heading3",
            "heading4", "heading5", "heading6", "heading7",
            "heading8", "heading9", "bullet", "ordered",
            "code", "quote", "todo",
        ):
            text_data = block.get(field)
            if isinstance(text_data, dict):
                elements = text_data.get("elements", [])
                parts: list[str] = []
                for elem in elements:
                    if not isinstance(elem, dict):
                        continue
                    # 真实飞书结构：text_run.content
                    text_run = elem.get("text_run")
                    if isinstance(text_run, dict):
                        content = text_run.get("content", "")
                        if content:
                            parts.append(content)
                        continue
                    # 提及文档：以标题占位，保留语义
                    mention_doc = elem.get("mention_doc")
                    if isinstance(mention_doc, dict):
                        title = mention_doc.get("title", "")
                        if title:
                            parts.append(str(title))
                        continue
                    # 防御性回退：平铺 content 字段（旧 mock / 部分 SDK 结构）
                    content = elem.get("content", "")
                    if content:
                        parts.append(content)
                if parts:
                    return "".join(parts)
        return ""

    def _table_to_markdown(
        self, block: dict[str, Any], block_map: dict[str, dict[str, Any]]
    ) -> str:
        """将表格块转为 Markdown 表格。"""
        table_data = block.get("table", {})
        if not isinstance(table_data, dict):
            return ""

        cells = block.get("children", [])
        if not cells:
            return ""

        # 飞书表格的 children 是按行优先排列的单元格块 ID
        # 每个单元格是一个 block，包含 text 内容
        rows = table_data.get("property", {}).get("row_size", 0) if isinstance(table_data.get("property"), dict) else 0
        cols = table_data.get("property", {}).get("column_size", 0) if isinstance(table_data.get("property"), dict) else 0

        if rows == 0 or cols == 0:
            return ""

        # 提取每个单元格的文本
        cell_texts: list[str] = []
        for cell_id in cells:
            cell_block = block_map.get(cell_id, {})
            cell_text = self._extract_text(cell_block) or ""
            cell_texts.append(cell_text.replace("|", "\\|").replace("\n", " "))

        # 按行列数组装表格
        lines: list[str] = []
        for r in range(rows):
            row_cells = cell_texts[r * cols : (r + 1) * cols]
            # 补齐不足的列
            while len(row_cells) < cols:
                row_cells.append("")
            lines.append("| " + " | ".join(row_cells) + " |")
            if r == 0:
                lines.append("| " + " | ".join(["---"] * cols) + " |")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_doc_token(doc_url_or_id: str) -> str:
        """从 URL 或纯 token 中提取飞书文档 token。

        支持格式：
            - 纯 token: ``"doccnXXXXXX"``
            - 文档 URL: ``https://xxx.feishu.cn/docs/doccnXXXXXX``
            - Wiki URL: ``https://xxx.feishu.cn/wiki/doccnXXXXXX``
            - lark URL: ``https://xxx.larksuite.com/docs/doccnXXXXXX``
        """
        import re

        # 纯 token（字母开头，不含 / :）
        if re.match(r"^[a-zA-Z]{4,}[a-zA-Z0-9]+$", doc_url_or_id) and "/" not in doc_url_or_id:
            return doc_url_or_id

        # URL 中的 /docs/{token} 或 /wiki/{token}
        match = re.search(r"/(?:docs|wiki)/([a-zA-Z0-9]+)", doc_url_or_id)
        if match:
            return match.group(1)

        # 尝试提取任何看起来像 token 的部分
        match = re.search(r"\b([a-zA-Z]{4,}[a-zA-Z0-9]{10,})\b", doc_url_or_id)
        if match:
            return match.group(1)

        raise AdapterError(
            "feishu",
            f"无法从输入提取文档 token: {doc_url_or_id[:100]}",
        )
