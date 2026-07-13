"""
中间件配置 — 单一职责：注册 CORS 与请求日志中间件。

遵循单一职责：本模块仅负责中间件的注册与配置，
不包含业务逻辑（CORS 策略来自 Settings，日志格式由 structlog 处理）。
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


def setup_middleware(app: FastAPI) -> None:
    """注册所有中间件到 FastAPI 应用实例。

    注册顺序（从外到内）：
    1. CORS — 处理跨域预检请求；
    2. 请求日志 — 记录每个请求的方法、路径、状态码与耗时。

    Args:
        app: FastAPI 应用实例。
    """
    settings = get_settings()

    # --- CORS ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- 请求日志中间件 ---
    @app.middleware("http")
    async def log_request(request: Request, call_next):
        """记录每个 HTTP 请求的方法、路径、状态码与响应耗时。"""
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
