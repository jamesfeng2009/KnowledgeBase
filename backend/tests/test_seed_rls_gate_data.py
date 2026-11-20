"""seed_rls_gate_data.py 离线单测（T3 安全门禁数据准备）。

验证：
1. 脚本可 import / py_compile；
2. 固定租户 UUID 稳定且互为不同租户；
3. _uid() 确定性（幂等键）且不同表/租户不同；
4. _to_asyncpg_dsn 正确去掉 SQLAlchemy 驱动前缀。
不依赖数据库。
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "seed_rls_gate_data.py"


def _load():
    spec = importlib.util.spec_from_file_location("_test_seed_rls_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["_test_seed_rls_gate"] = m
    spec.loader.exec_module(m)
    return m


def test_compile_and_import():
    py_compile.compile(str(SCRIPT), doraise=True)
    m = _load()
    assert m.TENANT_A != m.TENANT_B


def test_uid_deterministic():
    m = _load()
    a = m._uid(m.TENANT_A, "documents")
    assert a == m._uid(m.TENANT_A, "documents")
    assert a != m._uid(m.TENANT_B, "documents")
    assert a != m._uid(m.TENANT_A, "tickets")
    import uuid

    assert str(uuid.UUID(a)) == a


def test_to_asyncpg_dsn():
    m = _load()
    url = "postgresql+asyncpg://ekb:pw@host:5432/db"
    assert m._to_asyncpg_dsn(url) == "postgresql://ekb:pw@host:5432/db"
    # 已是 sync DSN 时原样返回
    plain = "postgresql://ekb:pw@host:5432/db"
    assert m._to_asyncpg_dsn(plain) == plain
