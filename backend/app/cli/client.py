"""
EKB CLI 客户端 — 单一职责：封装企业知识库 REST API 的轻量调用。

设计要点：
    - 同步 httpx 客户端，便于脚本/CI 场景直接调用；
    - 所有请求带 Bearer API Key（X-API-Key 兼容）；
    - 异常统一包装为 EkbClientError，附带状态码与响应体摘要；
    - 提供 search / list_kb / create_kb / upload_document / wiki_generate /
      queue_status 六个命令对应能力，供 scripts/ekb_cli.py 组装。
"""

from __future__ import annotations

from typing import Any

import httpx


class EkbClientError(Exception):
    """EKB API 调用失败。"""


class EkbClient:
    """企业知识库 API 客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        self._http = client or httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    # ------------------------------------------------------------------
    # 请求基元
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise EkbClientError(f"请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise EkbClientError(
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0):
            raise EkbClientError(
                f"业务错误 code={data.get('code')}: {data.get('message', '')}"
            )
        return data.get("data", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # 命令能力
    # ------------------------------------------------------------------

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """列出知识库。"""
        data = self._request("GET", "/api/v1/knowledge")
        return data if isinstance(data, list) else []

    def create_knowledge_base(self, name: str, description: str = "") -> dict[str, Any]:
        """创建知识库。"""
        return self._request(
            "POST", "/api/v1/knowledge",
            json={"name": name, "description": description},
        )

    def search(self, query: str, top_k: int = 5, kb_id: str | None = None) -> list[dict[str, Any]]:
        """全局检索。"""
        params: dict[str, Any] = {"query": query, "top_k": top_k}
        if kb_id:
            params["kb_id"] = kb_id
        data = self._request("GET", "/api/v1/search", params=params)
        return data if isinstance(data, list) else []

    def upload_document(
        self,
        kb_id: str,
        file_path: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """上传文档（multipart）。"""
        import pathlib

        path = pathlib.Path(file_path)
        if not path.exists():
            raise EkbClientError(f"文件不存在: {file_path}")
        files = {"file": (path.name, path.read_bytes())}
        data: dict[str, Any] = {"kb_id": kb_id}
        if title:
            data["title"] = title
        resp = self._http.post(
            "/api/v1/documents/upload", files=files, data=data
        )
        if resp.status_code >= 400:
            raise EkbClientError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise EkbClientError(
                f"业务错误 code={payload.get('code')}: {payload.get('message', '')}"
            )
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def wiki_generate(self, kb_id: str) -> dict[str, Any]:
        """生成 Wiki。"""
        return self._request("POST", f"/api/v1/knowledge/{kb_id}/wiki/generate")

    def queue_status(self) -> dict[str, Any]:
        """任务队列面板。"""
        return self._request("GET", "/api/v1/observability/queues")

    def close(self) -> None:
        """关闭底层连接。"""
        self._http.close()

    def __enter__(self) -> "EkbClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
