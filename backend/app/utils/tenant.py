"""多租户过滤工具 — 供 Service 层的原生 SQL 查询使用。

BaseRepository 已内置 _apply_tenant_filter，但 Service 层有大量直接
使用 select() / update() 的方法，这些方法需要手动调用本工具函数
追加租户过滤条件。

使用方式：
    from app.utils.tenant import apply_tenant_filter

    # SELECT 查询
    stmt = select(Document).where(Document.deleted_at.is_(None))
    stmt = apply_tenant_filter(stmt, Document, self._tenant_id)

    # UPDATE 查询
    stmt = update(TestCase).where(TestCase.id == case_id)
    stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
    stmt = stmt.values(status="approved")

    # 聚合查询
    stmt = select(func.count(Document.id)).select_from(Document)
    stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
"""
from __future__ import annotations

from uuid import UUID



def apply_tenant_filter(stmt, model, tenant_id: UUID | None):
    """为 SQLAlchemy 语句追加租户过滤条件。

    - 当 tenant_id 为 None 时，不过滤（单租户兜底场景）。
    - 当模型没有 tenant_id 列时，不过滤。
    - 否则追加 WHERE model.tenant_id = :tid 条件。

    支持 SELECT 和 UPDATE 语句，通过链式 .where() 追加条件。

    Args:
        stmt: SQLAlchemy Select 或 Update 语句。
        model: ORM 模型类（如 Document、TestCase）。
        tenant_id: 租户 ID，None 时不过滤。

    Returns:
        追加了租户过滤条件的语句（或原语句）。
    """
    if tenant_id is None:
        return stmt
    if not hasattr(model, "tenant_id"):
        return stmt
    return stmt.where(model.tenant_id == tenant_id)
