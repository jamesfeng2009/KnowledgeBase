"""add knowledge approvals table

P2 知识回流审批工作流：新建 knowledge_approvals 表，存储 FAQ 沉淀后的审批记录。

回流审批生命周期：pending → approved / rejected / expired
自动检测分流：quality_score >= 0.9 且无冲突且无 PII → 自动 approve；
              否则 → pending（人工审批）。

关联表：
    - knowledge_assets.id（沉淀的 FAQ 资产）
    - documents.id（沉淀的 FAQ 文档）
    - knowledge_bases.id（目标知识库）
    - users.id（审批人，自动通过时为 NULL）

Revision ID: fa7b8c9d0e1f
Revises: e9f6a7b8c9d0
Create Date: 2026-08-12 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'fa7b8c9d0e1f'
down_revision: Union[str, Sequence[str], None] = 'e9f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 创建 knowledge_approvals 表。"""
    op.create_table(
        'knowledge_approvals',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('doc_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reviewer_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='pending'),
        sa.Column('quality_score', sa.Float(), nullable=True),
        sa.Column('pii_detected', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('conflict_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('auto_detected_risks', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column('expire_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('auto_approved', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['knowledge_assets.id']),
        sa.ForeignKeyConstraint(['doc_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id']),
        sa.ForeignKeyConstraint(['reviewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_knowledge_approvals_asset_id'),
        'knowledge_approvals',
        ['asset_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_knowledge_approvals_doc_id'),
        'knowledge_approvals',
        ['doc_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_knowledge_approvals_kb_id'),
        'knowledge_approvals',
        ['kb_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_knowledge_approvals_status'),
        'knowledge_approvals',
        ['status'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — 删除 knowledge_approvals 表。"""
    op.drop_index(op.f('ix_knowledge_approvals_status'),
                  table_name='knowledge_approvals')
    op.drop_index(op.f('ix_knowledge_approvals_kb_id'),
                  table_name='knowledge_approvals')
    op.drop_index(op.f('ix_knowledge_approvals_doc_id'),
                  table_name='knowledge_approvals')
    op.drop_index(op.f('ix_knowledge_approvals_asset_id'),
                  table_name='knowledge_approvals')
    op.drop_table('knowledge_approvals')
