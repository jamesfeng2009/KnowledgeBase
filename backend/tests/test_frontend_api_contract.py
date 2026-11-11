"""前端 API 路径 ⇄ 后端路由表契约测试（不依赖数据库）。

对应 P1 里的一整类 bug：**前端写死的路径在后端不存在，页面静默 404**。
本轮用户列出的 6 个 P1 问题里，至少 4 个属于这一类：

    - MCP / Webhook 设置页全 404   → mcp.ts / webhooks.ts 写 ``/api/v1/openapi/*``，
      而后端 ``app.main`` 挂的是 ``/api/openapi/*``（openapi_router 自带 ``/openapi``
      前缀，挂在 ``/api`` 下，没有 v1）；
    - 测试管理模块大面积 404        → testing.ts 调 ``/projects``、``/requirements``、
      ``/reviews/*``，其中 ``getReviewsByCase`` 打的 ``/reviews/case/{id}`` 后端没有，
      真实端点是 ``/cases/{id}/reviews``；
    - IT 工单场景页失败            → tickets.ts 打 ``/api/v1/tickets`` 时后端零实现；
    - API 密钥设置页使用记录        → apikeys.ts 的 ``getApiKeyUsage`` 打
      ``/apikeys/{id}/usage``，后端从未实现该端点。

这些问题的共同点是：**TypeScript 编译能过、astro check 能过、CI 全绿，上线后
页面空白**。所以这里把"前端硬编码路径必须存在于后端路由表"变成可执行的断言。

实现方式：解析 ``frontend/src/lib/apis/*.ts`` 里的
``const BASE = '<path>'`` 与随后的 ``${BASE}/xxx`` / ``BASE + '/xxx'`` 拼接，
归一化后与 ``app.openapi()["paths"]`` 逐一比对。动态段统一归一成 ``{p}``，
query string 与 hash 一律剥掉（路径存在性与 query 无关）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
FRONTEND_LIB = REPO_ROOT / "frontend" / "src" / "lib"
API_DIR = FRONTEND_LIB / "apis"

sys.path.insert(0, str(BACKEND_DIR))

#: 模块级前缀声明，如 ``const BASE = '/api/v1/testing'``
BASE_RE = re.compile(r"^(?:export\s+)?const\s+BASE\s*=\s*['\"]([^'\"]+)['\"]", re.M)
#: 模板字符串 `` `/api/v1/testing/projects/${id}` `` 中 BASE 之后的剩余部分
TPL_RE = re.compile(r"`\$\{BASE\}([^`]*)`")
#: 字符串拼接 `` BASE + '/x' `` / `` BASE + `/x` ``
CAT_RE = re.compile(r"BASE\s*\+\s*['\"`]([^'\"`]*)['\"`]")
#: 直接把 BASE 当请求路径用：``getData<TicketPage>(BASE, params)`` / ``postData(BASE, ...)``
BARE_RE = re.compile(r"\(\s*BASE\s*[,)]")

_TS_INTERP = re.compile(r"\$\{[^}]*\}|<[^>]*>")
_BE_PARAM = re.compile(r"\{[^}]*\}")
_MULTI_SLASH = re.compile(r"/{2,}")


def strip_ts_comments(src: str) -> str:
    """把 TS/JS 源码里的注释替换成空格（保留行号），字符串字面量内部不动。

    为什么需要：注释里常常留着「这里曾写成 /api/v1/openapi」这类说明，
    直接做字符串扫描会把注释误判成真实调用。字符串状态机保证
    ``'http://x'`` 里的 ``//`` 不被当成行注释。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if quote is not None:
            out.append(ch)
            if ch == "\\":  # 转义字符，连同下一个字符一起吞掉
                if i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            if i < n:
                out.extend((" ", " "))
                i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_path(path: str) -> str:
    """把路径归一成只保留静态结构的形态。

    - 剥 query / hash：``/graph/data?x=1`` → ``/graph/data``
    - 动态段归一：``${docId}`` / ``<int:doc_id>`` / ``{doc_id}`` → ``{p}``
    - 折叠重复斜杠、去掉尾部斜杠
    """
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = _TS_INTERP.sub("{p}", path)
    path = _BE_PARAM.sub("{p}", path)
    path = _MULTI_SLASH.sub("/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _iter_api_modules():
    """产出 (文件, 已剥离注释的源码)，只取声明了 ``BASE`` 常量且真的用到 ``BASE`` 的模块。"""
    for f in sorted(API_DIR.glob("*.ts")):
        text = strip_ts_comments(f.read_text(encoding="utf-8"))
        if BASE_RE.search(text):
            yield f, text


def collect_frontend_paths() -> list[tuple[str, int, str]]:
    """收集前端所有硬编码请求路径，返回 ``[(文件名, 行号, 归一化路径), ...]``。"""
    out: list[tuple[str, int, str]] = []
    for f, text in _iter_api_modules():
        base = BASE_RE.search(text).group(1)  # type: ignore[union-attr]
        for rx in (TPL_RE, CAT_RE):
            for m in rx.finditer(text):
                line = text[: m.start()].count("\n") + 1
                out.append((f.name, line, normalize_path(base + m.group(1))))
        for m in BARE_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            out.append((f.name, line, normalize_path(base)))
    return out


@pytest.fixture(scope="module")
def backend_paths() -> set[str]:
    """后端全部路由（归一化后）。"""
    from app.main import app  # 延迟导入：避免收集期就加载整个应用

    return {normalize_path(p) for p in app.openapi()["paths"]}


@pytest.fixture(scope="module")
def frontend_paths() -> list[tuple[str, int, str]]:
    return collect_frontend_paths()


# ---------------------------------------------------------------------------
# 契约：前端每条路径都必须在后端路由表里
# ---------------------------------------------------------------------------


def test_frontend_api_dir_exists() -> None:
    assert API_DIR.is_dir(), f"前端 API 目录不存在：{API_DIR}"


def test_collector_actually_finds_paths(frontend_paths) -> None:
    """收集器自身不能退化成空集 —— 否则下面的断言会「全绿但什么都没测」。"""
    assert len(frontend_paths) >= 50, (
        f"只收集到 {len(frontend_paths)} 条前端路径，收集器可能已失效"
    )
    # 抽查几条本轮 P1 修过的路径，确认解析规则对得上
    paths = {p for _, _, p in frontend_paths}
    assert "/api/openapi/mcp/tools" in paths
    assert "/api/openapi/webhooks/subscriptions" in paths
    assert "/api/v1/testing/projects" in paths
    assert "/api/v1/tickets" in paths


def test_every_frontend_path_exists_in_backend(backend_paths, frontend_paths) -> None:
    """前端请求的每个路径，后端都必须有对应路由。

    失败说明有人加/改了前端调用但没动后端，或后端改了前缀没同步前端 ——
    这正是用户报的「页面全 404」的成因。
    """
    missing = [(f, line, p) for f, line, p in frontend_paths if p not in backend_paths]
    assert not missing, "前端调用了后端不存在的路径：\n" + "\n".join(
        f"  {f}:{line}  {p}" for f, line, p in missing
    )


def test_every_module_base_prefix_is_real(backend_paths) -> None:
    """每个模块的 ``BASE`` 前缀本身也必须落在后端路由的路径层级上。

    上面那条测试靠拼接结果断言，如果某个模块的 BASE 只声明不拼接（或拼法
    不在解析规则内），前缀写错也查不出来。这里单独守一层：BASE 必须是某条
    真实后端路径的前缀（按 / 分段）。
    """
    bad: list[str] = []
    for f, text in _iter_api_modules():
        raw = BASE_RE.search(text).group(1)  # type: ignore[union-attr]
        base = normalize_path(raw)
        hit = any(p == base or p.startswith(base + "/") for p in backend_paths)
        if not hit:
            bad.append(f"{f.name}  BASE={raw}")
    assert not bad, "模块 BASE 前缀在后端不存在：\n" + "\n".join(f"  {b}" for b in bad)


# ---------------------------------------------------------------------------
# 逐条回归：本轮 P1 修过的具体路径
# ---------------------------------------------------------------------------


def _module_base(filename: str) -> str:
    text = (API_DIR / filename).read_text(encoding="utf-8")
    m = BASE_RE.search(text)
    assert m, f"{filename} 里找不到 BASE 常量"
    return m.group(1)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # openapi_router 自带 /openapi 前缀挂载在 /api 下 —— 没有 v1
        ("mcp.ts", "/api/openapi/mcp"),
        ("webhooks.ts", "/api/openapi/webhooks"),
    ],
)
def test_openapi_modules_use_v1_less_prefix(filename: str, expected: str) -> None:
    """回归：MCP / Webhook 前缀曾写成 ``/api/v1/openapi/*``，两个设置页全 404。"""
    assert _module_base(filename) == expected


def test_no_frontend_reference_to_v1_openapi() -> None:
    """回归：前端任何角落都不该再出现 ``/api/v1/openapi``（注释除外）。"""
    offenders: list[str] = []
    targets = list(FRONTEND_LIB.rglob("*.ts")) + list(
        (REPO_ROOT / "frontend" / "src" / "pages").rglob("*.astro")
    )
    for f in targets:
        if f.name.endswith(".d.ts"):
            continue
        code = strip_ts_comments(f.read_text(encoding="utf-8"))
        for i, line in enumerate(code.splitlines(), 1):
            if "/api/v1/openapi" in line:
                offenders.append(f"{f.relative_to(REPO_ROOT)}:{i}")
    assert not offenders, "仍有地方引用不存在的 /api/v1/openapi：\n" + "\n".join(offenders)


def test_testing_reviews_by_case_uses_real_endpoint() -> None:
    """回归：``getReviewsByCase`` 曾打 ``/reviews/case/{id}``，后端没有该路由。

    真实端点是 ``/testing/cases/{case_id}/reviews``。
    """
    text = strip_ts_comments((API_DIR / "testing.ts").read_text(encoding="utf-8"))
    assert "/cases/${caseId}/reviews" in text, "getReviewsByCase 的路径被改回去了"
    assert "/reviews/case/" not in text, "又出现了不存在的 /reviews/case/{id}"


def test_testing_has_delete_project(backend_paths) -> None:
    """回归：测试管理删除项目 —— 前端有 ``delData``，后端曾无 DELETE 路由。"""
    assert "/api/v1/testing/projects/{p}" in backend_paths


def test_tickets_router_is_mounted(backend_paths) -> None:
    """回归：IT 工单场景页打 ``/api/v1/tickets``，后端曾零实现。"""
    assert "/api/v1/tickets" in backend_paths
    assert "/api/v1/tickets/{p}" in backend_paths


def test_api_key_usage_phantom_endpoint_removed() -> None:
    """回归：``getApiKeyUsage`` 打的 ``/apikeys/{id}/usage`` 后端从未存在。"""
    text = strip_ts_comments((API_DIR / "apikeys.ts").read_text(encoding="utf-8"))
    assert "getApiKeyUsage" not in text, "又加回了打不存在端点的 getApiKeyUsage"
    assert "/usage" not in text


def test_no_frontend_module_calls_unversioned_apikeys_usage(backend_paths) -> None:
    """反向兜底：除了上面删掉的，任何模块都不该再指向 ``/apikeys/{p}/usage``。"""
    assert "/api/v1/apikeys/{p}/usage" not in backend_paths, (
        "后端补上了 usage 端点 —— 请把 apikeys.ts 的调用与 settings/api.astro 面板改回来"
    )


# ---------------------------------------------------------------------------
# HTTP 方法也必须对得上 —— 只对路径不对方法，线上是 405 不是 404
# ---------------------------------------------------------------------------

#: 前端请求 helper → HTTP 方法
_HELPER_METHODS = {
    "getData": "GET",
    "postData": "POST",
    "putData": "PUT",
    "delData": "DELETE",
    "patchData": "PATCH",
}

#: ``getData<T>(`${BASE}/x`)`` 形式的方法调用
_METHOD_CALL_RE = re.compile(
    r"\b(getData|postData|putData|delData|patchData)\s*(?:<[^>]*>)?\s*\(\s*`\$\{BASE\}([^`]*)`"
)


@pytest.fixture(scope="module")
def backend_operations() -> dict[str, set[str]]:
    """后端路由 → 该路径支持的 HTTP 方法集合（均已归一化）。"""
    from app.main import app

    return {
        normalize_path(p): {m.upper() for m in ops}
        for p, ops in app.openapi()["paths"].items()
    }


def test_frontend_http_methods_match_backend(backend_operations) -> None:
    """前端调用用的 HTTP 方法，后端该路径必须支持。

    路径对、方法不对，线上同样是坏的 —— 只是错误码从 404 变成 405，
    页面表现一样是「功能不可用」。
    """
    mismatches: list[str] = []
    for f, text in _iter_api_modules():
        base = BASE_RE.search(text).group(1)  # type: ignore[union-attr]
        for m in _METHOD_CALL_RE.finditer(text):
            method = _HELPER_METHODS[m.group(1)]
            path = normalize_path(base + m.group(2))
            line = text[: m.start()].count("\n") + 1
            ops = backend_operations.get(path)
            if ops is None:
                continue  # 路径缺失由上面的测试单独报，避免重复噪音
            if method not in ops:
                mismatches.append(
                    f"  {f.name}:{line}  {method} {path}  后端只支持 {sorted(ops)}"
                )
    assert not mismatches, "前后端 HTTP 方法不一致：\n" + "\n".join(mismatches)
