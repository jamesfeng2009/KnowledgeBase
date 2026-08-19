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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.memory.forgetting import DefaultActivation
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


def _time_decay(
    created_at: datetime | None,
    now_ts: float,
    half_life_days: float,
) -> float:
    """计算时间衰减因子 — 近期事实权重更高。

    公式：decay = exp(-ln(2) * age_days / half_life_days)
    当 age = half_life 时 decay = 0.5，当 age = 2*half_life 时 decay = 0.25。

    Args:
        created_at: 事实创建时间（None 视为刚创建，decay=1.0）。
        now_ts: 当前 Unix 时间戳。
        half_life_days: 半衰期（天），0 或负数表示禁用衰减。

    Returns:
        衰减因子，范围 (0, 1]。
    """
    if half_life_days <= 0:
        return 1.0
    if created_at is None:
        return 1.0
    created_ts = created_at.timestamp() if isinstance(created_at, datetime) else float(created_at)
    age_days = (now_ts - created_ts) / 86400.0
    if age_days < 0:
        return 1.0  # 未来时间不衰减
    return math.exp(-0.693147 * age_days / half_life_days)  # ln(2) ≈ 0.693147


# === 事实类别注册表（开闭原则：新增类别只需追加） ===

FACT_CATEGORIES = {
    "preference": "用户偏好（如：偏好简洁回答、喜欢中文回复）",
    "fact": "事实信息（如：项目截止日期、团队成员姓名）",
    "working": "工作记忆（如：当前正在处理的报销单号）",
    "summary": "对话摘要（如：上次讨论了微服务架构设计）",
    "entity": "实体记忆（如：用户是产品部的高级工程师）",
    "detail": "对话历史细节（压缩后按需召回，如：第3轮确认的截止日期）",
}

# 时间衰减半衰期（天）— 超过半衰期的事实权重减半
_DEFAULT_HALF_LIFE_DAYS = 30.0


class Mem0Manager:
    """Mem0 当前事实管理器 — 存储和检索跨会话的用户事实。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._embedder = None  # 延迟初始化
        # 机制一：召回时实时算激活值（ACT-R 三因子，纯函数）
        self._activation = DefaultActivation()

    @property
    def activation_policy(self) -> DefaultActivation:
        """激活值策略（测试与上层可替换）。"""
        return self._activation

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
        source_type: str | None = None,
        source_ref_id: uuid.UUID | None = None,
        raw_excerpt: str | None = None,
    ) -> MemoryFact:
        """添加一条用户事实。

        冲突处理：当新事实与已有活跃事实冲突时（同 category + fact_key
        但 fact_value 不同），自动停用旧事实。

        Args:
            user_id: 用户 ID
            fact_text: 事实内容（自然语言描述）
            category: 类别（preference/working/summary/entity）
            fact_key: 结构化键（可选，用于精确查询）
            fact_value: 结构化值（可选）
            ttl_hours: 过期时间（小时），None 表示永不过期
            source_type: P0-1 来源类型（message/document/tool/feedback）
            source_ref_id: P0-1 来源引用 ID
            raw_excerpt: P0-1 原始摘录文本（溯源核验用）
        """
        if category not in FACT_CATEGORIES:
            logger.warning("unknown_fact_category", category=category)
            category = "working"

        # --- 冲突检测：同 key 不同 value → 自动停用旧事实 ---
        deactivated = await self._deactivate_conflicting(
            user_id=user_id,
            category=category,
            fact_key=fact_key,
            fact_value=fact_value,
        )
        if deactivated:
            logger.info(
                "conflicting_facts_deactivated",
                user_id=str(user_id),
                category=category,
                fact_key=fact_key,
                deactivated_count=deactivated,
            )

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
            embedding_vec=embedding,  # pgvector 列（同时写入）
            expires_at=expires_at,
            source_type=source_type,
            source_ref_id=source_ref_id,
            raw_excerpt=raw_excerpt,
        )
        self.db.add(fact)
        await self.db.flush()
        logger.info("fact_added", user_id=str(user_id), category=category, fact=fact_text[:100])
        return fact

    async def _deactivate_conflicting(
        self,
        user_id: uuid.UUID,
        category: str,
        fact_key: str | None,
        fact_value: str | None,
    ) -> int:
        """检测并停用与新事实冲突的已有活跃事实。

        冲突定义：同 category + fact_key，但 fact_value 不同。
        仅当新事实带有 fact_key 和 fact_value 时才触发。

        Returns:
            停用的旧事实数量。
        """
        if not fact_key or fact_value is None:
            return 0

        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.category == category,
            MemoryFact.fact_key == fact_key,
            MemoryFact.is_active == True,
            MemoryFact.fact_value != fact_value,
        )
        result = await self.db.execute(stmt)
        conflicts = result.scalars().all()

        for old in conflicts:
            old.is_active = False
            logger.info(
                "conflict_deactivated",
                old_fact=old.fact_text[:80],
                old_value=old.fact_value,
                new_value=fact_value,
            )

        if conflicts:
            await self.db.flush()
        return len(conflicts)

    async def correct_fact(
        self,
        fact_id: uuid.UUID,
        corrected_text: str | None = None,
        corrected_value: str | None = None,
    ) -> MemoryFact | None:
        """用户纠错入口 — 停用错误事实，可选写入纠正后的新事实。

        Args:
            fact_id: 要纠正的事实 ID。
            corrected_text: 纠正后的事实文本（None 则仅停用旧事实）。
            corrected_value: 纠正后的结构化值（None 则沿用旧值）。

        Returns:
            新创建的纠正事实；如果 fact_id 不存在返回 None。
        """
        stmt = select(MemoryFact).where(MemoryFact.id == fact_id)
        result = await self.db.execute(stmt)
        old_fact = result.scalar_one_or_none()

        if old_fact is None:
            logger.warning("correct_fact_not_found", fact_id=str(fact_id))
            return None

        # 停用旧事实
        old_fact.is_active = False
        await self.db.flush()

        logger.info(
            "fact_corrected",
            fact_id=str(fact_id),
            old_text=old_fact.fact_text[:80],
            new_text=(corrected_text or "")[:80],
        )

        # 无纠正内容 → 仅停用（纯删除纠错）
        if corrected_text is None:
            return old_fact

        # 写入纠正后的新事实
        new_fact = await self.add_fact(
            user_id=old_fact.user_id,
            fact_text=corrected_text,
            category=old_fact.category,
            fact_key=old_fact.fact_key,
            fact_value=corrected_value if corrected_value is not None else old_fact.fact_value,
        )
        return new_fact

    async def search_facts(
        self,
        user_id: uuid.UUID,
        query: str | None = None,
        category: str | None = None,
        limit: int = 10,
        similarity_threshold: float = 0.3,
        half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
        fallback_to_latest: bool = True,
        update_access_stats: bool = True,
    ) -> list[MemoryFact]:
        """检索用户事实 — 语义检索 + 激活值闸门 + 命中写回。

        检索策略（优先级降级）：
            1. 语义检索：query 非空时，生成 query 向量，与已存储的 embedding
               做余弦相似度排序，返回 top-k。仅取相似度 >= threshold 的事实。
            2. 激活值闸门（机制一）：排序分 = 相似度 × 激活值（ACT-R 三因子：
               时间衰减 + 频率增益 + 近期增益）。激活值低于地板值的当场跳过 —
               老且无人问津的记忆不再上场。
            3. 复活窗口（P2）：被冲突整合 superseded 的记忆保留 N 天，
               窗口期内若被强命中（相似度 >= 复活阈值）自动复活。
            4. 关键词降级：Embedder 不可用或事实无 embedding 时，回退到
               关键词包含匹配。
            5. 时间排序：query 为 None 时，按 created_at 降序返回最近事实
               （仅活跃记忆，不触发写回）。

        Args:
            user_id: 用户 ID
            query: 语义查询（为 None 则返回最近的事实）
            category: 类别过滤
            limit: 返回数量
            similarity_threshold: 语义相似度阈值（低于此值不返回）
            half_life_days: 已废弃 — 衰减尺度由激活值策略接管，保留签名兼容
            fallback_to_latest: 语义/关键词均无命中时是否兜底返回最新 N 条
                （与 query 无关）。记忆上下文注入场景保持 True；判重场景
                必须传 False，否则只要有任意同类别事实就会被误判为重复。
            update_access_stats: 命中后是否写回访问统计（access_count +1、
                最近访问刷新、偏好滚动续命）。内部判重/冲突检测必须传 False —
                检索 ≠ 用户真实召回。
        """
        revival_cutoff = datetime.utcnow() - timedelta(
            days=settings.MEMORY_REVIVAL_WINDOW_DAYS
        )
        stmt = (
            select(MemoryFact)
            .where(
                MemoryFact.user_id == user_id,
                # 活跃记忆 + 复活窗口内被 superseded 的记忆（P2 软删除窗口）
                or_(
                    MemoryFact.is_active == True,
                    and_(
                        MemoryFact.superseded_at.is_not(None),
                        MemoryFact.superseded_at > revival_cutoff,
                    ),
                ),
            )
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
            # 无 query 的浏览路径：只返回活跃记忆，不触发复活与写回
            return [f for f in db_facts if f.is_active]

        # --- 语义检索：使用 embedding 做余弦相似度排序 ---
        query_vec = None
        try:
            embeddings = await self.embedder.embed([query])
            query_vec = embeddings[0] if embeddings else None
        except Exception as e:
            logger.warning("search_query_embedding_failed", error=str(e))

        if query_vec is not None:
            # --- 优先尝试 pgvector 检索 ---
            try:
                pgvector_results = await self._search_by_pgvector(
                    user_id=user_id,
                    query_vec=query_vec,
                    category=category,
                    limit=limit,
                    similarity_threshold=similarity_threshold,
                    half_life_days=half_life_days,
                )
                if pgvector_results is not None:
                    return await self._finalize_results(
                        pgvector_results,
                        update_stats=update_access_stats,
                        query=query,
                    )
            except Exception as e:
                logger.debug("pgvector_search_fallback_jsonb", error=str(e))

            # --- JSONB 降级：Python 内存余弦相似度 ---
            scored: list[tuple[float, MemoryFact]] = []
            for fact in db_facts:
                if fact.embedding:
                    sim = _cosine_similarity(query_vec, fact.embedding)
                    if sim >= similarity_threshold:
                        scored.append((sim, fact))
                else:
                    if query.lower() in fact.fact_text.lower():
                        scored.append((0.0, fact))

            semantic_results, revived = self._rank_candidates(scored)
            if revived:
                await self.db.flush()
                logger.info("memory_revived_in_recall", count=len(revived))

            if semantic_results:
                logger.debug(
                    "semantic_search_done",
                    query=query[:50],
                    candidates=len(semantic_results),
                )
                return await self._finalize_results(
                    semantic_results[:limit],
                    update_stats=update_access_stats,
                    query=query,
                )

            logger.debug("semantic_search_no_match_fallback_keyword")

        # --- 关键词降级（在原始 DB 结果上操作） ---
        query_lower = query.lower()
        keyword_matched = [
            f
            for f in db_facts
            if f.is_active and query_lower in f.fact_text.lower()
        ]
        if keyword_matched:
            return await self._finalize_results(
                keyword_matched[:limit],
                update_stats=update_access_stats,
                query=query,
            )
        # 无命中时按调用方意图决定是否兜底返回最新 N 条（仅活跃记忆）
        latest = [f for f in db_facts if f.is_active]
        results = latest[:limit] if fallback_to_latest else []
        return await self._finalize_results(
            results, update_stats=update_access_stats, query=query
        )

    def _rank_candidates(
        self, scored: list[tuple[float, MemoryFact]]
    ) -> tuple[list[MemoryFact], list[MemoryFact]]:
        """按 相似度 × 激活值 排序，并应用两道闸门。

        闸门一（机制一）：激活值低于地板值的当场跳过 — 老且无人问津
        的记忆不再上场。
        闸门二（P2 复活窗口）：被 superseded 的窗口期记忆，强命中
        （相似度 >= 复活阈值）则复活并上场，否则继续挡在门外。

        Returns:
            (排序后的最终结果, 被复活的记忆列表)。
        """
        if not settings.MEMORY_ACTIVATION_ENABLED:
            ranked = sorted(scored, key=lambda x: x[0], reverse=True)
            return [f for _, f in ranked], []

        now = datetime.utcnow()
        floor = settings.MEMORY_ACTIVATION_FLOOR
        revival_threshold = settings.MEMORY_REVIVAL_THRESHOLD
        effective: list[tuple[float, MemoryFact]] = []
        revived: list[MemoryFact] = []

        for sim, fact in scored:
            if not getattr(fact, "is_active", True):
                # 窗口期内被 superseded：强命中复活，弱命中继续退场
                if sim >= revival_threshold:
                    fact.is_active = True
                    fact.superseded_by = None
                    fact.superseded_at = None
                    revived.append(fact)
                else:
                    continue
            activation = self._activation.activation(fact, now)
            if activation < floor:
                continue  # 时间衰减到地板以下 — 跳过
            effective.append((sim * activation, fact))

        effective.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in effective], revived

    async def _record_access(self, facts: list[MemoryFact]) -> None:
        """召回命中写回 — 访问次数 +1、最近访问刷新（激活值频率/近期因子的数据源）。

        偏好滚动续命：被召回即说明该偏好仍在使用，延长其 TTL —
        天天用的偏好十年不忘，长期不用则自然过期。
        """
        now = datetime.utcnow()
        for fact in facts:
            fact.access_count = (getattr(fact, "access_count", 0) or 0) + 1
            fact.last_accessed_at = now
            if fact.category == "preference" and fact.expires_at is not None:
                fact.expires_at = now + timedelta(hours=24 * 90)
        await self.db.flush()

    async def _finalize_results(
        self,
        results: list[MemoryFact],
        *,
        update_stats: bool,
        query: str | None,
    ) -> list[MemoryFact]:
        """统一收尾：真实语义召回（带 query）才写回访问统计。

        内部判重 / 冲突候选检索 / 管理查询由调用方传 update_stats=False。
        """
        if update_stats and query and results:
            try:
                await self._record_access(results)
            except Exception as e:
                logger.warning("access_stats_write_failed", error=str(e))
        return results

    async def _search_by_pgvector(
        self,
        user_id: uuid.UUID,
        query_vec: list[float],
        category: str | None,
        limit: int,
        similarity_threshold: float,
        half_life_days: float,
    ) -> list[MemoryFact] | None:
        """使用 pgvector 做语义检索（数据库内向量运算）。

        利用 pgvector 的 cosine_distance 操作符在数据库内计算相似度，
        避免将所有 embedding 加载到 Python 内存。

        流程：
            1. pgvector 按余弦距离排序取候选（含复活窗口内被
               superseded 的记忆）
            2. Python 侧做激活值重排 + 闸门过滤（_rank_candidates）
            3. 返回 top-k

        Returns:
            命中结果列表；如果 pgvector 不可用或无 vec 数据返回 None。
        """
        # 检查是否有任何 embedding_vec 数据
        check_stmt = (
            select(func.count(MemoryFact.id))
            .where(
                MemoryFact.user_id == user_id,
                MemoryFact.is_active == True,
                MemoryFact.embedding_vec.is_not(None),
            )
        )
        if category:
            check_stmt = check_stmt.where(MemoryFact.category == category)
        check_result = await self.db.execute(check_stmt)
        vec_count = check_result.scalar() or 0
        if vec_count == 0:
            return None  # 无 pgvector 数据，回退 JSONB

        revival_cutoff = datetime.utcnow() - timedelta(
            days=settings.MEMORY_REVIVAL_WINDOW_DAYS
        )
        # pgvector 余弦距离检索（距离越小越相似）
        distance_col = MemoryFact.embedding_vec.cosine_distance(query_vec)
        stmt = (
            select(MemoryFact, distance_col.label("distance"))
            .where(
                MemoryFact.user_id == user_id,
                # 活跃记忆 + 复活窗口内被 superseded 的记忆（P2 软删除窗口）
                or_(
                    MemoryFact.is_active == True,
                    and_(
                        MemoryFact.superseded_at.is_not(None),
                        MemoryFact.superseded_at > revival_cutoff,
                    ),
                ),
                MemoryFact.embedding_vec.is_not(None),
            )
            .order_by(distance_col)
            .limit(limit * 3)  # 多取候选再做激活值重排
        )

        if category:
            stmt = stmt.where(MemoryFact.category == category)

        stmt = stmt.where(
            (MemoryFact.expires_at.is_(None)) | (MemoryFact.expires_at > func.now())
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        if not rows:
            return None

        # 余弦距离 → 相似度（similarity = 1 - distance）
        scored: list[tuple[float, MemoryFact]] = []
        for row in rows:
            fact = row[0]
            distance = row[1]
            similarity = 1.0 - distance
            if similarity >= similarity_threshold:
                scored.append((similarity, fact))

        results, revived = self._rank_candidates(scored)
        if revived:
            await self.db.flush()
            logger.info("memory_revived_in_recall", count=len(revived))

        if results:
            logger.debug(
                "pgvector_search_done",
                candidates=len(results),
            )
            return results[:limit]

        return None  # 无满足阈值的结果，让上层走关键词降级

    async def get_preference(self, user_id: uuid.UUID, key: str) -> str | None:
        """获取用户偏好（精确查询）。

        滚动 TTL：命中时自动延期偏好生命周期。
        """
        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.category == "preference",
            MemoryFact.fact_key == key,
            MemoryFact.is_active == True,
        )
        result = await self.db.execute(stmt)
        fact = result.scalar_one_or_none()

        if fact:
            # 滚动 TTL：被访问时延期
            await self.touch_fact(fact.id)
            return fact.fact_value
        return None

    async def set_preference(
        self, user_id: uuid.UUID, key: str, value: str, fact_text: str | None = None
    ) -> MemoryFact:
        """设置用户偏好（覆盖旧值）。

        冲突停用由 add_fact 内置的 _deactivate_conflicting 处理。
        偏好使用滚动 TTL（90 天）：被访问时自动延期，长期不使用则过期。
        """
        return await self.add_fact(
            user_id=user_id,
            fact_text=fact_text or f"{key}: {value}",
            category="preference",
            fact_key=key,
            fact_value=value,
            ttl_hours=24 * 90,  # 偏好滚动 TTL：90 天
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

    async def touch_fact(
        self,
        fact_id: uuid.UUID,
        ttl_hours: int = 24 * 90,
    ) -> bool:
        """滚动 TTL — 事实被访问时延长过期时间。

        场景：用户偏好被命中使用时，说明该偏好仍然有效，
        延长其生命周期，避免常用偏好因固定 TTL 过期。

        Args:
            fact_id: 事实 ID。
            ttl_hours: 新的过期时间（小时），默认 90 天。

        Returns:
            是否成功延期。
        """
        stmt = select(MemoryFact).where(MemoryFact.id == fact_id)
        result = await self.db.execute(stmt)
        fact = result.scalar_one_or_none()
        if fact and fact.is_active:
            fact.expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
            await self.db.flush()
            return True
        return False

    async def touch_facts_by_category(
        self,
        user_id: uuid.UUID,
        category: str,
        ttl_hours: int = 24 * 90,
    ) -> int:
        """批量滚动 TTL — 对某类别的所有活跃事实延期。

        Args:
            user_id: 用户 ID。
            category: 事实类别。
            ttl_hours: 新的过期时间（小时）。

        Returns:
            延期的事实数量。
        """
        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id,
            MemoryFact.category == category,
            MemoryFact.is_active == True,
        )
        result = await self.db.execute(stmt)
        count = 0
        new_expiry = datetime.utcnow() + timedelta(hours=ttl_hours)
        for fact in result.scalars():
            fact.expires_at = new_expiry
            count += 1
        if count:
            await self.db.flush()
        return count

    async def search_similar_with_scores(
        self,
        user_id: uuid.UUID,
        fact_text: str,
        limit: int = 10,
        similarity_threshold: float = 0.0,
        exclude_ids: list[uuid.UUID] | None = None,
    ) -> list[tuple[MemoryFact, float]]:
        """检索相似记忆（带相似度分数）— 冲突整合的候选来源（机制二）。

        与 search_facts 的差异：
            - 返回 (fact, similarity) 对，供仲裁器按相似度分层决策
            - 跨类别检索（冲突可能发生在偏好与情节之间，如
              "喜欢VIP权益" vs "降级为基础版"）
            - 不做激活值闸门（整合关注"对不对"，不是"老不老"）
            - 不写回访问统计（检索 ≠ 用户真实召回）

        Returns:
            [(fact, similarity)] 按相似度降序；embedder 不可用时返回 []。
        """
        try:
            embeddings = await self.embedder.embed([fact_text])
            query_vec = embeddings[0] if embeddings else None
        except Exception as e:
            logger.warning("similar_search_embedding_failed", error=str(e))
            return []
        if query_vec is None:
            return []

        stmt = (
            select(MemoryFact)
            .where(
                MemoryFact.user_id == user_id,
                MemoryFact.is_active == True,
                MemoryFact.embedding.is_not(None),
            )
            .order_by(MemoryFact.created_at.desc())
            .limit(200)
        )
        stmt = stmt.where(
            (MemoryFact.expires_at.is_(None)) | (MemoryFact.expires_at > func.now())
        )
        if exclude_ids:
            stmt = stmt.where(MemoryFact.id.not_in(exclude_ids))

        result = await self.db.execute(stmt)
        scored: list[tuple[MemoryFact, float]] = []
        for fact in result.scalars():
            sim = _cosine_similarity(query_vec, fact.embedding or [])
            if sim >= similarity_threshold:
                scored.append((fact, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    async def mark_superseded(
        self,
        fact_ids: list[uuid.UUID],
        superseded_by: uuid.UUID,
    ) -> int:
        """标记旧记忆退场（机制二裁决的败者）。

        与 is_active=False 的普通软删除不同，superseded_* 专指
        "被新记忆语义覆写"，P2 软删除窗口依赖 superseded_at 判定
        窗口期内是否可复活。

        Args:
            fact_ids: 败者记忆 ID 列表。
            superseded_by: 取而代之的新记忆 ID。

        Returns:
            标记数量。
        """
        if not fact_ids:
            return 0
        stmt = select(MemoryFact).where(
            MemoryFact.id.in_(fact_ids), MemoryFact.is_active == True
        )
        result = await self.db.execute(stmt)
        now = datetime.utcnow()
        count = 0
        for fact in result.scalars():
            fact.is_active = False
            fact.superseded_by = superseded_by
            fact.superseded_at = now
            count += 1
        if count:
            await self.db.flush()
            logger.info(
                "memory_superseded",
                count=count,
                superseded_by=str(superseded_by),
            )
        return count

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
