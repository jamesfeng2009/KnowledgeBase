#!/usr/bin/env python3
"""开发环境自检 — 一条命令看清「后端到底连的是哪个 PostgreSQL」。

为什么需要它
------------
本仓有两套 PostgreSQL 启动方式，宿主端口不同：

    docker compose -f docker-compose.yml -f docker-compose.override.yml up -d postgres  → 15432
    docker run -d --name ekb-pg -p 55432:5432 ...                                      → 55432

`.env` 只能写一个。写错时的表现是：`uvicorn` 启动日志里一条
"Connection refused"，或者全量测试一次性两百多个连接错误 —— 报错本身
完全不提「端口写错了」，很容易被误判成代码问题。

用法
----
    cd backend && .venv/bin/python scripts/dev_env_check.py

退出码
------
    0 — 配置的库可达
    2 — 不可达（会列出哪些端口是活的，并给出可直接粘贴的修正命令）
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR / ".env"

#: 本仓已知的 PostgreSQL 宿主端口（见模块注释）
KNOWN_PORTS = (55432, 15432, 5432)

#: 每张表都该带上 tenant_id；探针只关心能否连通，不做业务查询
PROBE_SQL = "select current_user, current_database(), version()"


def _read_env(name: str) -> str | None:
    """从 backend/.env 读一个键（不加载 Settings，脚本要能在配置非法时也跑）。"""
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith(f"{name}="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if value:
            return value
    return None


def _with_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    return urlunsplit(
        (
            parts.scheme,
            f"{userinfo}{sep}{host}:{port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _tcp_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _mask(url: str) -> str:
    """日志里把密码打码（自检输出经常被贴进 issue / 群里）。"""
    parts = urlsplit(url)
    if not parts.password:
        return url
    masked = f"{parts.username}:***@{parts.hostname}"
    if parts.port:
        masked += f":{parts.port}"
    return urlunsplit((parts.scheme, masked, parts.path, parts.query, parts.fragment))


def _to_asyncpg_dsn(url: str) -> str:
    """SQLAlchemy 的 postgresql+asyncpg:// → asyncpg 直接可用的 postgresql://"""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _probe(url: str) -> tuple[bool, str]:
    """真实连一次库，返回 (是否成功, 说明文本)。"""
    try:
        import asyncpg
    except ImportError:
        return False, "未安装 asyncpg，只能做端口级探测"

    try:
        conn = await asyncpg.connect(dsn=_to_asyncpg_dsn(url), timeout=3.0)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        row = await conn.fetchrow(PROBE_SQL)
        is_super = await conn.fetchval(
            "select rolsuper from pg_roles where rolname = current_user"
        )
        bypass = await conn.fetchval(
            "select rolbypassrls from pg_roles where rolname = current_user"
        )
        version = row["version"].split(",")[0] if row and row["version"] else "?"
        lines = [
            f"数据库      : {row['current_database']}",
            f"连接角色    : {row['current_user']}",
            f"服务端      : {version}",
            f"超级用户    : {'是  ← RLS 会被无条件绕过' if is_super else '否'}",
            f"BYPASSRLS   : {'是  ← RLS 会被绕过' if bypass else '否'}",
        ]
        if is_super:
            lines.append(
                "提示        : 运行角色是 superuser，多租户 RLS 不会生效。"
                "改用 scripts/provision_app_role.py 创建的非超级用户角色连接。"
            )
        return True, "\n".join(lines)
    finally:
        await conn.close()


async def main() -> int:
    env_url = os.environ.get("DATABASE_URL") or _read_env("DATABASE_URL")
    if not env_url:
        print("✗ backend/.env 里没有 DATABASE_URL，且环境变量未设置")
        return 2

    parts = urlsplit(env_url)
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    print(
        f"配置来源      : {'环境变量 DATABASE_URL' if 'DATABASE_URL' in os.environ else 'backend/.env'}"
    )
    print(f"目标地址      : {_mask(env_url)}")

    if not _tcp_alive(host, port):
        print(f"✗ {host}:{port} 端口没有监听。以下是本机其它存活的 PostgreSQL 端口：")
        alive = [p for p in KNOWN_PORTS if p != port and _tcp_alive(host, p)]
        if not alive:
            print("  （没有探测到任何存活的 PostgreSQL，先启动一个：）")
            print(
                "   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d postgres"
            )
            return 2
        best: int | None = None
        for candidate_port in alive:
            ok, detail = await _probe(_with_port(env_url, candidate_port))
            mark = "✓" if ok else "✗"
            first_line = detail.splitlines()[0] if detail else ""
            print(f"  {mark} {host}:{candidate_port}  {first_line}")
            if ok and best is None:
                best = candidate_port
        if best:
            print("\n建议把 backend/.env 改成：")
            print(f"  DATABASE_URL={_with_port(env_url, best)}")
        return 2

    ok, detail = await _probe(env_url)
    if not ok:
        print("✗ 端口通但连库失败：")
        print(f"  {detail}")
        return 2

    print("✓ 连接成功")
    print(detail)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
