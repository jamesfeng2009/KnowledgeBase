"""OA 审批流适配器 — MCP 审批/IT 工具的企业系统对接层。

遵循依赖倒置：``server.py`` 的工具 handler 依赖 ``OaAdapter`` 协议而非
具体实现；工厂函数 ``get_oa_adapter`` 按配置返回 ``HttpOaAdapter``
（生产，对接真实 OA 系统）或 ``MockOaAdapter``（开发默认，返回模拟数据）。

返回字段结构与 MCP 工具历史 mock 输出完全一致，下游消费代码零改动。
与 ``app/connectors/oa.py``（搜索场景）是不同 API，仅复用同一组配置项：
``CONNECTOR_OA_ENABLED`` / ``CONNECTOR_OA_API_URL`` / ``CONNECTOR_OA_API_KEY``。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

#: OA API 请求超时（秒）— 审批查询/工单创建均为轻量接口，10s 足够
_REQUEST_TIMEOUT: float = 10.0


class OaAdapterError(Exception):
    """OA 适配器调用异常 — 包装超时/网络/HTTP/解析错误，供工具层结构化返回。"""


@runtime_checkable
class OaAdapter(Protocol):
    """OA 审批流适配器协议 — 覆盖 MCP 审批/IT 工具所需能力。

    所有方法返回 dict，字段结构与工具历史 mock 输出一致：
    - ``get_approval_status`` → bill_no / status / current_node /
      submitter / submit_time / history[]
    - ``create_it_ticket`` → ticket_id / title / description /
      priority / status / created_at
    """

    async def get_approval_status(self, bill_no: str) -> dict[str, Any]:
        """查询审批单状态。"""
        ...

    async def create_it_ticket(
        self,
        title: str,
        description: str,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """创建 IT 服务台工单。"""
        ...


class HttpOaAdapter:
    """HTTP 实现 — 通过 httpx 调用真实 OA 系统 API。

    - 认证：``Authorization: Bearer {api_key}``；
    - 超时 10s，瞬态错误（超时/连接失败/5xx/429）自动重试一次；
    - 响应映射到与 mock 相同的字段结构，保证下游消费代码零改动；
    - 兼容两种响应包装：直接返回字段，或包裹在 ``data`` 键下。

    接口路径约定（对接真实 OA 系统时按实际调整）：
    - ``GET  /approvals/{bill_no}``  查询审批单
    - ``POST /it-tickets``           创建 IT 工单
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: float = _REQUEST_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # 支持注入 http_client（测试/连接池复用）；未注入时惰性创建，
        # 避免工厂构造即占用连接资源
        self._client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建默认 httpx 客户端（base_url + 超时）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """发送请求 — 瞬态错误重试一次，其余异常包装为 OaAdapterError。

        认证头按请求附加（而非绑定客户端），保证注入的 http_client
        同样携带 Bearer 认证。

        Returns:
            响应 JSON（若包含 ``data`` 键则自动解包）。
        """
        client = self._get_client()
        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        if self._api_key:
            headers.setdefault("Authorization", f"Bearer {self._api_key}")
        last_error: Exception | None = None
        # 首次 + 重试一次（共 2 次尝试）
        for attempt in range(2):
            try:
                resp = await client.request(method, path, headers=headers, **kwargs)
                # 5xx/429 视为瞬态错误，重试一次
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = OaAdapterError(f"OA 系统返回错误状态: {resp.status_code}")
                    log.warning(
                        "oa_adapter.server_error",
                        path=path, attempt=attempt, status=resp.status_code,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise OaAdapterError("OA 系统响应格式非法（非 JSON 对象）")
                # 兼容包裹在 data 键下的响应
                inner = data.get("data")
                return inner if isinstance(inner, dict) else data
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning(
                    "oa_adapter.transient_error",
                    path=path, attempt=attempt, error=str(exc),
                )
            except httpx.HTTPStatusError as exc:
                # 4xx 客户端错误不重试，直接包装抛出
                raise OaAdapterError(
                    f"OA 系统请求失败: HTTP {exc.response.status_code}"
                ) from exc
            except (json.JSONDecodeError, ValueError) as exc:
                raise OaAdapterError(f"OA 系统响应解析失败: {exc}") from exc
        raise OaAdapterError(f"OA 系统调用失败（已重试一次）: {last_error}") from last_error

    async def get_approval_status(self, bill_no: str) -> dict[str, Any]:
        """查询审批单状态 — 映射 OA 响应到 mock 相同字段结构。"""
        data = await self._request("GET", f"/approvals/{bill_no}")
        history = [
            {
                "node": item.get("node", ""),
                "operator": item.get("operator", ""),
                "time": item.get("time", ""),
                "action": item.get("action", ""),
            }
            for item in data.get("history", [])
            if isinstance(item, dict)
        ]
        return {
            "bill_no": data.get("bill_no", bill_no),
            "status": data.get("status", "unknown"),
            "current_node": data.get("current_node", ""),
            "submitter": data.get("submitter", ""),
            "submit_time": data.get("submit_time", ""),
            "history": history,
        }

    async def create_it_ticket(
        self,
        title: str,
        description: str,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """创建 IT 工单 — 映射 OA 响应到 mock 相同字段结构。"""
        data = await self._request(
            "POST",
            "/it-tickets",
            json={"title": title, "description": description, "priority": priority},
        )
        return {
            "ticket_id": data.get("ticket_id", ""),
            "title": data.get("title", title),
            "description": data.get("description", description),
            "priority": data.get("priority", priority),
            "status": data.get("status", "open"),
            "created_at": data.get("created_at", ""),
        }


class MockOaAdapter:
    """Mock 实现 — 未配置 OA API 时的开发默认值。

    返回数据与原 ``server.py`` 内联 mock 完全一致，保证行为不变。
    """

    async def get_approval_status(self, bill_no: str) -> dict[str, Any]:
        """返回模拟审批流数据。"""
        return {
            "bill_no": bill_no,
            "status": "processing",
            "current_node": "部门经理审批",
            "submitter": "mock_user",
            "submit_time": "2026-07-06T10:00:00+00:00",
            "history": [
                {
                    "node": "发起申请",
                    "operator": "mock_user",
                    "time": "2026-07-06T09:00:00+00:00",
                    "action": "提交",
                },
                {
                    "node": "部门经理审批",
                    "operator": "mock_manager",
                    "time": "2026-07-06T10:00:00+00:00",
                    "action": "审批中",
                },
            ],
        }

    async def create_it_ticket(
        self,
        title: str,
        description: str,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """返回模拟工单号。"""
        ticket_id = f"IT-{uuid.uuid4().hex[:8].upper()}"
        return {
            "ticket_id": ticket_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "open",
            "created_at": "2026-07-06T10:00:00+00:00",
        }


def get_oa_adapter() -> OaAdapter:
    """工厂函数 — 按配置选择适配器实现。

    ``CONNECTOR_OA_ENABLED=True`` 且配置了 ``CONNECTOR_OA_API_URL`` 时
    返回 HTTP 实现（对接真实 OA 系统）；否则返回 Mock 实现
    （dev 默认，行为与历史 mock 一致）。
    """
    settings = get_settings()
    api_url = getattr(settings, "CONNECTOR_OA_API_URL", "")
    if getattr(settings, "CONNECTOR_OA_ENABLED", False) and api_url:
        log.info("oa_adapter.selected", impl="http", base_url=api_url)
        return HttpOaAdapter(
            base_url=api_url,
            api_key=getattr(settings, "CONNECTOR_OA_API_KEY", ""),
        )
    return MockOaAdapter()
