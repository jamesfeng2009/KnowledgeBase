"""
Prometheus 指标 — 多租户可观测规模化（P1）。

单一职责：集中定义与暴露应用运行指标，供 Prometheus 抓取、Grafana 展示与告警。
指标统一带 tenant_id / method / path / status 标签，支撑租户维度分析与告警。

主要指标：
    - http_requests_total          请求计数（method/path/status/tenant）
    - http_request_duration_seconds 请求耗时直方图
    - http_requests_inflight        当前在途请求数
    - llm_usage_tokens_total        LLM token 用量累计（tenant/model/type）
    - tenant_ratelimit_denied_total 租户限流拒绝计数
    - circuit_breaker_state         熔断器状态（0=closed 1=open）
"""

from __future__ import annotations

from typing import Any

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
)
from fastapi import Request
from fastapi.responses import Response

# 规范化 path 标签 — 避免高基数路径（UUID 等）撑爆指标维度。
# 将形如 /xxx/{uuid} 的路径归一化为 /xxx/{id}。
_DYNAMIC_SEGMENT_REPLACER: list[tuple[str, str]] = []


def _normalize_path(path: str) -> str:
    """将路径中的动态段（UUID / 长数字）归一化，控制指标基数。"""
    segments = path.split("/")
    out: list[str] = []
    for seg in segments:
        if len(seg) == 36 and seg.count("-") == 4:  # UUID
            out.append("{id}")
        elif seg.isdigit():
            out.append("{id}")
        else:
            out.append(seg)
    return "/".join(out)


# ---- 指标定义 ----
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求计数",
    ["method", "path", "status", "tenant"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path", "tenant"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
HTTP_INFLIGHT = Gauge("http_requests_inflight", "当前在途请求数", ["tenant"])
LLM_USAGE_TOKENS = Counter(
    "llm_usage_tokens_total",
    "LLM token 用量累计",
    ["tenant", "model", "type"],  # type: input/output
)
TENANT_RATELIMIT_DENIED = Counter(
    "tenant_ratelimit_denied_total",
    "租户限流拒绝计数",
    ["tenant"],
)
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "熔断器状态（0=closed, 1=open）",
    ["name"],
)


def record_http_request(
    method: str, path: str, status: int, duration_s: float, tenant: str
) -> None:
    """记录一次 HTTP 请求指标。"""
    norm_path = _normalize_path(path)
    tenant_label = tenant or "none"
    HTTP_REQUESTS.labels(method, norm_path, str(status), tenant_label).inc()
    HTTP_REQUEST_DURATION.labels(method, norm_path, tenant_label).observe(duration_s)


def record_llm_usage(tenant: str, model: str, kind: str, tokens: int) -> None:
    """记录一次 LLM token 用量。"""
    if tokens <= 0:
        return
    LLM_USAGE_TOKENS.labels(tenant or "none", model, kind).inc(tokens)


def record_tenant_ratelimit_denied(tenant: str) -> None:
    """记录租户限流拒绝。"""
    TENANT_RATELIMIT_DENIED.labels(tenant or "none").inc()


def update_circuit_breaker_state(name: str, is_open: bool) -> None:
    """更新熔断器状态。"""
    CIRCUIT_BREAKER_STATE.labels(name).set(1 if is_open else 0)


def metrics_response() -> Response:
    """返回 Prometheus 文本格式指标。"""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---- 中间件辅助 ----

async def metrics_middleware(request: Request, call_next: Any) -> Any:
    """记录请求指标的可观测中间件（挂载于 main 或 middleware）。

    使用 in-flight gauge 包裹真实处理，确保 429/异常也能记录耗时。
    """
    tenant = str(getattr(request.state, "tenant_id", "") or "")
    method = request.method
    norm_path = _normalize_path(request.url.path)

    HTTP_INFLIGHT.labels(tenant or "none").inc()
    try:
        import time

        start = time.perf_counter()
        response = await call_next(request)
        duration_s = time.perf_counter() - start
    finally:
        HTTP_INFLIGHT.labels(tenant or "none").dec()

    record_http_request(
        method, request.url.path, response.status_code, duration_s, tenant
    )
    return response
