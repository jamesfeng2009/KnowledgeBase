"""
文件夹模型 — 知识库文件夹树（P0-3）。

层级表达约定（与 Document.path 对齐，避免双轨）：
    - path 为 '/'-分隔的字符串，如 "产品/合规/数据安全"，无前导斜杠；
    - depth = path 分段数（根文件夹 depth=1，文档 depth=path 分段数）；
    - parent_path = path 去掉末级（根文件夹为 NULL）。

kb_folders 表承载显式创建的文件夹节点（可为空文件夹），
树形视图由 folders + documents 聚合（FolderService.list_tree）。
上传文档时保留原始目录结构写入 Document.path，不强制创建对应 folder 行。

遵循单一职责：本模块仅定义模型，不包含业务逻辑。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class KbFolder(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """知识库文件夹表。

    (kb_id, path) 唯一性由 Service 层保证（软删除下不建 DB 唯一约束，
    与知识候选池等模块的"Service 层查重"约定一致）。
    """

    __tablename__ = "kb_folders"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id"),
        nullable=False,
        index=True,
        comment="知识库 ID",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="文件夹名（末级名称）")
    path: Mapped[str] = mapped_column(
        String(1000), nullable=False, index=True, comment="完整路径 '产品/合规'"
    )
    parent_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, comment="父路径（根文件夹为 NULL）"
    )
    depth: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="层级深度（根文件夹=1）"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="同级排序"
    )
    # 多租户隔离 — SaaS 模式下按租户隔离，私有部署为 NULL
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
