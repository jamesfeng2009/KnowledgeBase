"""add document parse metadata

Revision ID: a1b2c3d4e5f6
Revises: 115a9c06ba4a
Create Date: 2026-07-19 10:00:00.000000

新增文档解析元数据字段（P1 增强：解析任务产物持久化）：
- parse_status: 解析状态（parsed/partial/failed/pending），区别于业务状态 status
- parse_warnings: 解析警告列表 JSONB（解析/向量化/索引失败信息）
- page_count: 页数/幻灯片数/工作表数（解析时固定，避免每次请求重新计算）
- char_count: 正文字符数（解析时固定）

向后兼容：所有新字段均有默认值，历史数据无影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "115a9c06ba4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 documents 表的解析元数据字段。"""
    # parse_status: 解析状态（可空，历史数据为 NULL 表示未解析）
    op.add_column(
        "documents",
        sa.Column(
            "parse_status",
            sa.String(length=20),
            nullable=True,
            comment="解析状态: parsed/partial/failed/pending",
        ),
    )

    # parse_warnings: 解析警告列表 JSONB（可空，历史数据为 NULL）
    op.add_column(
        "documents",
        sa.Column(
            "parse_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="解析警告列表（解析/向量化/索引失败信息）",
        ),
    )

    # page_count: 页数（非空，默认 0，历史数据回填为 0）
    op.add_column(
        "documents",
        sa.Column(
            "page_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="页数/幻灯片数/工作表数",
        ),
    )

    # char_count: 正文字符数（非空，默认 0，历史数据回填为 0）
    op.add_column(
        "documents",
        sa.Column(
            "char_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="正文字符数",
        ),
    )


def downgrade() -> None:
    """回滚解析元数据字段。"""
    op.drop_column("documents", "char_count")
    op.drop_column("documents", "page_count")
    op.drop_column("documents", "parse_warnings")
    op.drop_column("documents", "parse_status")
