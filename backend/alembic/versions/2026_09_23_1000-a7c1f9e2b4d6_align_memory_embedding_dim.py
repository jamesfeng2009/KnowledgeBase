"""align memory_facts embedding_vec dimension with embedder config

Revision ID: a7c1f9e2b4d6
Revises: f3c4d5e6a7b8
Create Date: 2026-09-23 10:00:00.000000

a3b4c5d6e7f8 建列时把维度写死成 1536，而写入侧用的是
``settings.DASHSCOPE_EMBED_DIM``（1024）。pgvector 的列宽在建列时就固定，
向量长度与列宽不符时 INSERT 直接报 dimension mismatch —— 记忆写入整条
失败。建列迁移本身已改成读配置，但**已经跑过它的库不会自动改宽**，
这里补齐历史库。

旧向量与当前 embedder 不同维度 = 不同模型产出，混进同一次余弦检索本身
就没有意义，所以直接重建列；宽度已经对齐的库不动。
"""
import re
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "a7c1f9e2b4d6"
down_revision: Union[str, Sequence[str], None] = "f3c4d5e6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "memory_facts"
COLUMN = "embedding_vec"
INDEX = "ix_memory_facts_embedding_vec"

TARGET_DIM = int(get_settings().DASHSCOPE_EMBED_DIM)


def _current_dim() -> int | None:
    """读取列当前的 vector 宽度；列不存在时返回 None。"""
    row = op.get_bind().execute(
        sa.text(
            "select format_type(a.atttypid, a.atttypmod) "
            "from pg_attribute a "
            "join pg_class c on c.oid = a.attrelid "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where c.relname = :table and a.attname = :column "
            "and n.nspname = current_schema() and a.attnum > 0"
        ),
        {"table": TABLE, "column": COLUMN},
    ).first()
    if row is None or row[0] is None:
        return None
    match = re.search(r"vector\((\d+)\)", row[0])
    return int(match.group(1)) if match else None


def _backfill_from_jsonb() -> None:
    """把维度确实匹配的 JSONB 向量灌回 vector 列（不匹配的一律跳过）。"""
    op.execute(
        f"UPDATE {TABLE} SET {COLUMN} = embedding::text::vector "
        "WHERE jsonb_typeof(embedding) = 'array' "
        f"AND jsonb_array_length(embedding) = {TARGET_DIM} "
        f"AND {COLUMN} IS NULL"
    )


def upgrade() -> None:
    """Upgrade schema — 列宽对齐 embedder 输出维度。"""
    current = _current_dim()
    if current is None:
        # 列还不存在：由建列迁移按配置维度创建，无需处理
        return
    if current == TARGET_DIM:
        return

    # 宽度不一致说明全部旧向量都来自别的模型，重建列比逐行转换干净
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} vector({TARGET_DIM})")
    _backfill_from_jsonb()
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} "
        f"USING ivfflat ({COLUMN} vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    """Downgrade schema — 无操作。

    建列迁移修正后不再记录「原来的宽度」，回到 1536 只会把同一个
    dimension mismatch 换个方向再制造一次；列宽本身由建列迁移与
    DASHSCOPE_EMBED_DIM 决定。
    """
