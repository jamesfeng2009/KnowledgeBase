#!/usr/bin/env python3
"""创建/校准多租户运行角色 — 让 PostgreSQL RLS 真正生效的那一步。

为什么必须拆角色
----------------
本仓 36 张含 ``tenant_id`` 的表都开了 ``ENABLE`` + ``FORCE ROW LEVEL SECURITY``，
``get_db_session`` 也确实在每个请求里 ``SET LOCAL app.tenant_id``。但
**PostgreSQL 对 superuser 无条件绕过 RLS**（``FORCE`` 只约束表 owner，不约束
superuser），而应用一直以来就是用建库的那个 superuser 连接的。实测对比：

    同一张 FORCE RLS 表、同一个 app.tenant_id
    superuser 连接  → 可见 2 行（两个租户全看见）
    普通角色连接    → 可见 1 行（RLS 生效）

所以「RLS 形同虚设」不是策略写错了，是**连接角色不对**。本脚本负责把运行角色
建出来并按最小权限授权：

    LOGIN / NOSUPERUSER / NOBYPASSRLS / NOCREATEDB / NOCREATEROLE
    + CONNECT / USAGE on schema public
    + SELECT, INSERT, UPDATE, DELETE on all tables
    + USAGE, SELECT on all sequences
    + ALTER DEFAULT PRIVILEGES（未来新建的表自动带权限）
    - 不给 CREATE on schema：建表必须走迁移（owner 连接），否则新表会绕过审查

用法
----
    cd backend
    # 1) 用 owner 连接建角色（默认读 MIGRATION_DATABASE_URL / DATABASE_URL）
    .venv/bin/python scripts/provision_app_role.py --password '强随机口令'

    # 2) 把输出里的 DATABASE_URL 写进部署环境 / .env，重启后端
    #    迁移仍用 MIGRATION_DATABASE_URL（owner），两者不可混用

    # 只看现状不动手
    .venv/bin/python scripts/provision_app_role.py --check

退出码
------
    0 — 成功（或 --check 判定已就绪）
    2 — 权限/连接问题，无法完成（会打印具体原因）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 角色名只允许常规标识符，避免拼接 SQL 时被注入
_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
#: 运行角色需要的表级权限（刻意不含 TRUNCATE / REFERENCES / TRIGGER）
_TABLE_PRIVS = "SELECT, INSERT, UPDATE, DELETE"


def _read_env(name: str) -> str | None:
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith(f"{name}="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if value:
            return value
    return None


def _owner_dsn() -> str:
    """owner 连接串：MIGRATION_DATABASE_URL 优先，其次 DATABASE_URL。"""
    url = (
        os.environ.get("MIGRATION_DATABASE_URL")
        or _read_env("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _read_env("DATABASE_URL")
    )
    if not url:
        raise SystemExit("找不到 MIGRATION_DATABASE_URL / DATABASE_URL，无法连接数据库")
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _quote_ident(name: str) -> str:
    if not _ROLE_RE.match(name):
        raise ValueError(f"非法角色名（只允许字母/数字/下划线）：{name!r}")
    return f'"{name}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _app_dsn(owner_dsn: str, role: str, password: str) -> str:
    """拼出运行角色应使用的连接串（保留 owner 的 host/port/库名）。"""
    parts = urlsplit(owner_dsn)
    netloc = f"{role}:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _table_owners(conn) -> list[str]:
    """列出当前库里 public schema 的表 owner（去重）。

    ALTER DEFAULT PRIVILEGES 是按「创建者」生效的，所以必须对每个真实 owner
    都设一次；只对 current_user 设会漏掉历史遗留的、owner 不同的表。
    """
    rows = await conn.fetch(
        """
        select distinct pg_get_userbyid(c.relowner) as owner
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind in ('r', 'p')
        """
    )
    return sorted({r["owner"] for r in rows if r["owner"]})


async def provision(
    dsn: str, role: str, password: str, *, database: str | None = None
) -> dict[str, object]:
    """幂等地创建/校准运行角色并授权，返回执行摘要。"""
    import asyncpg

    ident = _quote_ident(role)
    conn = await asyncpg.connect(dsn=dsn, timeout=5.0)
    try:
        me = await conn.fetchrow(
            "select current_user as u, (select rolsuper from pg_roles where rolname = current_user) as s"
        )
        if not me["s"]:
            raise PermissionError(
                f"连接角色 {me['u']} 不是 superuser，无法创建角色/授予表权限。"
                "请用 MIGRATION_DATABASE_URL（owner）执行本脚本。"
            )
        dbname = database or await conn.fetchval("select current_database()")

        existed = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
        # 密码里允许任意字符，但 CREATE/ALTER ROLE 不支持参数绑定，只能字面量转义
        pwd = _quote_literal(password)
        if existed:
            await conn.execute(
                f"alter role {ident} login password {pwd} "
                f"nosuperuser nobypassrls nocreatedb nocreaterole"
            )
            action = "updated"
        else:
            await conn.execute(
                f"create role {ident} login password {pwd} "
                f"nosuperuser nobypassrls nocreatedb nocreaterole"
            )
            action = "created"

        await conn.execute(
            f"grant connect on database {_quote_ident(dbname)} to {ident}"
        )
        await conn.execute(f"grant usage on schema public to {ident}")
        await conn.execute(
            f"grant {_TABLE_PRIVS} on all tables in schema public to {ident}"
        )
        await conn.execute(
            f"grant usage, select on all sequences in schema public to {ident}"
        )

        owners = await _table_owners(conn)
        for owner in owners:
            o = _quote_ident(owner)
            await conn.execute(
                f"alter default privileges for role {o} in schema public "
                f"grant {_TABLE_PRIVS} on tables to {ident}"
            )
            await conn.execute(
                f"alter default privileges for role {o} in schema public "
                f"grant usage, select on sequences to {ident}"
            )

        # 复核：权限位必须真的是「非超级用户 + 不绕过 RLS」
        check = await conn.fetchrow(
            "select rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname = $1",
            role,
        )
        if check["rolsuper"] or check["rolbypassrls"] or not check["rolcanlogin"]:
            raise RuntimeError(
                f"角色 {role} 权限位不符合预期（super={check['rolsuper']}, "
                f"bypassrls={check['rolbypassrls']}, login={check['rolcanlogin']}），"
                "可能被集群级策略或 pg_hba 之外的配置覆盖。"
            )

        return {
            "action": action,
            "role": role,
            "database": dbname,
            "owners": owners,
            "login": check["rolcanlogin"],
            "superuser": check["rolsuper"],
            "bypassrls": check["rolbypassrls"],
        }
    finally:
        await conn.close()


async def check(dsn: str, role: str) -> dict[str, object]:
    """只读检查：角色是否存在、权限位是否正确、能否连上目标库。"""
    import asyncpg

    conn = await asyncpg.connect(dsn=dsn, timeout=5.0)
    try:
        row = await conn.fetchrow(
            "select rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname = $1",
            role,
        )
        if row is None:
            return {"exists": False, "role": role}
        return {
            "exists": True,
            "role": role,
            "login": row["rolcanlogin"],
            "superuser": row["rolsuper"],
            "bypassrls": row["rolbypassrls"],
            "ready": bool(row["rolcanlogin"])
            and not row["rolsuper"]
            and not row["rolbypassrls"],
        }
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="创建/校准多租户运行角色（让 RLS 真正生效）"
    )
    parser.add_argument(
        "--role", default=os.environ.get("APP_DB_ROLE", "ekb_app"), help="运行角色名"
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("APP_DB_PASSWORD"),
        help="角色口令（也可用 APP_DB_PASSWORD 环境变量；生产务必用强随机值）",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="owner 连接串，默认取 MIGRATION_DATABASE_URL/DATABASE_URL",
    )
    parser.add_argument("--database", default=None, help="目标库名，默认取当前库")
    parser.add_argument("--check", action="store_true", help="只检查现状，不做任何变更")
    args = parser.parse_args(argv)

    try:
        dsn = args.dsn or _owner_dsn()
    except SystemExit as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    try:
        if args.check:
            result = asyncio.run(check(dsn, args.role))
            print(f"角色 {args.role}: {result}")
            return 0 if result.get("ready") else 2

        if not args.password:
            print(
                "✗ 缺少 --password（或 APP_DB_PASSWORD）。\n"
                "  例：.venv/bin/python scripts/provision_app_role.py "
                '--password "$(openssl rand -base64 24)"',
                file=sys.stderr,
            )
            return 2

        result = asyncio.run(
            provision(dsn, args.role, args.password, database=args.database)
        )
    except PermissionError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"✗ 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        f"✓ 角色 {result['role']} 已{('更新' if result['action'] == 'updated' else '创建')}"
    )
    print(f"  库         : {result['database']}")
    print(f"  超级用户   : {result['superuser']}   （必须为 False，否则 RLS 被绕过）")
    print(f"  BYPASSRLS  : {result['bypassrls']}   （必须为 False）")
    print(f"  覆盖表 owner: {', '.join(result['owners']) or '(无表)'}")
    print()
    print(
        "把下面这行写进运行时的环境变量（保留 owner 连接给 MIGRATION_DATABASE_URL）："
    )
    print(f"  DATABASE_URL={_app_dsn(dsn, args.role, args.password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
