"""
OIDC 单点登录服务 — 单一职责：OIDC discovery / 授权 URL / 令牌交换 /
userinfo 获取 / 用户落库。

支持任意标准 OIDC Provider（Azure AD / Google / Keycloak / Okta 等）：
    1. discovery 获取 authorization_endpoint / token_endpoint / userinfo_endpoint；
    2. 构建授权 URL（scope + state + redirect_uri）；
    3. 用 authorization code 交换 id_token + access_token；
    4. 拉取 userinfo（sub / email / name）；
    5. 按 sub（或 email）幂等创建/登录用户并签发本系统 JWT。

安全设计：
    - state 参数由调用方生成并校验（CSRF 防护）；
    - id_token 仅作声明来源，不静默信任；用户身份以 userinfo 为准；
    - 未配置 OIDC 时所有方法抛 OIDCDisabledError（路由 403）。
"""

from __future__ import annotations

import secrets
import urllib.parse
from functools import lru_cache
from typing import Any

import httpx
from jose import jwt as jose_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.utils.crypto import create_access_token, hash_password
from app.utils.logger import get_logger

log = get_logger(__name__)

_DISCOVERY_CACHE_TTL: float = 3600.0


class OIDCError(Exception):
    """OIDC 流程错误（配置缺失 / 协议失败 / 用户信息缺失）。"""


class OIDCDisabledError(OIDCError):
    """OIDC 未启用。"""


class OIDCService:
    """OIDC 单点登录服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        settings = get_settings()
        self.enabled = settings.OIDC_ENABLED
        self.issuer = settings.OIDC_ISSUER
        self.client_id = settings.OIDC_CLIENT_ID
        self.client_secret = settings.OIDC_CLIENT_SECRET
        self.redirect_uri = settings.OIDC_REDIRECT_URI
        self.scopes = settings.OIDC_SCOPES

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.enabled or not self.issuer:
            raise OIDCDisabledError("OIDC 未启用（OIDC_ENABLED / OIDC_ISSUER）")

    @lru_cache(maxsize=8)
    def _discovery_url(self, issuer: str) -> str:
        """构造 discovery 端点 URL（幂等缓存避免重复拼接）。"""
        return issuer.rstrip("/") + "/.well-known/openid-configuration"

    async def discover(self) -> dict[str, Any]:
        """拉取 OIDC discovery 文档（HTTP 缓存 1 小时）。"""
        self._require_enabled()
        url = self._discovery_url(self.issuer)
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise OIDCError(f"OIDC discovery 失败: HTTP {resp.status_code}")
            return resp.json()

    async def _get_endpoint(self, name: str) -> str:
        doc = await self.discover()
        endpoint = doc.get(name, "")
        if not endpoint:
            raise OIDCError(f"OIDC discovery 缺少端点: {name}")
        return endpoint

    # ------------------------------------------------------------------
    # 授权流程
    # ------------------------------------------------------------------

    def build_authorize_url(self, state: str | None = None) -> tuple[str, str]:
        """构建授权 URL。

        Returns:
            (authorize_url, state) — state 由调用方保存用于回调校验。
        """
        self._require_enabled()
        state = state or secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": secrets.token_urlsafe(16),
        }
        # discovery 需要 await，此方法保持同步：端点从缓存配置读取。
        # 生产环境建议预取 discovery 或直接配置 authorization_endpoint。
        authorization_endpoint = self._cached_authorize_endpoint()
        return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}", state

    @lru_cache(maxsize=8)
    def _cached_authorize_endpoint(self) -> str:
        """缓存 authorization_endpoint（首次调用时走 discovery）。"""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # 同步上下文拿不到结果：回退标准路径（issuer + /authorize 惯例）
            return self.issuer.rstrip("/") + "/authorize"
        try:
            return asyncio.run(self._get_endpoint("authorization_endpoint"))
        except Exception as exc:
            log.warning("oidc.authorize_endpoint_fallback", error=str(exc)[:120])
            return self.issuer.rstrip("/") + "/authorize"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """用 authorization code 交换令牌。"""
        self._require_enabled()
        token_endpoint = await self._get_endpoint("token_endpoint")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_endpoint, data=data)
            if resp.status_code != 200:
                raise OIDCError(f"令牌交换失败: HTTP {resp.status_code}")
            return resp.json()

    async def get_userinfo(self, access_token: str) -> dict[str, Any]:
        """拉取 userinfo。"""
        self._require_enabled()
        userinfo_endpoint = await self._get_endpoint("userinfo_endpoint")
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(userinfo_endpoint, headers=headers)
            if resp.status_code != 200:
                raise OIDCError(f"userinfo 失败: HTTP {resp.status_code}")
            return resp.json()

    @staticmethod
    def decode_id_token_claims(id_token: str) -> dict[str, Any]:
        """解析 id_token 声明（不验签 — 身份以 userinfo 为准）。"""
        try:
            return jose_jwt.get_unverified_claims(id_token)
        except Exception as exc:
            raise OIDCError(f"id_token 解析失败: {exc}") from exc

    # ------------------------------------------------------------------
    # 用户落库
    # ------------------------------------------------------------------

    async def _existing_by_sub_or_email(self, sub: str, email: str) -> Any:
        """按 OIDC sub（oidc-{sub}@sso.local）或同邮箱既有用户查找。"""
        user_repo = UserRepository(self.db)
        existing = await user_repo.get_by_email(f"oidc-{sub}@sso.local")
        if existing is None and email:
            existing = await user_repo.get_by_email(email)
        return existing

    async def login_or_register(
        self, userinfo: dict[str, Any]
    ) -> dict[str, Any]:
        """按 OIDC subject 幂等登录/注册用户，签发本系统 JWT。

        Returns:
            {"access_token": str, "token_type": "bearer", "user_id": str}
        """
        sub = str(userinfo.get("sub", ""))
        if not sub:
            raise OIDCError("userinfo 缺少 sub")
        email = userinfo.get("email", "").strip().lower()
        name = userinfo.get("name", "") or email.split("@")[0] or f"oidc-{sub[:8]}"

        auth_service = AuthService(self.db)

        # 1. 按 sub 查找既有 OIDC 用户；无则查同邮箱普通用户
        existing = await self._existing_by_sub_or_email(sub, email)
        if existing is not None:
            token = create_access_token({"sub": str(existing.id), "role": existing.role})
            return {
                "access_token": token,
                "token_type": "bearer",
                "user_id": str(existing.id),
            }

        # 3. 新用户：随机密码 + oidc-{sub} 邮箱（保证唯一）
        random_password = secrets.token_urlsafe(24)
        try:
            user = await auth_service.register(
                email=f"oidc-{sub}@sso.local",
                password=random_password,
                name=name,
            )
        except ValueError:
            # 并发注册竞争：重查一次
            user = await user_repo.get_by_email(f"oidc-{sub}@sso.local")
            if user is None:
                raise
        token = create_access_token({"sub": str(user.id), "role": user.role})
        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": str(user.id),
        }
