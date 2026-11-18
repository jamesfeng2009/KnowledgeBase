"""add documents.file_hash (A14 reindex reparse decision)

Revision ID: 9f8e7d6c5b4a
Revises: e8f9a0b1c2d3
Create Date: 2026-11-18 12:00:00.000000

A14 修复：重索引不重解析 —— ``_parse_document`` 见 content_text 非空即返回，
文件更新后 reindex 仍用旧文本。新增源文件 SHA-256 哈希列，解析成功时回填，
重索引时比对当前文件哈希决定是否重解析。

content_hash 是"解析后纯文本"的哈希（查重用），无法感知源文件变化，
故必须独立一列存"源文件字节"哈希。nullable：历史文档为 NULL 时首次
重索引会重解析一次并回填（安全侧）。

手写迁移（本仓约定：autogenerate 只用于"看差异"，见 docs/migrations.md）。
"""

from alembic import op

revision: str = "9f8e7d6c5b4a"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64)"
    )
    op.execute(
        "COMMENT ON COLUMN documents.file_hash IS "
        "'源文件 SHA-256 哈希（重索引重解析判定）'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS file_hash")