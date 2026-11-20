"""租户身份解析 fail-closed 回归测试。

背景：旧实现「JWT 解码失败 → pass → 采信 X-Tenant-Id」是一条可伪造的身份
注入路径。不带 JWT、只带 ``X-Tenant-Id: <victim-uuid>`` 就能把 RLS 的 GUC
设成任意租户，fail-closed RLS 反而会忠实地放行受害者全量数据。

本文件锁死三件事：
1. 默认只认 JWT，请求头一律不采信；
2. JWT 解码失败立即 fail-closed，不降级到任何请求头；
3. 网关注入的 X-Tenant-Id 只有在显式开关 + 共享标记同时满足时才生效。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app.config import get_settings
from app.core.tenant_resolver import (
    TENANT_GATEWAY_TOKEN_HEADER,
    extract_domain_from_request,
    parse_tenant_subdomain,
    resolve_tenant_id,
)
from app.utils.crypto import create_access_token

VICTIM = "33333333-3333-3333-3333-333333333333"
ATTACKER = "44444444-4444-4444-4444-444444444444"
GATEWAY_TOKEN = "s3cr3t-gateway-shared-token"


def _request(headers: dict[str, str] | None = None) -> StarletteRequest:
    """构造最小 HTTP Request；headers 会被转成 ASGI 要求的 lowercase bytes。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/documents",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("1.2.3.4", 1234),
        "root_path": "",
    }
    return StarletteRequest(scope)


@pytest.fixture
def hardened_settings():
    """默认安全配置：不信任请求头。"""
    return get_settings().model_copy(
        update={
            "TRUST_TENANT_ID_HEADER": False,
            "TENANT_GATEWAY_TOKEN": "",
            "TENANT_GATEWAY_TOKEN_HEADER": TENANT_GATEWAY_TOKEN_HEADER,
        }
    )


@pytest.fixture
def gateway_settings():
    """显式开启网关通道的配置。"""
    return get_settings().model_copy(
        update={
            "TRUST_TENANT_ID_HEADER": True,
            "TENANT_GATEWAY_TOKEN": GATEWAY_TOKEN,
            "TENANT_GATEWAY_TOKEN_HEADER": TENANT_GATEWAY_TOKEN_HEADER,
        }
    )


def _jwt(tenant_id: str | None) -> str:
    data = {"sub": "user@example.com"}
    if tenant_id is not None:
        data["tenant_id"] = tenant_id
    return create_access_token(data)


class TestDefaultRejectsHeaders:
    """默认配置：请求头永远不能决定租户身份。"""

    def test_no_credentials_yields_no_tenant(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request({"host": "attacker.example.com"}), hardened_settings
        )
        assert tid is None

    def test_forged_tenant_id_header_is_ignored(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request({"host": "attacker.example.com", "X-Tenant-Id": VICTIM}),
            hardened_settings,
        )
        assert tid is None

    def test_invalid_jwt_never_falls_back_to_header(self, hardened_settings) -> None:
        """核心回归：旧实现在这里会落到 X-Tenant-Id 分支。"""
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "Authorization": "Bearer garbage.invalid.token",
                    "X-Tenant-Id": VICTIM,
                    "X-Tenant-Domain": "victim-workspace",
                }
            ),
            hardened_settings,
        )
        assert tid is None

    def test_invalid_jwt_with_gateway_token_still_fail_closed(
        self, gateway_settings
    ) -> None:
        """JWT 解码失败是硬失败，连可信网关通道都不降级。"""
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "Authorization": "Bearer garbage.invalid.token",
                    "X-Tenant-Id": VICTIM,
                    TENANT_GATEWAY_TOKEN_HEADER: GATEWAY_TOKEN,
                }
            ),
            gateway_settings,
        )
        assert tid is None

    def test_valid_jwt_wins_over_forged_header(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "Authorization": f"Bearer {_jwt(ATTACKER)}",
                    "X-Tenant-Id": VICTIM,
                }
            ),
            hardened_settings,
        )
        assert str(tid) == ATTACKER

    def test_valid_jwt_without_tenant_claim_stays_fail_closed(
        self, hardened_settings
    ) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "Authorization": f"Bearer {_jwt(None)}",
                    "X-Tenant-Id": VICTIM,
                }
            ),
            hardened_settings,
        )
        assert tid is None

    def test_malformed_tenant_claim_does_not_raise(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request({"Authorization": f"Bearer {_jwt('not-a-uuid')}"}),
            hardened_settings,
        )
        assert tid is None


class TestGatewayOptIn:
    """网关注入通道：必须显式开启且共享标记匹配。"""

    def test_opt_in_off_rejects_everything(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "X-Tenant-Id": VICTIM,
                    TENANT_GATEWAY_TOKEN_HEADER: GATEWAY_TOKEN,
                }
            ),
            hardened_settings,
        )
        assert tid is None

    def test_missing_gateway_token_rejects(self, gateway_settings) -> None:
        tid, _ = resolve_tenant_id(_request({"X-Tenant-Id": VICTIM}), gateway_settings)
        assert tid is None

    def test_wrong_gateway_token_rejects(self, gateway_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "X-Tenant-Id": VICTIM,
                    TENANT_GATEWAY_TOKEN_HEADER: "wrong",
                }
            ),
            gateway_settings,
        )
        assert tid is None

    def test_valid_gateway_token_accepts(self, gateway_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "X-Tenant-Id": ATTACKER,
                    TENANT_GATEWAY_TOKEN_HEADER: GATEWAY_TOKEN,
                }
            ),
            gateway_settings,
        )
        assert str(tid) == ATTACKER

    def test_gateway_token_with_invalid_uuid_rejects(self, gateway_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "X-Tenant-Id": "not-a-uuid",
                    TENANT_GATEWAY_TOKEN_HEADER: GATEWAY_TOKEN,
                }
            ),
            gateway_settings,
        )
        assert tid is None

    def test_blank_gateway_secret_can_never_authorize(self) -> None:
        cfg = get_settings().model_copy(
            update={"TRUST_TENANT_ID_HEADER": True, "TENANT_GATEWAY_TOKEN": ""}
        )
        tid, _ = resolve_tenant_id(
            _request({"X-Tenant-Id": VICTIM, TENANT_GATEWAY_TOKEN_HEADER: ""}), cfg
        )
        assert tid is None

    def test_magicmock_settings_cannot_accidentally_enable_trust(self) -> None:
        """MagicMock 的任意属性都是 truthy，绝不能因此误开跨租户通道。"""
        tid, _ = resolve_tenant_id(
            _request(
                {
                    "X-Tenant-Id": VICTIM,
                    TENANT_GATEWAY_TOKEN_HEADER: "anything",
                }
            ),
            MagicMock(),
        )
        assert tid is None


class TestDomainIsSemanticOnly:
    """子域只是语义标识，必须经权威映射才能变成 tenant_id。"""

    def test_header_domain_without_mapper_yields_no_tenant(
        self, hardened_settings
    ) -> None:
        tid, domain = resolve_tenant_id(
            _request({"X-Tenant-Domain": "victim-workspace", "X-Tenant-Id": VICTIM}),
            hardened_settings,
        )
        assert tid is None
        assert domain == "victim-workspace"

    def test_mapper_is_authoritative(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request({"X-Tenant-Domain": "workspace-a"}),
            hardened_settings,
            tenant_domain_to_id=lambda _domain: ATTACKER,
        )
        assert str(tid) == ATTACKER

    def test_bad_mapper_result_is_ignored(self, hardened_settings) -> None:
        tid, _ = resolve_tenant_id(
            _request({"X-Tenant-Domain": "workspace-a"}),
            hardened_settings,
            tenant_domain_to_id=lambda _domain: "bogus",
        )
        assert tid is None

    def test_parse_tenant_subdomain(self) -> None:
        assert parse_tenant_subdomain("acme.example.com", "example.com") == "acme"
        assert parse_tenant_subdomain("example.com", "example.com") is None
        assert parse_tenant_subdomain("a.b.example.com", "example.com") is None
        assert parse_tenant_subdomain("acme.other.com", "example.com") is None

    def test_extract_domain_prefers_gateway_header(self) -> None:
        assert (
            extract_domain_from_request(
                _request({"host": "other.com", "X-Tenant-Domain": "Acme"})
            )
            == "acme"
        )


class TestHttpLayer:
    """端到端：真实 HTTP 请求带伪造租户头时，中间件不得注入身份。"""

    @staticmethod
    def _app() -> FastAPI:
        import app.middleware as mw
        from app.middleware import setup_middleware

        mw._rate_limiter = None
        mw._tenant_rate_limiter = None
        settings = MagicMock()
        settings.CORS_ORIGINS = ["*"]
        settings.RATE_LIMIT_ENABLED = False
        settings.RATE_LIMIT_PER_MINUTE = 60
        settings.RATE_LIMIT_BURST = 2
        settings.REDIS_URL = None
        settings.RATE_LIMIT_TENANT_ENABLED = False

        app = FastAPI()
        with patch("app.middleware.get_settings", return_value=settings):
            setup_middleware(app)

        @app.get("/api/tenant-probe")
        async def probe(request: Request):
            tid = getattr(request.state, "tenant_id", None)
            return {"tenant_id": str(tid) if tid else None}

        return app

    def test_forged_header_without_jwt_yields_null_tenant(self) -> None:
        client = TestClient(self._app())
        resp = client.get("/api/tenant-probe", headers={"X-Tenant-Id": VICTIM})
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    def test_invalid_jwt_plus_forged_header_yields_null_tenant(self) -> None:
        client = TestClient(self._app())
        resp = client.get(
            "/api/tenant-probe",
            headers={
                "Authorization": "Bearer garbage.invalid.token",
                "X-Tenant-Id": VICTIM,
                "X-Tenant-Domain": "victim-workspace",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    def test_valid_jwt_still_sets_tenant(self) -> None:
        client = TestClient(self._app())
        resp = client.get(
            "/api/tenant-probe",
            headers={
                "Authorization": f"Bearer {_jwt(ATTACKER)}",
                "X-Tenant-Id": VICTIM,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == ATTACKER


class TestAuthSystemSessionUnaffected:
    """register/login 用 get_system_db_session，不看 request.state.tenant_id。

    收紧身份解析后无 JWT 请求的 tenant_id 一律为 None，认证端点必须仍然
    能跨租户按邮箱找用户，否则所有账号都会表现为「密码错误」。
    """

    @staticmethod
    def test_auth_router_keeps_system_session() -> None:
        import inspect

        from app.api.v1 import auth

        src = inspect.getsource(auth.register) + inspect.getsource(auth.login)
        # 只匹配依赖声明；docstring 里把 get_db_session 当反面例子提过，不能算
        assert src.count("Depends(get_system_db_session)") == 2
        assert "Depends(get_db_session)" not in src

    @pytest.mark.asyncio
    async def test_system_session_still_applies_cross_tenant(self) -> None:
        """register/login 的会话必须无条件写 '*'，与请求头无关。

        用 mock 而非真实 DB：全局 async_session_factory 绑在共享事件循环上，
        全量跑时前置测试换过 loop 会抛 "Future attached to a different loop"。
        这里只验证依赖本身的行为，DB 层隔离由 test_rls_fail_closed.py 覆盖。
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.database import get_system_db_session

        session = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=ctx)

        with (
            patch("app.database.async_session_factory", factory),
            patch("app.database.apply_cross_tenant", new_callable=AsyncMock) as apply,
            patch("app.database.clear_tenant", new_callable=AsyncMock),
        ):
            gen = get_system_db_session(_request({"X-Tenant-Id": VICTIM}))
            got = await gen.__anext__()
            assert got is session
            apply.assert_awaited_once_with(session)
            await gen.aclose()
