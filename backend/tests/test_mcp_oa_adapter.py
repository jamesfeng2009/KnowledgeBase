"""MCP OA 审批流适配器测试 — HttpOaAdapter / MockOaAdapter / 工厂 / 工具 handler。

覆盖：
- HttpOaAdapter：字段映射（mock httpx 响应）、data 键解包、
  瞬态错误重试一次、超时/HTTP 错误包装为 OaAdapterError
- MockOaAdapter：返回数据与原 server.py 内联 mock 完全一致
- get_oa_adapter 工厂：enabled + url → HTTP 实现；否则 Mock 实现
- server.py 工具 handler：输出结构与 mock 时代一致；适配器异常时
  经 call_tool 包装为 {"error", "tool"} 结构化 JSON
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.mcp.oa_adapter import (
    HttpOaAdapter,
    MockOaAdapter,
    OaAdapter,
    OaAdapterError,
    get_oa_adapter,
)
from app.mcp.server import KnowledgeBaseMCPServer


# ======================================================================
# 测试辅助 — mock httpx 传输层
# ======================================================================


def _make_http_adapter(
    handler: Any,
    *,
    api_key: str = "test-key",
) -> HttpOaAdapter:
    """构造注入 MockTransport 客户端的 HttpOaAdapter。"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://oa.example.com",
    )
    return HttpOaAdapter(
        base_url="https://oa.example.com",
        api_key=api_key,
        http_client=client,
    )


def _make_server() -> KnowledgeBaseMCPServer:
    """构造真实 Server（db_factory 为 Mock，审批/IT 工具不访问数据库）。"""
    return KnowledgeBaseMCPServer(db_factory=MagicMock())


#: 原 server.py 内联 mock 的审批数据（bill_no 由入参决定）
def _expected_mock_approval(bill_no: str) -> dict[str, Any]:
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


# ======================================================================
# MockOaAdapter 测试
# ======================================================================


class TestMockOaAdapter:
    """Mock 实现返回结构与历史 mock 数据一致。"""

    @pytest.mark.asyncio
    async def test_get_approval_status_matches_legacy_mock(self) -> None:
        adapter = MockOaAdapter()
        result = await adapter.get_approval_status("BG2024001")
        assert result == _expected_mock_approval("BG2024001")

    @pytest.mark.asyncio
    async def test_create_it_ticket_structure(self) -> None:
        adapter = MockOaAdapter()
        result = await adapter.create_it_ticket("电脑故障", "无法开机", "high")
        assert result["ticket_id"].startswith("IT-")
        assert len(result["ticket_id"]) == len("IT-") + 8
        assert result["title"] == "电脑故障"
        assert result["description"] == "无法开机"
        assert result["priority"] == "high"
        assert result["status"] == "open"
        assert result["created_at"] == "2026-07-06T10:00:00+00:00"

    def test_protocol_compliance(self) -> None:
        """Mock 实现满足 OaAdapter 协议。"""
        assert isinstance(MockOaAdapter(), OaAdapter)


# ======================================================================
# HttpOaAdapter 测试
# ======================================================================


class TestHttpOaAdapterMapping:
    """HTTP 实现字段映射测试（mock httpx 响应）。"""

    @pytest.mark.asyncio
    async def test_get_approval_status_field_mapping(self) -> None:
        """OA 响应映射为与 mock 相同的字段结构。"""
        payload = {
            "bill_no": "BG2024001",
            "status": "approved",
            "current_node": "财务审批",
            "submitter": "zhangsan",
            "submit_time": "2026-08-01T09:00:00+00:00",
            "history": [
                {
                    "node": "发起申请",
                    "operator": "zhangsan",
                    "time": "2026-08-01T08:00:00+00:00",
                    "action": "提交",
                }
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/approvals/BG2024001"
            assert request.method == "GET"
            return httpx.Response(200, json=payload)

        adapter = _make_http_adapter(handler)
        result = await adapter.get_approval_status("BG2024001")
        assert result == payload
        # 字段集合与 mock 结构完全一致（下游零改动）
        assert set(result) == {
            "bill_no", "status", "current_node",
            "submitter", "submit_time", "history",
        }

    @pytest.mark.asyncio
    async def test_get_approval_status_unwraps_data_key(self) -> None:
        """兼容包裹在 data 键下的 OA 响应。"""
        payload = {"code": 0, "data": {"bill_no": "QJ2024002", "status": "processing"}}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        adapter = _make_http_adapter(handler)
        result = await adapter.get_approval_status("QJ2024002")
        assert result["bill_no"] == "QJ2024002"
        assert result["status"] == "processing"
        # 缺失字段兜底为默认值，保证结构完整
        assert result["current_node"] == ""
        assert result["history"] == []

    @pytest.mark.asyncio
    async def test_create_it_ticket_field_mapping(self) -> None:
        """工单创建响应映射 + 请求体透传。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={
                    "ticket_id": "IT-REAL001",
                    "status": "open",
                    "created_at": "2026-08-07T10:00:00+00:00",
                },
            )

        adapter = _make_http_adapter(handler)
        result = await adapter.create_it_ticket("网络故障", "办公室断网", "urgent")
        # 请求体透传
        assert captured["body"] == {
            "title": "网络故障", "description": "办公室断网", "priority": "urgent",
        }
        # Bearer 认证头
        assert captured["auth"] == "Bearer test-key"
        # 响应字段映射 + 入参兜底
        assert result == {
            "ticket_id": "IT-REAL001",
            "title": "网络故障",
            "description": "办公室断网",
            "priority": "urgent",
            "status": "open",
            "created_at": "2026-08-07T10:00:00+00:00",
        }


class TestHttpOaAdapterErrorHandling:
    """HTTP 实现超时/异常包装与重试测试。"""

    @pytest.mark.asyncio
    async def test_timeout_wrapped_after_one_retry(self) -> None:
        """连续超时 — 重试一次后包装为 OaAdapterError。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            raise httpx.ReadTimeout("read timeout")

        adapter = _make_http_adapter(handler)
        with pytest.raises(OaAdapterError, match="已重试一次"):
            await adapter.get_approval_status("BG2024001")
        # 首次 + 重试一次，共 2 次尝试
        assert calls["count"] == 2

    @pytest.mark.asyncio
    async def test_retry_once_then_success(self) -> None:
        """首次连接失败，重试一次后成功。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"bill_no": "BG2024001", "status": "processing"})

        adapter = _make_http_adapter(handler)
        result = await adapter.get_approval_status("BG2024001")
        assert result["status"] == "processing"
        assert calls["count"] == 2

    @pytest.mark.asyncio
    async def test_server_error_retried_once(self) -> None:
        """5xx 视为瞬态错误，重试一次后仍失败则包装抛出。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(503, json={"error": "unavailable"})

        adapter = _make_http_adapter(handler)
        with pytest.raises(OaAdapterError, match="503"):
            await adapter.get_approval_status("BG2024001")
        assert calls["count"] == 2

    @pytest.mark.asyncio
    async def test_client_error_not_retried(self) -> None:
        """4xx 客户端错误不重试，直接包装为 OaAdapterError。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(404, json={"error": "not found"})

        adapter = _make_http_adapter(handler)
        with pytest.raises(OaAdapterError, match="HTTP 404"):
            await adapter.get_approval_status("BG2024001")
        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_invalid_json_wrapped(self) -> None:
        """响应非 JSON — 包装为 OaAdapterError。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        adapter = _make_http_adapter(handler)
        with pytest.raises(OaAdapterError, match="解析失败"):
            await adapter.get_approval_status("BG2024001")


# ======================================================================
# 工厂函数测试
# ======================================================================


class TestGetOaAdapter:
    """工厂开关测试。"""

    def _fake_settings(
        self, *, enabled: bool, api_url: str = "", api_key: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            CONNECTOR_OA_ENABLED=enabled,
            CONNECTOR_OA_API_URL=api_url,
            CONNECTOR_OA_API_KEY=api_key,
        )

    def test_enabled_with_url_returns_http(self) -> None:
        """enabled + API_URL → HTTP 实现。"""
        fake = self._fake_settings(
            enabled=True, api_url="https://oa.example.com", api_key="k",
        )
        with patch("app.mcp.oa_adapter.get_settings", return_value=fake):
            adapter = get_oa_adapter()
        assert isinstance(adapter, HttpOaAdapter)

    def test_disabled_returns_mock(self) -> None:
        """未启用 → Mock 实现（dev 默认）。"""
        fake = self._fake_settings(enabled=False, api_url="https://oa.example.com")
        with patch("app.mcp.oa_adapter.get_settings", return_value=fake):
            adapter = get_oa_adapter()
        assert isinstance(adapter, MockOaAdapter)

    def test_enabled_without_url_returns_mock(self) -> None:
        """启用但未配置 API_URL → 回退 Mock 实现。"""
        fake = self._fake_settings(enabled=True, api_url="")
        with patch("app.mcp.oa_adapter.get_settings", return_value=fake):
            adapter = get_oa_adapter()
        assert isinstance(adapter, MockOaAdapter)

    def test_default_settings_returns_mock(self) -> None:
        """测试环境默认配置（CONNECTOR_OA_ENABLED=False）→ Mock 实现。"""
        assert isinstance(get_oa_adapter(), MockOaAdapter)


# ======================================================================
# server.py 工具 handler 集成测试
# ======================================================================


class TestServerOaTools:
    """工具 handler 输出结构与 mock 时代一致。"""

    @pytest.mark.asyncio
    async def test_query_oa_approval_output_unchanged(self) -> None:
        """默认（Mock 适配器）输出与原内联 mock 完全一致。"""
        server = _make_server()
        result_str = await server.call_tool("query_oa_approval", {"bill_no": "BG2024001"})
        result = json.loads(result_str)
        assert result == _expected_mock_approval("BG2024001")

    @pytest.mark.asyncio
    async def test_create_it_ticket_output_unchanged(self) -> None:
        """默认（Mock 适配器）输出结构与原内联 mock 一致。"""
        server = _make_server()
        result_str = await server.call_tool(
            "create_it_ticket",
            {"title": "电脑故障", "description": "无法开机", "priority": "high"},
        )
        result = json.loads(result_str)
        assert result["ticket_id"].startswith("IT-")
        assert result["title"] == "电脑故障"
        assert result["description"] == "无法开机"
        assert result["priority"] == "high"
        assert result["status"] == "open"
        assert result["created_at"] == "2026-07-06T10:00:00+00:00"
        assert set(result) == {
            "ticket_id", "title", "description", "priority", "status", "created_at",
        }

    @pytest.mark.asyncio
    async def test_create_it_ticket_default_priority(self) -> None:
        """priority 缺省为 normal。"""
        server = _make_server()
        result_str = await server.call_tool(
            "create_it_ticket", {"title": "t", "description": "d"},
        )
        assert json.loads(result_str)["priority"] == "normal"

    @pytest.mark.asyncio
    async def test_handler_uses_http_adapter_when_injected(self) -> None:
        """注入 HTTP 适配器后，工具返回真实 OA 数据（字段结构不变）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "bill_no": "BG2024001",
                    "status": "approved",
                    "current_node": "已完成",
                    "submitter": "lisi",
                    "submit_time": "2026-08-01T09:00:00+00:00",
                    "history": [],
                },
            )

        server = _make_server()
        server._oa_adapter = _make_http_adapter(handler)
        result_str = await server.call_tool("query_oa_approval", {"bill_no": "BG2024001"})
        result = json.loads(result_str)
        assert result["status"] == "approved"
        assert result["current_node"] == "已完成"
        assert set(result) == {
            "bill_no", "status", "current_node",
            "submitter", "submit_time", "history",
        }

    @pytest.mark.asyncio
    async def test_adapter_error_returns_structured_error(self) -> None:
        """适配器异常经 call_tool 包装为 {"error", "tool"} JSON。"""

        class _FailingAdapter:
            async def get_approval_status(self, bill_no: str) -> dict[str, Any]:
                raise OaAdapterError("OA 系统调用失败（已重试一次）: boom")

        server = _make_server()
        server._oa_adapter = _FailingAdapter()
        result_str = await server.call_tool("query_oa_approval", {"bill_no": "BG2024001"})
        result = json.loads(result_str)
        assert "error" in result
        assert result["tool"] == "query_oa_approval"
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_adapter_lazy_created_and_cached(self) -> None:
        """适配器惰性创建并缓存到实例（避免重复占用 HTTP 连接）。"""
        server = _make_server()
        assert server._oa_adapter is None
        await server.call_tool("query_oa_approval", {"bill_no": "BG2024001"})
        first = server._oa_adapter
        assert isinstance(first, MockOaAdapter)
        await server.call_tool("query_oa_approval", {"bill_no": "BG2024002"})
        assert server._oa_adapter is first
