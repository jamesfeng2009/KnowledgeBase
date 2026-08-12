"""
Confluence 适配器 — 通过 REST API 拉取 Confluence 页面。

支持两种部署模式：
    - Cloud: ``https://{domain}.atlassian.net`` + Basic Auth（email + API token）
    - Server/Data Center: ``https://{host}`` + Personal Access Token (Bearer)

API 版本：
    - Cloud v2: ``/wiki/api/v2/pages/{id}?body-format=storage``
    - v1 兼容: ``/rest/api/content/{id}?expand=body.storage,version``

输出格式：HTML（Confluence storage format），由 WikiHtmlCleaner 后续清洗。
Confluence storage format 使用 ac:/ri: XML 命名空间，清洗器会剥离宏外壳保留正文。

凭证格式（credentials dict）::
    Cloud:     {"username": "user@example.com", "api_token": "ATATT..."}
    Server/DC: {"pat": "NzU4M...", "username": "admin"}  # username 可选
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

# Confluence API 请求超时（秒）
_REQUEST_TIMEOUT: int = 30
# 列举页面分页大小
_LIST_PAGE_SIZE: int = 50


class ConfluenceAdapter(DocumentSourceAdapter):
    """Confluence 文档来源适配器 — REST API 拉取页面 HTML。

    使用方式::

        adapter = ConfluenceAdapter()
        doc = await adapter.fetch(
            "123456789",
            credentials={
                "base_url": "https://example.atlassian.net",
                "username": "user@example.com",
                "api_token": "ATATT...",
            },
        )
    """

    adapter_id = "confluence"
    display_name = "Confluence"
    supported_formats = ("html",)

    async def fetch(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> FetchedDocument:
        """拉取 Confluence 页面 — 返回 storage format HTML。

        Args:
            doc_url_or_id: 页面 ID（如 ``"123456789"``）或页面 URL。
                URL 形式如 ``https://example.atlassian.net/wiki/spaces/DEV/pages/123456789/Page+Title``
                会自动提取 pageId。
            credentials: 凭证，必须包含 ``base_url``，以及
                ``username`` + ``api_token``（Cloud）或 ``pat``（Server/DC）。

        Returns:
            FetchedDocument，format 为 ``"html"``。

        Raises:
            AdapterError: API 调用失败或凭证缺失。
        """
        base_url = credentials.get("base_url", "").rstrip("/")
        if not base_url:
            raise AdapterError(self.adapter_id, "缺少 base_url 配置")

        page_id = self._extract_page_id(doc_url_or_id)
        headers = self._build_auth_headers(credentials)

        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        # 优先使用 v1 API（expand=body.storage 获取完整 storage format HTML）
        # v2 API 的 body-format=storage 在某些 Cloud 实例上行为不一致
        api_url = f"{base_url}/rest/api/content/{page_id}"
        params = {"expand": "body.storage,version,space,history"}

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(api_url, headers=headers, params=params)
                if resp.status_code == 404:
                    raise AdapterError(
                        self.adapter_id,
                        f"页面 {page_id} 不存在或无权访问",
                        status_code=404,
                    )
                if resp.status_code == 401:
                    raise AdapterError(
                        self.adapter_id,
                        "认证失败，请检查用户名/API token 或 PAT",
                        status_code=401,
                    )
                resp.raise_for_status()
                data = resp.json()
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"API 请求失败: {exc}",
            ) from exc

        # 提取页面内容
        body = data.get("body", {}).get("storage", {}).get("value", "")
        title = data.get("title", f"Confluence Page {page_id}")
        space_key = data.get("space", {}).get("key", "")
        version_num = data.get("version", {}).get("number", 0)
        author = data.get("history", {}).get("createdBy", {}).get("displayName", "")
        when = data.get("history", {}).get("createdDate", "")

        source_url = self._build_page_url(base_url, page_id, space_key, title)

        return FetchedDocument(
            source=self.adapter_id,
            title=title,
            content=body,
            format="html",
            source_url=source_url,
            doc_id=str(page_id),
            metadata={
                "space_key": space_key,
                "version": version_num,
                "author": author,
                "created_date": when,
            },
        )

    async def list_documents(
        self,
        space_or_root: str,
        credentials: dict[str, Any],
    ) -> list[SourceDocumentInfo]:
        """列出 Confluence 空间下的页面。

        Args:
            space_or_root: 空间 Key（如 ``"DEV"``）。
            credentials: 凭证。

        Returns:
            页面信息列表。

        Raises:
            AdapterError: API 调用失败。
        """
        base_url = credentials.get("base_url", "").rstrip("/")
        if not base_url:
            raise AdapterError(self.adapter_id, "缺少 base_url 配置")

        space_key = space_or_root
        headers = self._build_auth_headers(credentials)

        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        api_url = f"{base_url}/rest/api/space/{space_key}/content/page"
        results: list[SourceDocumentInfo] = []
        start = 0

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                while True:
                    params = {"limit": _LIST_PAGE_SIZE, "start": start}
                    resp = await client.get(api_url, headers=headers, params=params)
                    if resp.status_code == 404:
                        raise AdapterError(
                            self.adapter_id,
                            f"空间 {space_key} 不存在",
                            status_code=404,
                        )
                    resp.raise_for_status()
                    data = resp.json()

                    for page in data.get("results", []):
                        page_id = str(page.get("id", ""))
                        title = page.get("title", "")
                        results.append(
                            SourceDocumentInfo(
                                doc_id=page_id,
                                title=title,
                                url=self._build_page_url(base_url, page_id, space_key, title),
                                updated_at="",
                                author="",
                            )
                        )

                    # 是否还有更多
                    if not data.get("_links", {}).get("next"):
                        break
                    start += _LIST_PAGE_SIZE

        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"列举空间页面失败: {exc}",
            ) from exc

        log.info(
            "confluence.listed",
            space_key=space_key,
            count=len(results),
        )
        return results

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        """测试 Confluence 连接 — 调用 /rest/api/user/current 验证凭证。"""
        base_url = credentials.get("base_url", "").rstrip("/")
        if not base_url:
            return False

        headers = self._build_auth_headers(credentials)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/rest/api/user/current",
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
        """轻量查询 Confluence 版本指纹 — 仅 expand=version，不拉 body。

        Confluence v1 API ``GET /rest/api/content/{id}?expand=version``
        返回 ``version.number``（int，每次编辑递增）+ ``version.when``（ISO 时间）。
        相比 fetch（expand=body.storage,version）只返回元信息，传输量小。
        """
        base_url = credentials.get("base_url", "").rstrip("/")
        if not base_url:
            raise AdapterError(self.adapter_id, "缺少 base_url 配置")

        page_id = self._extract_page_id(doc_url_or_id)
        headers = self._build_auth_headers(credentials)

        try:
            import httpx
        except ImportError:
            raise AdapterError(self.adapter_id, "httpx 未安装") from None

        api_url = f"{base_url}/rest/api/content/{page_id}"
        params = {"expand": "version"}  # 仅取 version，不取 body（更轻量）

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(api_url, headers=headers, params=params)
                if resp.status_code == 404:
                    raise AdapterError(
                        self.adapter_id,
                        f"页面 {page_id} 不存在或无权访问",
                        status_code=404,
                    )
                resp.raise_for_status()
                data = resp.json()
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"获取版本信息失败: {exc}",
            ) from exc

        version = data.get("version", {})
        number = version.get("number")
        if number is None:
            return None
        when = version.get("when", "")
        return RevisionInfo(fingerprint=str(number), last_modified=when)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_page_id(doc_url_or_id: str) -> str:
        """从 URL 或纯 ID 中提取 Confluence pageId。

        支持格式：
            - 纯数字 ID: ``"123456789"``
            - Cloud URL: ``https://example.atlassian.net/wiki/spaces/DEV/pages/123456789/Title``
            - Server URL: ``https://host/pages/viewpage.action?pageId=123456789``
        """
        import re

        # 纯数字
        if doc_url_or_id.isdigit():
            return doc_url_or_id

        # URL 中的 pages/{id} 路径段
        match = re.search(r"/pages/(\d+)", doc_url_or_id)
        if match:
            return match.group(1)

        # viewpage.action?pageId={id}
        match = re.search(r"pageId=(\d+)", doc_url_or_id)
        if match:
            return match.group(1)

        # 回退：尝试提取任何连续数字
        match = re.search(r"\d{6,}", doc_url_or_id)
        if match:
            return match.group(1)

        raise AdapterError(
            "confluence",
            f"无法从输入提取 pageId: {doc_url_or_id[:100]}",
        )

    @staticmethod
    def _build_auth_headers(credentials: dict[str, Any]) -> dict[str, str]:
        """构建认证请求头。

        Cloud: Basic Auth（username:api_token）
        Server/DC: Bearer PAT
        """
        import base64

        pat = credentials.get("pat", "")
        if pat:
            return {"Authorization": f"Bearer {pat}"}

        username = credentials.get("username", "")
        api_token = credentials.get("api_token", "")
        if username and api_token:
            creds = base64.b64encode(f"{username}:{api_token}".encode()).decode()
            return {"Authorization": f"Basic {creds}"}

        raise AdapterError("confluence", "缺少认证凭证（需要 api_token 或 pat）")

    @staticmethod
    def _build_page_url(
        base_url: str,
        page_id: str,
        space_key: str,
        title: str,
    ) -> str:
        """构建页面可访问 URL。"""
        # 简化 URL，使用 pageId 直接访问
        return f"{base_url}/pages/viewpage.action?pageId={page_id}"
