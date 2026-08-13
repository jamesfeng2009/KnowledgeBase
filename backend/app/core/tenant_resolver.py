"""
租户解析 — 多租户生产化：子域/workspace 路由与租户标识解析。

单一职责：集中实现"从请求解析租户标识"的规则，供中间件与网关调用，
避免租户路由逻辑散落在各处。

解析优先级（从强到弱）：
    1. JWT（Authorization Bearer token 内的 tenant_id）— 已登录用户；
    2. X-Tenant-Id 请求头（APISIX 边缘网关在子域路由时注入）；
    3. 子域 Host 解析（{workspace}.{root_domain}），需 DB 映射 domain → tenant_id。

安全说明：
    - X-Tenant-Id 由边缘网关注入并在网关层剥离客户端自带的同名头，
      后端信任该头的前提是网关已做清洗；直连后端（不经网关）时该头可被伪造，
      因此仅当无 JWT 时采用，且最终权限校验由 TenantService 依据 JWT/用户归属复核。
    - 子域解析仅是语义提取，不在此处直接信任；domain → tenant 的权威映射
      由 TenantRepository.get_by_domain 提供（查询 active 租户）。
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import Request

from app.config import Settings

# 网关注入的租户 ID 头（与 APISIX 配置一致）
TENANT_ID_HEADER = "X-Tenant-Id"
TENANT_DOMAIN_HEADER = "X-Tenant-Domain"


def get_tenant_id_header() -> str:
    """返回网关注入租户 ID 的请求头名（对齐 config.TENANT_DOMAIN_HEADER 语义）。"""
    return TENANT_ID_HEADER


def parse_tenant_subdomain(
    host: str | None, root_domain: str | None
) -> str | None:
    """从 Host 解析租户工作区子域（纯语法，不触 DB）。

    规则：host 形如 ``<workspace>.<root_domain>`` 时返回 workspace，
    否则返回 None。根域为空或 host 不匹配时不做路由。

    Args:
        host: 请求 Host（如 ``acme.example.com``）。
        root_domain: 部署根域名（如 ``example.com``）。

    Returns:
        子域前缀（如 ``acme``），无法解析返回 None。
    """
    if not host or not root_domain:
        return None
    host = host.strip().lower()
    root = root_domain.strip().lower().lstrip(".")
    if not host.endswith(f".{root}"):
        return None
    sub = host[: -len(root) - 1]
    if not sub or "." in sub:
        # 空子域或无意义的多级子域不路由
        return None
    return sub


def extract_domain_from_request(request: Request) -> str | None:
    """提取请求对应的租户域标识。

    优先取网关透传头 X-Tenant-Domain，其次从 Host 头解析子域。

    Args:
        request: FastAPI 请求。

    Returns:
        租户子域/域标识，无则 None。
    """
    header_domain = request.headers.get(TENANT_DOMAIN_HEADER)
    if header_domain:
        return header_domain.strip().lower()

    host = request.headers.get("host")
    if host:
        # 去除端口
        return urlparse(f"//{host}").hostname or host.split(":")[0].strip().lower()
    return None


def resolve_tenant_id(
    request: Request,
    settings: Settings,
    tenant_domain_to_id: Any | None = None,
) -> tuple[uuid.UUID | None, str | None]:
    """解析请求对应的租户 ID 与租户域标识。

    Args:
        request: FastAPI 请求。
        settings: 应用配置。
        tenant_domain_to_id: 可选的 ``domain -> UUID`` 异步映射函数
            （如 lambda domain: await repo.get_by_domain(domain)），
            用于子域 → 租户 ID 的权威解析；为 None 时跳过子域 DB 解析。

    Returns:
        (tenant_id, tenant_domain)；均可能为 None。
    """
    from app.utils.crypto import decode_access_token

    tenant_domain = extract_domain_from_request(request)

    # 1) JWT 优先
    auth_header = request.headers.get("authorization", "")
    token = (
        auth_header.replace("Bearer ", "")
        if auth_header.startswith("Bearer ")
        else ""
    )
    if token:
        try:
            payload = decode_access_token(token)
            tid = payload.get("tenant_id")
            if tid:
                return uuid.UUID(tid), tenant_domain
        except Exception:
            pass  # 无效 JWT 交由 get_current_user 处理 401

    # 2) 网关注入的 X-Tenant-Id（仅无 JWT 时采用）
    header_tid = request.headers.get(TENANT_ID_HEADER)
    if header_tid:
        try:
            return uuid.UUID(header_tid), tenant_domain
        except Exception:
            pass

    # 3) 子域 → 租户 ID（权威映射，需 DB）
    if tenant_domain and tenant_domain_to_id is not None:
        tenant_id = tenant_domain_to_id(tenant_domain)
        if tenant_id:
            return tenant_id, tenant_domain

    return None, tenant_domain
