#!/usr/bin/env python3
"""RLS 真实隔离效果验证 — 只读，可在生产直接执行。

## 它回答什么问题

「策略写对了」和「隔离真的生效」是两件事。上一轮就栽在这里：48 张表
``relrowsecurity = t``、``relforcerowsecurity = t``、``SET LOCAL`` 也在跑，
看起来无懈可击 —— 但连接角色是 superuser，PostgreSQL 对 superuser 无条件
绕过 RLS，实际隔离为零。

所以这个脚本不看 ``pg_policies``，只看**真实数据在真实角色下是否可见**。

## 用法

    # 必须用应用运行角色（非 superuser）连接，否则测不出任何问题
    python scripts/verify_rls_isolation.py \
        --url "postgresql://ekb_app:***@host:5432/ekb"

    # 指定要抽检的表
    python scripts/verify_rls_isolation.py --url ... --tables documents,document_chunks

    # 只看结论，不等全部表
    python scripts/verify_rls_isolation.py --url ... --only-critical

## 判定标准

对每张含 ``tenant_id`` 的表：

| 场景 | 期望 | 违反意味着 |
|------|------|-----------|
| GUC 未设置 | **0 行** | 策略仍是 fail-open，或角色是 superuser |
| GUC = 租户 A | 所有行 ``tenant_id = A`` | **跨租户数据泄漏** |
| GUC = '*' | 行数 ≥ 租户 A 行数 | 跨租户通道被误关 |

任何一项不满足 → 退出码 1。

## 安全性

全部查询包在 ``BEGIN ... ROLLBACK`` 里，不写任何数据；只 SELECT count 与
``tenant_id``，不读取正文内容。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

try:  # 延迟失败：--help 不该因为缺依赖报 ImportError
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

#: 必检的关键表 —— 存的是正文或最敏感的业务实体
CRITICAL_TABLES: tuple[str, ...] = (
    "documents",
    "document_chunks",
    "knowledge_bases",
    "users",
    "tickets",
    "qa_questions",
    "qa_answers",
)


@dataclass
class TableResult:
    table: str
    no_guc_rows: int
    tenant_a_rows: int
    tenant_a_foreign: int
    cross_rows: int
    expected_a: int

    @property
    def ok(self) -> bool:
        return (
            self.no_guc_rows == 0
            and self.tenant_a_foreign == 0
            and self.tenant_a_rows == self.expected_a
            and self.cross_rows >= self.expected_a
        )

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.no_guc_rows != 0:
            out.append(f"无 GUC 却可见 {self.no_guc_rows} 行（策略仍 fail-open）")
        if self.tenant_a_foreign:
            out.append(
                f"租户 A 视角看到 {self.tenant_a_foreign} 行其他租户数据（泄漏）"
            )
        if self.tenant_a_rows != self.expected_a:
            out.append(
                f"租户 A 应见 {self.expected_a} 行，实见 {self.tenant_a_rows}（漏行）"
            )
        if self.cross_rows < self.expected_a:
            out.append(
                f"跨租户 '*' 应 ≥ {self.expected_a} 行，实见 {self.cross_rows}（通道失效）"
            )
        return out


def _to_asyncpg_dsn(url: str) -> str:
    """把 SQLAlchemy 的 ``postgresql+asyncpg://`` 转成 asyncpg 原生 DSN。"""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _collect_tables(conn) -> list[str]:
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


async def _group_top_tenants(conn, tables: tuple[str, ...]) -> list[str]:
    """按行数取前两个租户；任一表凑够两个就返回。"""
    for t in tables:
        try:
            rows = await conn.fetch(
                f"SELECT tenant_id::text AS tid, count(*) AS n "
                f'FROM public."{t}" WHERE tenant_id IS NOT NULL '
                f"GROUP BY tenant_id ORDER BY n DESC LIMIT 2"
            )
        except Exception:
            continue
        if len(rows) >= 2:
            return [r["tid"] for r in rows]
    return []


async def _collect_tenants(dsn: str, conn, tables: list[str]) -> list[str]:
    """找出数据量最大的两个租户，用真实数据做对比。

    需要两条取法，对应两代策略，顺序不能反：

    1. **全新连接（从未设 GUC）** —— fail-open 库在这一步全可见；
       fail-closed 库则一行看不到。
    2. **``'*'`` 跨租户** —— fail-closed 库的正规通道；fail-open 库里
       ``lower(tenant_id) = lower('*')`` 恒假，反而看不到任何行。

    只试一种会在某种策略下误报"数据不足"，让脚本在真正出问题时闭嘴 ——
    这正是它要避免的失败模式。
    """
    probe = ("documents",) + tuple(tables)

    fresh = await asyncpg.connect(dsn)
    try:
        found = await _group_top_tenants(fresh, probe)
    finally:
        await fresh.close()
    if found:
        return found

    await conn.execute("SELECT set_config('app.tenant_id', '*', true)")
    return await _group_top_tenants(conn, probe)


async def _count(conn, table: str, guc: str) -> int:
    """在指定 GUC 下数行数（事务内设置，调用方负责回滚）。

    ``guc`` 传空串表示"没有租户身份"。
    """
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)
    row = await conn.fetchval(f'SELECT count(*) FROM public."{table}"')
    return int(row or 0)


async def _probe_fresh(dsn: str, table: str, tenant_a: str) -> tuple[int, int]:
    """在一条**从未设置过 GUC** 的新连接上探测，返回 (可见行数, 其中属于 A 的行数)。

    为什么不能用 RESET：PostgreSQL 对自定义 GUC 的 RESET 语义是「设为空串」，
    不是「恢复未定义」。同一连接上只要设过一次 ``app.tenant_id``，之后
    ``current_setting(..., true)`` 返回的是 ``''`` 而非 NULL —— 而 fail-open
    策略下 ``''`` 不匹配任何行。于是"无 GUC 是否越权可见"这条最关键的检查
    会在**策略最松的库上假绿**，正好把要抓的问题藏起来。

    只有全新连接的 ``current_setting`` 才是 NULL。所以这里单独开连接。

    顺带返回 A 的行数：fail-open 库里 ``'*'`` 视角恒为 0 行，算不出"租户 A
    应见多少"，用它兜底，免得在已经 FAIL 的库上再刷一堆连带误报。
    """
    conn = await asyncpg.connect(dsn)
    try:
        total = await conn.fetchval(f'SELECT count(*) FROM public."{table}"')
        mine = await conn.fetchval(
            f'SELECT count(*) FROM public."{table}" WHERE tenant_id = $1::uuid',
            tenant_a,
        )
        return int(total or 0), int(mine or 0)
    finally:
        await conn.close()


async def _foreign_in_tenant_a(conn, table: str, tenant_a: str) -> int:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_a)
    row = await conn.fetchval(
        f'SELECT count(*) FROM public."{table}" '
        f"WHERE tenant_id IS DISTINCT FROM $1::uuid",
        tenant_a,
    )
    return int(row or 0)


async def _true_total(conn, table: str) -> int:
    """跨租户视角下的真实总行数，用来算「租户 A 应见多少」。"""
    await conn.execute("SELECT set_config('app.tenant_id', '*', true)")
    row = await conn.fetchval(f'SELECT count(*) FROM public."{table}"')
    return int(row or 0)


async def verify(url: str, tables: list[str] | None, only_critical: bool) -> int:
    if asyncpg is None:
        print("需要 asyncpg：.venv/bin/pip install asyncpg", file=sys.stderr)
        return 2

    dsn = _to_asyncpg_dsn(url)
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        print(f"✗ 连接失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        # --- 前置检查：角色必须受 RLS 约束 ---
        role = await conn.fetchrow(
            """
            SELECT current_user AS name,
                   (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super,
                   (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass
            """
        )
        print(f"连接角色      : {role['name']}")
        print(f"超级用户      : {'是 ← 测不出任何问题' if role['is_super'] else '否'}")
        print(f"BYPASSRLS     : {'是 ← 测不出任何问题' if role['bypass'] else '否'}")
        if role["is_super"] or role["bypass"]:
            print(
                "\n✗ 当前角色绕过 RLS，验证结果无意义。请用应用运行角色（如 ekb_app）连接：\n"
                "  backend/scripts/provision_app_role.py --check    # 查看角色清单\n",
                file=sys.stderr,
            )
            return 2

        all_tables = await _collect_tables(conn)
        targets = tables or all_tables
        if only_critical:
            targets = [t for t in CRITICAL_TABLES if t in all_tables]
        missing = [t for t in targets if t not in all_tables]
        for t in missing:
            print(f"! 跳过 {t}：表不存在或没有 tenant_id 列")

        print(f"租户表总数    : {len(all_tables)}")
        print(f"本次抽检      : {len([t for t in targets if t not in missing])} 张\n")

        # 事务内探测，结束一律回滚
        await conn.execute("BEGIN")

        tenants = await _collect_tenants(dsn, conn, all_tables)
        if len(tenants) < 2:
            print(
                "数据不足：库内少于 2 个有数据的租户，无法做跨租户对比。\n"
                "至少需要两个租户各有数据才能验证隔离。",
                file=sys.stderr,
            )
            return 2
        tenant_a, tenant_b = tenants[0], tenants[1]
        print(f"对比租户 A    : {tenant_a}")
        print(f"对比租户 B    : {tenant_b}\n")

        results: list[TableResult] = []
        for table in [t for t in targets if t not in missing]:
            # 租户 A 应见 = 跨租户视角下 tenant_id = A 的行数
            await conn.execute("SELECT set_config('app.tenant_id', '*', true)")
            expected_a = int(
                await conn.fetchval(
                    f'SELECT count(*) FROM public."{table}" WHERE tenant_id = $1::uuid',
                    tenant_a,
                )
                or 0
            )
            no_guc, fresh_a = await _probe_fresh(dsn, table, tenant_a)
            if expected_a == 0:
                # fail-open 库上 '*' 视角看不到任何行，用无 GUC 探测的结果兜底
                expected_a = fresh_a
            seen_a = await _count(conn, table, tenant_a)
            foreign = await _foreign_in_tenant_a(conn, table, tenant_a)
            cross = await _true_total(conn, table)
            results.append(
                TableResult(
                    table=table,
                    no_guc_rows=no_guc,
                    tenant_a_rows=seen_a,
                    tenant_a_foreign=foreign,
                    cross_rows=cross,
                    expected_a=expected_a,
                )
            )

        await conn.execute("ROLLBACK")

        # --- 输出 ---
        header = (
            f"{'表名':<34} {'无GUC':>6} {'租户A':>7} {'异租户':>7} {'跨租户':>7}  结论"
        )
        print(header)
        print("-" * len(header.encode("gbk", errors="replace")))
        failed: list[TableResult] = []
        for r in results:
            status = "OK" if r.ok else "FAIL"
            print(
                f"{r.table:<34} {r.no_guc_rows:>6} {r.tenant_a_rows:>7} "
                f"{r.tenant_a_foreign:>7} {r.cross_rows:>7}  {status}"
            )
            if not r.ok:
                failed.append(r)

        print()
        if not failed:
            print(
                f"✓ {len(results)} 张表全部通过：无 GUC 不可见、租户间零泄漏、跨租户通道可用"
            )
            return 0

        print(f"✗ {len(failed)} 张表未通过：", file=sys.stderr)
        for r in failed:
            print(f"\n  {r.table}", file=sys.stderr)
            for reason in r.reasons():
                print(f"    - {reason}", file=sys.stderr)
        return 1
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="验证 RLS 在真实角色下的隔离效果（只读）"
    )
    parser.add_argument(
        "--url",
        default=None,
        help="数据库连接串；默认读 settings.DATABASE_URL（会把 +asyncpg 去掉）",
    )
    parser.add_argument(
        "--tables",
        default=None,
        help="逗号分隔的表名，默认全部含 tenant_id 的表",
    )
    parser.add_argument(
        "--only-critical",
        action="store_true",
        help="只抽检关键表（documents / chunks / tickets 等）",
    )
    args = parser.parse_args()

    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.config import get_settings

        url = get_settings().DATABASE_URL

    tables = (
        [t.strip() for t in args.tables.split(",") if t.strip()]
        if args.tables
        else None
    )
    return asyncio.run(verify(url, tables, args.only_critical))


if __name__ == "__main__":
    raise SystemExit(main())
