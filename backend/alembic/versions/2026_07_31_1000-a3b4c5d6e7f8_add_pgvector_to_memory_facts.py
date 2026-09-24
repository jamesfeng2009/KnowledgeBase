"""add pgvector extension and embedding_vec column to memory_facts

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-31 10:00:00.000000

P1-2 怎么召回 — embedding 迁移到 pgvector：

1. 启用 pgvector 扩展（CREATE EXTENSION IF NOT EXISTS vector）
2. 为 memory_facts 表新增 embedding_vec vector 列
3. 将已有 JSONB embedding 数据迁移到 vector 列
4. 创建 IVFFlat 向量索引加速余弦相似度检索

注意：
- 需要 pgvector/pgvector:pg16 Docker 镜像（已在 docker-compose.yml 更新）
- 原 JSONB embedding 列保留作为降级回退
- IVFFlat 索引在数据量 < 1000 时可能不生效，此处为预留
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# pgvector 向量维度 —— 必须与写入侧 Embedder 的输出维度一致，否则
# INSERT ... embedding_vec 会因 dimension mismatch 直接失败。
# 维度由应用配置统一决定（DASHSCOPE_EMBED_DIM，默认 1024）；这里走
# get_settings() 而不是 os.environ，是因为 pydantic-settings 把 .env
# 解析进 Settings 对象而不会回写 os.environ —— 读环境变量会拿到与
# 应用运行时不同的值，建出来的列宽度和 embedder 输出再度错位。
from app.config import get_settings  # noqa: E402

VECTOR_DIM = int(get_settings().DASHSCOPE_EMBED_DIM)


def upgrade() -> None:
    # 1. 启用 pgvector 扩展
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. 新增 vector 列（nullable，与 JSONB embedding 共存）
    op.execute(
        f"ALTER TABLE memory_facts "
        f"ADD COLUMN IF NOT EXISTS embedding_vec vector({VECTOR_DIM})"
    )

    # 3. 将已有 JSONB embedding 迁移到 vector 列
    #    pgvector 的文本字面量要求保留方括号（'[0.1,0.2]'::vector），
    #    JSONB 数组转 text 正好就是这个格式，所以直接 cast，不去括号。
    #    仅回填「确实是数组 且 维度和目标列一致」的行：换过 embedding
    #    模型的历史数据维度不同，cast 会让整条 UPDATE 报错、迁移直接中断。
    op.execute(
        f"UPDATE memory_facts "
        f"SET embedding_vec = embedding::text::vector "
        f"WHERE jsonb_typeof(embedding) = 'array' "
        f"AND jsonb_array_length(embedding) = {VECTOR_DIM} "
        f"AND embedding_vec IS NULL"
    )

    # 4. 创建 IVFFlat 向量索引（数据量大时加速检索）
    #    lists = 100 是常见起点，可按需调整
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_memory_facts_embedding_vec "
        f"ON memory_facts USING ivfflat (embedding_vec vector_cosine_ops) "
        f"WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_facts_embedding_vec")
    op.execute("ALTER TABLE memory_facts DROP COLUMN IF EXISTS embedding_vec")
    # 不卸载 pgvector 扩展（可能被其他表使用）
