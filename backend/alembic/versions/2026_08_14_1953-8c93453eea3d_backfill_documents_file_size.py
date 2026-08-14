"""backfill documents file_size

Revision ID: 8c93453eea3d
Revises: 825db09af229
Create Date: 2026-08-14 19:53:56.700073

2026_07_21_1800 为 documents 表新增 file_size 列后，历史文档该字段为 NULL。
_should_use_multipart_pipeline 读取时可能因 NULL 导致大文件分流静默失效。
本迁移将历史 NULL 回填为 0，不影响后续正常写入真实大小。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c93453eea3d"
down_revision: Union[str, Sequence[str], None] = "825db09af229"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """回填 documents.file_size 的 NULL 值。"""
    op.execute("UPDATE documents SET file_size = 0 WHERE file_size IS NULL")


def downgrade() -> None:
    """数据回填类迁移 downgrade 无操作（无法区分原本为 NULL 的行）。"""
    pass
