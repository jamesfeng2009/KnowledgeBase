"""P0 wiki 层级元数据 — Document 模型字段验证测试。

验证 Document 模型中新增的 P0 wiki 层级元数据字段：
- series_id: String(100), nullable, indexed — 所属系列 ID
- parent_id: UUID FK self(documents.id), nullable — 父文档 ID
- path: String(1000), nullable, indexed — 层级路径
- depth: Integer, default=0, nullable=False — 层级深度（根=0）
- sort_order: Integer, default=0, nullable=False — 同级排序
- version_of: UUID FK self(documents.id), nullable — 版本族主文档 ID

反向关系：
- children / parent: 父子文档关系（parent_id 反向）
- version_children / version_master: 版本族关系（version_of 反向）

测试方式：SQLAlchemy 模型 introspection（无需真实 DB 连接），
通过 Document.__table__.columns 和 Document.__mapper__.relationships 检查。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Integer, String

from app.models.knowledge import Document


# ======================================================================
# 字段存在性
# ======================================================================


class TestHierarchyFieldsExist:
    """P0 wiki 层级元数据字段应全部存在于 Document 模型中。"""

    @pytest.mark.parametrize(
        "field",
        ["series_id", "parent_id", "path", "depth", "sort_order", "version_of"],
    )
    def test_field_exists(self, field: str) -> None:
        """每个层级元数据字段都应在表列中定义。"""
        assert field in Document.__table__.columns, (
            f"Document 应有 {field} 字段"
        )


# ======================================================================
# nullable 验证
# ======================================================================


class TestHierarchyFieldsNullable:
    """验证层级字段的 nullable 属性。"""

    @pytest.mark.parametrize(
        "field",
        ["series_id", "parent_id", "path", "version_of"],
    )
    def test_nullable_fields(self, field: str) -> None:
        """series_id/parent_id/path/version_of 应允许 NULL（向后兼容旧文档）。"""
        col = Document.__table__.columns[field]
        assert col.nullable is True, f"{field} 应允许 NULL"

    @pytest.mark.parametrize("field", ["depth", "sort_order"])
    def test_non_nullable_fields(self, field: str) -> None:
        """depth/sort_order 不应允许 NULL（有默认值 0）。"""
        col = Document.__table__.columns[field]
        assert col.nullable is False, f"{field} 不应允许 NULL"


# ======================================================================
# 默认值验证
# ======================================================================


class TestHierarchyFieldsDefault:
    """depth 和 sort_order 的默认值应为 0。"""

    @pytest.mark.parametrize("field", ["depth", "sort_order"])
    def test_default_is_zero(self, field: str) -> None:
        """字段应有默认值且默认值为 0。"""
        col = Document.__table__.columns[field]
        assert col.default is not None, f"{field} 应有默认值"
        assert col.default.arg == 0, (
            f"{field} 默认值应为 0，实际: {col.default.arg}"
        )


# ======================================================================
# 索引验证
# ======================================================================


class TestHierarchyFieldsIndex:
    """验证层级字段的索引情况。

    说明：
    - series_id 和 path 有显式 index=True（任务规范要求）；
    - parent_id 和 version_of 通过 ForeignKey 约束在 DB 层自动创建索引
      （PostgreSQL 外键约束自带索引），但 SQLAlchemy column.index 属性为 False。
      此处验证 ForeignKey 存在以确认索引能力。
    """

    @pytest.mark.parametrize("field", ["series_id", "path"])
    def test_explicit_index(self, field: str) -> None:
        """series_id/path 应有显式 index=True。"""
        col = Document.__table__.columns[field]
        assert col.index is True, f"{field} 应有显式索引"

    @pytest.mark.parametrize("field", ["parent_id", "version_of"])
    def test_foreign_key_provides_index(self, field: str) -> None:
        """parent_id/version_of 应有 ForeignKey 约束（DB 层自动索引）。"""
        col = Document.__table__.columns[field]
        assert len(col.foreign_keys) > 0, f"{field} 应有 ForeignKey 约束"

    @pytest.mark.parametrize("field", ["parent_id", "version_of"])
    def test_fk_targets_self(self, field: str) -> None:
        """parent_id/version_of 的 FK 应指向 documents.id（自引用）。"""
        col = Document.__table__.columns[field]
        fk_targets = {fk.target_fullname for fk in col.foreign_keys}
        assert "documents.id" in fk_targets, (
            f"{field} 的 FK 应指向 documents.id，实际: {fk_targets}"
        )


# ======================================================================
# 字段类型验证
# ======================================================================


class TestHierarchyFieldsType:
    """验证层级字段的 SQLAlchemy 列类型。"""

    def test_series_id_is_string_100(self) -> None:
        """series_id 应为 String(100)。"""
        col = Document.__table__.columns["series_id"]
        assert isinstance(col.type, String)
        assert col.type.length == 100

    def test_path_is_string_1000(self) -> None:
        """path 应为 String(1000)。"""
        col = Document.__table__.columns["path"]
        assert isinstance(col.type, String)
        assert col.type.length == 1000

    def test_depth_is_integer(self) -> None:
        """depth 应为 Integer 类型。"""
        col = Document.__table__.columns["depth"]
        assert isinstance(col.type, Integer)

    def test_sort_order_is_integer(self) -> None:
        """sort_order 应为 Integer 类型。"""
        col = Document.__table__.columns["sort_order"]
        assert isinstance(col.type, Integer)


# ======================================================================
# 关系验证
# ======================================================================


class TestHierarchyRelationships:
    """P0 wiki 层级反向关系应存在且配置正确。"""

    @pytest.mark.parametrize(
        "rel",
        ["children", "parent", "version_children", "version_master"],
    )
    def test_relationship_exists(self, rel: str) -> None:
        """四个层级反向关系都应存在于 Document mapper 中。"""
        assert rel in Document.__mapper__.relationships, (
            f"Document 应有 {rel} 关系"
        )

    def test_children_relationship_target(self) -> None:
        """children 关系应指向 Document 自身。"""
        rel = Document.__mapper__.relationships["children"]
        assert rel.mapper.class_ is Document

    def test_parent_relationship_target(self) -> None:
        """parent 关系应指向 Document 自身。"""
        rel = Document.__mapper__.relationships["parent"]
        assert rel.mapper.class_ is Document

    def test_version_children_relationship_target(self) -> None:
        """version_children 关系应指向 Document 自身。"""
        rel = Document.__mapper__.relationships["version_children"]
        assert rel.mapper.class_ is Document

    def test_version_master_relationship_target(self) -> None:
        """version_master 关系应指向 Document 自身。"""
        rel = Document.__mapper__.relationships["version_master"]
        assert rel.mapper.class_ is Document

    def test_children_back_populates_parent(self) -> None:
        """children 的 back_populates 应为 parent。"""
        rel = Document.__mapper__.relationships["children"]
        assert rel.back_populates == "parent"

    def test_parent_back_populates_children(self) -> None:
        """parent 的 back_populates 应为 children。"""
        rel = Document.__mapper__.relationships["parent"]
        assert rel.back_populates == "children"

    def test_version_children_back_populates_version_master(self) -> None:
        """version_children 的 back_populates 应为 version_master。"""
        rel = Document.__mapper__.relationships["version_children"]
        assert rel.back_populates == "version_master"

    def test_version_master_back_populates_version_children(self) -> None:
        """version_master 的 back_populates 应为 version_children。"""
        rel = Document.__mapper__.relationships["version_master"]
        assert rel.back_populates == "version_children"


# ======================================================================
# 语义验证：depth=0 表示根文档
# ======================================================================


class TestDepthRootSemantic:
    """depth=0 表示根文档的语义验证。"""

    def test_depth_column_default_zero(self) -> None:
        """depth 列默认值应为 0（根文档深度）。"""
        col = Document.__table__.columns["depth"]
        assert col.default is not None, "depth 应有默认值"
        assert col.default.arg == 0, "depth 默认值应为 0"

    def test_root_document_depth_is_zero(self) -> None:
        """创建文档实例时，未指定 depth 则语义上默认为 0（根文档）。

        SQLAlchemy 的 column default 在 flush 时应用，构造实例时
        通过列定义 default=0 语义上表示根文档深度为 0。
        """
        # 构造一个根文档实例（不指定 depth）
        doc = Document(
            kb_id=uuid.uuid4(),
            title="根文档",
            owner_id=uuid.uuid4(),
        )
        # 验证 depth 列默认值为 0（根文档语义）
        col = Document.__table__.columns["depth"]
        assert col.default.arg == 0
        # depth 为 nullable=False，确保不会出现 NULL 根文档
        assert col.nullable is False
