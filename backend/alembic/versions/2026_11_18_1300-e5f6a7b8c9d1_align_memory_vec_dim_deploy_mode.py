"""align memory_facts.embedding_vec with DEPLOY_MODE embedder dim

Revision ID: e5f6a7b8c9d1
Revises: 9f8e7d6c5b4a
Create Date: 2026-11-18 13:00:00.000000

## 改了什么

a7c1f9e2b4d6 只把列宽对齐到 ``DASHSCOPE_EMBED_DIM``（1024），而默认
``DEPLOY_MODE=saas`` 的写入侧是 OpenAIEmbedder（3072 维）—— saas 部署
INSERT 直接 dimension mismatch，记忆整条写不进；查询侧 pgvector 报错降级
JSONB 后还会按短向量截断静默算错相似度（B2）。

列宽改为随部署模式取对应 embedder 的输出维度（``memory_embedding_dim``，
与 embedder 注册表一一对应）。宽度已对齐的库不动；不一致的库重建列 ——
旧向量与当前 embedder 不同维度 = 不同模型产出，混进同一次余弦检索没有意义，
维度确实匹配的 JSONB 向量回填。

## 注意

列宽在迁移期按当时的 DEPLOY_MODE 固定。**切换部署模式后必须重跑本迁移**
（或等价 ALTER），否则写入侧与列宽再次错位。
"""
import re
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from app.config import get_settings
from app.llm.embedder import memory_embedding_dim

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d1"
down_revision: Union[str, Sequence[str], None] = "9f8e7d6c5b4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "memory_facts"
COLUMN = "embedding_vec"
INDEX = "ix_memory_facts_embedding_vec"


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


def upgrade() -> None:
    target_dim = memory_embedding_dim(get_settings())
    current = _current_dim()
    if current is None or current == target_dim:
        return

    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} vector({target_dim})")
    op.execute(
        f"UPDATE {TABLE} SET {COLUMN} = embedding::text::vector "
        "WHERE jsonb_typeof(embedding) = 'array' "
        f"AND jsonb_array_length(embedding) = {target_dim} "
        f"AND {COLUMN} IS NULL"
    )
    # pgvector 硬限制：ivfflat 最多 2000 维（实测 3072 维报
    # ProgramLimitExceededError）。超宽列不建索引 —— 记忆表按 user_id
    # 过滤后候选集很小，精确扫描保证正确性，不为建索引降维或换半精度类型。
    if target_dim <= 2000:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} "
            f"USING ivfflat ({COLUMN} vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    """无操作 —— 回到旧宽度只会把 dimension mismatch 换个方向再制造一次。"""