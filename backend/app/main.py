"""
FastAPI 应用入口 — 单一职责：创建应用实例、注册中间件与路由、配置异常处理。

遵循分层架构：main.py 不包含业务逻辑，仅负责组装各层组件：
- 中间件 → app.middleware.setup_middleware
- 路由 → app.api.v1.api_router
- 异常处理 → 全局 exception_handler
- 生命周期 → lifespan 上下文管理器
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.openapi import openapi_router
from app.api.v1 import api_router
from app.config import get_settings
from app.middleware import setup_middleware
from app.schemas.common import ApiResponse
from app.utils.logger import configure_logging, get_logger

settings = get_settings()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理 — 启动时初始化日志，关闭时记录日志。"""
    configure_logging()
    log.info("app.starting", app_name=settings.APP_NAME, version=settings.APP_VERSION)
    yield
    log.info("app.stopped", app_name=settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="企业知识库大脑 — 知识管理、AI 问答、协同编辑一体化平台",
    lifespan=lifespan,
)

# --- 中间件 ---
setup_middleware(app)

# --- 异常处理 ---

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """ValueError → 404 Not Found。

    服务层以 ValueError 表示资源不存在（如 "知识库 xxx 不存在"），
    统一翻译为 404 状态码。
    """
    return JSONResponse(
        status_code=404,
        content=ApiResponse(code=404, message=str(exc)).model_dump(),
    )


@app.exception_handler(PermissionError)
async def permission_error_handler(
    request: Request, exc: PermissionError
) -> JSONResponse:
    """PermissionError → 403 Forbidden。

    服务层以 PermissionError 表示权限不足（如 "无权访问该知识库"），
    统一翻译为 403 状态码。
    """
    return JSONResponse(
        status_code=403,
        content=ApiResponse(code=403, message=str(exc)).model_dump(),
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """其他未捕获异常 → 500 Internal Server Error。

    记录完整堆栈日志，对客户端仅返回通用错误信息，
    避免泄露内部实现细节。
    """
    log.error("unhandled.exception", error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content=ApiResponse(code=500, message="内部服务器错误").model_dump(),
    )


# --- 路由 ---
app.include_router(api_router)
app.include_router(openapi_router, prefix="/api")


# --- 健康检查 ---
@app.get("/health", response_model=ApiResponse, tags=["系统"])
async def health_check() -> ApiResponse:
    """健康检查端点 — 返回服务运行状态。"""
    return ApiResponse(
        code=0,
        data={"status": "ok", "app": settings.APP_NAME},
        message="success",
    )
