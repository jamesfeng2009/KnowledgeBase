"""add chat faq compounding idempotent index

P0 聊天问答 → 知识库 FAQ 回流：为 knowledge_assets 表新增 (source_type, source_id)
部分唯一索引，保证同一好评反馈 / 采纳答案不重复沉淀为 FAQ 资产。

部分唯一索引（WHERE deleted_at IS NULL）：
    - 活跃资产保证唯一 — 同一 source 重复触发时 DB 层拦截（应用层 _get_asset_by_source
      的并发竞态兜底）；
    - 软删除资产不参与唯一约束 — 已废弃资产可重新沉淀（资产生命周期 draft→active→deprecated
      后，同一 source 可再次回流生成新资产）。

与现有 source_type 单字段索引（index=True）共存，不冲突。

Revision ID: e9f6a7b8c9d0
Revises: df4e5f608192
Create Date: 2026-08-12 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e9f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'df4e5f608192'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 新增 (source_type, source_id) 部分唯一索引。"""
    op.create_index(
        index_name="ix_knowledge_assets_source_idempotent",
        table_name="knowledge_assets",
        columns=["source_type", "source_id"],
        unique=True,
        postgresql_where="deleted_at IS NULL",
    )


def downgrade() -> None:
    """Downgrade schema — 删除部分唯一索引。"""
    op.drop_index(
        index_name="ix_knowledge_assets_source_idempotent",
        table_name="knowledge_assets",
    )
