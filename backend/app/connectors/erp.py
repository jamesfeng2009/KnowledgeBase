"""
ERP 连接器 — 对接 SAP/用友/金蝶 ERP API。

搜索采购单、库存记录、财务凭证。
"""

from __future__ import annotations

from app.connectors.base import BaseConnector, ExternalSearchResult
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class ERPConnector(BaseConnector):
    """ERP 系统连接器 — 搜索采购单/库存/财务记录。"""

    connector_id = "erp"
    display_name = "ERP 记录"

    def __init__(self) -> None:
        self.is_active = getattr(settings, "CONNECTOR_ERP_ENABLED", False)
        self.api_url = getattr(settings, "CONNECTOR_ERP_API_URL", "")
        self.api_key = getattr(settings, "CONNECTOR_ERP_API_KEY", "")

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索 ERP 系统。"""
        if not self.is_active or not self.api_url:
            return []
        try:
            return await self._call_erp_api(keyword, top_k)
        except Exception as exc:
            logger.warning("connector.erp.search_failed", keyword=keyword, error=str(exc))
            return []

    async def test_connection(self) -> bool:
        if not self.is_active or not self.api_url:
            return False
        try:
            return await self._call_erp_api("__health_check__", 1) is not None
        except Exception:
            return False

    async def _call_erp_api(self, keyword: str, top_k: int) -> list[ExternalSearchResult]:
        """调用 ERP API — 子类可覆盖。"""
        # TODO: 对接真实 ERP API（SAP/用友/金蝶）
        return []
