"""
日志模块 — 单一职责：基于 structlog 提供结构化日志。

遵循单一职责：本模块只负责日志配置与获取 logger，不包含任何业务逻辑。
遵循依赖倒置：日志级别与渲染方式由 app.config.Settings.DEBUG 决定，不在此硬编码。
调试约定：后端禁用 print，所有日志统一走 structlog。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config import get_settings

_initialized = False


def _shared_processors() -> list[Any]:
    """structlog 与 stdlib logging 共享的预处理链。"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging() -> None:
    """配置 structlog + stdlib logging。

    - DEBUG=True：开发环境，控制台彩色输出（ConsoleRenderer）。
    - DEBUG=False：生产环境，JSON 格式输出（JSONRenderer）。
    - 第三方库日志（uvicorn / sqlalchemy 等）经 ProcessorFormatter 同样被结构化。
    幂等：重复调用不会重复注册 handler。
    """
    global _initialized
    if _initialized:
        return

    settings = get_settings()
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True)
        if settings.DEBUG
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=_shared_processors()
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers[:] = [handler]
    root_logger.setLevel(log_level)

    _initialized = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """返回配置好的 structlog logger。

    所有日志输出统一走 structlog，禁止使用 print。

    用法::

        from app.utils.logger import get_logger

        log = get_logger(__name__)
        log.info("user.login", user_id=42)
    """
    configure_logging()
    return structlog.get_logger(name)
