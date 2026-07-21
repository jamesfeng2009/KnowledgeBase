"""
知识回流层模型 — 单一职责：定义知识资产、回流任务、知识冲突表。

核心流程（5 步）：
    执行结果收集 → AI 知识提取 → 知识资产沉淀 → 冲突检测 → 复用注入

4 类知识资产：
    defect_experience     — 缺陷经验文档（存入 Document + AI 摘要/标签）
    regression_sop        — 回归 SOP（存入 Document + 关联 TestPlan）
    graph_association     — 知识图谱关联（Neo4j: requirement→case→defect→fix）
    verification_baseline — 验证基线时序（Graphiti: 旧基线 → historical_reference）

复用现有能力：
    - Document 表：缺陷经验/回归 SOP 沉淀为知识库文档
    - GraphService：知识图谱关联写入 Neo4j
    - GraphitiManager：验证基线时序追踪
    - LLMProvider：AI 知识提取
    - 多租户：tenant_id 字段预留
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


# ======================================================================
# 知识资产 — 4 类知识资产的统一存储
# ======================================================================


class KnowledgeAsset(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """知识资产表 — 测试执行后沉淀的 4 类知识资产。

    资产类型（asset_type）：
        defect_experience     — 缺陷经验文档
        regression_sop        — 回归 SOP
        graph_association     — 知识图谱关联
        verification_baseline — 验证基线时序

    生命周期：draft → active → deprecated
    冲突状态：conflict（检测到与已有资产冲突时标记）
    """

    __tablename__ = "knowledge_assets"

    # 资产类型: defect_experience / regression_sop / graph_association / verification_baseline
    asset_type: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True, comment="资产类型"
    )
    # 来源类型: test_execution / test_case / test_requirement / manual
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="来源类型"
    )
    # 来源实体 ID（如 TestExecution.id / TestCase.id）
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="来源实体 ID"
    )
    # 关联测试项目
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_projects.id"), nullable=True, comment="测试项目 ID"
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="资产标题"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="知识内容（自然语言描述）"
    )
    # AI 生成的摘要
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="AI 生成的摘要"
    )
    # AI 提取的标签
    tags: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="标签列表"
    )
    # 沉淀为知识库文档时的 Document ID（defect_experience / regression_sop 类型）
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="沉淀的文档 ID（知识库 Document）"
    )
    # 知识图谱节点和关系（graph_association 类型）
    graph_nodes: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="图谱节点列表（graph_association 类型）"
    )
    graph_relationships: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="图谱关系列表（graph_association 类型）"
    )
    # Graphiti 实体 ID（verification_baseline 类型）
    graphiti_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="Graphiti 实体 ID（verification_baseline 类型）"
    )
    # AI 置信度（0.0 ~ 1.0）
    confidence_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="AI 置信度（0.0~1.0）"
    )
    # 资产状态: draft / active / deprecated / conflict
    status: Mapped[str] = mapped_column(
        String(20), default="draft", index=True, comment="状态: draft/active/deprecated/conflict"
    )
    # 冲突关联的资产 ID 列表
    conflict_with: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="冲突的资产 ID 列表"
    )
    # 关联的回流任务 ID
    compounding_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("compounding_tasks.id"), nullable=True, comment="回流任务 ID"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    compounding_task: Mapped["CompoundingTask | None"] = relationship(back_populates="assets")
    conflicts: Mapped[list["KnowledgeConflict"]] = relationship(
        back_populates="new_asset",
        foreign_keys="KnowledgeConflict.new_asset_id",
        cascade="all, delete-orphan",
    )


# ======================================================================
# 回流任务 — 跟踪异步知识提取过程
# ======================================================================


class CompoundingTask(UUIDMixin, TimestampMixin, Base):
    """回流任务表 — 跟踪知识提取的异步执行过程。

    任务类型（task_type）：
        extraction          — 知识提取（从执行结果提取知识资产）
        conflict_detection  — 冲突检测（检测新旧知识冲突）
        reuse_injection     — 复用注入（将历史知识注入新一轮用例生成）

    触发来源（trigger_source）：
        execution_completed — 执行完成后自动触发
        manual              — 手动触发
        scheduled           — 定时任务触发
    """

    __tablename__ = "compounding_tasks"

    # 关联的执行记录（extraction 类型任务）
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_executions.id"), nullable=True, comment="执行记录 ID"
    )
    # 关联测试项目
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_projects.id"), nullable=True, comment="测试项目 ID"
    )
    # 任务类型: extraction / conflict_detection / reuse_injection
    task_type: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True, comment="任务类型"
    )
    # 状态: pending / running / completed / failed / skipped
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, comment="状态: pending/running/completed/failed/skipped"
    )
    # 触发来源: execution_completed / manual / scheduled
    trigger_source: Mapped[str] = mapped_column(
        String(30), default="execution_completed", comment="触发来源"
    )
    # 提取的资产 ID 列表
    extracted_asset_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="提取的资产 ID 列表"
    )
    # 检测到的冲突数量
    conflicts_detected: Mapped[int] = mapped_column(
        Integer, default=0, comment="检测到的冲突数量"
    )
    # 注入的历史资产数量（reuse_injection 类型）
    assets_injected: Mapped[int] = mapped_column(
        Integer, default=0, comment="注入的历史资产数量"
    )
    # 错误信息（failed 时填写）
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误信息"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    assets: Mapped[list["KnowledgeAsset"]] = relationship(
        back_populates="compounding_task", cascade="all, delete-orphan"
    )


# ======================================================================
# 知识冲突 — 检测到的新旧知识冲突记录
# ======================================================================


class KnowledgeConflict(UUIDMixin, TimestampMixin, Base):
    """知识冲突表 — 记录检测到的新旧知识冲突。

    冲突类型（conflict_type）：
        contradiction — 矛盾（新旧知识直接冲突）
        supersede     — 替代（新知识替代旧知识）
        overlap       — 重叠（新旧知识内容重叠但不冲突）

    解决方案（resolution）：
        new_wins       — 新知识胜出
        existing_wins  — 旧知识胜出
        merged         — 合并
        pending        — 待处理
    """

    __tablename__ = "knowledge_conflicts"

    # 新资产 ID
    new_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_assets.id"), nullable=False, comment="新资产 ID"
    )
    # 已有资产 ID（不设外键，因为可能引用已软删除的资产）
    existing_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="已有资产 ID"
    )
    # 冲突类型: contradiction / supersede / overlap
    conflict_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="冲突类型"
    )
    # 冲突描述
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="冲突描述"
    )
    # 解决方案: new_wins / existing_wins / merged / pending
    resolution: Mapped[str] = mapped_column(
        String(20), default="pending", comment="解决方案"
    )
    # 处理人
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="处理人 ID"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="处理时间"
    )
    # 解决备注
    resolution_note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="解决备注"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    new_asset: Mapped["KnowledgeAsset"] = relationship(
        back_populates="conflicts", foreign_keys=[new_asset_id]
    )
