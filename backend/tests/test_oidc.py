"""OIDC 单点登录单测 — P1 OIDC 支持。

测试策略：
    - 配置校验：未启用时抛 OIDCDisabledError；
    - discovery / 令牌交换 / userinfo：mock httpx 验证端点调用与解析；
    - login_or_register：mock UserRepository/AuthService 验证既有用户登录
      与新用户注册两条路径；
    - 路由注册：/auth/oidc/login 与 /callback 存在。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.oidc_service import OIDCDisabledError, OIDCError, OIDCService


def _make_service(db=None, **overrides) -> OIDCService:
    settings = SimpleNamespace(
        OIDC_ENABLED=True,
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
        OIDC_REDIRECT_URI="http://localhost/cb",
        OIDC_SCOPES="openid profile email",
    )
    for k, v in overrides.items():
        setattr(settings, k, v)
    with patch("app.services.oidc_service.get_settings", return_value=settings):
        return OIDCService(db or MagicMock())


def _fake_http_post(get_json: dict, post_json: dict, status: int = 200):
    """构造 AsyncClient mock：get 返回 discovery 文档，post 返回令牌。"""
    get_resp = MagicMock()
    get_resp.status_code = status
    get_resp.json.return_value = get_json
    post_resp = MagicMock()
    post_resp.status_code = status
    post_resp.json.return_value = post_json
    mock_client = AsyncMock()
    mock_client.get.return_value = get_resp
    mock_client.post.return_value = post_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    return mock_client


_DISCOVERY_DOC = {
    "authorization_endpoint": "https://issuer.example.com/authorize",
    "token_endpoint": "https://issuer.example.com/token",
    "userinfo_endpoint": "https://issuer.example.com/userinfo",
}


class TestOIDCDisabled:
    @pytest.mark.asyncio
    async def test_disabled_raises(self) -> None:
        service = _make_service(OIDC_ENABLED=False)
        with pytest.raises(OIDCDisabledError):
            await service.discover()


class TestOIDCDiscovery:
    @pytest.mark.asyncio
    async def test_discover_fetches_document(self) -> None:
        service = _make_service()
        mock_client = _fake_http_post(_DISCOVERY_DOC, {})
        with patch("app.services.oidc_service.httpx.AsyncClient", return_value=mock_client):
            result = await service.discover()
        assert result["token_endpoint"].endswith("/token")
        called_url = mock_client.get.call_args.args[0]
        assert called_url.endswith("/.well-known/openid-configuration")

    @pytest.mark.asyncio
    async def test_discover_non_200_raises(self) -> None:
        service = _make_service()
        mock_client = _fake_http_post({}, {}, status=404)
        with patch("app.services.oidc_service.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OIDCError):
                await service.discover()


class TestOIDCExchange:
    @pytest.mark.asyncio
    async def test_exchange_code(self) -> None:
        service = _make_service()
        mock_client = _fake_http_post(
            _DISCOVERY_DOC, {"access_token": "AT", "id_token": "IT"}
        )
        with patch("app.services.oidc_service.httpx.AsyncClient", return_value=mock_client):
            tokens = await service.exchange_code("CODE")

        assert tokens["access_token"] == "AT"
        posted_data = mock_client.post.call_args.kwargs["data"]
        assert posted_data["code"] == "CODE"
        assert posted_data["grant_type"] == "authorization_code"

    @pytest.mark.asyncio
    async def test_get_userinfo(self) -> None:
        service = _make_service()
        # 第一次 get = discovery，第二次 get = userinfo
        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {"sub": "u1", "email": "a@b.com", "name": "Alice"}
        mock_client = _fake_http_post(_DISCOVERY_DOC, {})
        mock_client.get.side_effect = [mock_client.get.return_value, userinfo_resp]
        with patch("app.services.oidc_service.httpx.AsyncClient", return_value=mock_client):
            info = await service.get_userinfo("AT")

        assert info["sub"] == "u1"
        headers = mock_client.get.call_args_list[1].kwargs["headers"]
        assert headers["Authorization"] == "Bearer AT"


class TestOIDCLoginOrRegister:
    @pytest.mark.asyncio
    async def test_existing_user_logs_in(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        existing = SimpleNamespace(id="user-1", role="viewer")

        service._existing_by_sub_or_email = AsyncMock(return_value=existing)
        with patch("app.services.oidc_service.create_access_token", return_value="JWT"):
            result = await service.login_or_register({"sub": "u1", "email": "a@b.com"})

        assert result["access_token"] == "JWT"
        assert result["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_new_user_registers(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        new_user = SimpleNamespace(id="user-2", role="admin")

        # 第一次查不到 → 注册成功
        service._existing_by_sub_or_email = AsyncMock(return_value=None)

        class FakeAuth:
            async def register(self, email, password, name):
                assert email == "oidc-u1@sso.local"
                return new_user

        class FakeRepo:
            async def get_by_email(self, email):
                return None

        with patch("app.services.oidc_service.AuthService", return_value=FakeAuth()), patch(
            "app.services.oidc_service.UserRepository", return_value=FakeRepo()
        ), patch(
            "app.services.oidc_service.create_access_token", return_value="JWT2"
        ):
            result = await service.login_or_register({"sub": "u1", "email": "a@b.com", "name": "Alice"})

        assert result["user_id"] == "user-2"
        assert result["access_token"] == "JWT2"

    @pytest.mark.asyncio
    async def test_missing_sub_raises(self) -> None:
        service = _make_service()
        with pytest.raises(OIDCError):
            await service.login_or_register({"email": "x@y.com"})


class TestOIDCBuildAuthorizeUrl:
    def test_build_url_contains_params(self) -> None:
        service = _make_service()
        with patch.object(
            service, "_cached_authorize_endpoint", return_value="https://issuer.example.com/authorize"
        ):
            url, state = service.build_authorize_url("fixed-state")

        assert "response_type=code" in url
        assert "client_id=client-id" in url
        assert "state=fixed-state" in url
        assert state == "fixed-state"

    def test_disabled_raises_on_build(self) -> None:
        service = _make_service(OIDC_ENABLED=False)
        with pytest.raises(OIDCDisabledError):
            service.build_authorize_url()


class TestOIDCAPI:
    def test_router_registered(self) -> None:
        from app.api.v1.oidc import router

        paths = {r.path for r in router.routes}
        assert "/auth/oidc/login" in paths
        assert "/auth/oidc/callback" in paths
