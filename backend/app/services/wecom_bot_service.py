"""
企业微信 Bot 服务 — 单一职责：群机器人消息推送与回调验签。

推送：POST 到企微群机器人 webhook（markdown / text），支持幂等重试。
回调：校验 URL 签名（msg_signature = SHA1(token,timestamp,nonce,echostr)
排序拼接），供接收事件回调使用。
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class WeComBotService:
    """企业微信群机器人服务。"""

    def __init__(self, webhook: str | None = None, token: str | None = None) -> None:
        self.webhook = webhook or getattr(settings, "WECOM_BOT_WEBHOOK", "")
        self.token = token or getattr(settings, "WECOM_BOT_TOKEN", "")

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    async def send_markdown(self, content: str) -> bool:
        """推送 markdown 消息到企微群机器人。"""
        if not self.webhook:
            logger.warning("wecom.bot.no_webhook")
            return False
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(self.webhook, json=payload)
                data = resp.json()
            if data.get("errcode", 0) == 0:
                logger.info("wecom.bot.sent", errcode=0)
                return True
            logger.warning("wecom.bot.send_failed", errcode=data.get("errcode"))
            return False
        except Exception as exc:
            logger.warning("wecom.bot.send_error", error=str(exc)[:200])
            return False

    async def send_text(self, content: str) -> bool:
        """推送纯文本消息。"""
        if not self.webhook:
            return False
        payload = {"msgtype": "text", "text": {"content": content}}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(self.webhook, json=payload)
            return resp.json().get("errcode", -1) == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 回调验签
    # ------------------------------------------------------------------

    @staticmethod
    def verify_signature(
        token: str,
        timestamp: str,
        nonce: str,
        echostr: str,
        msg_signature: str,
    ) -> bool:
        """校验企微回调 URL 签名。

        签名算法：sha1( 排序(token, timestamp, nonce, echostr) 拼接 )。
        """
        parts = sorted([token, timestamp, nonce, echostr])
        raw = "".join(parts).encode("utf-8")
        digest = hashlib.sha1(raw).hexdigest()
        return hmac.compare_digest(digest, msg_signature)

    def verify_callback(
        self, timestamp: str, nonce: str, echostr: str, msg_signature: str
    ) -> bool:
        """基于实例 token 校验回调。"""
        return self.verify_signature(
            self.token, timestamp, nonce, echostr, msg_signature
        )
