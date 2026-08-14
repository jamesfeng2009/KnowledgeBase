"""pgvector 迁移正确性测试。

验证：
1. VECTOR_DIM 从环境变量读取，默认 1024（与项目配置一致），而非硬编码 1536。
2. JSONB embedding 迁移使用 regexp_replace 去掉方括号后再 cast，避免语法错误。
3. 迁移包含创建 vector 列、回填数据、创建索引等必要步骤。
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestPgvectorMigration:
    """验证 2026_07_31 pgvector 迁移。"""

    @pytest.fixture
    def migration_content(self) -> str:
        backend = Path(__file__).resolve().parent.parent
        migration_path = (
            backend
            / "alembic"
            / "versions"
            / "2026_07_31_1000-a3b4c5d6e7f8_add_pgvector_to_memory_facts.py"
        )
        assert migration_path.exists(), "pgvector 迁移文件应存在"
        return migration_path.read_text()

    def test_vector_dim_reads_from_env(self, migration_content: str) -> None:
        """VECTOR_DIM 应从 DASHSCOPE_EMBED_DIM 环境变量读取，默认 1024。"""
        assert 'os.environ.get("DASHSCOPE_EMBED_DIM", "1024")' in migration_content
        assert "VECTOR_DIM = 1536" not in migration_content

    def test_vector_dim_used_in_add_column(self, migration_content: str) -> None:
        assert 'vector({VECTOR_DIM})' in migration_content

    def test_jsonb_migration_removes_brackets(self, migration_content: str) -> None:
        """回填 SQL 应使用 regexp_replace 去掉 JSONB 数组方括号。"""
        assert "regexp_replace(embedding::text" in migration_content
        assert "embedding::text::vector" not in migration_content

    def test_migration_creates_extension(self, migration_content: str) -> None:
        assert "CREATE EXTENSION IF NOT EXISTS vector" in migration_content

    def test_migration_creates_index(self, migration_content: str) -> None:
        assert "ix_memory_facts_embedding_vec" in migration_content
        assert "vector_cosine_ops" in migration_content
