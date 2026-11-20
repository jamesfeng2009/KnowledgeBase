"""
OA 连接器 — 对接钉钉/企微/飞书 OA API。

搜索审批文档、公告、流程记录。
当 API 未配置时返回空结果（优雅降级）。
"""

from __future__ import annotations

from app.config import get_settings
from app.connectors.base import (
    BaseConnector,
    ConnectorNotImplementedError,
    ExternalSearchResult,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class OAConnector(BaseConnector):
    """OA 系统连接器 — 搜索审批文档、公告、流程记录。"""

    connector_id = "oa"
    display_name = "OA 审批"

    def __init__(self) -> None:
        self.is_active = getattr(settings, "CONNECTOR_OA_ENABLED", False)
        self.api_url = getattr(settings, "CONNECTOR_OA_API_URL", "")
        self.api_key = getattr(settings, "CONNECTOR_OA_API_KEY", "")

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索 OA 系统 — 审批文档/公告/流程记录。

        当 API 未配置时返回空列表。
        """
        if not self.is_active or not self.api_url:
            return []

        try:
            results = await self._call_oa_api(keyword, top_k)
            return results
        except ConnectorNotImplementedError as exc:
            # 已启用但适配器还没对接真实 API —— 配置错误，不是「没搜到」
            logger.error(
                "connector.oa.api_not_implemented", keyword=keyword, error=str(exc)
            )
            return []
        except Exception as exc:
            logger.warning(
                "connector.oa.search_failed", keyword=keyword, error=str(exc)
            )
            return []

    async def test_connection(self) -> bool:
        """测试 OA API 连接是否正常。"""
        if not self.is_active or not self.api_url:
            return False
        try:
            return await self._call_oa_api("__health_check__", 1) is not None
        except Exception:
            return False

    async def _call_oa_api(
        self, keyword: str, top_k: int
    ) -> list[ExternalSearchResult]:
        """调用 OA API — 子类可覆盖以对接不同 OA 系统。

        **默认实现抛 ConnectorNotImplementedError**：宁可显式报「没对接」，
        也不要 return [] 让上层以为「查了但没搜到」。生产环境替换为真实
        API 调用（钉钉/企微/飞书）后删除 raise。
        """
        # TODO: 对接真实 OA API
        # import httpx
        # async with httpx.AsyncClient() as client:
        #     resp = await client.get(
        #         f"{self.api_url}/search",
        #         headers={"Authorization": f"Bearer {self.api_key}"},
        #         params={"keyword": keyword, "top_k": top_k},
        #     )
        #     data = resp.json()
        #     return [ExternalSearchResult(...) for item in data["items"]]
        raise ConnectorNotImplementedError(
            "OA 连接器已启用（CONNECTOR_OA_API_URL 已配置），"
            "但适配器尚未对接真实 API"
        )
