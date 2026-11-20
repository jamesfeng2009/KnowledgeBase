"""迁移漂移守卫的单元测试（不依赖数据库）。

对应任务 #14：本仓 ``alembic autogenerate`` 曾是地雷 —— 在"只跑过迁移"的库上
一份 diff 里有 84 条 ``remove_index`` + 33 条 ``remove_fk``，直接 apply 会删掉
生产索引、丢外键。防线是两件东西：

1. ``alembic/env.py`` 里的 ``include_object`` 过滤器；
2. ``scripts/check_migration_drift.py`` 的分类门禁。

本文件锁住这两件事的**行为契约**，防止后人图省事把过滤器摘掉、或把破坏性
类别挪进"允许"集合。真实库上的端到端验证见 ``test_agent_migration_drift_e2e``
（需要显式提供迁移库 URL，否则跳过）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Index, MetaData, Table, Text

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from alembic_drift import (  # noqa: E402
    DESTRUCTIVE_KINDS,
    NOISE_KINDS,
    explode,
    has_blocking_drift,
    include_object,
    kind_of,
    split_by_severity,
    summarize,
)

# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------


def test_destructive_kinds_cover_the_known_landmines() -> None:
    """删索引 / 丢外键 / 删表 / 删列 必须在破坏性集合里。"""
    for kind in ("remove_index", "remove_fk", "remove_table", "remove_column"):
        assert kind in DESTRUCTIVE_KINDS, f"{kind} 必须硬失败"


def test_noise_kinds_do_not_overlap_destructive() -> None:
    assert not (NOISE_KINDS & DESTRUCTIVE_KINDS)


def test_kind_of_parses_tuple_diff() -> None:
    md = MetaData()
    t = Table("t", md, Column("c", Text))
    idx = Index("ix_t_c", t.c.c)
    assert kind_of(("remove_index", idx)) == "remove_index"


def test_kind_of_rejects_list_input() -> None:
    """list 形态必须显式展开 —— 静默取 [0] 会漏掉同列的其它变更。"""
    with pytest.raises(TypeError):
        kind_of([("modify_comment", "public", "t", "c", {})])


def test_kind_of_maps_table_object_to_remove_table() -> None:
    md = MetaData()
    t = Table("orphan", md, Column("c", Text))
    assert kind_of(t) == "remove_table"


def test_explode_flattens_list_form() -> None:
    """alembic 对"同一列多个属性同时变化"返回 list，必须逐条展开。"""
    diffs = [
        ("remove_index", "x"),
        [
            ("modify_comment", "public", "t", "c", {}),
            ("modify_default", "public", "t", "c", {}),
        ],
        ("remove_fk", "y"),
    ]
    assert explode(diffs) == [
        "remove_index",
        "modify_comment",
        "modify_default",
        "remove_fk",
    ]


def test_split_by_severity_separates_three_buckets() -> None:
    diffs = [
        ("remove_index", "a"),
        ("add_column", "b"),
        ("modify_comment", "c"),
        ("modify_default", "d"),
    ]
    destructive, structural, noise = split_by_severity(diffs)
    assert destructive == ["remove_index"]
    assert structural == ["add_column"]
    assert noise == ["modify_comment", "modify_default"]


def test_unknown_kind_falls_into_structural_not_noise() -> None:
    """未知操作必须落到"要人看"的桶里 —— 宁可多拦，不可漏放。"""
    _, structural, noise = split_by_severity([("some_future_op", "x")])
    assert structural == ["some_future_op"]
    assert noise == []


def test_has_blocking_drift_only_for_destructive_and_structural() -> None:
    assert has_blocking_drift([]) is False
    assert (
        has_blocking_drift([("modify_comment", "x"), ("modify_default", "y")]) is False
    )
    assert has_blocking_drift([("remove_index", "x")]) is True
    assert has_blocking_drift([("add_table", "x")]) is True


def test_summarize_counts_by_kind() -> None:
    diffs = [("remove_index", "a"), ("remove_index", "b"), ("modify_comment", "c")]
    assert summarize(diffs) == {"modify_comment": 1, "remove_index": 2}


# ---------------------------------------------------------------------------
# include_object 过滤器
# ---------------------------------------------------------------------------


def _fake_table() -> Table:
    return Table("demo", MetaData(), Column("id", Text))


def test_filter_hides_db_only_index() -> None:
    """库里有、模型没有的索引 → 隐身（否则 autogenerate 会生成 DROP INDEX）。"""
    t = _fake_table()
    idx = Index("ix_demo_id", t.c.id)
    assert (
        include_object(idx, "ix_demo_id", "index", reflected=True, compare_to=None)
        is False
    )


def test_filter_hides_db_only_fk_and_unique() -> None:
    t = _fake_table()
    for type_ in ("foreign_key_constraint", "unique_constraint", "check_constraint"):
        assert (
            include_object(t, "some_name", type_, reflected=True, compare_to=None)
            is False
        )


def test_filter_keeps_metadata_side_objects() -> None:
    """模型侧新增的对象必须保留，否则新加的索引/外键会被静默吞掉。"""
    t = _fake_table()
    idx = Index("ix_new", t.c.id)
    assert (
        include_object(idx, "ix_new", "index", reflected=False, compare_to=None) is True
    )


def test_filter_keeps_matched_reflected_objects() -> None:
    """库里有且模型也声明了（compare_to 非 None）→ 保留，交给正常比对。"""
    t = _fake_table()
    idx = Index("ix_demo_id", t.c.id)
    assert (
        include_object(idx, "ix_demo_id", "index", reflected=True, compare_to=idx)
        is True
    )


def test_filter_does_not_hide_tables_or_columns() -> None:
    """删表/删列必须暴露出来（它们是破坏性变更，由门禁硬失败拦下）。"""
    t = _fake_table()
    assert include_object(t, "demo", "table", reflected=True, compare_to=None) is True
    assert (
        include_object(t.c.id, "id", "column", reflected=True, compare_to=None) is True
    )


# ---------------------------------------------------------------------------
# 接线回归 — 防止过滤器被"顺手删掉"
# ---------------------------------------------------------------------------


def test_env_py_still_wires_include_object() -> None:
    """``alembic/env.py`` 若不传 include_object，autogenerate 立刻退回删索引模式。"""
    src = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "include_object=include_object" in src, (
        "alembic/env.py 必须把 alembic_drift.include_object 传给 context.configure，"
        "否则 autogenerate 会重新生成 remove_index/remove_fk"
    )


def test_env_py_imports_tables_outside_models_package() -> None:
    """eval_results / tool_audit_log 的表定义不在 app/models 包里。

    不显式导入 → autogenerate 认为"库里有表、模型没有" → 生成 DROP TABLE。
    """
    src = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "import app.eval.repository" in src
    assert "import app.models.tool_audit" in src


def test_gate_script_in_ci_workflow() -> None:
    """门禁脚本必须挂在 CI 上，否则等于没写。"""
    workflow = BACKEND_DIR.parent / ".github" / "workflows" / "backend-migrations.yml"
    assert workflow.exists(), "缺少 backend-migrations.yml"
    assert "check_migration_drift.py" in workflow.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 端到端（可选）：对真实迁移库跑门禁
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("EKB_MIGRATION_DB_URL"),
    reason="需要设置 EKB_MIGRATION_DB_URL 指向已 alembic upgrade head 的库",
)
def test_gate_passes_on_migrated_database() -> None:
    """在一个"只跑过迁移"的库上，门禁必须返回 0（无破坏性漂移）。"""
    env = dict(os.environ, DATABASE_URL=os.environ["EKB_MIGRATION_DB_URL"])
    proc = subprocess.run(
        [sys.executable, "scripts/check_migration_drift.py"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"门禁失败：\n{combined[-4000:]}"
    assert "remove_index" not in combined
    assert "remove_fk" not in combined
