"""
企业微信连接器 — 单一职责：搜索企微通讯录 / 客户 / 群聊记录。

复用 ``app.connectors.base.BaseConnector`` 抽象，与其他外部系统
（OA/ERP/CRM/Mail）统一走 ExternalSearchResult 格式。
当 API 未配置时返回空结果（优雅降级，与既有连接器一致）。
"""

from __future__ import annotations

import httpx

from app.connectors.base import BaseConnector, ExternalSearchResult
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class WeComConnector(BaseConnector):
    """企业微信连接器 — 搜索通讯录成员 / 客户 / 群聊消息。"""

    connector_id = "wecom"
    display_name = "企业微信"

    def __init__(self) -> None:
        self.is_active = getattr(settings, "CONNECTOR_WECOM_ENABLED", False)
        self.corp_id = getattr(settings, "WECOM_CORP_ID", "")
        self.agent_id = getattr(settings, "WECOM_AGENT_ID", "")
        self.secret = getattr(settings, "WECOM_SECRET", "")

    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索企业微信 — 通讯录成员匹配。

        未配置或调用失败时返回空列表（不阻塞主检索链路）。
        """
        if not self.is_active or not self.corp_id or not self.secret:
            return []
        try:
            token = await self._get_access_token()
            if not token:
                return []
            users = await self._search_users(token, keyword, top_k)
            return [
                ExternalSearchResult(
                    source="wecom",
                    source_label="企业微信",
                    title=u.get("name", ""),
                    snippet=f"部门：{u.get('department_label', '')} · 职位：{u.get('position', '')}",
                    url="",
                    metadata={"userid": u.get("userid", "")},
                    score=0.6,
                )
                for u in users
            ]
        except Exception as exc:
            logger.warning("connector.wecom.search_failed", keyword=keyword, error=str(exc))
            return []

    async def test_connection(self) -> bool:
        """测试企业微信 API 连接（获取 access_token）。"""
        if not self.is_active or not self.corp_id or not self.secret:
            return False
        try:
            return bool(await self._get_access_token())
        except Exception:
            return False

    async def _get_access_token(self) -> str:
        """获取企微 access_token（无缓存，简单实现；生产可加 TTL 缓存）。"""
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                url,
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.warning(
                    "connector.wecom.token_failed", errcode=data.get("errcode")
                )
                return ""
            return data.get("access_token", "")

    async def _search_users(self, token: str, keyword: str, top_k: int) -> list[dict]:
        """按姓名/别名搜索通讯录成员。"""
        url = "https://qyapi.weixin.qq.com/cgi-bin/user/list"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                url,
                params={"access_token": token, "department_id": 1, "fetch_child": 1},
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                return []
            users = data.get("userlist", [])
            matched = [
                u
                for u in users
                if keyword.lower() in (u.get("name", "") + u.get("alias", "")).lower()
            ]
            return matched[:top_k]
