"""
租户解析 — 多租户生产化：子域/workspace 路由与租户标识解析。

单一职责：集中实现"从请求解析租户标识"的规则，供中间件与网关调用，
避免租户路由逻辑散落在各处。

解析优先级（从强到弱）：
    1. JWT（Authorization Bearer token 内的 tenant_id）— 唯一默认可信来源；
    2. 域标识 → ``tenant_domain_to_id`` 权威映射（调用方提供，查 tenants 表）；
    3. 网关注入的 ``X-Tenant-Id`` — **默认完全不采信**，见下文。

域标识是**完整 Host**（如 ``acme.example.com``），不是剥出来的子域前缀：
APISIX 在剥离客户端同名头后注入 ``ngx.var.host``，后端直接读 Host 时得到的
也是同一个值。``tenant_domain_to_id`` 的实现应按完整域名匹配 tenants 表；
若需要子域前缀，用 ``parse_tenant_subdomain(host, root_domain)`` 显式转换。

**隐藏前提**：``tenant_domain_to_id`` 目前没有调用方传入（中间件保持无 DB 访问），
所以第 2 条在生产里尚未生效 —— 单租户部署必须靠 JWT 的 tenant_id，
或显式打开网关注入通道。这一点在 docs/P0-multi-tenant-isolation-design.md 有记录。

## 为什么默认不信任请求头

``X-Tenant-Id`` 和 ``X-Tenant-Domain`` 都是普通 HTTP 头，客户端可以任意构造。
此前 resolve_tenant_id 的兜底顺序是「JWT 失败 → 直接采信 X-Tenant-Id」，
于是只要后端能被直连（k8s 内网、运维 curl、绕过网关的服务），
一个 ``X-Tenant-Id: <victim-uuid>`` 就能把 RLS 的 GUC 设成受害租户。
RLS fail-closed 在这里帮不上忙 —— 它忠实执行了被伪造的身份。

APISIX 会剥离客户端自带的租户头，但那是**部署层**保证，不能作为代码层的安全前提：
- 后端在 k8s 内网可直接访问；
- 新增路由容易漏配 serverless-pre-function；
- 单测/TestClient 完全不经过网关。

所以默认安全侧是「只认 JWT」。若部署确实需要网关按子域注入租户 ID，
必须显式打开 ``TRUST_TENANT_ID_HEADER`` 并配置 ``TENANT_GATEWAY_TOKEN``：
网关在剥离客户端头之后，同时注入共享标记头 ``X-Gateway-Trust``，
后端用 ``hmac.compare_digest`` 比对通过才采信 ``X-Tenant-Id``。
这个开关默认关闭，打开前必须确保后端不对外暴露（否则等于把洞放回来）。

## JWT 解析失败必须 fail-closed

旧实现 ``except Exception: pass`` 之后继续落到 header 分支，理由是
「无效 JWT 交由 get_current_user 返回 401」。但公开端点没有认证依赖，
根本不会有 401 —— 结果是无效 JWT 反而拿到了 header 指定的租户身份。
现在解析失败直接返回 None，无身份即无 GUC，RLS 拒绝所有行。
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import Request

from app.config import Settings

# 网关注入的租户 ID 头（与 APISIX 配置一致）
TENANT_ID_HEADER = "X-Tenant-Id"
TENANT_DOMAIN_HEADER = "X-Tenant-Domain"
# 网关可信标记头：由 APISIX 在剥离客户端同名头之后注入
TENANT_GATEWAY_TOKEN_HEADER = "X-Gateway-Trust"


def get_tenant_id_header() -> str:
    """返回网关注入租户 ID 的请求头名（对齐 config.TENANT_DOMAIN_HEADER 语义）。"""
    return TENANT_ID_HEADER


def parse_tenant_subdomain(host: str | None, root_domain: str | None) -> str | None:
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

    注意：该头同样可被客户端伪造，调用方**不得**直接据此授权。
    它只作为语义标识传给 ``tenant_domain_to_id`` 做权威 DB 映射。

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


def _parse_uuid(value: Any) -> uuid.UUID | None:
    """尽力把值转成 UUID；非法值返回 None（绝不把异常抛进请求链路变成 500）。"""
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _trusted_gateway_tenant_id(
    request: Request, settings: Settings
) -> uuid.UUID | None:
    """仅在显式开启 + 网关共享标记校验通过时，采信 X-Tenant-Id。

    ``hmac.compare_digest`` 避免时序侧信道；标记头为空或配置缺失时
    直接拒绝，不做任何降级。
    """
    # 严格类型判断：配置对象可能是测试里的 MagicMock，其任意属性都为 truthy。
    # 不是明确的 True / 非空字符串一律按「未开启」处理，避免 Mock 意外放行。
    if getattr(settings, "TRUST_TENANT_ID_HEADER", False) is not True:
        return None

    token_cfg = getattr(settings, "TENANT_GATEWAY_TOKEN", "")
    if not isinstance(token_cfg, str) or not token_cfg.strip():
        return None
    expected = token_cfg.strip()

    header_cfg = getattr(settings, "TENANT_GATEWAY_TOKEN_HEADER", "")
    header_name = (
        header_cfg.strip()
        if isinstance(header_cfg, str) and header_cfg.strip()
        else TENANT_GATEWAY_TOKEN_HEADER
    )
    presented = (request.headers.get(header_name, "") or "").strip()
    if not presented or not hmac.compare_digest(presented, expected):
        return None

    return _parse_uuid(request.headers.get(TENANT_ID_HEADER))


def resolve_tenant_id(
    request: Request,
    settings: Settings,
    tenant_domain_to_id: Any | None = None,
) -> tuple[uuid.UUID | None, str | None]:
    """解析请求对应的租户 ID 与租户域标识。

    Args:
        request: FastAPI 请求。
        settings: 应用配置。
        tenant_domain_to_id: 可选的**同步** ``domain -> UUID`` 映射
            （如 ``lambda domain: repo.get_by_domain_sync(domain)``），
            用于子域 → 租户 ID 的**权威**解析；为 None 时跳过子域解析。
            实现必须查 ``tenants`` 表（该表无 RLS），不能信任请求头本身。
            中间件保持无 DB 访问，当前未传入该参数。

    Returns:
        ``(tenant_id, tenant_domain)``；均可能为 None。
        tenant_id 为 None 时调用方不得设置 RLS GUC —— fail-closed 会拒绝所有行。
    """
    from app.utils.crypto import decode_access_token

    tenant_domain = extract_domain_from_request(request)

    # 1) JWT：唯一默认可信来源。解析失败立即 fail-closed，绝不降级到请求头。
    auth_header = request.headers.get("authorization", "")
    token = (
        auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""
    )
    if token:
        try:
            payload = decode_access_token(token)
        except Exception:
            return None, tenant_domain
        return _parse_uuid(payload.get("tenant_id")), tenant_domain

    # 2) 子域 → 权威 DB 映射（调用方显式提供才生效）
    if tenant_domain and tenant_domain_to_id is not None:
        resolved = _parse_uuid(tenant_domain_to_id(tenant_domain))
        if resolved is not None:
            return resolved, tenant_domain

    # 3) 网关注入的 X-Tenant-Id：默认不采信，需显式开关 + 共享标记
    return _trusted_gateway_tenant_id(request, settings), tenant_domain
