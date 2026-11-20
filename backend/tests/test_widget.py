"""Widget 令牌服务与 API 单测 — P2 网站嵌入 Widget。

测试策略：
    - 令牌：签发 → 校验回读声明；篡改/过期/错误签名拒绝；
    - 限流：同 key 连续超限抛 WidgetRateLimited，窗口滑动后可恢复；
    - API 路由：/widget/token 与 /widget/ask 注册。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.widget_token_service import (
    WidgetClaims,
    WidgetRateLimited,
    WidgetRateLimiter,
    WidgetTokenError,
    WidgetTokenService,
)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
KB_ID = "aaaaaaaa-0000-4000-8000-000000000001"


def _legacy_token(service: WidgetTokenService) -> str:
    """构造改造前签发的、不含 tenant_id 字段的合法签名令牌。"""
    payload = {
        "kb_id": KB_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "nonce": "legacy",
    }
    body = service._b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    sig = hmac.new(
        service._secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


@pytest.fixture
def service() -> WidgetTokenService:
    with patch("app.services.widget_token_service.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.WIDGET_HMAC_SECRET = "test-secret"
        s.WIDGET_TOKEN_TTL = 3600
        s.SECRET_KEY = "sk"
        yield WidgetTokenService(secret="test-secret")


class TestTokenLifecycle:
    def test_create_and_verify(self, service) -> None:
        data = service.create_token("kb-1", tenant_id=TENANT_ID)
        assert data["kb_id"] == "kb-1"
        assert data["tenant_id"] == TENANT_ID
        assert data["expires_at"] > int(time.time())

        claims = service.verify_token(data["token"])
        assert isinstance(claims, WidgetClaims)
        assert claims.kb_id == "kb-1"
        assert claims.tenant_id == TENANT_ID

    def test_tenant_id_is_required(self, service) -> None:
        """不带租户签不出牌：fail-closed 下那必然是一张查不到数据的废牌。"""
        with pytest.raises(TypeError):
            service.create_token("kb-1")  # type: ignore[call-arg]

    def test_legacy_token_without_tenant_is_detected(self, service) -> None:
        """改造前签发的旧令牌没有 tenant_id 字段，调用方必须能识别并拒绝。"""
        claims = service.verify_token(_legacy_token(service))
        assert claims.tenant_id is None

    def test_signature_covers_tenant_id(self, service) -> None:
        """换签名（等价于篡改 payload 里的租户）必须验签失败。"""
        a = service.create_token("kb-1", tenant_id=TENANT_ID)
        b = service.create_token("kb-1", tenant_id=OTHER_TENANT)
        body_a, _, _ = a["token"].partition(".")
        _, _, sig_b = b["token"].partition(".")
        with pytest.raises(WidgetTokenError):
            service.verify_token(f"{body_a}.{sig_b}")

    def test_tampered_token_rejected(self, service) -> None:
        data = service.create_token("kb-1", tenant_id=TENANT_ID)
        tampered = data["token"][:-2] + (
            "aa" if not data["token"].endswith("aa") else "bb"
        )
        with pytest.raises(WidgetTokenError):
            service.verify_token(tampered)

    def test_expired_token_rejected(self, service) -> None:
        data = service.create_token("kb-1", tenant_id=TENANT_ID, ttl=-10)
        with pytest.raises(WidgetTokenError, match="过期"):
            service.verify_token(data["token"])

    def test_malformed_token_rejected(self, service) -> None:
        with pytest.raises(WidgetTokenError):
            service.verify_token("not-a-token")

    def test_custom_ttl(self, service) -> None:
        data = service.create_token("kb-1", tenant_id=TENANT_ID, ttl=60)
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


class TestWidgetAskTenantContext:
    """匿名端点运行时验收：租户只能来自签名令牌，伪造请求头无效。"""

    @staticmethod
    def _client():
        from app.api.v1 import widget
        from app.database import get_db_session

        app = FastAPI()
        app.include_router(widget.router)

        db = AsyncMock()
        db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))

        async def _override():
            yield db

        app.dependency_overrides[get_db_session] = _override
        return TestClient(app), WidgetTokenService(secret="test-secret")

    def test_valid_token_binds_tenant_from_claims_even_with_forged_header(self) -> None:
        client, svc = self._client()
        token = svc.create_token(KB_ID, tenant_id=TENANT_ID)["token"]

        with (
            patch.object(widget_module(), "_token_service", svc),
            patch.object(widget_module(), "_rate_limiter"),
            patch.object(
                widget_module(), "apply_tenant", new_callable=AsyncMock
            ) as apply,
        ):
            resp = client.post(
                "/widget/ask",
                json={"token": token, "question": "报销标准"},
                headers={"X-Tenant-Id": OTHER_TENANT},
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["kb_id"] == KB_ID
        apply.assert_awaited_once()
        assert apply.await_args.args[1] == TENANT_ID, "租户必须来自签名令牌，不是请求头"

    def test_legacy_token_without_tenant_is_rejected(self) -> None:
        client, svc = self._client()

        with (
            patch.object(widget_module(), "_token_service", svc),
            patch.object(widget_module(), "_rate_limiter"),
            patch.object(
                widget_module(), "apply_tenant", new_callable=AsyncMock
            ) as apply,
        ):
            resp = client.post(
                "/widget/ask",
                json={"token": _legacy_token(svc), "question": "q"},
                headers={"X-Tenant-Id": OTHER_TENANT},
            )

        assert resp.status_code == 401
        apply.assert_not_awaited()


def widget_module():
    from app.api.v1 import widget

    return widget
