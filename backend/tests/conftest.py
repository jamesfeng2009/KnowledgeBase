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
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import pytest_asyncio

# 必须在 import app.* 之前设置
os.environ.setdefault("SECRET_KEY", "pytest-secret-key-do-not-use-in-prod")


# ---------------------------------------------------------------------------
# 一、pytest 临时根目录隔离
# ---------------------------------------------------------------------------
# 现象：不加 --basetemp 跑本仓全量测试，会冒出近百个 FileExistsError，堆栈落在
# _pytest.tmpdir.TempPathFactory.getbasetemp 的 basetemp.mkdir() 上。
#
# 根因不在项目代码，是「环境 shim + 目录残留」叠加：
#   1. basetemp 默认取 tempfile.gettempdir()/pytest-of-<user>
#      （本机即 /var/folders/.../T/pytest-of-<user>），上一轮跑完并不会被清掉；
#   2. 该运行环境的 sitecustomize 把 shutil.rmtree 换成「移入回收站」，
#      pytest 建目录前的清场 rm_rf() 对这类路径静默失败 →
#      紧接着的 mkdir() 撞上已存在目录 → FileExistsError；
#   3. pytest 只对 "pytest-of-root" 重试一次（换成 "pytest-of-unknown"），
#      而那个目录同样存在，异常于是逃出到每个用例的 fixture。
#
# 处理：导入期把临时根指向一次性的独立目录，mkdir 走「目录不存在」分支即可。
# 显式传了 --basetemp 时不干预（那是调用方明确的选择）。
def _isolate_pytest_temproot() -> None:
    has_explicit_basetemp = any(
        arg == "--basetemp" or arg.startswith("--basetemp=") for arg in sys.argv
    )
    if has_explicit_basetemp or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    root = Path("/tmp")
    if not root.is_dir():  # Windows / 无 /tmp 的环境保持默认行为
        return
    # tempfile.mkdtemp 走 os.mkdir（未被 shim 拦截），目录名保证唯一
    os.environ["PYTEST_DEBUG_TEMPROOT"] = tempfile.mkdtemp(
        prefix="ekb-pytest-root-", dir=str(root)
    )


_isolate_pytest_temproot()


# ---------------------------------------------------------------------------
# 二、测试库地址解析（端口自动探测）
# ---------------------------------------------------------------------------
_FALLBACK_DB_URL = "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb_test"
#: 本仓常见的 PostgreSQL 宿主端口，按优先级探测：
#:   55432 — 手工 `docker run` 起的 ekb-pg 容器（本机当前实际在跑的就是它）
#:   15432 — docker compose（docker-compose.override.yml）的设计值
#: 5432 不列入候选：本机 5432 常被其它项目的 PostgreSQL 占用，误连代价更高；
#: 如果 .env 写的就是 5432，它会作为「首选」被直接探测。
_DB_PORT_CANDIDATES = (55432, 15432)


def _db_url_from_env_file() -> str | None:
    """读 backend/.env 的 DATABASE_URL（不加载 Settings，避免过早实例化配置）。"""
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith("DATABASE_URL="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if value:
            return value
    return None


def _with_dbname(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment)
    )


def _with_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    return urlunsplit(
        (
            parts.scheme,
            f"{userinfo}{sep}{host}:{port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _tcp_alive(url: str, timeout: float = 0.4) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection(
            (parts.hostname or "localhost", parts.port or 5432), timeout=timeout
        ):
            return True
    except OSError:
        return False


def _resolve_test_db_url() -> str:
    """返回可达的测试库 URL — 库名固定 ekb_test，端口按实际存活的 PostgreSQL 探测。

    为什么不让调用方自己改：`.env` 里写的是 compose 设计值（15432），而机器上
    真正在跑的往往是另一个实例（手工起的容器映射 55432）。两者不一致时全量测试
    会一次性产生两百多个 "Connection refused"，而报错本身不提示端口写错了，
    排查成本远高于这里自动探测一次。
    """
    configured = _with_dbname(_db_url_from_env_file() or _FALLBACK_DB_URL, "ekb_test")
    configured_port = urlsplit(configured).port or 5432
    if _tcp_alive(configured):
        return configured
    for port in _DB_PORT_CANDIDATES:
        if port == configured_port:
            continue
        candidate = _with_port(configured, port)
        if _tcp_alive(candidate):
            print(
                f"[conftest] .env 里的 PostgreSQL 端口 {configured_port} 不可达，已自动改用 {port}：\n"
                f"[conftest]   {candidate}\n"
                f"[conftest] 想固定下来：export DATABASE_URL='{candidate}'，"
                f"或把 backend/.env 的 DATABASE_URL 改到该端口。",
                file=sys.stderr,
            )
            return candidate
    print(
        "[conftest] 未探测到可用的 PostgreSQL（已尝试端口："
        + ", ".join(str(p) for p in [configured_port, *_DB_PORT_CANDIDATES])
        + "）。启动方式二选一：\n"
        "[conftest]   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d postgres   # 宿主 15432\n"
        "[conftest]   docker run -d --name ekb-pg -p 55432:5432 \\\n"
        "[conftest]     -e POSTGRES_USER=ekb -e POSTGRES_PASSWORD=ekb -e POSTGRES_DB=ekb pgvector/pgvector:pg16\n"
        "[conftest] 起来后跑 python scripts/dev_env_check.py 复核。",
        file=sys.stderr,
    )
    return configured


if "DATABASE_URL" in os.environ:
    # 显式导出优先：只在不可达时告警，不擅自改写调用方的选择
    if not _tcp_alive(os.environ["DATABASE_URL"]):
        print(
            f"[conftest] 注意：显式指定的 DATABASE_URL 不可达 → {os.environ['DATABASE_URL']}",
            file=sys.stderr,
        )
else:
    os.environ["DATABASE_URL"] = _resolve_test_db_url()
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
    import celery  # noqa: F401

    import celery_app  # noqa: F401  # 项目模块：堵住独立守卫块
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
