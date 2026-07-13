"""
跨系统统一搜索测试 — 测试连接器框架和统一搜索。

不依赖外部 API，使用 Mock 连接器。
"""

import pytest

from app.connectors.base import BaseConnector, ExternalSearchResult
from app.connectors.registry import ConnectorRegistry


class MockConnector(BaseConnector):
    """测试用 Mock 连接器。"""

    connector_id = "mock"
    display_name = "测试连接器"
    is_active = True

    def __init__(self, results: list[ExternalSearchResult] | None = None):
        self._results = results or []

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        return self._results[:top_k]

    async def test_connection(self) -> bool:
        return True


class TestBaseConnector:
    """连接器基类测试。"""

    def test_external_search_result_creation(self):
        """ExternalSearchResult 应能正确创建。"""
        result = ExternalSearchResult(
            source="oa",
            source_label="OA 审批",
            title="测试审批",
            snippet="审批摘要",
            url="https://oa.example.com/123",
            metadata={"author": "张三"},
            score=0.95,
        )
        assert result.source == "oa"
        assert result.title == "测试审批"
        assert result.score == 0.95
        assert result.metadata["author"] == "张三"

    def test_external_search_result_defaults(self):
        """ExternalSearchResult 应有合理默认值。"""
        result = ExternalSearchResult(
            source="erp",
            source_label="ERP",
            title="测试",
            snippet="摘要",
        )
        assert result.url == ""
        assert result.score == 0.0
        assert result.metadata == {}

    def test_base_connector_get_permissions_default(self):
        """BaseConnector.get_permissions 默认返回空列表。"""
        connector = MockConnector()
        import asyncio
        permissions = asyncio.run(connector.get_permissions("user-123"))
        assert permissions == []


class TestConnectorRegistry:
    """连接器注册表测试。"""

    def test_register_and_get(self):
        """注册后应能通过 ID 获取连接器。"""
        registry = ConnectorRegistry()
        connector = MockConnector()
        registry.register(connector)
        assert registry.get("mock") is connector

    def test_unregister(self):
        """注销后应无法获取连接器。"""
        registry = ConnectorRegistry()
        connector = MockConnector()
        registry.register(connector)
        assert registry.unregister("mock") is True
        assert registry.get("mock") is None

    def test_unregister_nonexistent(self):
        """注销不存在的连接器应返回 False。"""
        registry = ConnectorRegistry()
        assert registry.unregister("nonexistent") is False

    def test_get_active(self):
        """get_active 应只返回已启用的连接器。"""
        registry = ConnectorRegistry()
        active = MockConnector()
        active.is_active = True
        inactive = MockConnector()
        inactive.connector_id = "inactive"
        inactive.is_active = False

        registry.register(active)
        registry.register(inactive)

        active_list = registry.get_active()
        assert len(active_list) == 1
        assert active_list[0].connector_id == "mock"

    def test_toggle(self):
        """toggle 应能切换连接器状态。"""
        registry = ConnectorRegistry()
        connector = MockConnector()
        connector.is_active = False
        registry.register(connector)

        assert registry.toggle("mock", True) is True
        assert connector.is_active is True

        assert registry.toggle("mock", False) is True
        assert connector.is_active is False

    def test_toggle_nonexistent(self):
        """toggle 不存在的连接器应返回 False。"""
        registry = ConnectorRegistry()
        assert registry.toggle("nonexistent", True) is False

    def test_list_connectors(self):
        """list_connectors 应返回所有连接器信息。"""
        registry = ConnectorRegistry()
        c1 = MockConnector()
        c1.is_active = True
        c2 = MockConnector()
        c2.connector_id = "mock2"
        c2.is_active = False
        registry.register(c1)
        registry.register(c2)

        connectors = registry.list_connectors()
        assert len(connectors) == 2
        ids = [c["connector_id"] for c in connectors]
        assert "mock" in ids
        assert "mock2" in ids

    @pytest.mark.asyncio
    async def test_mock_connector_search(self):
        """Mock 连接器 search 应返回预设结果。"""
        results = [
            ExternalSearchResult(
                source="mock",
                source_label="测试",
                title="结果1",
                snippet="摘要1",
                score=0.9,
            )
        ]
        connector = MockConnector(results=results)
        search_results = await connector.search("test", top_k=5)
        assert len(search_results) == 1
        assert search_results[0].title == "结果1"


class TestBuiltinConnectors:
    """内置连接器测试（OA/ERP/CRM/Mail）。"""

    def test_oa_connector_disabled_by_default(self):
        """OA 连接器默认应返回空结果（未配置 API）。"""
        from app.connectors.oa import OAConnector
        connector = OAConnector()
        # is_active 取决于环境变量，测试环境通常未配置
        import asyncio
        results = asyncio.run(connector.search("test"))
        assert results == []

    def test_erp_connector_disabled_by_default(self):
        """ERP 连接器默认应返回空结果。"""
        from app.connectors.erp import ERPConnector
        connector = ERPConnector()
        import asyncio
        results = asyncio.run(connector.search("test"))
        assert results == []

    def test_crm_connector_disabled_by_default(self):
        """CRM 连接器默认应返回空结果。"""
        from app.connectors.crm import CRMConnector
        connector = CRMConnector()
        import asyncio
        results = asyncio.run(connector.search("test"))
        assert results == []

    def test_mail_connector_disabled_by_default(self):
        """邮件连接器默认应返回空结果。"""
        from app.connectors.mail import MailConnector
        connector = MailConnector()
        import asyncio
        results = asyncio.run(connector.search("test"))
        assert results == []

    def test_global_registry_has_all_connectors(self):
        """全局注册表应包含所有 4 个内置连接器。"""
        from app.connectors.registry import connector_registry
        all_connectors = connector_registry.list_connectors()
        ids = [c["connector_id"] for c in all_connectors]
        assert "oa" in ids
        assert "erp" in ids
        assert "crm" in ids
        assert "mail" in ids
