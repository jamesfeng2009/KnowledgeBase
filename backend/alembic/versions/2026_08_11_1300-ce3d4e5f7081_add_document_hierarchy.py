"""add document hierarchy metadata

P0 wiki 层级改造：Document 表新增 6 个层级元数据字段，支持系列/子 wiki/子文档
的层级检索过滤。采用扁平存储 + 元数据编码层级策略（非多跳存储），所有新字段
nullable，旧文档无层级信息时向后兼容。

新增字段：
    - series_id   所属系列 ID（同系列文档共享）
    - parent_id   父文档 ID（自引用外键）
    - path        层级路径 '产品/合规/数据安全'
    - depth       层级深度（根=0）
    - sort_order  同级排序
    - version_of  版本族主文档 ID（自引用外键）

新增索引：series_id, path, parent_id, version_of（depth/sort_order 数据量小不加索引）

Revision ID: ce3d4e5f7081
Revises: bd2c3d4e5f70
Create Date: 2026-08-11 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ce3d4e5f7081'
down_revision: Union[str, Sequence[str], None] = 'bd2c3d4e5f70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 新增 Document 层级字段 + 索引。"""
    # === 新增列（全部 nullable，向后兼容旧文档）===
    op.add_column(
        'documents',
        sa.Column('series_id', sa.String(length=100), nullable=True, comment='所属系列 ID（同系列文档共享）'),
    )
    op.add_column(
        'documents',
        sa.Column('parent_id', sa.UUID(), nullable=True, comment='父文档 ID'),
    )
    op.add_column(
        'documents',
        sa.Column('path', sa.String(length=1000), nullable=True, comment="层级路径 '产品/合规/数据安全'"),
    )
    op.add_column(
        'documents',
        sa.Column('depth', sa.Integer(), nullable=False, server_default='0', comment='层级深度（根=0）'),
    )
    op.add_column(
        'documents',
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0', comment='同级排序'),
    )
    op.add_column(
        'documents',
        sa.Column('version_of', sa.UUID(), nullable=True, comment='版本族主文档 ID'),
    )

    # === 自引用外键 ===
    op.create_foreign_key(
        'fk_documents_parent_id',
        'documents',
        'documents',
        ['parent_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_documents_version_of',
        'documents',
        'documents',
        ['version_of'],
        ['id'],
        ondelete='SET NULL',
    )

    # === 索引（层级过滤高频字段）===
    op.create_index('ix_documents_series_id', 'documents', ['series_id'])
    op.create_index('ix_documents_path', 'documents', ['path'])
    op.create_index('ix_documents_parent_id', 'documents', ['parent_id'])
    op.create_index('ix_documents_version_of', 'documents', ['version_of'])


def downgrade() -> None:
    """Downgrade schema — 移除 Document 层级字段 + 索引。"""
    op.drop_index('ix_documents_version_of', table_name='documents')
    op.drop_index('ix_documents_parent_id', table_name='documents')
    op.drop_index('ix_documents_path', table_name='documents')
    op.drop_index('ix_documents_series_id', table_name='documents')

    op.drop_constraint('fk_documents_version_of', 'documents', type_='foreignkey')
    op.drop_constraint('fk_documents_parent_id', 'documents', type_='foreignkey')

    op.drop_column('documents', 'version_of')
    op.drop_column('documents', 'sort_order')
    op.drop_column('documents', 'depth')
    op.drop_column('documents', 'path')
    op.drop_column('documents', 'parent_id')
    op.drop_column('documents', 'series_id')
