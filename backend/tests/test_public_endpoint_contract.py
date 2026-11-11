"""公开端点契约门禁 — 无认证依赖的路由必须逐项登记并复核。

背景：上一轮把「87 个端点无认证」写进了风险清单，那个数字是错的。
按函数签名 + ``APIRouter(dependencies=...)`` 精确统计，319 个路由函数里
只有 **10 个**无认证依赖（不是 9 个 —— 旧统计把 ``OAuth2PasswordRequestForm``
里的 "oauth2" 当成了 OAuth2 认证依赖，漏掉 ``auth.login``）。

这 10 个端点是对外的全部匿名入口，每个都必须回答三个问题：

1. **租户通道**：要不要挂 ``get_system_db_session``？不挂的话 GUC 从哪来？
2. **签名/凭据校验**：匿名请求凭什么被接受？
3. **限流**：能否被无限刷？

本门禁把答案固化成清单：新增或删除公开端点会让集合比对变红，
从而强制走一次评审，而不是悄悄混进代码里。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_API_DIR = _BACKEND / "app" / "api"
_APISIX_YML = _BACKEND.parent / "infra" / "apisix" / "apisix.yml"

# 认证依赖的特征串。注意 **不要** 用裸 "oauth2"：
# OAuth2PasswordRequestForm 是登录表单，不是认证依赖。
AUTH_PATTERN = re.compile(
    r"get_current_user|get_current_active|require_|verify_token|"
    r"decode_access_token|get_optional_user|api_key|check_permission|"
    r"OAuth2PasswordBearer|HTTPBearer|Security\(",
    re.I,
)
ROUTE_PATTERN = re.compile(
    r"\.(get|post|put|patch|delete|api_route|websocket)\(|@router\."
)


def _public_endpoints() -> set[tuple[str, str]]:
    """AST 扫描出全部无认证依赖的路由函数（相对路径, 函数名）。"""
    found: set[tuple[str, str]] = set()
    for path in sorted(_API_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        router_auth = any(
            isinstance(node, ast.Assign)
            and "APIRouter" in ast.unparse(node.value)
            and AUTH_PATTERN.search(ast.unparse(node))
            for node in tree.body
        )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if not ROUTE_PATTERN.search(decorators):
                continue
            has_auth = (
                router_auth
                or bool(AUTH_PATTERN.search(ast.unparse(node.args)))
                or bool(AUTH_PATTERN.search(decorators))
            )
            if not has_auth:
                found.add((str(path.relative_to(_API_DIR)), node.name))
    return found


#: 公开端点白名单。每项 = (文件, 函数, 租户通道, 凭据/签名, 限流)
PUBLIC_ENDPOINTS = {
    # 文件, 函数, 租户通道, 凭据/签名, 限流
    ("v1/auth.py", "register", "system_session", "REGISTRATION_ENABLED 开关", "网关 + 全局中间件"),
    ("v1/auth.py", "login", "system_session", "密码校验", "网关 + 全局中间件"),
    ("v1/oidc.py", "oidc_login", "no_db", "OIDC state/一次性 code", "网关 + 全局中间件"),
    ("v1/oidc.py", "oidc_callback", "system_session", "OIDC 一次性 code", "网关 + 全局中间件"),
    ("v1/widget.py", "widget_ask", "signed_token", "HMAC widget token", "网关 + 进程内限流"),
    ("v1/external_webhooks.py", "receive_external_webhook", "verified_credential", "HMAC 签名（默认强制）", "网关 + 全局中间件"),
    ("v1/im.py", "wecom_callback_verify", "no_db", "企微 SHA1 签名", "网关 + 全局中间件"),
    ("v1/im.py", "wecom_callback_events", "no_db", "企微 SHA1 签名", "网关 + 全局中间件"),
    ("v1/i18n.py", "locales", "no_db", "纯静态只读", "网关 + 全局中间件"),
    ("v1/i18n.py", "messages", "no_db", "纯静态只读", "网关 + 全局中间件"),
}

_EXPECTED = {(f, fn) for f, fn, *_ in PUBLIC_ENDPOINTS}


def _source(rel_path: str) -> str:
    """相对 backend/ 读源码，如 ``app/api/v1/auth.py``。"""
    return (_BACKEND / rel_path).read_text(encoding="utf-8")


def _function_source(rel_path: str, name: str) -> str:
    """取单个函数源码（含 docstring），避免读到模块里其它函数。"""
    path = _BACKEND / rel_path
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"找不到函数 {rel_path}::{name}")


# ======================================================================
# 一、清单完整性
# ======================================================================


def test_public_endpoint_set_matches_manifest() -> None:
    """新增/删除公开端点必须同步改本清单，并重新回答三个问题。"""
    actual = _public_endpoints()
    assert actual == _EXPECTED, (
        f"公开端点集合变化：新增={sorted(actual - _EXPECTED)} "
        f"移除={sorted(_EXPECTED - actual)}"
    )
    assert len(actual) == 10


def test_manifest_records_all_five_fields() -> None:
    for row in PUBLIC_ENDPOINTS:
        assert len(row) == 5
        assert all(isinstance(x, str) and x.strip() for x in row), row


# ======================================================================
# 二、租户通道
# ======================================================================


@pytest.mark.parametrize(
    ("rel_path", "name"),
    [
        ("app/api/v1/auth.py", "register"),
        ("app/api/v1/auth.py", "login"),
        ("app/api/v1/oidc.py", "oidc_callback"),
    ],
)
def test_login_family_uses_system_session(rel_path: str, name: str) -> None:
    """登录前按邮箱/sub 找用户天生跨租户，必须显式声明系统会话。"""
    src = _function_source(rel_path, name)
    assert "Depends(get_system_db_session)" in src
    assert "Depends(get_db_session)" not in src


def test_oidc_login_has_no_db_session() -> None:
    """只拼授权 URL，不该为公开端点开一条无 GUC 的会话。"""
    src = _function_source("app/api/v1/oidc.py", "oidc_login")
    assert "Depends(" not in src
    assert "OIDCService()" in src


def test_widget_ask_binds_tenant_from_signed_token() -> None:
    """匿名端点唯一可信身份是 HMAC 令牌里的 tenant_id。"""
    src = _function_source("app/api/v1/widget.py", "widget_ask")
    assert "claims.tenant_id" in src
    assert "apply_tenant(db, claims.tenant_id)" in src
    assert "缺少租户上下文" in src  # 旧令牌必须拒绝，不兜底
    # 租户不能来自请求头 / query
    assert "X-Tenant-Id" not in src
    assert "query_params" not in src


def test_widget_token_carries_tenant_id() -> None:
    src = _source("app/services/widget_token_service.py")
    assert '"tenant_id": str(tenant_id)' in src
    assert "tenant_id: str," in src  # create_token 的必需 keyword-only 参数


def test_webhook_tenant_comes_from_verified_credential() -> None:
    """派发的租户必须来自验签命中的凭证行，不能是 query param。"""
    src = _source("app/api/v1/external_webhooks.py")
    assert "_resolve_dispatch_tenant(matched.tenant_id)" in src
    assert "tenant_id=tenant_id_str," in src
    assert "system_scope(db, reason=" in src  # 跨租户查凭证是可审计的
    assert "tenant_id=tenant_hint_str" not in src
    assert "_parse_uuid_safe(request.query_params.get" in src  # 非法值不 500


def test_unsigned_webhook_defaults_to_rejected() -> None:
    from app.config import Settings

    assert Settings().WEBHOOK_ALLOW_UNSIGNED is False
    assert Settings().WEBHOOK_DEFAULT_TENANT_ID == ""


def test_high_risk_audit_endpoints_pass_tenant() -> None:
    """审计服务自建会话，端点必须显式把已认证用户的租户传进去。"""
    src = _source("app/api/v1/observability.py")
    assert src.count("tenant_id=str(user.tenant_id) if user.tenant_id else None") == 3
    src2 = _source("app/services/high_risk_audit_service.py")
    assert src2.count("await self._bind_tenant(session, tenant_id)") == 4


def test_queue_monitor_keeps_injected_tenant_session() -> None:
    """上一轮误判：它用的是依赖注入的 get_db_session，GUC 本来就存在。

    锁住这一点，防止以后有人把它改成裸 async_session_factory 而引入空 GUC。
    """
    src = _source("app/api/v1/queue_monitor.py")
    assert "Depends(get_db_session)" in src
    assert "Depends(get_current_active_user)" in src
    monitor = _source("app/services/queue_monitor_service.py")
    assert "async_session_factory" not in monitor
    assert "session_factory" not in monitor


# ======================================================================
# 三、签名 / 凭据校验
# ======================================================================


@pytest.mark.parametrize(
    "name", ["wecom_callback_verify", "wecom_callback_events"]
)
def test_both_wecom_callbacks_verify_signature(name: str) -> None:
    """旧版 POST 事件回调完全没验签，任何匿名 POST 都能拿 200。"""
    src = _function_source("app/api/v1/im.py", name)
    assert "_verify_wecom_callback(" in src
    assert "msg_signature" in src


def test_wecom_missing_token_is_rejected() -> None:
    """WECOM_BOT_TOKEN 默认为空；空 token 的 sha1 仍可算，必须显式拒绝。"""
    src = _function_source("app/api/v1/im.py", "_verify_wecom_callback")
    assert 'status_code=503' in src or "HTTPException(503" in src
    assert "WECOM_BOT_TOKEN" in src


def test_i18n_endpoints_are_pure_static() -> None:
    for name in ("locales", "messages"):
        src = _function_source("app/api/v1/i18n.py", name)
        assert "Depends(" not in src
        assert "session" not in src.lower()


# ======================================================================
# 四、限流
# ======================================================================


def test_gateway_rate_limit_covers_api_routes() -> None:
    import yaml

    doc = yaml.safe_load(_APISIX_YML.read_text(encoding="utf-8"))
    main = next(r for r in doc["routes"] if r["id"] == "api-main")
    assert main["uri"] == "/api/*"
    limit = main["plugins"]["limit-req"]
    assert limit["rejected_code"] == 429
    assert limit["key"] == "$http_x_tenant_domain"


def test_widget_has_process_rate_limiter() -> None:
    src = _function_source("app/api/v1/widget.py", "widget_ask")
    assert "_rate_limiter.check(" in src
    assert "WidgetRateLimited" in _source("app/api/v1/widget.py")


def test_global_rate_limit_middleware_exists() -> None:
    src = _source("app/middleware.py")
    assert "RATE_LIMIT_ENABLED" in src
    assert "rate_limiter" in src.lower()
