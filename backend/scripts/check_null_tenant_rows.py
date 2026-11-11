#!/usr/bin/env python3
"""上线前检查：有多少行的 tenant_id 是 NULL。

## 为什么必须查

fail-closed 策略是::

    current_setting('app.tenant_id', true) = '*'
    OR tenant_id::text = current_setting('app.tenant_id', true)

``tenant_id`` 为 NULL 时第二个分支是 NULL（不是 true），所以**这批行对任何
身份都不可见** —— 包括 ``'*'`` 跨租户（除了第一条分支命中，但那要求会话是
跨租户模式；业务请求不是）。

回填之前上线 fail-closed，等于让这批数据在应用里凭空消失，而且不报错。
48 张租户表里有 47 张的 ``tenant_id`` 允许为 NULL，所以这不是理论风险。

## 用法

    # 只统计（需要能跨租户读的超级用户连接）
    python scripts/check_null_tenant_rows.py \
        --url "postgresql+asyncpg://ekb:***@host:5432/ekb"

    # 输出修复建议 SQL（不执行）
    python scripts/check_null_tenant_rows.py --url ... --emit-sql

退出码：0 = 没有 NULL 行；1 = 存在 NULL 行（阻塞上线）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


def _to_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _tenant_tables(conn) -> list[str]:
    rows = await conn.fetch(
        """
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
        """
    )
    return [r["name"] for r in rows]


async def run(url: str, emit_sql: bool) -> int:
    if asyncpg is None:
        print("需要 asyncpg", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(_to_dsn(url))
    try:
        role = await conn.fetchrow(
            "SELECT current_user AS name, "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super"
        )
        if not role["is_super"]:
            print(
                f"! 当前角色 {role['name']} 不是超级用户，统计结果可能被 RLS 过滤。"
                "\n  这个检查本身需要看到全部行，请用迁移/owner 连接执行。\n",
                file=sys.stderr,
            )

        tables = await _tenant_tables(conn)
        print(f"含 tenant_id 的表: {len(tables)}\n")

        offenders: list[tuple[str, int]] = []
        for t in tables:
            try:
                n = await conn.fetchval(
                    f'SELECT count(*) FROM public."{t}" WHERE tenant_id IS NULL'
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {t}: 查询失败 {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if n:
                offenders.append((t, int(n)))

        if not offenders:
            print("✓ 没有 tenant_id 为 NULL 的行，可以安全上线 fail-closed")
            return 0

        total = sum(n for _, n in offenders)
        print(f"✗ {len(offenders)} 张表共 {total} 行 tenant_id 为 NULL：\n")
        for t, n in sorted(offenders, key=lambda x: -x[1]):
            print(f"  {t:<40} {n:>8}")

        print(
            "\n这些行在 fail-closed 下对任何业务身份都不可见（且不报错）。"
            "\n回填之前不要上线。按归属选择："
            "\n  a) 全部归到一个默认租户（单租户时期的历史数据）"
            "\n  b) 按业务线索逐表推断（kb_id / owner_id 反查）"
        )
        if emit_sql:
            print("\n--- 回填模板（把 <TENANT_UUID> 换成实际租户）---")
            for t, _ in sorted(offenders, key=lambda x: -x[1]):
                print(
                    f'UPDATE public."{t}" SET tenant_id = \'<TENANT_UUID>\' '
                    f"WHERE tenant_id IS NULL;"
                )
        return 1
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="统计 tenant_id 为 NULL 的行（上线前检查）")
    ap.add_argument("--url", default=None)
    ap.add_argument("--emit-sql", action="store_true", help="输出回填 SQL 模板")
    args = ap.parse_args()

    url = args.url
    if not url:
        import os
        import pathlib

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from app.config import get_settings

        url = os.environ.get("DATABASE_URL") or get_settings().DATABASE_URL
    return asyncio.run(run(url, args.emit_sql))


if __name__ == "__main__":
    raise SystemExit(main())
