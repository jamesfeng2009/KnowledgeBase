"""D6: document_chunks 资产化字段 — chunk_type / asset_ref / parent_chunk_id

图片/表格成为独立检索单元：chunk_type 标记类型（text/image/table），
asset_ref 存资产引用（图片=minio:// 地址，表格=内联 HTML），
parent_chunk_id 回链来源正文块。存量行回填 chunk_type='text'。

Revision ID: b7c8d9e0f1a2
Revises: a9b8c7d6e5f4
Create Date: 2026-11-19 12:00:00

"""
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN IF NOT EXISTS chunk_type VARCHAR(16) NOT NULL DEFAULT 'text'"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS asset_ref TEXT"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS parent_chunk_id UUID"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_document_chunks_parent'
                  AND conrelid = 'document_chunks'::regclass
            ) THEN
                ALTER TABLE document_chunks
                    ADD CONSTRAINT fk_document_chunks_parent
                    FOREIGN KEY (parent_chunk_id) REFERENCES document_chunks(id);
            END IF;
        END $$
        """
    )
    op.execute(
        "COMMENT ON COLUMN document_chunks.chunk_type IS '分块类型: text/image/table'"
    )
    op.execute(
        "COMMENT ON COLUMN document_chunks.asset_ref IS '资产引用: 图片=minio:// 地址, 表格=内联 HTML'"
    )
    op.execute(
        "COMMENT ON COLUMN document_chunks.parent_chunk_id IS '父块回链（资产块→来源正文块 / 子块→父块）'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS fk_document_chunks_parent"
    )
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS parent_chunk_id")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS asset_ref")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS chunk_type")