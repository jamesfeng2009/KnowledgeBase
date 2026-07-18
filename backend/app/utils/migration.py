"""
Alembic 迁移运行器 — 单一职责：以编程方式执行 Alembic 迁移。

将 alembic 命令行调用封装为 Python 函数，便于在 FastAPI lifespan 中调用。
不依赖子进程，直接使用 Alembic Python API。

使用方式::

    from app.utils.migration import run_migrations

    # 升级到最新版本
    run_migrations()

    # 升级到指定版本
    run_migrations(revision="abc123")

    # 回滚一个版本
    run_migrations(revision="-1")
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def _get_alembic_config() -> Config:
    """构建 Alembic Config — 定位 alembic.ini 并注入 DATABASE_URL。

    定位策略：
        1. 从本文件向上查找 backend/ 目录下的 alembic.ini；
        2. 找到后设置 prepend_sys_path 确保能导入 app 包；
        3. 从环境变量 DATABASE_URL 读取连接字符串（与 app.config 一致）。
    """
    # 定位 alembic.ini — backend/alembic.ini
    current = Path(__file__).resolve().parent  # app/utils/
    for _ in range(5):
        candidate = current / "alembic.ini"
        if candidate.exists():
            ini_path = str(candidate)
            break
        current = current.parent
    else:
        raise FileNotFoundError("无法定位 alembic.ini，请在 backend/ 目录下运行")

    config = Config(ini_path)

    # 确保 alembic 能导入 app 包
    backend_dir = str(Path(ini_path).parent)
    config.set_main_option("prepend_sys_path", backend_dir)

    # 从环境变量读取 DATABASE_URL（与 app.config.Settings 一致）
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        config.set_main_option("sqlalchemy.url", db_url)

    return config


def run_migrations(revision: str = "head") -> str:
    """执行 Alembic 迁移 — 默认升级到最新版本。

    Args:
        revision: 目标版本号，默认 "head"（最新）。
                  支持相对值如 "-1"（回滚一步）。

    Returns:
        执行结果摘要字符串。

    Raises:
        RuntimeError: 迁移执行失败时抛出，包含原始异常信息。
    """
    try:
        config = _get_alembic_config()
        command.upgrade(config, revision)
        return f"alembic upgrade -> {revision} 成功"
    except Exception as exc:
        raise RuntimeError(f"alembic 迁移失败 (target={revision}): {exc}") from exc


def get_current_revision() -> str | None:
    """获取当前数据库中的 Alembic 版本号。

    Returns:
        当前版本号字符串，未迁移过返回 None。
    """
    try:
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine

        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            return None

        # alembic stamp 表使用同步连接查询
        sync_url = db_url.replace("+asyncpg", "+psycopg2").replace(
            "+aiosqlite", ""
        )
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            return context.get_current_revision()
    except Exception:
        return None


def stamp_head() -> str:
    """标记当前数据库为最新版本（不执行 SQL，仅写版本号）。

    适用场景：数据库已通过 create_all 建表，切换到 migration 模式时
    用 stamp 跳过首次迁移。

    Returns:
        执行结果摘要字符串。
    """
    try:
        config = _get_alembic_config()
        command.stamp(config, "head")
        return "alembic stamp -> head 成功"
    except Exception as exc:
        raise RuntimeError(f"alembic stamp 失败: {exc}") from exc
