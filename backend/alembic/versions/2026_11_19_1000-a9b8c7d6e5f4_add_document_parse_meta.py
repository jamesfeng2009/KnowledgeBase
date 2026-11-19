"""add documents.parse_meta (D8 parse engine/fallback chain traceability)

Revision ID: a9b8c7d6e5f4
Revises: 7a8b9c0d1e2f
Create Date: 2026-11-19 10:00:00.000000

D8 修复：解析路径/质量元数据不落库 —— parser_type（docling/legacy）仅在日志，
「最终走了哪条引擎、是否降级」不可回溯。新增 JSONB 列记录
engine/chain（逐级引擎+结果）/duration_ms/ocr_pages，解析完成时写入，
重索引重解析时刷新。

nullable：历史文档为 NULL，下次重解析时回填（安全侧）。
手写迁移（本仓约定：autogenerate 只用于"看差异"，见 docs/migrations.md）。
"""

from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision = "7a8b9c0d1e2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS parse_meta JSONB"
    )
    op.execute(
        "COMMENT ON COLUMN documents.parse_meta IS "
        "'解析元数据: engine/chain/duration_ms/ocr_pages'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS parse_meta")