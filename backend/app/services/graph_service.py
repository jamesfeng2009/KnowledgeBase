"""Neo4j 知识图谱服务 — 单一职责：图谱 CRUD + Cypher 查询 + 三元组提取。

定位：L7 知识图谱层的核心服务，管理文档/概念/政策/产品之间的实体关系网络。

与 Graphiti 的分工：
  Graphiti：时序追踪（"什么时候变了"）— 时间维度
  GraphService：关系网络（"谁和谁有关系"）— 空间维度

能力：
1. 实体管理：创建/删除/查询节点（文档、概念、政策、产品等）
2. 关系管理：创建/删除关系（引用、提及、替代、上位词等）
3. 三元组提取：从文档内容自动提取 (subject, predicate, object) 三元组
4. 图遍历查询：多跳关系查询（如"与文档A间接相关的所有政策"）
5. 图谱可视化：返回节点和边数据供前端力导向图渲染
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

# 延迟导入 neo4j
try:
    from neo4j import AsyncGraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    AsyncGraphDatabase = None
    ServiceUnavailable = Exception
    AuthError = Exception
    logger.info("neo4j driver not installed, GraphService will use fallback mode")


class GraphService:
    """Neo4j 知识图谱服务。

    节点类型（label）：
    - Document: 文档节点（含 doc_id, title, kb_id）
    - Concept: 概念节点（如"微服务"、"容器化"）
    - Policy: 政策节点（如"报销制度"、"休假政策"）
    - Product: 产品节点
    - Department: 部门节点
    - Person: 人员节点

    关系类型（type）：
    - REFERENCES: 引用（文档A引用文档B）
    - MENTIONS: 提及（文档提及概念）
    - RELATES_TO: 关联
    - REPLACES: 替代（产品A替代产品B）
    - BELONGS_TO: 属于
    - HYPERNYM: 上位词（概念A是概念B的上位词）
    - AUTHORED_BY: 作者
    - APPROVED_BY: 审批人
    """

    #: 推荐缓存 TTL（秒）— 5 分钟，平衡新鲜度与命中率。
    _RECOMMEND_TTL: int = 300

    def __init__(self) -> None:
        self._driver = None
        self._initialized = False
        # Redis 懒初始化（关联推荐缓存用）
        self._redis = None
        self._redis_available: bool | None = None

    async def _ensure_connected(self) -> bool:
        """延迟初始化 Neo4j 连接。"""
        if self._initialized:
            return self._driver is not None
        if not NEO4J_AVAILABLE:
            return False
        try:
            self._driver = AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
            # 验证连接
            await self._driver.verify_connectivity()
            self._initialized = True
            logger.info("neo4j.connected", uri=settings.NEO4J_URI)
            return True
        except Exception as e:
            logger.warning("neo4j.connection_failed", error=str(e))
            self._driver = None
            self._initialized = True  # 标记已尝试，不重复连接
            return False

    async def _get_redis(self):
        """懒初始化 Redis 连接 — 首次调用时建立，失败则标记不可用并返回 None。

        遵循优雅降级：Redis 不可用时跳过 L1 缓存，直接走 Neo4j / PG。
        """
        if self._redis_available is False:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info("graph.redis.connected", url=settings.REDIS_URL)
        except Exception as exc:
            self._redis_available = False
            self._redis = None
            logger.warning("graph.redis.unavailable", error=str(exc))
        return self._redis

    async def close(self) -> None:
        """关闭 Neo4j 与 Redis 连接。"""
        if self._driver:
            await self._driver.close()
            self._driver = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.warning("graph.redis.close_error", error=str(exc))
            finally:
                self._redis = None

    async def invalidate_recommend_cache(self, doc_id: str) -> None:
        """失效指定文档的推荐缓存 — 文档内容变更后调用。

        采用 pattern 批量删除，覆盖所有用户的缓存条目。
        """
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            pattern = f"graph:recommend:{doc_id}:*"
            keys = []
            async for key in redis.scan_iter(match=pattern, count=100):
                keys.append(key)
            if keys:
                await redis.delete(*keys)
                logger.info("graph.recommend_cache_invalidated", doc_id=doc_id, keys=len(keys))
        except Exception as exc:
            logger.warning("graph.cache_invalidate_error", error=str(exc))

    # ------------------------------------------------------------------
    # 节点管理
    # ------------------------------------------------------------------

    async def create_node(
        self,
        label: str,
        properties: dict[str, Any],
    ) -> dict[str, Any] | None:
        """创建图节点。

        Args:
            label: 节点标签（Document/Concept/Policy/Product/Department/Person）。
            properties: 节点属性字典。

        Returns:
            创建的节点信息，失败返回 None。
        """
        if not await self._ensure_connected():
            return None
        try:
            query = f"CREATE (n:{label} $props) RETURN n"
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, props=properties)
                record = await result.single()
                if record:
                    node = record["n"]
                    return dict(node.items())
            return None
        except Exception as e:
            logger.error("neo4j.create_node_error", label=label, error=str(e))
            return None

    async def get_node(self, label: str, node_id: str) -> dict[str, Any] | None:
        """获取节点详情。"""
        if not await self._ensure_connected():
            return None
        try:
            query = f"MATCH (n:{label} {{id: $node_id}}) RETURN n"
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, node_id=node_id)
                record = await result.single()
                if record:
                    return dict(record["n"].items())
            return None
        except Exception as e:
            logger.error("neo4j.get_node_error", error=str(e))
            return None

    async def delete_node(self, label: str, node_id: str) -> bool:
        """删除节点及其所有关系。"""
        if not await self._ensure_connected():
            return False
        try:
            query = f"MATCH (n:{label} {{id: $node_id}}) DETACH DELETE n"
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                await session.run(query, node_id=node_id)
            return True
        except Exception as e:
            logger.error("neo4j.delete_node_error", error=str(e))
            return False

    # ------------------------------------------------------------------
    # 关系管理
    # ------------------------------------------------------------------

    async def create_relationship(
        self,
        from_label: str,
        from_id: str,
        to_label: str,
        to_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        """创建节点间的关系。

        Args:
            from_label: 源节点标签。
            from_id: 源节点 ID。
            to_label: 目标节点标签。
            to_id: 目标节点 ID。
            rel_type: 关系类型（REFERENCES/MENTIONS/REPLACES 等）。
            properties: 关系属性（可选）。

        Returns:
            是否成功。
        """
        if not await self._ensure_connected():
            return False
        try:
            query = (
                f"MATCH (a:{from_label} {{id: $from_id}}), "
                f"(b:{to_label} {{id: $to_id}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"SET r += $props RETURN r"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                await session.run(query, from_id=from_id, to_id=to_id,
                                 props=properties or {})
            return True
        except Exception as e:
            logger.error("neo4j.create_rel_error", rel_type=rel_type, error=str(e))
            return False

    async def delete_relationship(
        self,
        from_label: str,
        from_id: str,
        to_label: str,
        to_id: str,
        rel_type: str,
    ) -> bool:
        """删除关系。"""
        if not await self._ensure_connected():
            return False
        try:
            query = (
                f"MATCH (a:{from_label} {{id: $from_id}})"
                f"-[r:{rel_type}]->"
                f"(b:{to_label} {{id: $to_id}}) DELETE r"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                await session.run(query, from_id=from_id, to_id=to_id)
            return True
        except Exception as e:
            logger.error("neo4j.delete_rel_error", error=str(e))
            return False

    # ------------------------------------------------------------------
    # 图遍历查询
    # ------------------------------------------------------------------

    async def find_related_nodes(
        self,
        node_label: str,
        node_id: str,
        max_depth: int = 2,
        rel_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """多跳图遍历：查找与指定节点间接相关的所有节点。

        这是 Neo4j 相对 PostgreSQL 的核心优势 — 多跳遍历。

        Args:
            node_label: 起始节点标签。
            node_id: 起始节点 ID。
            max_depth: 最大遍历深度（1=直接关系，2=两跳，3=三跳）。
            rel_types: 限定关系类型（None=所有关系）。

        Returns:
            相关节点列表。
        """
        if not await self._ensure_connected():
            return []
        try:
            rel_filter = ""
            if rel_types:
                rel_filter = "|".join(f"r:{t}" for t in rel_types)
                rel_filter = f":{rel_filter}"

            query = (
                f"MATCH (n:{node_label} {{id: $node_id}})"
                f"-[r{rel_filter}*1..{max_depth}]->(m) "
                f"RETURN DISTINCT m, length(r) as depth "
                f"ORDER BY depth LIMIT 50"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, node_id=node_id)
                records = await result.data()
            return [{"node": r["m"], "depth": r["depth"]} for r in records]
        except Exception as e:
            logger.error("neo4j.find_related_error", error=str(e))
            return []

    # ------------------------------------------------------------------
    # 关联推荐 — 三级缓存保障（L1 Redis → L2 Neo4j → L3 PG 降级）
    # ------------------------------------------------------------------

    async def get_related_recommendations(
        self,
        doc_id: str,
        user_id: str,
        top_k: int = 5,
        permission_filter=None,
        db_session=None,
    ) -> list[dict[str, Any]]:
        """关联推荐 — 用户浏览文档时调用，需 <50ms。

        三级保障策略：
            L1 — Redis 缓存（命中率 60%+，<5ms）：按 doc_id + user_id 缓存过滤后的结果
            L2 — Neo4j 2 跳图遍历（<30ms）：缓存未命中时查图谱
            L3 — PostgreSQL 全文检索降级（<200ms）：Neo4j 不可用时走 PG

        权限过滤在缓存写入前执行，确保缓存中的数据均为用户可见。

        Args:
            doc_id: 当前文档 ID。
            user_id: 当前用户 ID（用于权限过滤 + 缓存隔离）。
            top_k: 返回的推荐数量上限。
            permission_filter: 权限服务实例（PermissionService.filter_documents），
                              传入则对推荐结果做密级过滤；None 则不过滤。
            db_session: 数据库会话（L3 PG 降级时使用）。

        Returns:
            推荐文档列表，每项含 {id, title, depth, score}。
        """
        import json as _json

        # === L1: Redis 精确缓存 ===
        cache_key = f"graph:recommend:{doc_id}:{user_id}"
        redis = await self._get_redis()
        if redis is not None:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    logger.debug("graph.recommend.hit", level="L1", doc_id=doc_id)
                    return _json.loads(cached)
            except Exception as exc:
                logger.warning("graph.recommend.l1_error", error=str(exc))

        # === L2: Neo4j 2 跳图遍历 ===
        recommendations: list[dict[str, Any]] = []
        if await self._ensure_connected():
            try:
                # 2 跳图遍历：从当前文档出发，经 REFERENCES/MENTIONS/RELATES_TO 关系找到关联文档
                query = (
                    "MATCH (d:Document {id: $doc_id})"
                    "-[r:REFERENCES|MENTIONS|RELATES_TO*1..2]->(m:Document) "
                    "WHERE m.id <> $doc_id "
                    "RETURN DISTINCT m.id as id, m.title as title, "
                    "m.doc_type as doc_type, "
                    "min(length(r)) as depth "
                    "ORDER BY depth LIMIT $top_k"
                )
                async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                    result = await session.run(query, doc_id=doc_id, top_k=top_k * 2)
                    records = await result.data()
                recommendations = [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "doc_type": r.get("doc_type", "md"),
                        "depth": r["depth"],
                        "score": 1.0 / r["depth"],  # 深度越浅分数越高
                    }
                    for r in records
                ]
                logger.info(
                    "graph.recommend.neo4j",
                    doc_id=doc_id,
                    count=len(recommendations),
                )
            except Exception as exc:
                logger.warning("graph.recommend.neo4j_error", error=str(exc))
                recommendations = []

        # === L3: PG 降级 — Neo4j 不可用或结果为空时 ===
        if not recommendations and db_session is not None:
            recommendations = await self._pg_fallback_recommendations(
                doc_id, db_session, top_k
            )
            logger.info(
                "graph.recommend.pg_fallback",
                doc_id=doc_id,
                count=len(recommendations),
            )

        # === 权限过滤 ===
        if permission_filter is not None and recommendations:
            try:
                # 将推荐结果转换为可过滤格式
                from app.models.knowledge import Document

                doc_ids = [r["id"] for r in recommendations]
                if doc_ids:
                    from sqlalchemy import select

                    stmt = select(Document).where(Document.id.in_(doc_ids))
                    result = await db_session.execute(stmt)
                    docs = list(result.scalars().all())
                    filtered_docs = await permission_filter(docs)
                    filtered_ids = {str(d.id) for d in filtered_docs}
                    recommendations = [
                        r for r in recommendations if r["id"] in filtered_ids
                    ]
            except Exception as exc:
                logger.warning("graph.recommend.permission_filter_error", error=str(exc))

        recommendations = recommendations[:top_k]

        # === 回写 L1 Redis 缓存 ===
        if redis is not None and recommendations:
            try:
                await redis.setex(
                    cache_key,
                    self._RECOMMEND_TTL,
                    _json.dumps(recommendations, ensure_ascii=False),
                )
            except Exception as exc:
                logger.warning("graph.recommend.l1_set_error", error=str(exc))

        return recommendations

    async def _pg_fallback_recommendations(
        self,
        doc_id: str,
        db_session,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """PG 降级推荐 — 当 Neo4j 不可用时，使用 PostgreSQL 全文检索做近似推荐。

        策略：基于当前文档的标题关键词，检索标题/内容相似的文档。
        这是 Neo4j 的降级方案，推荐质量不如图谱多跳遍历。

        Args:
            doc_id: 当前文档 ID。
            db_session: 异步数据库会话。
            top_k: 返回数量上限。

        Returns:
            推荐文档列表。
        """
        try:
            from uuid import UUID

            from sqlalchemy import or_, select

            from app.models.knowledge import Document

            # 获取当前文档标题
            doc_result = await db_session.execute(
                select(Document).where(Document.id == UUID(doc_id))
            )
            current_doc = doc_result.scalars().first()
            if not current_doc or not current_doc.title:
                return []

            # 从标题提取关键词（简单分词）
            title = current_doc.title
            # 使用 ILIKE 做模糊匹配
            keywords = [w for w in title.split() if len(w) >= 2]
            if not keywords:
                # 回退：取同知识库的最新文档
                stmt = (
                    select(Document)
                    .where(
                        Document.kb_id == current_doc.kb_id,
                        Document.id != current_doc.id,
                        Document.deleted_at.is_(None),
                        Document.status == "published",
                    )
                    .order_by(Document.view_count.desc())
                    .limit(top_k)
                )
            else:
                # OR ILIKE 模糊匹配
                conditions = []
                for kw in keywords:
                    conditions.append(Document.title.ilike(f"%{kw}%"))
                stmt = (
                    select(Document)
                    .where(
                        Document.id != current_doc.id,
                        Document.deleted_at.is_(None),
                        Document.status == "published",
                        or_(*conditions) if conditions else True,
                    )
                    .order_by(Document.view_count.desc())
                    .limit(top_k)
                )

            result = await db_session.execute(stmt)
            docs = list(result.scalars().all())

            return [
                {
                    "id": str(d.id),
                    "title": d.title,
                    "doc_type": d.doc_type,
                    "depth": 3,  # 标记为 PG 降级结果
                    "score": 0.3,  # 降级结果分数较低
                }
                for d in docs
            ]
        except Exception as exc:
            logger.error("graph.recommend.pg_fallback_error", error=str(exc))
            return []

    async def shortest_path(
        self,
        from_label: str,
        from_id: str,
        to_label: str,
        to_id: str,
    ) -> list[dict[str, Any]] | None:
        """查找两个节点间的最短路径。"""
        if not await self._ensure_connected():
            return None
        try:
            query = (
                f"MATCH p = shortestPath("
                f"(a:{from_label} {{id: $from_id}})-[*..5]->"
                f"(b:{to_label} {{id: $to_id}})) "
                f"RETURN [node in nodes(p) | node] as path_nodes, "
                f"[rel in relationships(p) | type(rel)] as path_rels"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, from_id=from_id, to_id=to_id)
                record = await result.single()
                if record:
                    return [{
                        "nodes": [dict(n.items()) for n in record["path_nodes"]],
                        "relationships": record["path_rels"],
                    }]
            return None
        except Exception as e:
            logger.error("neo4j.shortest_path_error", error=str(e))
            return None

    # ------------------------------------------------------------------
    # 图谱可视化
    # ------------------------------------------------------------------

    async def get_graph_data(
        self,
        node_label: str | None = None,
        node_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """获取图谱可视化数据（节点 + 边）。

        供前端 knowledge/graph.astro 力导向图渲染使用。

        Args:
            node_label: 限定节点标签（None=所有）。
            node_id: 限定起始节点（None=全图采样）。
            limit: 最多返回节点数。

        Returns:
            {"nodes": [{id, label, type, properties}], "edges": [{source, target, type, properties}]}
        """
        if not await self._ensure_connected():
            return {"nodes": [], "edges": []}
        try:
            if node_id and node_label:
                # 以指定节点为中心，获取 2 跳子图
                query = (
                    f"MATCH (n:{node_label} {{id: $node_id}})-[r*1..2]-(m) "
                    f"WITH collect(DISTINCT n) + collect(DISTINCT m) AS all_nodes, "
                    f"collect(DISTINCT r) AS all_rels "
                    f"UNWIND all_nodes AS node "
                    f"WITH collect(DISTINCT node)[..$limit] AS nodes, all_rels "
                    f"UNWIND nodes AS n "
                    f"OPTIONAL MATCH (n)-[r]-(m) WHERE m IN nodes "
                    f"RETURN collect(DISTINCT {id: n.id, label: labels(n)[0], properties: n}) as nodes, "
                    f"collect(DISTINCT {source: startNode(r).id, target: endNode(r).id, type: type(r)}) as edges"
                )
                async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                    result = await session.run(query, node_id=node_id, limit=limit)
                    record = await result.single()
            else:
                # 全图采样
                label_filter = f":{node_label}" if node_label else ""
                query = (
                    f"MATCH (n{label_filter}) WITH n LIMIT $limit "
                    f"OPTIONAL MATCH (n)-[r]-(m) "
                    f"RETURN collect(DISTINCT n) as nodes, "
                    f"collect(DISTINCT {{source: startNode(r).id, target: endNode(r).id, type: type(r)}}) as edges"
                )
                async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                    result = await session.run(query, limit=limit)
                    record = await result.single()

            if record:
                nodes_data = []
                for n in record["nodes"]:
                    if n:
                        props = dict(n.items()) if hasattr(n, 'items') else dict(n)
                        nodes_data.append({
                            "id": str(props.get("id", "")),
                            "label": props.get("name", props.get("title", "未知")),
                            "type": props.get("entity_type", "unknown"),
                            "properties": props,
                        })
                edges_data = []
                for e in record["edges"]:
                    if e and e.get("source") and e.get("target"):
                        edges_data.append({
                            "source": str(e["source"]),
                            "target": str(e["target"]),
                            "type": e.get("type", "RELATES_TO"),
                        })
                return {"nodes": nodes_data[:limit], "edges": edges_data}
            return {"nodes": [], "edges": []}
        except Exception as e:
            logger.error("neo4j.get_graph_data_error", error=str(e))
            return {"nodes": [], "edges": []}

    # ------------------------------------------------------------------
    # 混合三元组提取 — 规则优先（快、免费），LLM 兜底（准、有成本）
    # ------------------------------------------------------------------

    # 规则模板：中文常见关系模式（正则 + 关系类型映射）
    # 格式：(正则模式, 关系类型, 分组顺序)
    # 正则中 (?P<s>...) 是 subject，(?P<o>...) 是 object
    _RULE_PATTERNS: list[tuple[str, str]] = [
        # X属于Y / X属于Y的Z
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})属于(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "属于"),
        # X包含Y / X包括Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})包[含括](?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "包含"),
        # X引用Y / X参考Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})引[用考](?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "引用"),
        # X替代Y / X取代Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})[替取]代(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "替代"),
        # X依赖Y / X依赖Y的Z
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})依赖(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "依赖"),
        # X基于Y / X基于Y构建
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})基于(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "基于"),
        # X是Y / X是一种Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,15})是一?[种个项类]?(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})", "属于"),
        # X使用Y / X采用Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})[使采]用(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "使用"),
        # X定义Y / X定义了Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})定义了?(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "定义"),
        # X管理Y / X管理器Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})管理(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "管理"),
        # X实现Y / X实现了Y
        (r"(?P<s>[\u4e00-\u9fa5A-Za-z0-9_]{2,20})实现了?(?P<o>[\u4e00-\u9fa5A-Za-z0-9_]{2,30})", "实现"),
    ]

    async def extract_triples_from_text(
        self,
        text: str,
        doc_id: str,
        llm_provider=None,
        use_rules: bool = True,
        use_llm: bool = True,
        llm_fallback_threshold: int = 3,
    ) -> list[tuple[str, str, str]]:
        """从文本中混合提取三元组 (subject, predicate, object) 并存入图谱。

        混合策略（成本优化）：
            1. 规则提取（快速、免费）— 正则匹配中文常见关系模式，覆盖 60%+ 简单关系
            2. LLM 提取（准确、有成本）— 仅当规则提取结果不足时，调用 LLM 补充复杂关系

        如果规则提取的三元组数量 >= llm_fallback_threshold，则跳过 LLM 调用，
        直接使用规则结果，节省 LLM 调用成本。

        Args:
            text: 文档文本内容。
            doc_id: 文档 ID（用于关联 Document 节点）。
            llm_provider: LLM Provider 实例（LLM 兜底用）。
            use_rules: 是否启用规则提取（默认 True）。
            use_llm: 是否启用 LLM 提取（默认 True）。
            llm_fallback_threshold: 规则提取达到此数量时跳过 LLM（默认 3）。

        Returns:
            提取的三元组列表 [(subject, predicate, object), ...]
        """
        all_triples: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()  # 去重

        # === 阶段 1：规则提取（快速、免费）===
        if use_rules:
            rule_triples = self._extract_triples_by_rules(text)
            for t in rule_triples:
                if t not in seen:
                    seen.add(t)
                    all_triples.append(t)
            logger.info(
                "graph.triples.rule_extracted",
                doc_id=doc_id,
                count=len(all_triples),
            )

        # === 阶段 2：LLM 提取（兜底，仅当规则结果不足时）===
        if use_llm and len(all_triples) < llm_fallback_threshold and llm_provider:
            llm_triples = await self._extract_triples_by_llm(text, llm_provider)
            for t in llm_triples:
                if t not in seen:
                    seen.add(t)
                    all_triples.append(t)
            logger.info(
                "graph.triples.llm_fallback",
                doc_id=doc_id,
                rule_count=len(rule_triples) if use_rules else 0,
                llm_count=len(all_triples) - (len(rule_triples) if use_rules else 0),
                total=len(all_triples),
            )

        if not all_triples:
            logger.info("graph.triples.empty", doc_id=doc_id)
            return []

        # === 批量写入图谱 ===
        nodes: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []

        for subject, predicate, obj in all_triples:
            # Concept 节点
            nodes.append({
                "label": "Concept",
                "id": subject,
                "name": subject,
                "entity_type": "concept",
            })
            nodes.append({
                "label": "Concept",
                "id": obj,
                "name": obj,
                "entity_type": "concept",
            })
            # 概念间关系
            rel_type = predicate.upper().replace(" ", "_")
            relationships.append({
                "from_label": "Concept",
                "from_id": subject,
                "to_label": "Concept",
                "to_id": obj,
                "type": rel_type,
            })
            # 文档 → 概念
            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "Concept",
                "to_id": subject,
                "type": "MENTIONS",
            })
            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "Concept",
                "to_id": obj,
                "type": "MENTIONS",
            })

        # 批量导入（比逐条快 10-50x）
        await self.batch_import_graph(nodes, relationships)

        logger.info("graph.triples_extracted", doc_id=doc_id, count=len(all_triples))
        return all_triples

    def _extract_triples_by_rules(self, text: str) -> list[tuple[str, str, str]]:
        """规则提取 — 正则匹配中文常见关系模式。

        优势：无需 LLM 调用，零成本，毫秒级响应。
        覆盖：属于/包含/引用/替代/依赖/基于/是/使用/定义/管理/实现 等常见关系。

        Args:
            text: 文档文本。

        Returns:
            三元组列表 [(subject, predicate, object), ...]
        """
        import re

        triples: list[tuple[str, str, str]] = []

        for pattern, predicate in self._RULE_PATTERNS:
            matches = re.finditer(pattern, text)
            for match in matches:
                subject = match.group("s").strip()
                obj = match.group("o").strip()
                # 过滤常见停用词（避免"我们属于公司"这类噪声）
                if self._is_valid_entity(subject) and self._is_valid_entity(obj):
                    triples.append((subject, predicate, obj))

        return triples

    @staticmethod
    def _is_valid_entity(entity: str) -> bool:
        """判断实体是否有效 — 过滤停用词和过短/过长文本。

        Args:
            entity: 待验证的实体字符串。

        Returns:
            True 表示有效。
        """
        # 停用词集合
        stopwords = {
            "我们", "你们", "他们", "她们", "它们", "这个", "那个", "这些", "那些",
            "什么", "怎么", "为什么", "因为", "所以", "但是", "不过", "而且",
            "可以", "不能", "能够", "应该", "需要", "必须", "如果", "虽然",
            "一个", "一种", "一些", "这项", "该", "此", "其", "本",
            "本文", "本文档", "该文档", "该系统", "该功能",
        }
        if entity in stopwords:
            return False
        if len(entity) < 2:
            return False
        if len(entity) > 30:
            return False
        # 过滤纯数字
        if entity.isdigit():
            return False
        return True

    async def _extract_triples_by_llm(
        self,
        text: str,
        llm_provider,
    ) -> list[tuple[str, str, str]]:
        """LLM 提取 — 用于规则无法覆盖的复杂关系。

        仅在规则提取结果不足时作为兜底方案调用，节省 LLM 成本。

        Args:
            text: 文档文本。
            llm_provider: LLM Provider 实例。

        Returns:
            三元组列表 [(subject, predicate, object), ...]
        """
        prompt = f"""从以下文本中提取知识三元组（subject, predicate, object）。
格式：每行一个三元组，用 | 分隔，如：微服务|属于|架构模式

文本（前 2000 字）：
{text[:2000]}

只输出三元组，每行一个，不要额外解释。"""

        try:
            response_text = ""
            async for chunk in llm_provider.chat(
                [{"role": "system", "content": prompt}],
                stream=False,
                max_tokens=500,
            ):
                if isinstance(chunk, str):
                    response_text += chunk

            triples: list[tuple[str, str, str]] = []
            for line in response_text.strip().split("\n"):
                parts = line.split("|")
                if len(parts) == 3:
                    s, p, o = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    if s and p and o:
                        triples.append((s, p, o))

            return triples
        except Exception as e:
            logger.error("graph.triples.llm_error", error=str(e))
            return []

    # ------------------------------------------------------------------
    # 批量导入 — 文档入库时批量建图（UNWIND 高效写入）
    # ------------------------------------------------------------------

    async def batch_import_graph(
        self,
        nodes: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        batch_size: int = 500,
    ) -> dict[str, int]:
        """批量导入图谱数据 — 文档入库 / Celery 定时任务调用。

        使用 Neo4j UNWIND 语法批量写入，比逐条 CREATE 快 10-50x。
        节点用 MERGE（幂等），关系用 MERGE（幂等），支持重复导入不报错。

        典型用法：
            graph = get_graph_service()
            result = await graph.batch_import_graph(
                nodes=[
                    {"label": "Document", "id": "doc-1", "title": "架构规范", ...},
                    {"label": "Concept", "id": "微服务", "name": "微服务", ...},
                ],
                relationships=[
                    {"from_label": "Document", "from_id": "doc-1",
                     "to_label": "Concept", "to_id": "微服务", "type": "MENTIONS"},
                ],
            )

        Args:
            nodes: 节点列表，每项 {"label": ..., "id": ..., ...其他属性}。
            relationships: 关系列表，每项 {"from_label", "from_id", "to_label", "to_id", "type"}。
            batch_size: 每批写入数量（默认 500，Neo4j 单次事务建议 ≤ 10000）。

        Returns:
            {"nodes_created": int, "relationships_created": int}。
        """
        if not await self._ensure_connected():
            return {"nodes_created": 0, "relationships_created": 0}

        nodes_created = 0
        rels_created = 0

        # === 批量创建节点（按 label 分组，UNWIND 批量 MERGE）===
        if nodes:
            # 按 label 分组
            label_groups: dict[str, list[dict]] = {}
            for node in nodes:
                label = node.get("label", "Concept")
                label_groups.setdefault(label, []).append(
                    {k: v for k, v in node.items() if k != "label"}
                )

            for label, props_list in label_groups.items():
                # 分批写入
                for i in range(0, len(props_list), batch_size):
                    batch = props_list[i : i + batch_size]
                    try:
                        query = (
                            f"UNWIND $batch AS props "
                            f"MERGE (n:{label} {{id: props.id}}) "
                            f"SET n += props "
                            f"RETURN count(n) as created"
                        )
                        async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                            result = await session.run(query, batch=batch)
                            record = await result.single()
                            if record:
                                nodes_created += record["created"]
                    except Exception as exc:
                        logger.error(
                            "graph.batch_import_nodes_error",
                            label=label,
                            batch_size=len(batch),
                            error=str(exc),
                        )

        # === 批量创建关系（按 type 分组，UNWIND 批量 MERGE）===
        if relationships:
            # 按 (from_label, to_label, rel_type) 分组
            rel_groups: dict[str, list[dict]] = {}
            for rel in relationships:
                key = f"{rel['from_label']}|{rel['to_label']}|{rel['type']}"
                rel_groups.setdefault(key, []).append(rel)

            for key, rel_list in rel_groups.items():
                parts = key.split("|")
                if len(parts) != 3:
                    continue
                from_label, to_label, rel_type = parts

                # 分批写入
                for i in range(0, len(rel_list), batch_size):
                    batch = rel_list[i : i + batch_size]
                    try:
                        query = (
                            f"UNWIND $batch AS rel "
                            f"MATCH (a:{from_label} {{id: rel.from_id}}) "
                            f"MATCH (b:{to_label} {{id: rel.to_id}}) "
                            f"MERGE (a)-[r:{rel_type}]->(b) "
                            f"RETURN count(r) as created"
                        )
                        async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                            result = await session.run(query, batch=batch)
                            record = await result.single()
                            if record:
                                rels_created += record["created"]
                    except Exception as exc:
                        logger.error(
                            "graph.batch_import_rels_error",
                            rel_type=rel_type,
                            batch_size=len(batch),
                            error=str(exc),
                        )

        logger.info(
            "graph.batch_import_complete",
            nodes_created=nodes_created,
            relationships_created=rels_created,
        )
        return {
            "nodes_created": nodes_created,
            "relationships_created": rels_created,
        }

    async def batch_import_document(
        self,
        doc_id: str,
        title: str,
        content: str,
        triples: list[tuple[str, str, str]] | None = None,
        kb_id: str | None = None,
    ) -> dict[str, int]:
        """单文档批量建图 — 文档入库时的便捷入口。

        创建：
        1. Document 节点（含 doc_id, title, kb_id）
        2. 三元组对应的 Concept 节点 + 关系
        3. Document → Concept 的 MENTIONS 关系

        Args:
            doc_id: 文档 ID。
            title: 文档标题。
            content: 文档纯文本内容。
            triples: 预提取的三元组列表（None 则仅创建 Document 节点）。
            kb_id: 所属知识库 ID。

        Returns:
            {"nodes_created": int, "relationships_created": int}。
        """
        nodes: list[dict[str, Any]] = [
            {
                "label": "Document",
                "id": doc_id,
                "title": title,
                "kb_id": kb_id,
                "doc_type": "md",
            }
        ]
        relationships: list[dict[str, Any]] = []

        if triples:
            for subject, predicate, obj in triples:
                nodes.append({
                    "label": "Concept",
                    "id": subject,
                    "name": subject,
                    "entity_type": "concept",
                })
                nodes.append({
                    "label": "Concept",
                    "id": obj,
                    "name": obj,
                    "entity_type": "concept",
                })
                # 概念间关系
                rel_type = predicate.upper().replace(" ", "_")
                relationships.append({
                    "from_label": "Concept",
                    "from_id": subject,
                    "to_label": "Concept",
                    "to_id": obj,
                    "type": rel_type,
                })
                # 文档 → 概念的 MENTIONS 关系
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": "Concept",
                    "to_id": subject,
                    "type": "MENTIONS",
                })
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": "Concept",
                    "to_id": obj,
                    "type": "MENTIONS",
                })

        return await self.batch_import_graph(nodes, relationships)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """获取图谱统计信息。"""
        if not await self._ensure_connected():
            return {"total_nodes": 0, "total_edges": 0, "labels": {}}
        try:
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                # 节点总数
                result = await session.run("MATCH (n) RETURN count(n) as count")
                record = await result.single()
                total_nodes = record["count"] if record else 0

                # 关系总数
                result = await session.run("MATCH ()-[r]->() RETURN count(r) as count")
                record = await result.single()
                total_edges = record["count"] if record else 0

                # 各标签数量
                result = await session.run(
                    "MATCH (n) UNWIND labels(n) as label "
                    "RETURN label, count(*) as count ORDER BY count DESC"
                )
                records = await result.data()
                labels = {r["label"]: r["count"] for r in records}

            return {
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "labels": labels,
            }
        except Exception as e:
            logger.error("neo4j.stats_error", error=str(e))
            return {"total_nodes": 0, "total_edges": 0, "labels": {}}


# 单例
_graph_service: GraphService | None = None

def get_graph_service() -> GraphService:
    """获取 GraphService 单例。"""
    global _graph_service
    if _graph_service is None:
        _graph_service = GraphService()
    return _graph_service
