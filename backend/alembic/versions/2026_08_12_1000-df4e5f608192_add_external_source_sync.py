"""add external source sync fields

P0+P3 外部文档实时同步：Document 表新增 6 个外部来源元数据字段，
新建 external_credentials 表存储加密的平台凭证。

新增字段（documents 表，全部 nullable 向后兼容）：
    - source           来源适配器 ID（feishu/confluence/notion/obsidian）
    - source_doc_id    外部平台文档 ID（飞书 doc_token / Confluence pageId）
    - source_url       原始文档 URL（用于引用展示 + P3 prompt 注入）
    - source_revision  外部版本指纹（飞书 revision_id / Confluence version.number /
                       Notion last_edited_time / Obsidian mtime_ns）
    - last_synced_at   最后拉取全文时间（阶段 B 触发时间）
    - last_checked_at  最后轻量探测时间（阶段 A + 短窗口缓存）

新表 external_credentials：
    - 加密存储各租户的外部平台凭证（AES-GCM，由 app.utils.crypto.encrypt_secret）
    - 唯一约束：(tenant_id, adapter_id) — 每租户每适配器一份凭证
    - 私有部署 tenant_id 为 NULL，由应用层填充

Revision ID: df4e5f608192
Revises: ce3d4e5f7081
Create Date: 2026-08-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'df4e5f608192'
down_revision: Union[str, Sequence[str], None] = 'ce3d4e5f7081'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — documents 表新增外部来源字段 + external_credentials 表。"""
    # === documents 表新增字段（全部 nullable，向后兼容旧文档）===
    op.add_column(
        'documents',
        sa.Column('source', sa.String(length=30), nullable=True,
                  comment='来源适配器 ID: feishu/confluence/notion/obsidian'),
    )
    op.add_column(
        'documents',
        sa.Column('source_doc_id', sa.String(length=200), nullable=True,
                  comment='外部平台文档 ID（飞书 doc_token / Confluence pageId 等）'),
    )
    op.add_column(
        'documents',
        sa.Column('source_url', sa.String(length=500), nullable=True,
                  comment='原始文档 URL（引用展示 + P3 prompt 注入）'),
    )
    op.add_column(
        'documents',
        sa.Column('source_revision', sa.String(length=100), nullable=True,
                  comment='外部版本指纹（飞书 revision_id / Confluence version.number / '
                          'Notion last_edited_time / Obsidian mtime_ns）'),
    )
    op.add_column(
        'documents',
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True,
                  comment='最后拉取全文时间（阶段 B 触发时间）'),
    )
    op.add_column(
        'documents',
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True,
                  comment='最后轻量探测时间（阶段 A + 短窗口缓存）'),
    )

    # === 复合索引：按 source + last_checked_at 查询待校验文档（P2 轮询用）===
    op.create_index(
        'ix_documents_source_sync',
        'documents',
        ['source', 'last_checked_at'],
    )

    # === external_credentials 表 ===
    op.create_table(
        'external_credentials',
        sa.Column('id', sa.UUID(), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        # tenant_id NULL = 私有部署（单租户），由应用层填充
        sa.Column('tenant_id', sa.UUID(), nullable=True,
                  comment='租户 ID（NULL=私有部署）'),
        sa.Column('adapter_id', sa.String(length=30), nullable=False,
                  comment='适配器 ID: feishu/confluence/notion/obsidian'),
        # AES-GCM 加密 blob：nonce(12B) + ciphertext+tag
        sa.Column('credentials_encrypted', sa.LargeBinary(), nullable=False,
                  comment='AES-GCM 加密的凭证 JSON'),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'),
                  nullable=False, comment='是否启用'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        # 唯一约束：每租户每适配器一份凭证
        # 注意：NULL 在 PostgreSQL 中不被 UNIQUE 视为相等，所以私有部署
        # （tenant_id IS NULL）允许多条同 adapter_id 记录；应用层应限制单条。
        sa.UniqueConstraint('tenant_id', 'adapter_id',
                           name='uq_external_credentials_tenant_adapter'),
    )
    op.create_index(
        'ix_external_credentials_tenant_adapter',
        'external_credentials',
        ['tenant_id', 'adapter_id', 'is_active'],
    )


def downgrade() -> None:
    """Downgrade schema — 移除外部来源字段 + external_credentials 表。"""
    # === external_credentials 表 ===
    op.drop_index('ix_external_credentials_tenant_adapter',
                  table_name='external_credentials')
    op.drop_table('external_credentials')

    # === documents 表字段 ===
    op.drop_index('ix_documents_source_sync', table_name='documents')
    op.drop_column('documents', 'last_checked_at')
    op.drop_column('documents', 'last_synced_at')
    op.drop_column('documents', 'source_revision')
    op.drop_column('documents', 'source_url')
    op.drop_column('documents', 'source_doc_id')
    op.drop_column('documents', 'source')
