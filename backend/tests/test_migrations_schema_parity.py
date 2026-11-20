"""模型 ↔ 迁移一致性回归测试。

背景（为什么需要这组测试）：
    项目有两套建表路径：
      1. 测试/开发：Base.metadata.create_all()
      2. 生产：AUTO_MIGRATE=true → alembic upgrade head（AUTO_CREATE_TABLES=false）

    两套路径各走各的，中间没有任何约束。实测踩到两个坑：

    - app/models/ai_eval.py 定义了 3 张 ai_eval_injection_* 表，端点和
      service 也都写完了，但从未写过迁移。create_all 会建表，所以单元测试
      全绿；生产环境表根本不存在，/api/v1/ai-eval/injection/* 一调就
      UndefinedTable（500）。
    - app/models/tool_audit.py 没有被 app/models/__init__.py 导入，
      Base.metadata 里没有 tool_audit_log。后果是 create_all 不建表，
      且 `alembic revision --autogenerate` 会把它判成"库里有、模型没有"
      从而生成 DROP TABLE。

    这两类问题都不靠运行时才暴露，静态扫一遍就能拦住，所以放在这里做门禁。
"""

from __future__ import annotations

import pathlib
import re

import pytest

# 显式导入定义在 models 包之外的模型，否则 eval_results 是否被检查
# 取决于测试执行顺序（导入即注册到 metadata，不导入则本测试看不见它）。
import app.eval.repository  # noqa: F401  — 定义 eval_results
from app.models import Base

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODELS_DIR = BACKEND_ROOT / "app" / "models"
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"

# op.create_table("xxx", ...)
_CREATE_TABLE_RE = re.compile(r'create_table\(\s*["\']([A-Za-z0-9_]+)["\']')
# 迁移里的原始 SQL：op.execute("CREATE TABLE ...")
_RAW_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([A-Za-z0-9_]+)", re.I
)
# 模型文件里的 __tablename__ = "xxx"
_TABLENAME_RE = re.compile(r'__tablename__\s*=\s*["\']([A-Za-z0-9_]+)["\']')


def _tables_created_by_migrations() -> set[str]:
    """收集所有迁移文件里建过的表名（含原始 SQL 写法）。"""
    created: set[str] = set()
    assert VERSIONS_DIR.is_dir(), f"迁移目录不存在: {VERSIONS_DIR}"
    for path in VERSIONS_DIR.glob("*.py"):
        src = path.read_text(encoding="utf-8", errors="ignore")
        created |= set(_CREATE_TABLE_RE.findall(src))
        created |= {m.lower() for m in _RAW_CREATE_TABLE_RE.findall(src)}
    return created


def _tables_declared_in_model_files() -> dict[str, str]:
    """收集 app/models/*.py 里声明的表名 → 所在文件名。"""
    declared: dict[str, str] = {}
    for path in MODELS_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        for name in _TABLENAME_RE.findall(src):
            declared[name] = path.name
    return declared


def test_every_model_table_has_a_migration() -> None:
    """每个 ORM 表都必须有迁移建表语句。

    否则生产环境（AUTO_MIGRATE=true / AUTO_CREATE_TABLES=false）会缺表，
    而单元测试因为走 create_all 依然全绿 —— 这类缺陷只能靠本测试拦住。
    """
    created = _tables_created_by_migrations()
    missing = sorted(
        name for name in Base.metadata.tables if name.lower() not in created
    )
    assert not missing, (
        "以下 ORM 表在 alembic/versions 中找不到任何建表语句，"
        "生产环境会 500（UndefinedTable）：\n  " + "\n  ".join(missing)
    )


def test_all_model_modules_are_registered_in_metadata() -> None:
    """模型文件里声明的表必须真的注册进 Base.metadata。

    漏在 app/models/__init__.py 里 import 的模型不会进 metadata：
    create_all 不建表，alembic autogenerate 还会把它当成待删除的表。
    """
    declared = _tables_declared_in_model_files()
    registered = set(Base.metadata.tables)
    unregistered = sorted(
        f"{name} ({filename})"
        for name, filename in declared.items()
        if name not in registered
    )
    assert not unregistered, (
        "以下模型未注册到 Base.metadata，请检查 app/models/__init__.py 的导入：\n  "
        + "\n  ".join(unregistered)
    )


def test_migration_chain_has_single_head() -> None:
    """迁移链必须只有一个 head，且所有 down_revision 都能找到父节点。

    多 head 会让 `alembic upgrade head` 直接报错；悬空的 down_revision
    说明有迁移文件被误删。
    """
    revisions: dict[str, str | None] = {}
    for path in VERSIONS_DIR.glob("*.py"):
        src = path.read_text(encoding="utf-8", errors="ignore")
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', src, re.M)
        if not rev:
            continue
        down = re.search(
            r'^down_revision(?::\s*[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)',
            src,
            re.M,
        )
        revisions[rev.group(1)] = down.group(1) if down and down.group(1) else None

    assert revisions, "未解析到任何迁移文件"

    parents = {d for d in revisions.values() if d}
    dangling = sorted(p for p in parents if p not in revisions)
    assert not dangling, f"以下 down_revision 指向不存在的迁移: {dangling}"

    heads = sorted(r for r in revisions if r not in parents)
    assert (
        len(heads) == 1
    ), f"迁移链存在多个 head（alembic upgrade head 会失败）: {heads}"


@pytest.mark.parametrize("table", ["ai_eval_injection_suites"])
def test_injection_tables_present_in_metadata(table: str) -> None:
    """Prompt Injection 评测三张表必须是 metadata 的一部分（防止回退）。"""
    assert table in Base.metadata.tables
