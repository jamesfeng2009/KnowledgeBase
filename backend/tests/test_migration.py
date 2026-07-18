"""
Alembic 迁移 + Pydantic V2 校验测试。

测试覆盖：
    1. Pydantic V2 field_validator — 结构性校验硬失败；
    2. Pydantic V2 model_validator — 运营性校验发 warning；
    3. Alembic 迁移文件存在性 + 可导入性；
    4. 迁移 runner（run_migrations）在 SQLite 上端到端执行。
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


# ============================================================
# Pydantic V2 field_validator 测试
# ============================================================


class TestDatabaseUrlValidator:
    """DATABASE_URL 校验 — 必须使用异步驱动。"""

    def test_valid_postgresql_asyncpg(self):
        """postgresql+asyncpg 驱动通过校验。"""
        s = Settings(DATABASE_URL="postgresql+asyncpg://u:p@localhost/db")
        assert s.DATABASE_URL.startswith("postgresql+asyncpg://")

    def test_valid_sqlite_aiosqlite(self):
        """sqlite+aiosqlite 驱动通过校验。"""
        s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db")
        assert s.DATABASE_URL.startswith("sqlite+aiosqlite://")

    def test_invalid_sync_postgresql_rejected(self):
        """同步 postgresql:// 驱动被拒绝。"""
        with pytest.raises(ValidationError, match="异步驱动"):
            Settings(DATABASE_URL="postgresql://u:p@localhost/db")

    def test_invalid_mysql_rejected(self):
        """MySQL 驱动被拒绝。"""
        with pytest.raises(ValidationError, match="异步驱动"):
            Settings(DATABASE_URL="mysql+pymysql://u:p@localhost/db")

    def test_invalid_sqlite_sync_rejected(self):
        """同步 sqlite:// 驱动被拒绝。"""
        with pytest.raises(ValidationError, match="异步驱动"):
            Settings(DATABASE_URL="sqlite:///test.db")


class TestPositiveIntValidator:
    """正整数校验 — 零/负数硬失败。"""

    @pytest.mark.parametrize("field", [
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "RATE_LIMIT_PER_MINUTE",
        "RATE_LIMIT_BURST",
        "SKILL_MAX_LOADED",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "RAG_RETRIEVE_TOP_K",
        "RAG_RERANK_TOP_K",
        "RAG_MAX_ITERATIONS",
    ])
    def test_zero_rejected(self, field):
        """零值被拒绝。"""
        with pytest.raises(ValidationError, match="正整数"):
            Settings(**{field: 0})

    @pytest.mark.parametrize("field", [
        "DATABASE_POOL_SIZE",
        "RATE_LIMIT_PER_MINUTE",
    ])
    def test_negative_rejected(self, field):
        """负值被拒绝。"""
        with pytest.raises(ValidationError, match="正整数"):
            Settings(**{field: -5})

    def test_valid_positive_passes(self):
        """正整数通过校验。"""
        s = Settings(DATABASE_POOL_SIZE=20, RATE_LIMIT_PER_MINUTE=100)
        assert s.DATABASE_POOL_SIZE == 20
        assert s.RATE_LIMIT_PER_MINUTE == 100


class TestNonNegativeIntValidator:
    """非负整数校验 — 允许零，拒绝负数。"""

    def test_zero_allowed(self):
        """零值允许（重试次数可为 0）。"""
        s = Settings(SKILL_MATCH_THRESHOLD=0, RAG_RETRIEVAL_MAX_RETRIES=0)
        assert s.SKILL_MATCH_THRESHOLD == 0

    def test_negative_rejected(self):
        """负值被拒绝。"""
        with pytest.raises(ValidationError, match="非负整数"):
            Settings(SKILL_MATCH_THRESHOLD=-1)


class TestFloat01Validator:
    """[0, 1] 浮点校验。"""

    @pytest.mark.parametrize("field", [
        "RAG_RETRIEVAL_SCORE_THRESHOLD",
        "EVAL_REGRESSION_THRESHOLD",
        "VIDEO_KEYFRAME_SCENE_THRESHOLD",
    ])
    def test_below_zero_rejected(self, field):
        """小于 0 被拒绝。"""
        with pytest.raises(ValidationError, match=r"\[0\.0, 1\.0\]"):
            Settings(**{field: -0.1})

    @pytest.mark.parametrize("field", [
        "RAG_RETRIEVAL_SCORE_THRESHOLD",
        "EVAL_REGRESSION_THRESHOLD",
    ])
    def test_above_one_rejected(self, field):
        """大于 1 被拒绝。"""
        with pytest.raises(ValidationError, match=r"\[0\.0, 1\.0\]"):
            Settings(**{field: 1.5})

    def test_valid_boundaries_pass(self):
        """边界值 0.0 和 1.0 通过。"""
        s = Settings(RAG_RETRIEVAL_SCORE_THRESHOLD=0.0)
        assert s.RAG_RETRIEVAL_SCORE_THRESHOLD == 0.0
        s = Settings(EVAL_REGRESSION_THRESHOLD=1.0)
        assert s.EVAL_REGRESSION_THRESHOLD == 1.0


class TestCorsOriginsValidator:
    """CORS 来源校验 — 必须是合法 URL。"""

    def test_valid_urls_pass(self):
        """合法 URL 通过。"""
        s = Settings(CORS_ORIGINS=["http://localhost:3000", "https://example.com"])
        assert len(s.CORS_ORIGINS) == 2

    def test_invalid_origin_rejected(self):
        """非 URL 字符串被拒绝。"""
        with pytest.raises(ValidationError, match="合法 URL"):
            Settings(CORS_ORIGINS=["not-a-url"])

    def test_missing_scheme_rejected(self):
        """缺少 scheme 的 URL 被拒绝。"""
        with pytest.raises(ValidationError, match="合法 URL"):
            Settings(CORS_ORIGINS=["localhost:3000"])


# ============================================================
# Pydantic V2 model_validator 测试
# ============================================================


class TestDeployModeValidator:
    """部署模式与 API Key 交叉校验 — 仅 warning。"""

    def test_saas_dashscope_without_key_warns(self):
        """saas_dashscope 模式无 DASHSCOPE_API_KEY 发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(DEPLOY_MODE="saas_dashscope", DASHSCOPE_API_KEY="")
            assert any("DASHSCOPE_API_KEY" in str(x.message) for x in w)

    def test_saas_dashscope_with_key_no_warn(self):
        """saas_dashscope 模式有 key 不发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(DEPLOY_MODE="saas_dashscope", DASHSCOPE_API_KEY="sk-xxx")
            dashscope_warns = [x for x in w if "DASHSCOPE_API_KEY" in str(x.message)]
            assert len(dashscope_warns) == 0

    def test_saas_without_any_key_warns(self):
        """saas 模式无任何 API Key 发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(
                DEPLOY_MODE="saas",
                ANTHROPIC_API_KEY="",
                OPENAI_API_KEY="",
                COHERE_API_KEY="",
            )
            assert any("LLM API Key" in str(x.message) for x in w)

    def test_saas_with_key_no_warn(self):
        """saas 模式有 key 不发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(DEPLOY_MODE="saas", OPENAI_API_KEY="sk-xxx")
            key_warns = [x for x in w if "LLM API Key" in str(x.message)]
            assert len(key_warns) == 0


class TestSecretKeyValidator:
    """SECRET_KEY 校验 — 默认值 + 非 DEBUG 发 warning。"""

    def test_default_secret_in_production_warns(self):
        """生产环境使用默认 SECRET_KEY 发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(SECRET_KEY="change-me-in-production", DEBUG=False)
            assert any("SECRET_KEY" in str(x.message) for x in w)

    def test_custom_secret_no_warn(self):
        """自定义 SECRET_KEY 不发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(SECRET_KEY="my-random-secret", DEBUG=False)
            key_warns = [x for x in w if "SECRET_KEY" in str(x.message)]
            assert len(key_warns) == 0

    def test_default_secret_in_debug_no_warn(self):
        """DEBUG 模式下使用默认 SECRET_KEY 不发 warning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(SECRET_KEY="change-me-in-production", DEBUG=True)
            key_warns = [x for x in w if "SECRET_KEY" in str(x.message)]
            assert len(key_warns) == 0


# ============================================================
# Alembic 迁移文件测试
# ============================================================


class TestMigrationFiles:
    """迁移文件存在性 + 可导入性。"""

    @pytest.fixture
    def versions_dir(self) -> Path:
        """定位 alembic/versions 目录。"""
        backend = Path(__file__).resolve().parent.parent
        return backend / "alembic" / "versions"

    def test_versions_dir_exists(self, versions_dir):
        """versions 目录存在。"""
        assert versions_dir.is_dir(), f"versions 目录不存在: {versions_dir}"

    def test_init_migration_exists(self, versions_dir):
        """首版迁移文件存在。"""
        migrations = list(versions_dir.glob("*.py"))
        assert len(migrations) >= 1, "至少应有一个迁移文件"
        # 查找 init schema 迁移
        init_files = [f for f in migrations if "init" in f.name.lower()]
        assert len(init_files) >= 1, "应包含 init schema 迁移"

    def test_migration_has_upgrade_and_downgrade(self, versions_dir):
        """迁移文件包含 upgrade() 和 downgrade() 函数。"""
        init_files = [f for f in versions_dir.glob("*.py") if "init" in f.name.lower()]
        assert init_files, "无 init schema 迁移文件"
        content = init_files[0].read_text()
        assert "def upgrade()" in content, "缺少 upgrade() 函数"
        assert "def downgrade()" in content, "缺少 downgrade() 函数"

    def test_migration_creates_core_tables(self, versions_dir):
        """迁移文件包含核心业务表。"""
        init_files = [f for f in versions_dir.glob("*.py") if "init" in f.name.lower()]
        assert init_files, "无 init schema 迁移文件"
        content = init_files[0].read_text()
        core_tables = [
            "users",
            "knowledge_bases",
            "documents",
            "conversations",
            "messages",
            "tenants",
        ]
        for table in core_tables:
            assert f"'{table}'" in content, f"迁移缺少核心表: {table}"


class TestAlembicEnvConfig:
    """alembic env.py 配置正确性。"""

    def test_env_py_imports_base(self):
        """env.py 导入了 Base 和 app.models。"""
        backend = Path(__file__).resolve().parent.parent
        env_path = backend / "alembic" / "env.py"
        content = env_path.read_text()
        assert "from app.models import Base" in content
        assert "target_metadata = Base.metadata" in content

    def test_env_py_uses_async_engine(self):
        """env.py 使用异步引擎。"""
        backend = Path(__file__).resolve().parent.parent
        env_path = backend / "alembic" / "env.py"
        content = env_path.read_text()
        assert "async_engine_from_config" in content
        assert "asyncio" in content

    def test_env_py_compares_type(self):
        """env.py 启用 compare_type（检测列类型变化）。"""
        backend = Path(__file__).resolve().parent.parent
        env_path = backend / "alembic" / "env.py"
        content = env_path.read_text()
        assert "compare_type=True" in content


# ============================================================
# 迁移 Runner 端到端测试（SQLite）
# ============================================================


class TestMigrationRunner:
    """迁移运行器 — 在临时 SQLite DB 上端到端测试。

    使用 SQLite+aiosqlite 执行 alembic upgrade head，
    验证所有表的 DDL 能正确执行。
    """

    @pytest.fixture
    def temp_sqlite_db(self, tmp_path, monkeypatch):
        """创建临时 SQLite DB 并设置 DATABASE_URL。"""
        db_path = tmp_path / "test_migration.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"
        monkeypatch.setenv("DATABASE_URL", db_url)
        # 同时清除可能存在的旧 DB 文件
        if db_path.exists():
            db_path.unlink()
        yield db_url
        # 清理
        if db_path.exists():
            db_path.unlink()

    def test_run_migrations_creates_tables(self, temp_sqlite_db):
        """run_migrations 在 SQLite 上创建所有表。"""
        from app.utils.migration import run_migrations

        result = run_migrations("head")
        assert "成功" in result

        # 验证表已创建 — 用同步 sqlite 检查
        import sqlite3

        db_file = temp_sqlite_db.replace("sqlite+aiosqlite:///", "")
        conn = sqlite3.connect(db_file)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        # 核心表应存在
        expected = {"users", "knowledge_bases", "documents", "conversations", "messages"}
        assert expected.issubset(tables), f"缺少表: {expected - tables}"

    def test_run_migrations_idempotent(self, temp_sqlite_db):
        """重复执行 run_migrations 不报错（幂等性）。"""
        from app.utils.migration import run_migrations

        run_migrations("head")
        # 再次执行应无异常
        result = run_migrations("head")
        assert "成功" in result

    def test_get_current_revision_after_migrate(self, temp_sqlite_db):
        """迁移后 get_current_revision 返回版本号。"""
        from app.utils.migration import get_current_revision, run_migrations

        run_migrations("head")
        rev = get_current_revision()
        assert rev is not None, "迁移后应有版本号"
        assert len(rev) > 0

    def test_get_current_revision_none_before_migrate(self, temp_sqlite_db):
        """未迁移前 get_current_revision 返回 None。"""
        from app.utils.migration import get_current_revision

        rev = get_current_revision()
        assert rev is None
