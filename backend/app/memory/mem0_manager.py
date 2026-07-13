"""
Mem0 当前事实管理 — 单一职责：存储和检索跨会话的用户事实。

定位：高频缓存、用户偏好、工作记忆。
特点：KV + Embedding 双索引，支持语义检索和精确匹配。

遵循开闭原则：新增事实类型只需在 FACT_CATEGORIES 注册。
ORM 模型定义在 app.models.memory.MemoryFact，避免循环导入。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.memory import MemoryFact
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


# === 事实类别注册表（开闭原则：新增类别只需追加） ===

FACT_CATEGORIES = {
    "preference": "用户偏好（如：偏好简洁回答、喜欢中文回复）",
    "working": "工作记忆（如：当前正在处理的报销单号）",
    "summary": "对话摘要（如：上次讨论了微服务架构设计）",
    "entity": "实体记忆（如：用户是产品部的高级工程师）",
}


class Mem0Manager:
    """Mem0 当前事实管理器 — 存储和检索跨会话的用户事实。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._embedder = None  # 延迟初始化

    @property
    def embedder(self):
        """延迟初始化 Embedder（避免启动时连接模型服务）。"""
        if self._embedder is None:
            from app.llm.factory import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    async def add_fact(
        self,
        user_id: uuid.UUID,
        fact_text: str,
        category: str = "working",
        fact_key: str | None = None,
        fact_value: str | None = None,
        ttl_hours: int | None = None,
    ) -> MemoryFact:
        """添加一条用户事实。

        Args:
            user_id: 用户 ID
            fact_text: 事实内容（自然语言描述）
            category: 类别（preference/working/summary/entity）
            fact_key: 结构化键（可选，用于精确查询）
            fact_value: 结构化值（可选）
            ttl_hours: 过期时间（小时），None 表示永不过期
        """
        if category not in FACT_CATEGORIES:
            logger.warning("unknown_fact_category", category=category)
            category = "working"

        # 生成向量嵌入（用于语义检索）
        embedding = None
        try:
            embeddings = await self.embedder.embed([fact_text])
            embedding = embeddings[0] if embeddings else None
        except Exception as e:
            logger.error("embedding_failed", error=str(e))

        # 计算过期时间
        expires_at = None
        if ttl_hours is not None:
            expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)

        fact = MemoryFact(
            user_id=user_id,
            category=category,
            fact_text=fact_text,
            fact_key=fact_key,
            fact_value=fact_value,
            embedding=embedding,
            expires_at=expires_at,
        )
        self.db.add(fact)
        await self.db.flush()
        logger.info("fact_added", user_id=str(user_id), category=category, fact=fact_text[:100])
        return fact

    async def search_facts(
        self,
        user_id: uuid.UUID,
        query: str | None = None,
        category: str | None = None,
        limit: int = 10,
    ) -> list[MemoryFact]:
        """检索用户事实。

        Args:
            user_id: 用户 ID
            query: 语义查询（为 None 则返回最近的事实）
            category: 类别过滤
            limit: 返回数量
        """
        stmt = (
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id, MemoryFact.is_active == True)
            .order_by(MemoryFact.created_at.desc())
            .limit(limit)
        )

        # 过期的事实标记为无效
        stmt = stmt.where(
            (MemoryFact.expires_at.is_(None)) | (MemoryFact.expires_at > func.now())
        )

        if category:
            stmt = stmt.where(MemoryFact.category == category)

        result = await self.db.execute(stmt)
        facts = result.scalars().all()

        # 如果有查询文本，做简单的关键词过滤（语义检索由向量数据库完成）
        if query and facts:
            query_lower = query.lower()
            facts = [f for f in facts if query_lower in f.fact_text.lower()] or facts

        return facts

    async def get_preference(self, user_id: uuid.UUID, key: str) -> str | None:
        """获取用户偏好（精确查询）。"""
        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.category == "preference",
            MemoryFact.fact_key == key,
            MemoryFact.is_active == True,
        )
        result = await self.db.execute(stmt)
        fact = result.scalar_one_or_none()
        return fact.fact_value if fact else None

    async def set_preference(
        self, user_id: uuid.UUID, key: str, value: str, fact_text: str | None = None
    ) -> MemoryFact:
        """设置用户偏好（覆盖旧值）。"""
        # 先禁用旧的
        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.category == "preference",
            MemoryFact.fact_key == key,
            MemoryFact.is_active == True,
        )
        result = await self.db.execute(stmt)
        for old in result.scalars():
            old.is_active = False

        # 创建新的
        return await self.add_fact(
            user_id=user_id,
            fact_text=fact_text or f"{key}: {value}",
            category="preference",
            fact_key=key,
            fact_value=value,
        )

    async def deactivate_fact(self, fact_id: uuid.UUID) -> bool:
        """停用一条事实（软删除）。"""
        stmt = select(MemoryFact).where(MemoryFact.id == fact_id)
        result = await self.db.execute(stmt)
        fact = result.scalar_one_or_none()
        if fact:
            fact.is_active = False
            await self.db.flush()
            return True
        return False

    async def cleanup_expired(self) -> int:
        """清理过期事实 — 定时任务调用。"""
        stmt = select(MemoryFact).where(
            MemoryFact.is_active == True,
            MemoryFact.expires_at.is_not(None),
            MemoryFact.expires_at < func.now(),
        )
        result = await self.db.execute(stmt)
        count = 0
        for fact in result.scalars():
            fact.is_active = False
            count += 1
        if count:
            await self.db.flush()
            logger.info("expired_facts_cleaned", count=count)
        return count
