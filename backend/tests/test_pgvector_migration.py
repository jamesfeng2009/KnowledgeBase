"""pgvector 迁移正确性测试。

验证：
1. 列维度取自应用配置（DASHSCOPE_EMBED_DIM，默认 1024），不硬编码 1536。
2. JSONB → vector 回填保留方括号，并按维度筛选，避免整条 UPDATE 中断。
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

    def test_vector_dim_comes_from_app_settings(self, migration_content: str) -> None:
        """维度必须与写入侧 embedder 同源。

        只能读 get_settings()：pydantic-settings 把 .env 解析进 Settings 对象，
        不会回写 os.environ，所以读环境变量会拿到与运行时不同的值。
        """
        assert "get_settings().DASHSCOPE_EMBED_DIM" in migration_content
        assert "VECTOR_DIM = 1536" not in migration_content

    def test_vector_dim_default_matches_config(self) -> None:
        """迁移的默认维度与配置默认值一致（1024），否则空环境建出的列宽不对。"""
        from app.config import Settings

        assert Settings.model_fields["DASHSCOPE_EMBED_DIM"].default == 1024

    def test_vector_dim_used_in_add_column(self, migration_content: str) -> None:
        assert 'vector({VECTOR_DIM})' in migration_content

    def test_jsonb_backfill_keeps_vector_brackets(self, migration_content: str) -> None:
        """回填必须保留方括号。

        pgvector 的文本字面量要求以 '[' 开头（实测
        '0.1,0.2'::vector 报 Vector contents must start with "["），
        所以去括号的写法会让整条 UPDATE 失败；JSONB 数组转 text 本就带括号。
        """
        assert "embedding::text::vector" in migration_content
        assert "regexp_replace" not in migration_content

    def test_jsonb_backfill_filters_by_dimension(self, migration_content: str) -> None:
        """历史行可能来自别的 embedding 模型，维度不齐时 cast 直接报错。"""
        assert "jsonb_typeof(embedding) = 'array'" in migration_content
        assert "jsonb_array_length(embedding) = {VECTOR_DIM}" in migration_content

    def test_migration_creates_extension(self, migration_content: str) -> None:
        assert "CREATE EXTENSION IF NOT EXISTS vector" in migration_content

    def test_migration_creates_index(self, migration_content: str) -> None:
        assert "ix_memory_facts_embedding_vec" in migration_content
        assert "vector_cosine_ops" in migration_content

    def test_orm_column_dim_matches_config(self) -> None:
        """ORM 列宽必须与 embedder 输出同源，否则 INSERT 维度不匹配。"""
        from app.config import get_settings
        from app.models.memory import MemoryFact

        assert MemoryFact.__table__.c.embedding_vec.type.dim == (
            get_settings().DASHSCOPE_EMBED_DIM
        )
