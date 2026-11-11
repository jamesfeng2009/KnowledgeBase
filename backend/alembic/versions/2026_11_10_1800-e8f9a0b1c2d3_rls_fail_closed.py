"""RLS fail-closed：GUC 未设置时拒绝所有行

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-10 18:00:00

## 改了什么

此前 48 张租户表的 ``tenant_isolation`` 策略都是 fail-open：

    USING (current_setting('app.tenant_id', true) IS NULL
           OR tenant_id::text = current_setting('app.tenant_id', true))

GUC 缺失时第一个分支恒真 → 全表可见。这使 RLS 实际只有"设了 GUC 的请求路径"
受保护，Celery、脚本、单租户兜底全部越权读写，且不报错。

改成 fail-closed：

    USING (current_setting('app.tenant_id', true) = '*'
           OR tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (同上)

- GUC 未设置 → 两边 NULL → **一行都看不见**；
- GUC = 租户 UUID → 只见本租户；
- GUC = ``'*'`` → 跨租户（只由 ``app.core.tenant_context.system_scope`` 写入）。

## 为什么用 ``tenant_id::text`` 而不是 ``tenant_id = (... )::uuid``

旧策略里有 ``tenant_id = current_setting(...)::uuid`` 的写法。GUC 为 ``'*'`` 时
``'*'::uuid`` 会直接抛 ``invalid input syntax for type uuid``，策略变成报错而不是
放行。用 ``::text`` 比较不会抛，也顺带统一了 uuid 列与文本 GUC 的类型。

## 为什么动态遍历表

写死 48 张表名意味着"以后新建的租户表默认又是零防护"。这里按
``information_schema.columns`` 里存在 ``tenant_id`` 的表遍历，覆盖当下全部表。
新表由 ``tests/test_rls_role_split.py`` 的静态覆盖门禁兜住。
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


# fail-closed：GUC 缺失 → NULL → 拒绝
#
# 注意双写单引号：这段谓词会被塞进 DO 块里的 format('...') 字符串字面量，
# SQL 里字面量内部的单引号必须写成 ''。写成单引号会在 ``current_setting(``
# 处断句，报 ``syntax error at or near "app"``。
_STRICT_PREDICATE = (
    "current_setting(''app.tenant_id'', true) = ''*'' "
    "OR tenant_id::text = current_setting(''app.tenant_id'', true)"
)

# fail-open：GUC 缺失 → TRUE → 全表可见（仅用于 downgrade 还原原状）
_LEGACY_PREDICATE = (
    "current_setting(''app.tenant_id'', true) IS NULL "
    "OR lower(tenant_id::text) = lower(current_setting(''app.tenant_id'', true))"
)

_TENANT_TABLES_LOOP = """
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT c.relname AS name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND EXISTS (
              SELECT 1 FROM information_schema.columns col
              WHERE col.table_schema = 'public'
                AND col.table_name = c.relname
                AND col.column_name = 'tenant_id'
          )
        ORDER BY c.relname
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t.name);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t.name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
            'USING ({predicate}) WITH CHECK ({predicate})',
            t.name
        );
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(_TENANT_TABLES_LOOP.format(predicate=_STRICT_PREDICATE))


def downgrade() -> None:
    op.execute(_TENANT_TABLES_LOOP.format(predicate=_LEGACY_PREDICATE))
