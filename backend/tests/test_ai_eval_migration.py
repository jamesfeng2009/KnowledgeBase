"""AI 评测表迁移正确性测试。

覆盖：
1. 2026_07_24 迁移应创建 ai_eval_rag_datasets/queries/results 三张表。
2. 2026_07_27 迁移不应再尝试给 ai_eval_rag_queries 添加 deleted_at
   （该列已在 2026_07_24 中创建）。
3. 2026_07_27 迁移仍需给 doc_parse_cases 与 judge_cases 添加 deleted_at。
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestAiEvalRagMigration:
    """验证 RAG 评测表在 2026_07_24 迁移中被正确创建。"""

    @pytest.fixture
    def migration_2026_07_24(self) -> str:
        backend = Path(__file__).resolve().parent.parent
        migration_path = (
            backend
            / "alembic"
            / "versions"
            / "2026_07_24_1000-e1f2a3b4c5d6_add_doc_parse_and_judge_eval_tables.py"
        )
        assert migration_path.exists(), "2026_07_24 迁移文件应存在"
        return migration_path.read_text()

    @pytest.fixture
    def migration_2026_07_27(self) -> str:
        backend = Path(__file__).resolve().parent.parent
        migration_path = (
            backend
            / "alembic"
            / "versions"
            / "2026_07_27_1000-f2a3b4c5d6e7_add_soft_delete_to_ai_eval_cases.py"
        )
        assert migration_path.exists(), "2026_07_27 迁移文件应存在"
        return migration_path.read_text()

    def test_rag_datasets_created(self, migration_2026_07_24: str) -> None:
        assert 'op.create_table(\n        "ai_eval_rag_datasets"' in migration_2026_07_24

    def test_rag_queries_created_with_deleted_at(self, migration_2026_07_24: str) -> None:
        assert 'op.create_table(\n        "ai_eval_rag_queries"' in migration_2026_07_24
        # 软删除列应在建表时就存在
        rag_section = migration_2026_07_24.split(
            'op.create_table(\n        "ai_eval_rag_queries"'
        )[1].split('op.create_table(\n        "ai_eval_rag_results"')[0]
        assert '"deleted_at"' in rag_section

    def test_rag_results_created(self, migration_2026_07_24: str) -> None:
        assert 'op.create_table(\n        "ai_eval_rag_results"' in migration_2026_07_24

    def test_downgrade_drops_rag_tables(self, migration_2026_07_24: str) -> None:
        assert 'op.drop_table("ai_eval_rag_results")' in migration_2026_07_24
        assert 'op.drop_table("ai_eval_rag_queries")' in migration_2026_07_24
        assert 'op.drop_table("ai_eval_rag_datasets")' in migration_2026_07_24

    def test_2026_07_27_does_not_touch_rag_queries(
        self, migration_2026_07_27: str
    ) -> None:
        """2026_07_27 不应再给 ai_eval_rag_queries 添加 deleted_at。"""
        assert "ai_eval_rag_queries" not in migration_2026_07_27

    def test_2026_07_27_adds_deleted_at_to_other_case_tables(
        self, migration_2026_07_27: str
    ) -> None:
        assert "ai_eval_doc_parse_cases" in migration_2026_07_27
        assert "ai_eval_judge_cases" in migration_2026_07_27
        assert "deleted_at" in migration_2026_07_27
