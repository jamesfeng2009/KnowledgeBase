"""迁移漂移守卫 — 让 ``alembic autogenerate`` 在本仓从"地雷"变成"可用但受限"。

## 背景（为什么需要这个模块）

本仓的 schema 由 **108 个手写迁移**建立，迁移里显式命名了约束（``fk_documents_tenant_id``）、
建了模型从未声明的索引（``ix_constraint_rules_domains`` 等）、并给列/表加了 comment。
ORM 模型则是后来逐步补的，两边从未对齐。后果是在一个"只跑过迁移"的库上执行
``alembic revision --autogenerate`` 会生成：

    - ``remove_index``   84 条  → 应用即删掉生产索引，查询性能雪崩
    - ``remove_fk``      33 条  → 应用即丢外键，脏数据开始堆积
    - ``remove_constraint`` 1 条
    - ``modify_default`` 97 条 / ``modify_comment`` 142 条（噪音，但会把迁移文件淹掉）

## 两道防线

1. **``include_object`` 过滤器**（``alembic/env.py`` 使用）：
   *库里有、模型没有* 的索引 / 外键 / 唯一约束 / CHECK 约束一律对 autogenerate 隐身。
   即"迁移是 schema 的权威来源，模型不是"。要删这些结构必须**手写迁移**并显式
   ``op.drop_index(...)``——这是刻意的摩擦，不是遗漏。

2. **分类门禁**（``scripts/check_migration_drift.py`` 使用）：
   把 autogenerate 的 diff 按危险等级分三类，破坏性/结构性变更 **硬失败**，
   注释与默认值差异只告警。漏网的破坏性操作会让 CI 红，而不是让生产丢索引。

## 为什么不是"把 84 个索引补进模型"

可以补，但收益远小于风险：``tests/conftest.py`` 用 ``Base.metadata.create_all`` 建测试库，
往模型上补 28 个 ``tenant_id`` 外键会让测试库突然具备外键约束，任何构造
"孤儿 tenant_id"的用例立刻 IntegrityError。本仓的模型是**查询用映射**，
不是**建库用 DDL 源**——保持这个定位，把权威交给迁移。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 危险等级分类
# ---------------------------------------------------------------------------

#: 会丢失数据或删除库中已有结构 — 任何一条都硬失败
DESTRUCTIVE_KINDS: frozenset[str] = frozenset(
    {
        "remove_table",
        "remove_column",
        "remove_index",
        "remove_fk",
        "remove_constraint",
        "remove_unique_constraint",
        "remove_check_constraint",
    }
)

#: 结构性新增/变更 — 必须由人审阅后写成手写迁移，autogenerate 生成的裸 diff 不允许直接 apply
SCHEMA_KINDS: frozenset[str] = frozenset(
    {
        "add_table",
        "add_column",
        "add_index",
        "add_fk",
        "add_constraint",
        "add_unique_constraint",
        "add_check_constraint",
        "modify_type",
        "modify_nullable",
        "modify_name",
    }
)

#: 纯文档性差异 — 告警但不失败
NOISE_KINDS: frozenset[str] = frozenset(
    {
        "modify_default",
        "modify_comment",
        "remove_table_comment",
        "add_table_comment",
    }
)

#: 库里存在、但迁移没建、模型也没声明的约束类型 — 一律对 autogenerate 隐身
DB_ONLY_CONSTRAINT_TYPES: frozenset[str] = frozenset(
    {
        "index",
        "foreign_key_constraint",
        "unique_constraint",
        "check_constraint",
    }
)


def kind_of(diff: Any) -> str:
    """把 alembic ``compare_metadata`` 的一条差异规约成单一类别名。

    alembic 的返回结构不统一：
        - 单条操作用 tuple ``("remove_index", Index(...))``；
        - 同一列上多个属性变化时是 **list of tuple**；
        - 建表/删表直接给 ``Table`` 对象本身。

    本函数把 list 拆开、Table 对象映射成 add/remove_table，其余取 ``tuple[0]``。
    调用方（门禁脚本）应当对 list 逐条展开后再分类，见 ``explode``。
    """
    if isinstance(diff, list):
        raise TypeError("kind_of() 不接受 list，请先用 explode() 展开")
    if isinstance(diff, tuple) and diff:
        return str(diff[0])
    name = type(diff).__name__
    if name == "Table":
        return "remove_table"  # 库里有表、metadata 没有
    return name


def explode(diffs: list[Any]) -> list[str]:
    """展开 diff 列表，返回每条的类别名（list 内的 tuple 逐个展开）。"""
    kinds: list[str] = []
    for diff in diffs:
        if isinstance(diff, list):
            kinds.extend(str(d[0]) for d in diff if isinstance(d, tuple) and d)
        else:
            kinds.append(kind_of(diff))
    return kinds


def summarize(diffs: list[Any]) -> dict[str, int]:
    """按类别统计，供门禁脚本打印摘要。"""
    counts: dict[str, int] = {}
    for kind in explode(diffs):
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def split_by_severity(diffs: list[Any]) -> tuple[list[str], list[str], list[str]]:
    """把类别名分成 (破坏性, 结构性, 噪音) 三组。

    未知类别归入结构性（宁可多拦一次，不可漏放），保证新增的 alembic 操作类型
    默认是"要人看"的，而不是静默放过。
    """
    destructive: list[str] = []
    structural: list[str] = []
    noise: list[str] = []
    for kind in explode(diffs):
        if kind in DESTRUCTIVE_KINDS:
            destructive.append(kind)
        elif kind in NOISE_KINDS:
            noise.append(kind)
        else:
            structural.append(kind)
    return destructive, structural, noise


def has_blocking_drift(diffs: list[Any]) -> bool:
    """是否存在必须人工介入的漂移（破坏性 或 结构性）。"""
    destructive, structural, _ = split_by_severity(diffs)
    return bool(destructive or structural)


# ---------------------------------------------------------------------------
# autogenerate 过滤器
# ---------------------------------------------------------------------------


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """autogenerate 对象过滤器 — 让"库里有、模型没有"的约束对 autogenerate 隐身。

    Args:
        obj: 被检查的对象（Table / Column / Index / Constraint）。
        name: 对象名（部分场景为 None）。
        type_: alembic 的对象类型串（``"table"``/``"column"``/``"index"``/...）。
        reflected: True 表示该对象来自数据库反射（而不是 metadata）。
        compare_to: metadata 中对应的对象；None 表示 metadata 里没有。

    Returns:
        False 表示本次 autogenerate 忽略该对象。

    行为契约：
        - 反射到的索引/外键/唯一约束/CHECK，且 metadata 无对应项 → **忽略**
          （迁移建的，不允许 autogenerate 提议删除）；
        - metadata 侧新增的对象 → 全部保留（否则模型新加字段会被吞掉）；
        - 表与列不做过滤（删表删列属于必须暴露的破坏性变更，交给门禁硬失败）。
    """
    if not reflected:
        return True
    if type_ in DB_ONLY_CONSTRAINT_TYPES and compare_to is None:
        return False
    return True
