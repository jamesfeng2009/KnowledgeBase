"""safe read_at type conversion

Revision ID: 6fff5feb64c4
Revises: 8c93453eea3d
Create Date: 2026-08-14 19:54:22.807066

2026_07_19_1400 将 notifications.read_at 从 String(30) 升级为 DateTime(timezone=True)，
但其 downgrade 直接转回 String(30) 会丢失时间数据。

本迁移提供安全的双向类型转换：
- upgrade：确保 read_at 为 timestamptz（已是该类型时无实际影响）。
- downgrade：使用 TO_CHAR 保留时间数据后再转为 varchar(30)。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6fff5feb64c4"
down_revision: Union[str, Sequence[str], None] = "8c93453eea3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """确保 read_at 为带时区的时间戳类型。"""
    op.execute(
        "ALTER TABLE notifications "
        "ALTER COLUMN read_at TYPE TIMESTAMPTZ "
        "USING read_at::TIMESTAMPTZ"
    )


def downgrade() -> None:
    """降级为 varchar(30) 时使用 TO_CHAR 保留时间数据。"""
    op.execute(
        "ALTER TABLE notifications "
        "ALTER COLUMN read_at TYPE VARCHAR(30) "
        "USING TO_CHAR(read_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    )
