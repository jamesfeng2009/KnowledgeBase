"""
Widget 令牌服务 — 单一职责：网站嵌入 Widget 的匿名访问令牌与限流。

设计要点：
    - 无状态令牌：HMAC-SHA256 签名（payload = kb_id + exp + iat + nonce），
      服务端不落库，校验即验签 + 过期检查；
    - 密钥：优先 WIDGET_HMAC_SECRET，缺省从 SECRET_KEY 派生（单一主密钥）；
    - 限流：进程内滑动窗口（token 维度），超限抛 WidgetRateLimited；
    - 不存储任何用户信息，仅允许访问令牌绑定的 kb_id。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings


class WidgetTokenError(Exception):
    """Widget 令牌无效/过期。"""


class WidgetRateLimited(Exception):
    """Widget 请求超限。"""


@dataclass
class WidgetClaims:
    """解析后的令牌声明。"""

    kb_id: str
    exp: int
    iat: int


def _derive_secret() -> str:
    settings = get_settings()
    if settings.WIDGET_HMAC_SECRET:
        return settings.WIDGET_HMAC_SECRET
    # 从 SECRET_KEY 派生独立子密钥（避免直接复用主密钥签名空间）
    material = f"ekb-widget:{settings.SECRET_KEY}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class WidgetTokenService:
    """Widget 令牌签发与校验。"""

    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret or _derive_secret()

    # ------------------------------------------------------------------
    # 签发 / 校验
    # ------------------------------------------------------------------

    def create_token(self, kb_id: str, ttl: int | None = None) -> dict[str, Any]:
        """签发 widget 令牌。

        Returns:
            {"token": str, "expires_at": int(epoch秒), "kb_id": str}
        """
        settings = get_settings()
        ttl = ttl or settings.WIDGET_TOKEN_TTL
        now = int(time.time())
        payload = {
            "kb_id": str(kb_id),
            "iat": now,
            "exp": now + ttl,
            "nonce": secrets.token_urlsafe(8),
        }
        token = self._sign(payload)
        return {
            "token": token,
            "expires_at": payload["exp"],
            "kb_id": payload["kb_id"],
        }

    def verify_token(self, token: str) -> WidgetClaims:
        """校验令牌并返回声明（无效/过期抛 WidgetTokenError）。"""
        try:
            payload = self._unsign(token)
        except Exception as exc:
            raise WidgetTokenError(f"令牌无效: {exc}") from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise WidgetTokenError("令牌已过期")
        return WidgetClaims(
            kb_id=str(payload.get("kb_id", "")),
            exp=int(payload.get("exp", 0)),
            iat=int(payload.get("iat", 0)),
        )

    # ------------------------------------------------------------------
    # 签名原语
    # ------------------------------------------------------------------

    @staticmethod
    def _b64encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(raw: str) -> bytes:
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding)

    def _sign(self, payload: dict[str, Any]) -> str:
        body = self._b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = hmac.new(
            self._secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{body}.{sig}"

    def _unsign(self, token: str) -> dict[str, Any]:
        body, _, sig = token.partition(".")
        expected = hmac.new(
            self._secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise WidgetTokenError("签名不匹配")
        return json.loads(self._b64decode(body))


class WidgetRateLimiter:
    """进程内滑动窗口限流（token 维度，60s 窗口）。"""

    def __init__(self, limit: int | None = None) -> None:
        settings = get_settings()
        self._limit = limit or settings.WIDGET_RATE_LIMIT
        self._window: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        """检查并记录一次请求；超限抛 WidgetRateLimited。"""
        now = time.monotonic()
        window_start = now - 60
        hits = [t for t in self._window.get(key, []) if t >= window_start]
        if len(hits) >= self._limit:
            raise WidgetRateLimited(f"请求过于频繁，请 {60 - int(now - hits[0])} 秒后重试")
        hits.append(now)
        self._window[key] = hits
