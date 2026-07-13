"""
连接器注册表 — 管理连接器的注册、发现和启停状态。

支持动态注册自定义连接器，遵循开闭原则。
"""

from __future__ import annotations

from app.connectors.base import BaseConnector
from app.connectors.crm import CRMConnector
from app.connectors.erp import ERPConnector
from app.connectors.mail import MailConnector
from app.connectors.oa import OAConnector
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ConnectorRegistry:
    """连接器注册表 — 管理 CRUD 和启停状态。

    使用方式：
        registry = connector_registry  # 全局单例
        active = registry.get_active()  # 获取已启用连接器
        registry.register(MyConnector())  # 注册自定义连接器
    """

    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}

    def register(self, connector: BaseConnector) -> None:
        """注册连接器。

        Args:
            connector: 连接器实例。
        """
        self._connectors[connector.connector_id] = connector
        logger.info(
            "connector.registered",
            connector_id=connector.connector_id,
            display_name=connector.display_name,
        )

    def unregister(self, connector_id: str) -> bool:
        """注销连接器。

        Args:
            connector_id: 连接器 ID。

        Returns:
            是否成功注销。
        """
        if connector_id in self._connectors:
            del self._connectors[connector_id]
            logger.info("connector.unregistered", connector_id=connector_id)
            return True
        return False

    def get(self, connector_id: str) -> BaseConnector | None:
        """获取指定连接器。

        Args:
            connector_id: 连接器 ID。

        Returns:
            连接器实例或 None。
        """
        return self._connectors.get(connector_id)

    def get_all(self) -> list[BaseConnector]:
        """获取所有已注册连接器（含未启用的）。"""
        return list(self._connectors.values())

    def get_active(self) -> list[BaseConnector]:
        """获取所有已启用的连接器。

        Returns:
            已启用连接器列表。
        """
        return [c for c in self._connectors.values() if c.is_active]

    def toggle(self, connector_id: str, active: bool) -> bool:
        """启停连接器。

        Args:
            connector_id: 连接器 ID。
            active: 是否启用。

        Returns:
            是否成功切换。
        """
        connector = self._connectors.get(connector_id)
        if connector is None:
            return False
        connector.is_active = active
        logger.info(
            "connector.toggled",
            connector_id=connector_id,
            active=active,
        )
        return True

    def list_connectors(self) -> list[dict[str, str | bool]]:
        """列出所有连接器及其状态。

        Returns:
            连接器信息列表。
        """
        return [
            {
                "connector_id": c.connector_id,
                "display_name": c.display_name,
                "is_active": c.is_active,
            }
            for c in self._connectors.values()
        ]


# ------------------------------------------------------------------
# 全局单例 — 自动注册内置连接器
# ------------------------------------------------------------------

connector_registry = ConnectorRegistry()

# 注册内置连接器（各自根据环境变量决定是否启用）
connector_registry.register(OAConnector())
connector_registry.register(ERPConnector())
connector_registry.register(CRMConnector())
connector_registry.register(MailConnector())
