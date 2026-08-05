"""
MCP Resources 原语测试 — resources/list + resources/read + Resource Metadata。

覆盖：
- protocol.py：方法常量、参数构造、RESOURCE_NOT_FOUND 错误码
- resources.py：ResourceMetadata 序列化、split_usage_boundaries 提取、URI 构造/解析
- server.py：list_resources 元数据完整性、read_resource 内容与错误路径
- streamable_http.py：resources/list + resources/read 路由与错误响应
- client.py：list_resources / read_resource / jsonrpc_resources_*
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.mcp.client import MCPClient
from app.mcp.protocol import (
    INVALID_PARAMS,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    RESOURCE_NOT_FOUND,
    SUPPORTED_MCP_METHODS,
    make_resources_list_params,
    make_resources_read_params,
)
from app.mcp.resources import (
    RESOURCE_URI_SCHEME,
    Resource,
    ResourceMetadata,
    make_resource_uri,
    parse_resource_uri,
    split_usage_boundaries,
)
from app.mcp.server import KnowledgeBaseMCPServer
from app.mcp.streamable_http import StreamableHTTPTransport


# ======================================================================
# protocol.py 测试
# ======================================================================


class TestProtocolResources:
    """协议层资源方法常量与参数构造测试。"""

    def test_resource_methods_supported(self) -> None:
        """resources/list 与 resources/read 注册到支持方法集。"""
        assert METHOD_RESOURCES_LIST in SUPPORTED_MCP_METHODS
        assert METHOD_RESOURCES_READ in SUPPORTED_MCP_METHODS

    def test_make_resources_list_params(self) -> None:
        assert make_resources_list_params() == {}

    def test_make_resources_read_params(self) -> None:
        params = make_resources_read_params("resource://skill/knowledge_search")
        assert params == {"uri": "resource://skill/knowledge_search"}


# ======================================================================
# resources.py 测试
# ======================================================================


class TestResourceMetadata:
    """ResourceMetadata 数据模型测试。"""

    def test_to_dict_fields(self) -> None:
        meta = ResourceMetadata(
            domain="search",
            tags=["全文检索", "知识库"],
            when_to_use="用户想查找文档",
            when_not_to_use="已知文档 ID",
            output_interpretation="results 为文档列表",
            version="1.2.0",
            review_status="reviewed",
        )
        d = meta.to_dict()
        assert d["domain"] == "search"
        assert d["tags"] == ["全文检索", "知识库"]
        assert d["when_to_use"] == "用户想查找文档"
        assert d["when_not_to_use"] == "已知文档 ID"
        assert d["output_interpretation"] == "results 为文档列表"
        assert d["version"] == "1.2.0"
        assert d["review_status"] == "reviewed"

    def test_defaults(self) -> None:
        meta = ResourceMetadata()
        assert meta.domain == "general"
        assert meta.version == "1.0.0"
        assert meta.review_status == "draft"


class TestSplitUsageBoundaries:
    """适用场景/不适用于 提取测试。"""

    def test_extract_both(self) -> None:
        desc = (
            "搜索企业知识库，返回匹配的文档列表。"
            "适用场景：用户想查找、搜索、了解某主题的相关文档。"
            "不适用于：已知具体文档 ID 的查询；查询 OA 审批状态。"
        )
        pos, neg = split_usage_boundaries(desc)
        assert "查找" in pos
        assert "已知具体文档 ID" in neg
        assert "OA 审批" in neg

    def test_extract_none(self) -> None:
        pos, neg = split_usage_boundaries("一个普通的工具描述")
        assert pos == ""
        assert neg == ""

    def test_extract_empty(self) -> None:
        assert split_usage_boundaries("") == ("", "")


class TestResourceURI:
    """资源 URI 构造与解析测试。"""

    def test_make_uri(self) -> None:
        assert make_resource_uri("knowledge_search") == (
            f"{RESOURCE_URI_SCHEME}knowledge_search"
        )

    def test_parse_uri(self) -> None:
        assert parse_resource_uri("resource://skill/document_get") == "document_get"

    def test_parse_uri_invalid(self) -> None:
        assert parse_resource_uri("http://example.com/x") is None
        assert parse_resource_uri("") is None
        assert parse_resource_uri("resource://skill/") is None


class TestResource:
    """Resource 序列化测试。"""

    def test_to_list_dict_excludes_content(self) -> None:
        res = Resource(
            uri=make_resource_uri("t1"),
            name="t1",
            description="desc",
            content='{"big": "content"}',
        )
        d = res.to_list_dict()
        assert d["uri"].endswith("t1")
        assert d["name"] == "t1"
        assert d["mimeType"] == "application/json"
        assert "metadata" in d
        assert "text" not in d  # 列表不含 content

    def test_to_read_dict_includes_content(self) -> None:
        res = Resource(
            uri=make_resource_uri("t1"),
            name="t1",
            description="desc",
            content='{"big": "content"}',
        )
        d = res.to_read_dict()
        assert d["text"] == '{"big": "content"}'


# ======================================================================
# server.py 测试 — 真实 Server 实例（资源方法不触 DB）
# ======================================================================


def _make_server() -> KnowledgeBaseMCPServer:
    """构造真实 Server（db_factory 为 Mock，资源方法不访问数据库）。"""
    return KnowledgeBaseMCPServer(db_factory=MagicMock())


class TestServerResources:
    """Server 资源注册与读取测试。"""

    @pytest.mark.asyncio
    async def test_list_resources_count(self) -> None:
        """每个注册工具对应一个资源。"""
        server = _make_server()
        resources = await server.list_resources()
        tools = await server.list_tools()
        assert len(resources) == len(tools)
        assert len(resources) > 0

    @pytest.mark.asyncio
    async def test_list_resources_metadata_fields(self) -> None:
        """资源元数据包含完整 7 字段。"""
        server = _make_server()
        resources = await server.list_resources()
        res = next(r for r in resources if r["name"] == "knowledge_search")
        assert res["uri"] == make_resource_uri("knowledge_search")
        assert res["mimeType"] == "application/json"
        meta = res["metadata"]
        for field in (
            "domain", "tags", "when_to_use", "when_not_to_use",
            "output_interpretation", "version", "review_status",
        ):
            assert field in meta
        assert meta["domain"] == "search"
        assert "全文检索" in meta["tags"]

    @pytest.mark.asyncio
    async def test_list_resources_boundaries_extracted(self) -> None:
        """未显式传 when_to_use/when_not_to_use 时自动从描述提取。"""
        server = _make_server()
        resources = await server.list_resources()
        res = next(r for r in resources if r["name"] == "knowledge_search")
        # knowledge_search 描述含「适用场景：/ 不适用于：」段落
        assert "查找" in res["metadata"]["when_to_use"]
        assert "document_get" in res["metadata"]["when_not_to_use"]

    @pytest.mark.asyncio
    async def test_read_resource_success(self) -> None:
        """read_resource 返回完整内容与元数据。"""
        server = _make_server()
        resource = await server.read_resource("resource://skill/document_get")
        assert resource is not None
        assert resource["name"] == "document_get"
        assert "text" in resource
        content = json.loads(resource["text"])
        assert content["name"] == "document_get"
        assert "inputSchema" in content
        assert content["metadata"]["domain"] == "document"

    @pytest.mark.asyncio
    async def test_read_resource_not_found(self) -> None:
        server = _make_server()
        assert await server.read_resource("resource://skill/nonexistent") is None

    @pytest.mark.asyncio
    async def test_read_resource_invalid_uri(self) -> None:
        server = _make_server()
        assert await server.read_resource("invalid-uri") is None


# ======================================================================
# streamable_http.py 路由测试
# ======================================================================


class TestStreamableHTTPResources:
    """StreamableHTTP 传输层资源路由测试。"""

    @pytest.mark.asyncio
    async def test_resources_list_route(self) -> None:
        server = _make_server()
        transport = StreamableHTTPTransport(server)
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "resources/list",
            "params": {},
            "id": "1",
        })
        response = await transport.handle_request(body)
        assert not hasattr(response, "__aiter__")
        assert response.error is None
        result = response.result
        assert "resources" in result
        assert len(result["resources"]) > 0
        names = [r["name"] for r in result["resources"]]
        assert "knowledge_search" in names

    @pytest.mark.asyncio
    async def test_resources_read_route(self) -> None:
        server = _make_server()
        transport = StreamableHTTPTransport(server)
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {"uri": "resource://skill/knowledge_search"},
            "id": "2",
        })
        response = await transport.handle_request(body)
        assert response.error is None
        contents = response.result["contents"]
        assert len(contents) == 1
        assert contents[0]["name"] == "knowledge_search"
        assert contents[0]["metadata"]["domain"] == "search"

    @pytest.mark.asyncio
    async def test_resources_read_not_found(self) -> None:
        server = _make_server()
        transport = StreamableHTTPTransport(server)
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {"uri": "resource://skill/ghost"},
            "id": "3",
        })
        response = await transport.handle_request(body)
        assert response.error is not None
        assert response.error.code == RESOURCE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_resources_read_missing_uri(self) -> None:
        server = _make_server()
        transport = StreamableHTTPTransport(server)
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {},
            "id": "4",
        })
        response = await transport.handle_request(body)
        assert response.error is not None
        assert response.error.code == INVALID_PARAMS


# ======================================================================
# client.py 测试
# ======================================================================


class TestClientResources:
    """MCPClient 资源发现与读取测试。"""

    @pytest.mark.asyncio
    async def test_list_resources(self) -> None:
        client = MCPClient(_make_server())
        resources = await client.list_resources()
        assert len(resources) > 0
        assert all("metadata" in r for r in resources)

    @pytest.mark.asyncio
    async def test_read_resource(self) -> None:
        client = MCPClient(_make_server())
        resource = await client.read_resource("resource://skill/create_it_ticket")
        assert resource is not None
        assert resource["name"] == "create_it_ticket"
        assert resource["metadata"]["domain"] == "workflow"

    @pytest.mark.asyncio
    async def test_read_resource_none(self) -> None:
        client = MCPClient(_make_server())
        assert await client.read_resource("resource://skill/ghost") is None

    @pytest.mark.asyncio
    async def test_jsonrpc_resources_list(self) -> None:
        client = MCPClient(_make_server())
        response = await client.jsonrpc_resources_list(request_id="10")
        assert response.error is None
        assert "resources" in response.result
        names = [r["name"] for r in response.result["resources"]]
        assert "document_create" in names

    @pytest.mark.asyncio
    async def test_jsonrpc_resources_read(self) -> None:
        client = MCPClient(_make_server())
        response = await client.jsonrpc_resources_read(
            "resource://skill/knowledge_search", request_id="11",
        )
        assert response.error is None
        contents = response.result["contents"]
        assert contents[0]["name"] == "knowledge_search"

    @pytest.mark.asyncio
    async def test_jsonrpc_resources_read_not_found(self) -> None:
        client = MCPClient(_make_server())
        response = await client.jsonrpc_resources_read(
            "resource://skill/ghost", request_id="12",
        )
        assert response.error is not None
        assert response.error.code == RESOURCE_NOT_FOUND
