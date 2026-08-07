"""
知识推荐服务 — 单一职责：基于用户行为 + 内容语义 + 图谱关联做个性化推荐。

架构（对应 SKILL 第 14 节）：
    - 三路召回按数据成熟度分级启停（冷启动 → 行为积累），非默认全开：
        * 协同过滤（UserCF + ItemCF）— 需行为数达阈值才启用
        * 向量内容召回 — 用户偏好向量 → 向量库 Top-K（冷启动兜底）
        * 图谱关联推荐 — 复用 GraphService.get_related_recommendations
        * 无行为用户 → 热门文档兜底
    - RRF（Reciprocal Rank Fusion）融合，规避三路分数不可比与权重调参。
    - 权限过滤（PermissionService.filter_documents）在写缓存前执行。

遵循单一职责：本服务只做推荐召回 + 融合 + 过滤，不涉及行为采集的写入
（record_behavior 仅作行为上报入口，权重由调用方决策）。
遵循依赖倒置：embedder / vector_store / graph_service / cache 均可注入，
便于测试与优雅降级（任一外部服务不可用时不阻塞整体推荐）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.behavior import ACTION_WEIGHTS, UserBehavior
from app.models.knowledge import Document
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)
settings = get_settings()

#: 实体召回时最多取多少篇用户近期浏览文档做图谱关联（控制图遍历开销）
_GRAPH_SEED_DOCS: int = 5
#: 偏好向量聚合时最多取多少篇文档标题（控制 embedding 成本）
_PREFERENCE_MAX_DOCS: int = 20


class RecommendationService:
    """知识推荐服务。

    Args:
        db: 异步数据库会话。
        tenant_id: 租户 ID（多租户隔离）。
        embedder: EmbeddingProvider，缺失时走 get_embedder()，不可用则向量路降级。
        vector_store: VectorStoreBase，缺失时走 get_vector_store()。
        graph_service: GraphService，用于图谱关联召回。
        cache: 可选缓存对象（async get/set），None 时不缓存。
    """

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None = None,
        embedder: Any | None = None,
        vector_store: Any | None = None,
        graph_service: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self.db = db
        self._tenant_id = tenant_id
        self._embedder = embedder
        self._vector_store = vector_store
        self._graph_service = graph_service
        self._cache = cache

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def record_behavior(
        self,
        user_id: uuid.UUID,
        doc_id: uuid.UUID,
        action_type: str,
        acted_at: datetime | None = None,
    ) -> None:
        """记录/累加用户行为（upsert 到 user_behaviors）。

        同 (tenant_id, user_id, doc_id, action_type) 唯一，多次行为累加 weight，
        避免行为表行数随点击无限膨胀。软删除记录不予恢复（视为已放弃）。

        Args:
            user_id: 用户 ID。
            doc_id: 文档 ID。
            action_type: view / search_click / collect / like。
            acted_at: 行为时间，默认当前时间。
        """
        if action_type not in ACTION_WEIGHTS:
            raise ValueError(
                f"不支持的行为类型: {action_type}，可选: {list(ACTION_WEIGHTS)}"
            )
        acted_at = acted_at or datetime.now(timezone.utc)
        weight = ACTION_WEIGHTS[action_type]

        stmt = select(UserBehavior).where(
            UserBehavior.user_id == user_id,
            UserBehavior.doc_id == doc_id,
            UserBehavior.action_type == action_type,
            UserBehavior.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, UserBehavior, self._tenant_id)
        existing = (await self.db.execute(stmt)).scalars().first()

        if existing is not None:
            existing.weight += weight
        else:
            self.db.add(
                UserBehavior(
                    tenant_id=self._tenant_id,
                    user_id=user_id,
                    doc_id=doc_id,
                    action_type=action_type,
                    weight=weight,
                    acted_at=acted_at,
                )
            )
        await self.db.flush()
        logger.info(
            "recommend.behavior.recorded",
            user_id=str(user_id),
            doc_id=str(doc_id),
            action_type=action_type,
        )

    async def recommend_for_user(
        self,
        user_id: uuid.UUID,
        top_k: int = 10,
        permission_filter: Callable[[list[Document]], Awaitable[list[Document]]] | None = None,
    ) -> list[dict[str, Any]]:
        """个性化推荐（猜你想看）— 三路召回 + RRF 融合 + 权限过滤。

        冷启动策略：
            - 无行为用户 → 热门文档兜底 + 向量路（若有偏好信号）；
            - 行为数 < RECOMMEND_CF_MIN_INTERACTIONS → 关闭协同过滤。

        Args:
            user_id: 目标用户 ID。
            top_k: 返回数量上限。
            permission_filter: 异步权限过滤函数，接收 Document 列表返回可见子集。
                传 None 则不过滤（调用方已保证权限的场景）。

        Returns:
            推荐列表，每项含 doc_id / title / reason / score。
            reason 为召回来源（user_cf / item_cf / vector / graph / hot）。
        """
        if not settings.RECOMMEND_ENABLED:
            return []

        cache_key = self._cache_key("user", user_id, top_k)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        behaviors = await self._load_user_behaviors(user_id)
        user_doc_ids = {str(b.doc_id) for b in behaviors}

        # 无任何行为 → 热门兜底（最冷启动）
        if not behaviors:
            result = await self._hot_fallback(user_id=user_id, exclude=set(), top_k=top_k)
            result = await self._finalize(result, user_doc_ids, permission_filter, top_k)
            await self._cache_set(cache_key, result)
            return result

        # 分阶段启停：行为数达标才开启协同过滤
        cf_enabled = (
            settings.RECOMMEND_ENABLE_CF
            and len(behaviors) >= settings.RECOMMEND_CF_MIN_INTERACTIONS
        )
        vector_enabled = settings.RECOMMEND_ENABLE_VECTOR
        graph_enabled = settings.RECOMMEND_ENABLE_GRAPH

        import asyncio

        paths: list[list[dict[str, Any]]] = []
        if cf_enabled:
            paths.append(await self._cf_recall(user_id, behaviors, top_k))
        if vector_enabled:
            paths.append(await self._vector_content_recall(behaviors, top_k))
        if graph_enabled:
            paths.append(await self._graph_recall(user_id, behaviors, top_k))
        # 所有召回路都为空时兜底热门，保证首页不为空
        non_empty = [p for p in paths if p]
        if not non_empty:
            non_empty = [await self._hot_fallback(user_id=user_id, exclude=set(), top_k=top_k)]

        fused = self._rrf_fuse(non_empty, k=settings.RECOMMEND_RRF_K)
        result = await self._finalize(fused, user_doc_ids, permission_filter, top_k)
        await self._cache_set(cache_key, result)
        return result

    async def get_related_documents(
        self,
        doc_id: uuid.UUID,
        user_id: uuid.UUID,
        top_k: int = 5,
        permission_filter: Callable[[list[Document]], Awaitable[list[Document]]] | None = None,
    ) -> list[dict[str, Any]]:
        """相关阅读 — 复用 GraphService.get_related_recommendations（L1 Redis → L2 Neo4j → L3 PG）。

        Args:
            doc_id: 当前文档 ID。
            user_id: 当前用户 ID（权限过滤 + 缓存隔离）。
            top_k: 返回数量上限。
            permission_filter: 异步权限过滤函数。

        Returns:
            推荐文档列表，每项含 doc_id / title / reason="related" / score。
        """
        cached = await self._cache_get(self._cache_key("related", f"{doc_id}:{user_id}", top_k))
        if cached is not None:
            return cached

        graph = self._graph_service or self._get_graph_service()
        raw: list[dict[str, Any]] = []
        try:
            raw = await graph.get_related_recommendations(
                str(doc_id),
                str(user_id),
                top_k=top_k,
                permission_filter=None,
                db_session=self.db,
            )
        except Exception as exc:
            logger.warning("recommend.related.graph_error", error=str(exc))

        result = [
            {"doc_id": r.get("id", r.get("doc_id")), "title": r.get("title"), "reason": "related", "score": r.get("score", 0)}
            for r in raw
            if r.get("id") or r.get("doc_id")
        ]
        doc_ids = {r["doc_id"] for r in result}
        docs = await self._fetch_docs(doc_ids)
        result = [
            {
                "doc_id": r["doc_id"],
                "title": docs[r["doc_id"]].title or r["title"],
                "reason": r["reason"],
                "score": r["score"],
            }
            for r in result
            if r["doc_id"] in docs
        ]
        if permission_filter is not None:
            doc_map = {str(d.id): d for d in docs.values()}
            allowed = await permission_filter(list(docs.values()))
            allowed_ids = {str(d.id) for d in allowed}
            result = [r for r in result if r["doc_id"] in allowed_ids]

        await self._cache_set(self._cache_key("related", f"{doc_id}:{user_id}", top_k), result)
        return result

    # ------------------------------------------------------------------
    # 三路召回
    # ------------------------------------------------------------------

    async def _cf_recall(
        self,
        user_id: uuid.UUID,
        behaviors: list[UserBehavior],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """协同过滤召回 — UserCF + ItemCF 合并。
        """
        import asyncio

        user_cf, item_cf = await asyncio.gather(
            self._user_cf_recall(user_id, behaviors, top_k),
            self._item_cf_recall(user_id, behaviors, top_k),
            return_exceptions=True,
        )
        user_cf = user_cf if isinstance(user_cf, list) else []
        item_cf = item_cf if isinstance(item_cf, list) else []
        return self._rrf_fuse(
            [user_cf, item_cf], k=settings.RECOMMEND_RRF_K
        )[:top_k]

    async def _user_cf_recall(
        self,
        user_id: uuid.UUID,
        behaviors: list[UserBehavior],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """UserCF — 找行为最相似的用户，推荐他们看过但我没看过的文档。"""
        my_doc_ids = {str(b.doc_id) for b in behaviors}
        all_rows = await self._load_all_behaviors()
        my_weights = all_rows.get(str(user_id), {})

        # 计算与其他用户的 Jaccard 相似度
        similar: list[tuple[float, str]] = []
        for other_uid, other_weights in all_rows.items():
            if other_uid == str(user_id):
                continue
            inter = set(my_weights) & set(other_weights)
            union = set(my_weights) | set(other_weights)
            if not union:
                continue
            sim = len(inter) / len(union)
            if sim > 0:
                similar.append((sim, other_uid))
        similar.sort(key=lambda x: x[0], reverse=True)
        similar = similar[: settings.RECOMMEND_CF_SIMILAR_USERS]

        # 聚合相似用户看过的、我未看过的文档
        scores: dict[str, float] = {}
        for sim, other_uid in similar:
            other_weights = all_rows.get(other_uid, {})
            for doc_id, w in other_weights.items():
                if doc_id in my_doc_ids:
                    continue
                scores[doc_id] = scores.get(doc_id, 0) + sim * w

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {"doc_id": doc_id, "score": score, "reason": "user_cf"}
            for doc_id, score in ranked
        ]

    async def _item_cf_recall(
        self,
        user_id: uuid.UUID,
        behaviors: list[UserBehavior],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """ItemCF — 找用户看过文档的相似文档（基于共现用户的 Jaccard）。"""
        my_doc_ids = {str(b.doc_id) for b in behaviors}
        all_rows = await self._load_all_behaviors()

        # 文档 → 看过它的用户集合
        doc_user_sets: dict[str, set] = {}
        for uid, weights in all_rows.items():
            for doc_id in weights:
                doc_user_sets.setdefault(doc_id, set()).add(uid)

        # 对每个用户看过的文档，找相似文档（共现用户 Jaccard）
        scores: dict[str, float] = {}
        for my_doc in my_doc_ids:
            my_users = doc_user_sets.get(my_doc, set())
            if not my_users:
                continue
            for other_doc, other_users in doc_user_sets.items():
                if other_doc == my_doc or other_doc in my_doc_ids:
                    continue
                inter = my_users & other_users
                if not inter:
                    continue
                union = my_users | other_users
                sim = len(inter) / len(union)
                scores[other_doc] = scores.get(other_doc, 0) + sim

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {"doc_id": doc_id, "score": score, "reason": "item_cf"}
            for doc_id, score in ranked
        ]

    async def _vector_content_recall(
        self,
        behaviors: list[UserBehavior],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """向量内容召回 — 用户浏览文档标题聚合为偏好向量，向量库 Top-K。

        解决新文档（无行为但语义可召回）与冷启动；embedder / 向量库不可用时
        优雅降级返回空列表。
        """
        # 取用户近期浏览的前 N 篇文档标题
        recent = sorted(
            behaviors, key=lambda b: b.acted_at, reverse=True
        )[:_PREFERENCE_MAX_DOCS]
        doc_ids = [b.doc_id for b in recent]
        docs = await self._fetch_docs({str(d) for d in doc_ids})
        titles = [d.title for d in docs.values() if d.title]
        if not titles:
            return []

        embedder = self._embedder or self._get_embedder()
        if embedder is None:
            return []
        try:
            vec = (await embedder.embed([" ".join(titles)]))[0]
        except Exception as exc:
            logger.warning("recommend.vector.embed_error", error=str(exc))
            return []

        store = self._vector_store or self._get_vector_store()
        try:
            results = await store.search(vec, kb_ids=None, top_k=top_k)
        except Exception as exc:
            logger.warning("recommend.vector.search_failed", error=str(exc))
            return []

        return [
            {
                "doc_id": str(r.get("doc_id")),
                "title": r.get("title"),
                "score": float(r.get("score", 0)),
                "reason": "vector",
            }
            for r in results
            if r.get("doc_id")
        ]

    async def _graph_recall(
        self,
        user_id: uuid.UUID,
        behaviors: list[UserBehavior],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """图谱关联召回 — 用户近期浏览文档的关联文档。

        复用 GraphService.get_related_recommendations；Neo4j 不可用时返回空。
        """
        graph = self._graph_service or self._get_graph_service()
        recent = sorted(
            behaviors, key=lambda b: b.acted_at, reverse=True
        )[:_GRAPH_SEED_DOCS]
        collected: dict[str, dict[str, Any]] = {}
        for b in recent:
            try:
                raw = await graph.get_related_recommendations(
                    str(b.doc_id),
                    str(user_id),
                    top_k=top_k,
                    permission_filter=None,
                    db_session=self.db,
                )
            except Exception as exc:
                logger.warning("recommend.graph_error", error=str(exc))
                continue
            for r in raw:
                doc_id = r.get("id", r.get("doc_id"))
                if not doc_id:
                    continue
                if doc_id not in collected:
                    collected[doc_id] = {
                        "doc_id": str(doc_id),
                        "title": r.get("title"),
                        "score": float(r.get("score", 0)),
                        "reason": "graph",
                    }
        return list(collected.values())[:top_k]

    async def _hot_fallback(
        self,
        user_id: uuid.UUID,
        exclude: set,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """热门兜底 — 按 view_count 降序取可见文档（冷启动无行为用户）。"""
        stmt = (
            select(Document)
            .where(
                Document.deleted_at.is_(None),
                Document.status == "published",
            )
            .order_by((Document.view_count or 0).desc())
            .limit(top_k * 2 + len(exclude))
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        docs = (await self.db.execute(stmt)).scalars().all()
        result = []
        for d in docs:
            if str(d.id) in exclude:
                continue
            result.append(
                {"doc_id": str(d.id), "title": d.title, "score": 0.0, "reason": "hot"}
            )
            if len(result) >= top_k:
                break
        return result

    # ------------------------------------------------------------------
    # RRF 融合与收尾
    # ------------------------------------------------------------------

    @staticmethod
    def _rrf_fuse(
        paths: list[list[dict[str, Any]]],
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion — 按排名位置融合多路结果。

        各路径分数尺度不可比（协同过滤无向量语义），RRF 仅用排名位置，
        规避权重调参无底洞且冷启动零配置。
        """
        fused: dict[str, dict[str, Any]] = {}
        for path in paths:
            for rank, item in enumerate(path):
                doc_id = item["doc_id"]
                if doc_id not in fused:
                    fused[doc_id] = {"doc_id": doc_id, "score": 0.0, "reasons": []}
                fused[doc_id]["score"] += 1.0 / (k + rank + 1)
                fused[doc_id]["reasons"].append(item.get("reason", "unknown"))
        ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
        result = []
        for item in ranked:
            result.append(
                {
                    "doc_id": item["doc_id"],
                    "score": round(item["score"], 4),
                    "reason": "/".join(sorted(set(item["reasons"]))),
                }
            )
        return result

    async def _finalize(
        self,
        candidates: list[dict[str, Any]],
        user_doc_ids: set[str],
        permission_filter: Callable[[list[Document]], Awaitable[list[Document]]] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """补全标题、剔除用户已看过的文档、权限过滤，并按 top_k 截断。"""
        candidates = [c for c in candidates if c["doc_id"] not in user_doc_ids]
        if not candidates:
            return []
        doc_ids = {c["doc_id"] for c in candidates}
        docs = await self._fetch_docs(doc_ids)
        if not docs:
            return []

        if permission_filter is not None:
            allowed = await permission_filter(list(docs.values()))
            allowed_ids = {str(d.id) for d in allowed}
        else:
            allowed_ids = set(docs.keys())

        result = []
        for c in candidates:
            if c["doc_id"] not in allowed_ids:
                continue
            doc = docs[c["doc_id"]]
            result.append(
                {
                    "doc_id": c["doc_id"],
                    "title": doc.title,
                    "reason": c["reason"],
                    "score": c["score"],
                }
            )
        return result[:top_k]

    # ------------------------------------------------------------------
    # 数据辅助
    # ------------------------------------------------------------------

    async def _load_user_behaviors(self, user_id: uuid.UUID) -> list[UserBehavior]:
        """加载指定用户的全部行为记录。"""
        stmt = (
            select(UserBehavior)
            .where(
                UserBehavior.user_id == user_id,
                UserBehavior.deleted_at.is_(None),
            )
            .order_by(UserBehavior.acted_at)
        )
        stmt = apply_tenant_filter(stmt, UserBehavior, self._tenant_id)
        return list((await self.db.execute(stmt)).scalars().all())

    async def _load_all_behaviors(self) -> dict[str, dict[str, float]]:
        """加载同租户全部用户 → 文档权重映射（协同过滤矩阵）。"""
        stmt = select(UserBehavior).where(UserBehavior.deleted_at.is_(None))
        stmt = apply_tenant_filter(stmt, UserBehavior, self._tenant_id)
        rows = (await self.db.execute(stmt)).scalars().all()
        matrix: dict[str, dict[str, float]] = {}
        for b in rows:
            matrix.setdefault(str(b.user_id), {})
            matrix[str(b.user_id)][str(b.doc_id)] = (
                matrix[str(b.user_id)].get(str(b.doc_id), 0) + b.weight
            )
        return matrix

    async def _fetch_docs(self, doc_ids: set[str]) -> dict[str, Document]:
        """按 doc_id 批量获取文档（含标题），过滤已删除。"""
        if not doc_ids:
            return {}
        valid: list[uuid.UUID] = []
        for raw in doc_ids:
            try:
                valid.append(uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                continue
        if not valid:
            return {}
        stmt = select(Document).where(
            Document.id.in_(valid),
            Document.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        docs = (await self.db.execute(stmt)).scalars().all()
        return {str(d.id): d for d in docs}

    # ------------------------------------------------------------------
    # 缓存（优雅降级）
    # ------------------------------------------------------------------

    def _cache_key(self, kind: str, suffix: str, top_k: int) -> str:
        return f"recommend:{self._tenant_id or 'default'}:{kind}:{suffix}:{top_k}"

    async def _cache_get(self, key: str) -> list[dict[str, Any]] | None:
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.debug("recommend.cache_get_error", error=str(exc))
        return None

    async def _cache_set(self, key: str, value: list[dict[str, Any]]) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(
                key, json.dumps(value, ensure_ascii=False), ttl=settings.RECOMMEND_CACHE_TTL
            )
        except Exception as exc:
            logger.debug("recommend.cache_set_error", error=str(exc))

    # ------------------------------------------------------------------
    # 外部服务懒加载
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Any | None:
        """懒加载 Embedder — 不可用返回 None（向量路降级）。"""
        try:
            from app.llm.embedder import get_embedder

            return get_embedder()
        except Exception as exc:
            logger.warning("recommend.embedder.unavailable", error=str(exc))
            return None

    def _get_vector_store(self) -> Any | None:
        """懒加载向量存储 — 不可用返回 None（向量路降级）。"""
        try:
            from app.rag.vector_store import get_vector_store

            return get_vector_store()
        except Exception as exc:
            logger.warning("recommend.vector_store.unavailable", error=str(exc))
            return None

    def _get_graph_service(self) -> Any:
        """懒加载 GraphService。"""
        from app.services.graph_service import GraphService

        return GraphService(tenant_id=self._tenant_id)