"""测试公共夹具 — 确保环境变量在 app 模块导入前就位。

关键点：
    - SECRET_KEY 必须在导入 app.utils.crypto 前设置，否则 AES-GCM 密钥派生
      会因默认值不稳定而失败（项目要求 SECRET_KEY 必须显式配置）。
    - DATABASE_URL 指向 PostgreSQL（项目硬约束：禁止 SQLite，含测试环境）。
      本批单测全部通过 mock 隔离 DB，不会真实连接，但 env 仍需合法。
    - AUTO_MIGRATE=false 避免单测触发迁移。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

# 必须在 import app.* 之前设置
os.environ.setdefault("SECRET_KEY", "pytest-secret-key-do-not-use-in-prod")


def _default_test_db_url() -> str:
    """推导测试库 URL — 与 backend/.env 的开发库同主机/端口/凭据，库名固定 ekb_test。

    真实连接型测试（如幂等并发验证）需要可达的 PostgreSQL；
    端口等随环境变化，硬编码 5432 会在开发库跑在非标准端口时认证失败。
    """
    fallback = "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb_test"
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return fallback
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("DATABASE_URL="):
            continue
        url = line.split("=", 1)[1].strip().strip('"').strip("'")
        base, _, dbname_qs = url.rpartition("/")
        dbname = dbname_qs.split("?", 1)[0]
        if base and dbname:
            return f"{base}/ekb_test"
    return fallback


os.environ.setdefault("DATABASE_URL", _default_test_db_url())
os.environ.setdefault("AUTO_MIGRATE", "false")
os.environ.setdefault("AUTO_CREATE_TABLES", "false")
# OpenAI SDK v1 在 api_key 为空时直接抛 OpenAIError — 构造 OpenAIEmbedder/
# AsyncOpenAI 的单测（熔断器/故障注入/Provider 池）需要非空 key 才能实例化。
# 本地未配置 OPENAI_API_KEY 时注入 dummy 值（仅测试进程，不发起真实请求）。
os.environ.setdefault("OPENAI_API_KEY", "pytest-dummy-key-not-real")

# 预导入真实 celery — celery 是项目硬依赖（requirements.txt），各测试文件的
# "if 'celery' not in sys.modules" mock 守卫在此之后全部短路，避免首个测试
# 文件把 mock celery_app 塞进 sys.modules 污染后续按字母序导入的任务模块
# （patrol/rescan 等 Celery 任务测试在全量运行时拿到 MagicMock 的根因）。
# 无 celery 的环境下此处静默跳过，各文件守卫回退为 mock 行为。
try:
    import celery  # noqa: F401,E402
    import celery_app  # noqa: F401,E402  # 项目模块：堵住独立守卫块
except ImportError:  # pragma: no cover - 环境缺 celery 时回退
    pass


@pytest.fixture
def mock_user():
    """认证用户替身 — 被 api_endpoints / api_service_security_fixes 等复用。"""
    return SimpleNamespace(
        id=uuid4(),
        email="test@ekb.local",
        name="测试用户",
        role="editor",
        clearance_level="internal",
        dept_id=None,
        is_active=True,
    )


@pytest_asyncio.fixture
async def db_session():
    """PostgreSQL 测试会话 — 供缺省定义的测试文件使用（同 test_analytics 模式）。

    各测试文件可自定义同名 fixture 覆盖本实现（本地优先）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.base import Base

    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        # 先 drop 再 create — 清理前次测试残留数据，保证隔离
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()
