"""enable rls on tenant tables that were missed

Revision ID: d7e8f9a0b1c2
Revises: c1d2e3f4a5b6
Create Date: 2026-11-10 17:00:00

补齐 RLS 覆盖缺口。

背景
----
前四个 RLS 迁移用「显式表名列表」的方式逐张开启策略
（`b8c9d0e1f2a3` / `c9d0e1f2a3b4` / `d0652e7caf21` / `b8d2a0f3c5e7`），
但之后新增的表没有跟着补 —— 结果是 48 张带 `tenant_id` 的表里只有 36 张有
策略。漏掉的 12 张里最要紧的是：

    document_chunks        文档切片正文（含检索召回内容）
    knowledge_candidates   知识候选（含原文片段）
    kb_folders             目录树
    tickets                IT 工单（工单正文常含内部信息）
    wiki_pages / research_jobs / constraint_rules / constraint_audit_records
    ai_eval_*_datasets ×4  （评测数据集，含业务样本）

也就是说：即便将来把运行角色换成非 superuser，这些表的行级隔离依然是 0。

策略与既有 4 个迁移完全一致（fail-open：GUC 未设置时全表可见，供迁移/系统
任务使用），此处不改语义，只补覆盖面。fail-open 的收敛需要单独决策，见
docs/P0-multi-tenant-isolation-design.md 第 8.4 节第 2 步。
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 有 tenant_id 但历史迁移漏掉 RLS 的表（与 tests/test_rls_role_split.py 的
#: 覆盖门禁一一对应：那里若报出新缺口，说明新表又忘了开策略）
MISSED_TENANT_TABLES = [
    "ai_eval_doc_parse_datasets",
    "ai_eval_injection_suites",
    "ai_eval_judge_datasets",
    "ai_eval_rag_datasets",
    "constraint_audit_records",
    "constraint_rules",
    "document_chunks",
    "kb_folders",
    "knowledge_candidates",
    "research_jobs",
    "tickets",
    "wiki_pages",
]


def upgrade() -> None:
    for table in MISSED_TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;')
        # 幂等：重复执行不应炸（个别环境可能已手工补过策略）
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON "{table}"
            FOR ALL
            USING (
                current_setting('app.tenant_id', true) IS NULL
                OR "{table}".tenant_id = current_setting('app.tenant_id', true)::uuid
            )
            WITH CHECK (
                current_setting('app.tenant_id', true) IS NULL
                OR "{table}".tenant_id = current_setting('app.tenant_id', true)::uuid
            );
            """
        )


def downgrade() -> None:
    for table in MISSED_TENANT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;')
