"""APISIX 租户头清洗门禁 — 防止新增路由漏配导致身份注入口回归。

后端 app/core/tenant_resolver.py 默认只认 JWT，但网关仍必须剥离客户端自带的
租户头：只要有一条路由漏配，X-Tenant-Domain / X-Gateway-Trust 就可能带着
客户端伪造值进入后端（前者影响子域映射与限流维度，后者是跨租户开关的凭据）。

本门禁直接读 infra/apisix/apisix.yml，不启动 APISIX，纯静态校验。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_APISIX_YML = (
    pathlib.Path(__file__).resolve().parents[2] / "infra" / "apisix" / "apisix.yml"
)

_MUST_CLEAR = ("X-Tenant-Id", "X-Tenant-Domain", "X-Gateway-Trust")
_FORBIDDEN_SET = ('set_header("X-Tenant-Id"', 'set_header("X-Gateway-Trust"')


@pytest.fixture(scope="module")
def routes() -> list[dict]:
    assert _APISIX_YML.exists(), f"缺少网关配置: {_APISIX_YML}"
    doc = yaml.safe_load(_APISIX_YML.read_text(encoding="utf-8"))
    return doc["routes"]


def _lua_of(route: dict) -> str:
    funcs = (
        route.get("plugins", {}).get("serverless-pre-function", {}).get("functions", [])
    )
    return "\n".join(funcs)


def test_every_route_has_serverless_pre_function(routes) -> None:
    missing = [r["id"] for r in routes if not _lua_of(r).strip()]
    assert not missing, f"以下路由缺少租户头清洗: {missing}"


@pytest.mark.parametrize("header", _MUST_CLEAR)
def test_every_route_clears_all_tenant_headers(routes, header: str) -> None:
    for route in routes:
        lua = _lua_of(route)
        assert header in lua, f"路由 {route['id']} 未清洗 {header}"


def test_no_route_injects_tenant_id_or_gateway_token(routes) -> None:
    """网关没有权威的 domain → tenant_id 映射，不得注入这两个头。"""
    for route in routes:
        lua = _lua_of(route)
        for forbidden in _FORBIDDEN_SET:
            assert forbidden not in lua, f"路由 {route['id']} 违规注入 {forbidden}"


def test_only_main_route_injects_tenant_domain(routes) -> None:
    for route in routes:
        injects = 'set_header("X-Tenant-Domain"' in _lua_of(route)
        assert injects == (
            route["id"] == "api-main"
        ), f"路由 {route['id']} 的 X-Tenant-Domain 注入行为不符预期"


def test_main_route_injects_host_without_environment(routes) -> None:
    """根域名不能靠 os.getenv：nginx worker 默认清除继承的环境变量。

    旧实现 ``os.getenv("TENANT_ROOT_DOMAIN")`` 在没有 nginx_config.envs 时
    恒返回 nil，于是 X-Tenant-Domain 不注入、limit-req key 退化成空串，
    全站共用一个限流桶。现在直接注入 ngx.var.host，永远有值。
    """
    main = next(r for r in routes if r["id"] == "api-main")
    lua = _lua_of(main)
    assert "{TENANT_ROOT_DOMAIN}" not in lua, "仍残留未渲染的占位符"
    assert "os.getenv" not in lua, "不得依赖 worker 环境变量（nginx 默认清除）"
    assert "ngx.var.host" in lua
    assert 'set_header("X-Tenant-Domain", host' in lua
    assert main["plugins"]["limit-req"]["key"] == "$http_x_tenant_domain"


def test_health_route_is_covered(routes) -> None:
    """旧版 /health* 没有任何清洗，是隐蔽的注入口。"""
    health = next((r for r in routes if r["id"] == "health"), None)
    assert health is not None
    assert _lua_of(health).strip()


def test_compose_does_not_depend_on_root_domain_env() -> None:
    """网关不再读自定义环境变量，compose 里不该残留无效的环境变量声明。"""
    compose = _APISIX_YML.parent / "docker-compose.apisix.yml"
    text = compose.read_text(encoding="utf-8")
    assert (
        "- TENANT_ROOT_DOMAIN=" not in text
    ), "网关已改用 ngx.var.host，不该再声明一个 worker 读不到的变量"
    assert "- APISIX_STAND_ALONE=true" in text
    assert "apisix.yml:/usr/local/apisix/conf/apisix.yaml:ro" in text
