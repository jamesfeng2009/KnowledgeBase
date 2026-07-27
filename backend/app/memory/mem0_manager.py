"""
Mem0 当前事实管理 — 单一职责：存储和检索跨会话的用户事实。

定位：高频缓存、用户偏好、工作记忆。
特点：KV + Embedding 双索引，支持语义检索和精确匹配。

遵循开闭原则：新增事实类型只需在 FACT_CATEGORIES 注册。
ORM 模型定义在 app.models.memory.MemoryFact，避免循环导入。
"""

import math
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.memory import MemoryFact
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    用于 Mem0 事实的语义检索 — 将 query 向量与已存储的 embedding 比较。
    """
    if not vec_a or not vec_b:
        return 0.0
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(x * x for x in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    return dot / (norm_a * norm_b)


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
        similarity_threshold: float = 0.3,
    ) -> list[MemoryFact]:
        """检索用户事实 — 支持向量语义检索 + 关键词降级。

        检索策略（优先级降级）：
            1. 语义检索：query 非空时，生成 query 向量，与已存储的 embedding
               做余弦相似度排序，返回 top-k。仅取相似度 >= threshold 的事实。
            2. 关键词降级：Embedder 不可用或事实无 embedding 时，回退到
               关键词包含匹配。
            3. 时间排序：query 为 None 时，按 created_at 降序返回最近事实。

        Args:
            user_id: 用户 ID
            query: 语义查询（为 None 则返回最近的事实）
            category: 类别过滤
            limit: 返回数量
            similarity_threshold: 语义相似度阈值（低于此值不返回）
        """
        stmt = (
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id, MemoryFact.is_active == True)
            .order_by(MemoryFact.created_at.desc())
            .limit(limit * 3 if query else limit)  # 语义检索时多取候选再排序
        )

        # 过期的事实标记为无效
        stmt = stmt.where(
            (MemoryFact.expires_at.is_(None)) | (MemoryFact.expires_at > func.now())
        )

        if category:
            stmt = stmt.where(MemoryFact.category == category)

        result = await self.db.execute(stmt)
        db_facts = result.scalars().all()

        if not query or not db_facts:
            return db_facts

        # --- 语义检索：使用 embedding 做余弦相似度排序 ---
        query_vec = None
        try:
            embeddings = await self.embedder.embed([query])
            query_vec = embeddings[0] if embeddings else None
        except Exception as e:
            logger.warning("search_query_embedding_failed", error=str(e))

        if query_vec is not None:
            # 计算每条事实的相似度
            scored: list[tuple[float, MemoryFact]] = []
            for fact in db_facts:
                if fact.embedding:
                    sim = _cosine_similarity(query_vec, fact.embedding)
                    if sim >= similarity_threshold:
                        scored.append((sim, fact))
                else:
                    # 无 embedding 的事实，用关键词匹配兜底
                    if query.lower() in fact.fact_text.lower():
                        scored.append((0.0, fact))

            # 按相似度降序排序
            scored.sort(key=lambda x: x[0], reverse=True)
            semantic_results = [f for _, f in scored[:limit]]

            if semantic_results:
                logger.debug(
                    "semantic_search_done",
                    query=query[:50],
                    candidates=len(semantic_results),
                    top_score=scored[0][0] if scored else 0.0,
                )
                return semantic_results

            # 语义检索无结果 → 降级到关键词匹配（在原始 DB 结果上）
            logger.debug("semantic_search_no_match_fallback_keyword")

        # --- 关键词降级（在原始 DB 结果上操作） ---
        query_lower = query.lower()
        keyword_matched = [f for f in db_facts if query_lower in f.fact_text.lower()]
        return keyword_matched[:limit] if keyword_matched else db_facts[:limit]

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
