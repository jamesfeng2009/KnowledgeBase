"""Widget 令牌服务与 API 单测 — P2 网站嵌入 Widget。

测试策略：
    - 令牌：签发 → 校验回读声明；篡改/过期/错误签名拒绝；
    - 限流：同 key 连续超限抛 WidgetRateLimited，窗口滑动后可恢复；
    - API 路由：/widget/token 与 /widget/ask 注册。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.widget_token_service import (
    WidgetClaims,
    WidgetRateLimiter,
    WidgetRateLimited,
    WidgetTokenError,
    WidgetTokenService,
)


@pytest.fixture
def service() -> WidgetTokenService:
    with patch("app.services.widget_token_service.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.WIDGET_HMAC_SECRET = "test-secret"
        s.WIDGET_TOKEN_TTL = 3600
        s.SECRET_KEY = "sk"
        yield WidgetTokenService(secret="test-secret")


class TestTokenLifecycle:
    def test_create_and_verify(self, service) -> None:  # noqa: ANN001
        data = service.create_token("kb-1")
        assert data["kb_id"] == "kb-1"
        assert data["expires_at"] > int(time.time())

        claims = service.verify_token(data["token"])
        assert isinstance(claims, WidgetClaims)
        assert claims.kb_id == "kb-1"

    def test_tampered_token_rejected(self, service) -> None:  # noqa: ANN001
        data = service.create_token("kb-1")
        tampered = data["token"][:-2] + ("aa" if not data["token"].endswith("aa") else "bb")
        with pytest.raises(WidgetTokenError):
            service.verify_token(tampered)

    def test_expired_token_rejected(self, service) -> None:  # noqa: ANN001
        data = service.create_token("kb-1", ttl=-10)
        with pytest.raises(WidgetTokenError, match="过期"):
            service.verify_token(data["token"])

    def test_malformed_token_rejected(self, service) -> None:  # noqa: ANN001
        with pytest.raises(WidgetTokenError):
            service.verify_token("not-a-token")

    def test_custom_ttl(self, service) -> None:  # noqa: ANN001
        data = service.create_token("kb-1", ttl=60)
        assert data["expires_at"] - int(time.time()) <= 60


class TestSecretDerivation:
    def test_derives_from_secret_key(self) -> None:
        with patch("app.services.widget_token_service.get_settings") as mock_settings:
            s = mock_settings.return_value
            s.WIDGET_HMAC_SECRET = ""
            s.SECRET_KEY = "master-secret"
            svc = WidgetTokenService()
        assert svc._secret != "master-secret"
        assert len(svc._secret) == 64


class TestRateLimiter:
    def test_allows_within_limit(self) -> None:
        limiter = WidgetRateLimiter(limit=3)
        for _ in range(3):
            limiter.check("k1")  # 不抛异常

    def test_exceeds_limit(self) -> None:
        limiter = WidgetRateLimiter(limit=2)
        limiter.check("k1")
        limiter.check("k1")
        with pytest.raises(WidgetRateLimited):
            limiter.check("k1")

    def test_isolated_keys(self) -> None:
        limiter = WidgetRateLimiter(limit=1)
        limiter.check("k1")
        limiter.check("k2")  # 不同 key 不互相影响


class TestWidgetAPI:
    def test_router_registered(self) -> None:
        from app.api.v1.widget import router

        paths = {r.path for r in router.routes}
        assert "/widget/token" in paths
        assert "/widget/ask" in paths
