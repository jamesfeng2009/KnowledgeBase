"""
邮件连接器 — 对接 Exchange/Gmail API。

搜索邮件内容、附件、日历事件。
"""

from __future__ import annotations

from app.connectors.base import BaseConnector, ExternalSearchResult
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class MailConnector(BaseConnector):
    """邮件系统连接器 — 搜索邮件/附件/日历。"""

    connector_id = "mail"
    display_name = "邮件"

    def __init__(self) -> None:
        self.is_active = getattr(settings, "CONNECTOR_MAIL_ENABLED", False)
        self.api_url = getattr(settings, "CONNECTOR_MAIL_API_URL", "")
        self.api_key = getattr(settings, "CONNECTOR_MAIL_API_KEY", "")

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索邮件系统。"""
        if not self.is_active or not self.api_url:
            return []
        try:
            return await self._call_mail_api(keyword, top_k)
        except Exception as exc:
            logger.warning("connector.mail.search_failed", keyword=keyword, error=str(exc))
            return []

    async def test_connection(self) -> bool:
        if not self.is_active or not self.api_url:
            return False
        try:
            return await self._call_mail_api("__health_check__", 1) is not None
        except Exception:
            return False

    async def _call_mail_api(self, keyword: str, top_k: int) -> list[ExternalSearchResult]:
        """调用邮件 API — 子类可覆盖。"""
        # TODO: 对接真实邮件 API（Exchange/Gmail）
        return []
