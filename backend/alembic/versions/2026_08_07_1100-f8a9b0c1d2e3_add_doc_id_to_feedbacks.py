"""add doc_id to feedbacks

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-08-07 11:00:00.000000

P0 质量评分 doc_id 维度：

feedbacks 表新增 doc_id 冗余列（外键 → documents.id，可空），
质量评分（citation_accuracy / feedback_score）由原先的
Feedback.related_message_id → Message.sources（JSONB）Python 侧过滤链路，
改为按 doc_id 直查。

存量数据回填：从关联消息的引用来源（Message.sources JSONB 数组）
解析第一个引用卡片的 doc_id 回填；仅当首元素 doc_id 为合法 UUID 格式时回填，
其余（无关联消息 / sources 为空 / 非法格式）保持 NULL，由查询侧旧链路兜底。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feedbacks",
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="关联文档 ID（质量评分冗余列）",
        ),
    )
    op.create_foreign_key(
        "fk_feedbacks_doc_id", "feedbacks", "documents", ["doc_id"], ["id"]
    )
    op.create_index("ix_feedbacks_doc_id", "feedbacks", ["doc_id"])

    # 存量回填：取关联消息引用来源中第一个引用卡片的 doc_id。
    # 正则为 UUID 标准格式校验，避免 sources 中混入非法值导致类型转换失败。
    op.execute(
        """
        UPDATE feedbacks f
        SET doc_id = (m.sources -> 0 ->> 'doc_id')::uuid
        FROM messages m
        WHERE f.related_message_id = m.id
          AND f.doc_id IS NULL
          AND jsonb_typeof(m.sources) = 'array'
          AND m.sources -> 0 ->> 'doc_id'
              ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_feedbacks_doc_id", table_name="feedbacks")
    op.drop_constraint("fk_feedbacks_doc_id", "feedbacks", type_="foreignkey")
    op.drop_column("feedbacks", "doc_id")
