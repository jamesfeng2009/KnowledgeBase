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


# ======================================================================
# 空壳适配器必须显式报「未对接」，而不是 return [] 假装「没搜到」
# ======================================================================
#
# 背景（P2）：OA/ERP/CRM/邮件 四个连接器的 _call_*_api 过去直接 return []，
# 于是「没启用连接器」和「启用了但适配器是空壳」返回同一个空列表，
# 运维看到空结果无法判断是配置问题还是真的没数据。


class TestConnectorNotImplemented:
    """已启用但未对接真实 API → 记 error 日志，返回空列表（不炸调用方）。"""

    @pytest.mark.parametrize(
        ("module_name", "class_name", "call_attr"),
        [
            ("app.connectors.oa", "OAConnector", "_call_oa_api"),
            ("app.connectors.erp", "ERPConnector", "_call_erp_api"),
            ("app.connectors.crm", "CRMConnector", "_call_crm_api"),
            ("app.connectors.mail", "MailConnector", "_call_mail_api"),
        ],
    )
    def test_adapter_raises_not_implemented(
        self, module_name: str, class_name: str, call_attr: str
    ) -> None:
        """底层 _call_*_api 必须抛 ConnectorNotImplementedError。"""
        import asyncio
        import importlib

        from app.connectors.base import ConnectorNotImplementedError

        mod = importlib.import_module(module_name)
        connector = getattr(mod, class_name)()
        with pytest.raises(ConnectorNotImplementedError):
            asyncio.run(getattr(connector, call_attr)("k", 1))

    def test_search_logs_api_not_implemented_on_enabled_connector(self) -> None:
        """启用连接器（配了 api_url）后搜索 → 空列表 + 专用 error 事件。

        关键断言是日志事件名不同于普通的 search_failed：
        ``connector.oa.api_not_implemented`` 一眼能看出是配置/对接问题。
        """
        import asyncio
        from unittest.mock import patch

        from app.connectors.oa import OAConnector

        connector = OAConnector()
        connector.is_active = True
        connector.api_url = "https://oa.example.com"

        with patch("app.connectors.oa.logger") as mock_logger:
            results = asyncio.run(connector.search("报销"))

        assert results == []
        mock_logger.error.assert_called_once()
        assert "connector.oa.api_not_implemented" in mock_logger.error.call_args[0][0]
        # 不应落到通用失败分支
        mock_logger.warning.assert_not_called()

    def test_disabled_connector_returns_empty_without_alarm(self) -> None:
        """未启用（默认）→ 直接空列表，不产生任何错误日志。

        「没配」是正确的降级，不该和其它错误混在一起告警。
        """
        import asyncio
        from unittest.mock import patch

        from app.connectors.erp import ERPConnector

        connector = ERPConnector()
        connector.is_active = False
        connector.api_url = ""

        with patch("app.connectors.erp.logger") as mock_logger:
            results = asyncio.run(connector.search("库存"))

        assert results == []
        mock_logger.error.assert_not_called()
        mock_logger.warning.assert_not_called()

    def test_enabled_connector_test_connection_fails(self) -> None:
        """启用但未对接 → 连接测试返回 False（不要假装连上了）。"""
        import asyncio

        from app.connectors.crm import CRMConnector

        connector = CRMConnector()
        connector.is_active = True
        connector.api_url = "https://crm.example.com"
        assert asyncio.run(connector.test_connection()) is False
