"""
Graphiti 时序图谱管理 — 单一职责：追踪知识时间线和实体演化。

定位：知识时间线、实体关系演化、知识过期预警。
特点：图 + 时间区间，记录事实的"有效时间段"。

与 Mem0 的分工：
  Mem0：当前事实（"现在是什么"）— 高频读写
  Graphiti：时间线追踪（"什么时候变成了什么"）— 低频写、分析用

遵循开闭原则：新增事件类型只需在 EVENT_TYPES 注册。
ORM 模型定义在 app.models.memory（KnowledgeEntity / EntityEvent），避免循环导入。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.memory import EntityEvent, KnowledgeEntity
from app.utils.logger import get_logger

logger = get_logger(__name__)


# === 事件类型注册表 ===

EVENT_TYPES = {
    "version_updated": "版本更新（如：架构规范从 v1 升到 v2）",
    "status_changed": "状态变更（如：文档从 draft 变为 published）",
    "expired": "知识过期（如：旧版政策已失效）",
    "deprecated": "知识废弃（如：技术栈不再推荐）",
    "merged": "实体合并（如：两个重复概念合并）",
    "split": "实体拆分（如：一个产品拆成两个）",
}


class GraphitiManager:
    """Graphiti 时序图谱管理器 — 追踪知识演化和过期。"""

    def __init__(self, db):
        self.db = db

    async def register_entity(
        self,
        entity_type: str,
        name: str,
        entity_ref_id: uuid.UUID | None = None,
        version: str = "v1",
    ) -> KnowledgeEntity:
        """注册一个知识实体（开始追踪其时间线）。"""
        entity = KnowledgeEntity(
            entity_type=entity_type,
            entity_ref_id=entity_ref_id,
            name=name,
            current_version=version,
            valid_from=datetime.utcnow(),
        )
        self.db.add(entity)
        await self.db.flush()
        logger.info(
            "entity_registered",
            entity_type=entity_type,
            name=name,
            version=version,
        )
        return entity

    async def record_event(
        self,
        entity_id: uuid.UUID,
        event_type: str,
        old_value: str | None = None,
        new_value: str | None = None,
        source: str = "system",
    ) -> EntityEvent:
        """记录一个实体变更事件，并更新实体的当前状态。"""
        if event_type not in EVENT_TYPES:
            logger.warning("unknown_event_type", event_type=event_type)
            event_type = "status_changed"

        now = datetime.utcnow()

        # 关闭上一个事件的 valid_to
        prev_event = await self.db.execute(
            select(EntityEvent).where(
                EntityEvent.entity_id == entity_id,
                EntityEvent.valid_to.is_(None),
            )
        )
        for prev in prev_event.scalars():
            prev.valid_to = now

        # 创建新事件
        event = EntityEvent(
            entity_id=entity_id,
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            event_source=source,
            valid_from=now,
        )
        self.db.add(event)

        # 更新实体版本（如果是版本更新事件）
        if event_type == "version_updated" and new_value:
            entity_result = await self.db.execute(
                select(KnowledgeEntity).where(KnowledgeEntity.id == entity_id)
            )
            entity = entity_result.scalar_one_or_none()
            if entity:
                entity.current_version = new_value

        await self.db.flush()
        logger.info(
            "event_recorded",
            entity_id=str(entity_id),
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
        )
        return event

    async def get_entity_timeline(
        self, entity_id: uuid.UUID
    ) -> list[EntityEvent]:
        """获取实体的完整变更历史（时间线）。"""
        result = await self.db.execute(
            select(EntityEvent)
            .where(EntityEvent.entity_id == entity_id)
            .order_by(EntityEvent.valid_from.asc())
        )
        return list(result.scalars().all())

    async def get_expired_entities(self) -> list[KnowledgeEntity]:
        """获取已过期的实体 — 用于知识过期预警。"""
        result = await self.db.execute(
            select(KnowledgeEntity).where(
                KnowledgeEntity.valid_to.is_not(None),
                KnowledgeEntity.valid_to < datetime.utcnow(),
            )
        )
        return list(result.scalars().all())

    async def get_expiring_soon(
        self, days: int = 30
    ) -> list[KnowledgeEntity]:
        """获取即将过期的实体（默认 30 天内）— 用于过期预警提醒。"""
        threshold = datetime.utcnow() + timedelta(days=days)
        result = await self.db.execute(
            select(KnowledgeEntity).where(
                KnowledgeEntity.valid_to.is_not(None),
                KnowledgeEntity.valid_to <= threshold,
                KnowledgeEntity.valid_to > datetime.utcnow(),
            )
        )
        return list(result.scalars().all())

    async def expire_entity(self, entity_id: uuid.UUID, source: str = "system") -> None:
        """将实体标记为过期。"""
        await self.record_event(
            entity_id=entity_id,
            event_type="expired",
            new_value="expired",
            source=source,
        )

        result = await self.db.execute(
            select(KnowledgeEntity).where(KnowledgeEntity.id == entity_id)
        )
        entity = result.scalar_one_or_none()
        if entity:
            entity.valid_to = datetime.utcnow()
            await self.db.flush()

    # ------------------------------------------------------------------
    # Neo4j 可选后端（时序事件同步到图数据库）
    # ------------------------------------------------------------------

    async def sync_to_graph(self, entity_id: uuid.UUID) -> bool:
        """将实体及其事件时间线同步到 Neo4j 图数据库。
        
        在 Neo4j 中创建：
        - 实体节点（带 current_version, valid_from, valid_to）
        - 事件节点（带 event_type, old_value, new_value, valid_from）
        - (实体)-[:HAS_EVENT]->(事件) 关系
        
        PostgreSQL 保留为主存储，Neo4j 为可选的图分析后端。
        
        Args:
            entity_id: 实体 ID。
            
        Returns:
            是否同步成功。
        """
        try:
            from app.services.graph_service import get_graph_service
            graph = get_graph_service()
            
            # 获取实体和时间线
            entity_result = await self.db.execute(
                select(KnowledgeEntity).where(KnowledgeEntity.id == entity_id)
            )
            entity = entity_result.scalar_one_or_none()
            if not entity:
                return False
            
            timeline = await self.get_entity_timeline(entity_id)
            
            # 创建实体节点
            await graph.create_node("KnowledgeEntity", {
                "id": str(entity.id),
                "entity_type": entity.entity_type,
                "name": entity.name,
                "current_version": entity.current_version,
                "valid_from": entity.valid_from.isoformat() if entity.valid_from else None,
                "valid_to": entity.valid_to.isoformat() if entity.valid_to else None,
            })
            
            # 创建事件节点并建立关系
            for event in timeline:
                await graph.create_node("TimelineEvent", {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "old_value": event.old_value,
                    "new_value": event.new_value,
                    "event_source": event.event_source,
                    "valid_from": event.valid_from.isoformat() if event.valid_from else None,
                    "valid_to": event.valid_to.isoformat() if event.valid_to else None,
                })
                await graph.create_relationship(
                    "KnowledgeEntity", str(entity.id),
                    "TimelineEvent", str(event.id),
                    "HAS_EVENT",
                )
            
            logger.info("graphiti.synced_to_graph", entity_id=str(entity_id), events=len(timeline))
            return True
        except Exception as e:
            logger.warning("graphiti.sync_to_graph_failed", error=str(e))
            return False
