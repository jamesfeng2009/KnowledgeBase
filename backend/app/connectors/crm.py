"""
CRM 连接器 — 对接 Salesforce/纷享销客 API。

搜索客户信息、商机、合同记录。
"""

from __future__ import annotations

from app.connectors.base import (
    BaseConnector,
    ConnectorNotImplementedError,
    ExternalSearchResult,
)
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class CRMConnector(BaseConnector):
    """CRM 系统连接器 — 搜索客户/商机/合同。"""

    connector_id = "crm"
    display_name = "CRM 客户"

    def __init__(self) -> None:
        self.is_active = getattr(settings, "CONNECTOR_CRM_ENABLED", False)
        self.api_url = getattr(settings, "CONNECTOR_CRM_API_URL", "")
        self.api_key = getattr(settings, "CONNECTOR_CRM_API_KEY", "")

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索 CRM 系统。"""
        if not self.is_active or not self.api_url:
            return []
        try:
            return await self._call_crm_api(keyword, top_k)
        except ConnectorNotImplementedError as exc:
            # 已启用但适配器还没对接真实 API —— 配置错误，不是「没搜到」
            logger.error(
                "connector.crm.api_not_implemented", keyword=keyword, error=str(exc)
            )
            return []
        except Exception as exc:
            logger.warning("connector.crm.search_failed", keyword=keyword, error=str(exc))
            return []

    async def test_connection(self) -> bool:
        if not self.is_active or not self.api_url:
            return False
        try:
            return await self._call_crm_api("__health_check__", 1) is not None
        except Exception:
            return False

    async def _call_crm_api(self, keyword: str, top_k: int) -> list[ExternalSearchResult]:
        """调用 CRM API — 子类可覆盖。"""
        # TODO: 对接真实 CRM API（Salesforce/纷享销客）
        raise ConnectorNotImplementedError(
            "CRM 连接器已启用（CONNECTOR_CRM_API_URL 已配置），"
            "但适配器尚未对接真实 API"
        )
