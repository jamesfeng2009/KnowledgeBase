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
    """应用生命周期管理。

    启动时：
        - 配置结构化日志；
        - 执行 Alembic 迁移（AUTO_MIGRATE=True 时，默认开启）。
          自动运行 `alembic upgrade head`，确保 schema 与模型一致。
        - 兼容旧逻辑：AUTO_CREATE_TABLES=True 时直接 create_all（仅 demo）。
    关闭时：
        - 记录停止日志。
    """
    configure_logging()
    log.info("app.starting", app_name=settings.APP_NAME, version=settings.APP_VERSION)

    # Alembic 迁移 — 生产级 schema 管理
    if settings.AUTO_MIGRATE:
        try:
            from app.utils.migration import run_migrations

            # 迁移在同步线程中执行（alembic 内部使用同步 SQLAlchemy）
            import asyncio

            result = await asyncio.to_thread(run_migrations, "head")
            log.info("app.migration_done", result=result)
        except Exception as exc:
            log.warning("app.migration_failed", error=str(exc))

    # 兼容旧逻辑 — 直接 create_all（不经过 migration，仅 demo 快速启动）
    if settings.AUTO_CREATE_TABLES:
        try:
            from app.database import engine
            from app.models import Base  # noqa: F401 — 触发所有模型注册

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            log.info("app.tables_created")
        except Exception as exc:
            log.warning("app.table_creation_failed", error=str(exc))

    # P1-5: 服务重启恢复 — 扫描 pending 审批，标记过期，加载活跃审批
    try:
        from app.database import async_session_factory
        from app.services.approval_service import ApprovalService

        async with async_session_factory() as session:
            approval_service = ApprovalService(session)
            restored_count = await approval_service.restore_pending_approvals()
            await session.commit()
            if restored_count > 0:
                log.info(
                    "app.approval_restored",
                    active_count=restored_count,
                )
    except Exception as exc:
        log.warning("app.approval_restore_failed", error=str(exc))

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
    """健康检查端点 — 返回服务运行状态（不触 DB，用于存活探针）。"""
    return ApiResponse(
        code=0,
        data={"status": "ok", "app": settings.APP_NAME},
        message="success",
    )


@app.get("/health/db", response_model=ApiResponse, tags=["系统"])
async def health_check_db() -> ApiResponse:
    """数据库健康检查 — 触 DB 连接，用于就绪探针。

    docker-compose 的 core-engine healthcheck 使用此端点，
    确保表已创建且 DB 连接正常后，才让 frontend 依赖启动。
    """
    from sqlalchemy import text

    from app.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return ApiResponse(
            code=0,
            data={"status": "ok", "database": "connected"},
            message="success",
        )
    except Exception as exc:
        log.error("health.db_failed", error=str(exc))
        return ApiResponse(
            code=500,
            data={"status": "error", "database": str(exc)},
            message="database connection failed",
        )
