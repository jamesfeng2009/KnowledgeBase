"""
Alembic 迁移环境 — 异步引擎 + 自动导入所有 ORM 模型。

配置要点：
    1. 从 app.config 读取 DATABASE_URL，不硬编码连接字符串；
    2. 异步引擎运行 migration（SQLAlchemy 2.0 + asyncpg）；
    3. 导入 app.models 包，确保所有 ORM 类注册到 Base.metadata；
    4. autogenerate 对比 Base.metadata 与 DB 实际 schema 生成差异。

使用方式::

    # 生成首版 migration
    alembic revision --autogenerate -m "init schema"

    # 升级到最新版本
    alembic upgrade head

    # 回滚一个版本
    alembic downgrade -1

    # 查看当前版本
    alembic current

    # 查看历史
    alembic history
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 导入 app 包 — 确保 Base.metadata 包含所有 ORM 模型
from app.config import get_settings
from app.models import Base  # noqa: F401 — 导入触发所有模型注册
# 下面两个模块里的表定义在 app/models 包之外，不导入会让 autogenerate 认为
# "库里有表、模型没有" → 生成 DROP TABLE eval_results / tool_audit_log。
import app.eval.repository  # noqa: F401,E402 — EvalResultRecord
import app.models.tool_audit  # noqa: F401,E402 — ToolAuditLog

from alembic_drift import include_object  # noqa: E402 — backend/alembic_drift.py

config = context.config

# 迁移用 MIGRATION_DATABASE_URL（表 owner / superuser），而非运行时的 DATABASE_URL。
# 运行时角色刻意不是 superuser —— PG 对 superuser 无条件绕过 RLS，
# 一旦让应用用 superuser 连库，多租户行级隔离就完全失效（详见
# docs/P0-multi-tenant-isolation-design.md 第八章）。
# 两者未分离时 migration_database_url 回退为 DATABASE_URL，行为与旧版一致。
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.migration_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata — autogenerate 对比基准
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式 — 生成 SQL 脚本不连接数据库。

    使用方式::
        alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # 比较列类型变化
        compare_server_default=True,  # 比较 server_default 变化
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在已有连接上执行 migration。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # 迁移是 schema 权威：库里已有、模型未声明的索引/外键/唯一约束不参与
        # autogenerate 比较，避免"生成即删 84 个索引 + 33 个外键"。详见 alembic_drift.py
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式 — 使用 asyncpg 连接 PostgreSQL 执行 migration。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式 — 连接数据库执行 migration（异步引擎）。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
