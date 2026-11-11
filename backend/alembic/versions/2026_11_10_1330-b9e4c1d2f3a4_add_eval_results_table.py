"""add eval_results table (offline eval persistence)

Revision ID: b9e4c1d2f3a4
Revises: f4d5e6a7b8c9
Create Date: 2026-11-10 13:30:00.000000

补齐离线评测结果持久化表 eval_results。

背景（为什么必须补）：
    app/eval/repository.py 里定义了 EvalResultRecord（__tablename__ =
    "eval_results"），并被 EvalRepository.save() / get_baseline() /
    compare_with_baseline() 读写，但从未写过迁移。

    更糟的是它的写入路径是"优雅降级"实现：_session_ctx() 里
    `except Exception: log.warning("eval_repo.session_error")` 会吞掉
    所有异常。于是在生产环境（AUTO_MIGRATE=true / AUTO_CREATE_TABLES=false）
    表不存在时，save() 不报错、只留一条 warning 日志，
    评测结果、基线、回归对比全部静默失效。

    该模型不在 app/models/ 包内（定义在 app/eval/repository.py），
    因此也不会被 `from app.models import Base` 的常规检查覆盖，
    属于容易长期潜伏的一类缺陷。

字段与模型严格对齐：
    run_id      唯一索引（跨服务引用的业务 ID）
    dataset_name 索引（按数据集查询/清基线）
    result_json JSONB 完整快照
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9e4c1d2f3a4"
down_revision: Union[str, None] = "f4d5e6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 eval_results 表。"""
    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            nullable=False,
            comment="运行 ID（UUID 字符串）",
        ),
        sa.Column(
            "dataset_name",
            sa.String(255),
            nullable=False,
            comment="数据集名称",
        ),
        sa.Column(
            "evaluated_at",
            sa.String(50),
            nullable=False,
            comment="评测时间（ISO 字符串）",
        ),
        sa.Column(
            "avg_recall_at_5",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
            comment="平均 Recall@5",
        ),
        sa.Column(
            "avg_mrr",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
            comment="平均 MRR",
        ),
        sa.Column(
            "avg_ndcg_at_5",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
            comment="平均 NDCG@5",
        ),
        sa.Column(
            "avg_judge_score",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
            comment="平均 Judge 总分",
        ),
        sa.Column(
            "total",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="用例总数",
        ),
        sa.Column(
            "passed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="通过用例数",
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="完整评测结果 JSON 快照",
        ),
        sa.Column(
            "is_baseline",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否为回归基线",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="离线评测结果记录",
    )
    # 模型侧 run_id 声明为 unique=True + index=True → 唯一索引
    op.create_index(
        "ix_eval_results_run_id",
        "eval_results",
        ["run_id"],
        unique=True,
    )
    op.create_index(
        "ix_eval_results_dataset_name",
        "eval_results",
        ["dataset_name"],
    )


def downgrade() -> None:
    """回滚 eval_results 表。"""
    op.drop_index("ix_eval_results_dataset_name", table_name="eval_results")
    op.drop_index("ix_eval_results_run_id", table_name="eval_results")
    op.drop_table("eval_results")
