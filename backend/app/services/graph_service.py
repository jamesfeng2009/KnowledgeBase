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

import re
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)
settings = get_settings()

#: Cypher 标识符（标签/关系类型）合法形式 — 防止 f-string 拼接导致的 Cypher 注入。
#: Neo4j 标签/关系类型无法参数化，只能白名单式校验后拼接。
_CYPHER_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_cypher_ident(value: str, kind: str = "label") -> str:
    """校验 Cypher 标识符（节点标签/关系类型），非法值抛 ValueError。

    Cypher 不支持标签/关系类型参数化，本项目以 f-string 拼接，必须严格校验，
    防止 ``label`` 等用户可控参数注入任意 Cypher 片段。
    """
    if not value or not _CYPHER_IDENT_RE.match(value):
        raise ValueError(f"非法 Cypher {kind}: {value!r}")
    return value

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
    - Image: 图片节点（P3：跨模态图片，含 description, doc_id）

    关系类型（type）：
    - REFERENCES: 引用（文档A引用文档B）
    - MENTIONS: 提及（文档提及概念）
    - RELATES_TO: 关联
    - REPLACES: 替代（产品A替代产品B）
    - BELONGS_TO: 属于
    - HYPERNYM: 上位词（概念A是概念B的上位词）
    - AUTHORED_BY: 作者
    - APPROVED_BY: 审批人
    - CONTAINS: 包含（文档包含图片，P3）
    """

    #: 推荐缓存 TTL（秒）— 5 分钟，平衡新鲜度与命中率。
    _RECOMMEND_TTL: int = 300

    def __init__(self, tenant_id: UUID | None = None) -> None:
        self._driver = None
        self._initialized = False
        # Redis 懒初始化（关联推荐缓存用）
        self._redis = None
        self._redis_available: bool | None = None
        self._tenant_id = tenant_id

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
            label = _validate_cypher_ident(label)
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
            label = _validate_cypher_ident(label)
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
            label = _validate_cypher_ident(label)
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
            from_label = _validate_cypher_ident(from_label)
            to_label = _validate_cypher_ident(to_label)
            rel_type = _validate_cypher_ident(rel_type, "relationship type")
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
            from_label = _validate_cypher_ident(from_label)
            to_label = _validate_cypher_ident(to_label)
            rel_type = _validate_cypher_ident(rel_type, "relationship type")
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

        P2-T4: 增加 tenant_id 过滤，补齐图谱层租户隔离。

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
            # 防注入：标签与关系类型校验（Cypher 无法参数化标识符）
            node_label = _validate_cypher_ident(node_label)
            rel_filter = ""
            if rel_types:
                rel_filter = ":" + "|".join(
                    _validate_cypher_ident(t, "relationship type") for t in rel_types
                )
            # 防注入：深度强制为有限整数
            max_depth = max(1, min(int(max_depth), 10))

            # P2-T4: 租户隔离过滤 — 全局节点（tenant_id IS NULL）对所有租户可见，
            # 租户节点仅本租户可见。起始节点与目标节点需分别过滤。
            query_params: dict[str, Any] = {"node_id": node_id}
            n_tenant_cond = "n.tenant_id IS NULL"
            m_tenant_cond = "m.tenant_id IS NULL"
            if self._tenant_id:
                n_tenant_cond = "(n.tenant_id IS NULL OR n.tenant_id = $tenant_id)"
                m_tenant_cond = "(m.tenant_id IS NULL OR m.tenant_id = $tenant_id)"
                query_params["tenant_id"] = str(self._tenant_id)

            query = (
                f"MATCH (n:{node_label} {{id: $node_id}})-[r{rel_filter}*1..{max_depth}]->(m) "
                f"WHERE {n_tenant_cond} AND {m_tenant_cond} "
                f"RETURN DISTINCT m, length(r) as depth "
                f"ORDER BY depth LIMIT 50"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, **query_params)
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
            doc_stmt = select(Document).where(Document.id == UUID(doc_id))
            doc_stmt = apply_tenant_filter(doc_stmt, Document, self._tenant_id)
            doc_result = await db_session.execute(doc_stmt)
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
                )

            stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
            stmt = stmt.order_by(Document.view_count.desc()).limit(top_k)
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
            from_label = _validate_cypher_ident(from_label)
            to_label = _validate_cypher_ident(to_label)
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
            if node_label:
                node_label = _validate_cypher_ident(node_label)
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
        # P2-T3: 使用 EntityRegistry 归一化实体类型和关系类型
        nodes, relationships = self._build_normalized_graph_data(
            all_triples, doc_id
        )

        # 批量导入（比逐条快 10-50x）
        await self.batch_import_graph(nodes, relationships)

        logger.info("graph.triples_extracted", doc_id=doc_id, count=len(all_triples))
        return all_triples

    async def extract_triples_from_chunks(
        self,
        chunks: list[Any],
        doc_id: str,
        llm_provider=None,
        use_rules: bool = True,
        use_llm: bool = True,
        llm_fallback_threshold: int = 3,
    ) -> list[tuple[str, str, str]]:
        """从已分块的 Chunk 对象列表中提取三元组（计算复用优化）。

        与 extract_triples_from_text 的区别：
            - extract_triples_from_text：接收原始文本，用于 API 手动触发场景
            - extract_triples_from_chunks：接收已分块的 Chunk 对象，用于文档处理流水线

        优势：
            - 避免重复分块计算（文档处理流水线已分块，无需再次切分）
            - 每个 chunk 独立提取，粒度更细，三元组边界更准确
            - chunk 的 title_path 可作为实体消歧的上下文锚点

        Args:
            chunks: Chunk 对象列表（需有 content 和 title_path 属性）。
            doc_id: 文档 ID（用于关联 Document 节点）。
            llm_provider: LLM Provider 实例（LLM 兜底用）。
            use_rules: 是否启用规则提取（默认 True）。
            use_llm: 是否启用 LLM 提取（默认 True）。
            llm_fallback_threshold: 规则提取达到此数量时跳过 LLM（默认 3）。

        Returns:
            提取的三元组列表 [(subject, predicate, object), ...]
        """
        all_triples: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()  # 全局去重（用于实体间关系去重）

        # 逐 chunk 提取三元组，保留 chunk → triples 关联用于溯源
        # chunk_data: [(chunk_id, chunk_content, chunk_title_path, chunk_triples), ...]
        chunk_data: list[tuple[str, str, str, list[tuple[str, str, str]]]] = []

        for chunk in chunks:
            text = getattr(chunk, "content", "") or ""
            if not text.strip():
                continue

            chunk_id = getattr(chunk, "id", "") or ""
            chunk_title_path = getattr(chunk, "title_path", "") or ""
            chunk_triples: list[tuple[str, str, str]] = []

            # 规则提取（快速、免费）
            if use_rules:
                rule_triples = self._extract_triples_by_rules(text)
                for t in rule_triples:
                    chunk_triples.append(t)
                    if t not in seen:
                        seen.add(t)
                        all_triples.append(t)

            # LLM 兜底（仅当全局规则结果不足时，避免每 chunk 都调 LLM）
            if (
                use_llm
                and len(all_triples) < llm_fallback_threshold
                and llm_provider
            ):
                llm_triples = await self._extract_triples_by_llm(text, llm_provider)
                for t in llm_triples:
                    chunk_triples.append(t)
                    if t not in seen:
                        seen.add(t)
                        all_triples.append(t)

            if chunk_triples:
                chunk_data.append((chunk_id, text, chunk_title_path, chunk_triples))

        if not all_triples:
            logger.info("graph.triples.empty_from_chunks", doc_id=doc_id)
            return []

        # 批量写入图谱 — 传入 chunk_data 构建 DocumentChunk 节点和 Chunk → Entity MENTIONS 边
        # P2-T3: 使用 EntityRegistry 归一化实体类型和关系类型
        # 溯源: Document → HAS_CHUNK → DocumentChunk → MENTIONS → KnowledgeEntity
        nodes, relationships = self._build_normalized_graph_data(
            all_triples, doc_id, chunk_data=chunk_data
        )

        await self.batch_import_graph(nodes, relationships)

        logger.info(
            "graph.triples_extracted_from_chunks",
            doc_id=doc_id,
            count=len(all_triples),
            chunk_count=len(chunks),
            chunk_with_triples=len(chunk_data),
        )
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
    # P2-T3: 三元组归一化 — EntityRegistry 集成
    # ------------------------------------------------------------------

    def _build_normalized_graph_data(
        self,
        triples: list[tuple[str, str, str]],
        doc_id: str,
        chunk_data: list[tuple[str, str, str, list[tuple[str, str, str]]]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """构建归一化后的图谱数据 — 使用标准实体类型和关系类型。

        P2-T3: 替代原有硬编码 Concept label + 中文谓词的逻辑。
        通过 EntityRegistry.normalize_triple() 将：
        - 实体名归一化（"合约" → "contract"）
        - 实体类型分类（Concept / Policy / Product / Person / Department）
        - 谓词映射（"属于" → BELONGS_TO）
        - 节点属性增加 tenant_id（补齐图谱层租户隔离）

        溯源链路: Document → HAS_CHUNK → DocumentChunk → MENTIONS → KnowledgeEntity
        - 当 chunk_data 可用时，创建 DocumentChunk 节点和 HAS_CHUNK 边，
          MENTIONS 边从 Chunk 指向 Entity（支持段落级溯源）。
        - 当 chunk_data 为 None 时（如 API 手动触发），回退到 Document → Entity MENTIONS。

        Args:
            triples: 原始三元组列表 [(subject, predicate, object), ...]。
            doc_id: 文档 ID（用于 Document → Chunk / Entity 关联）。
            chunk_data: 逐 chunk 提取数据 [(chunk_id, content, title_path, chunk_triples), ...]，
                        用于构建 DocumentChunk 节点和段落级 MENTIONS 溯源。

        Returns:
            (nodes, relationships) 归一化后的节点和关系列表。
        """
        try:
            from app.ontology.entity_registry import EntityRegistry
        except ImportError:
            # EntityRegistry 不可用 → 降级到原有逻辑（硬编码 Concept）
            return self._build_legacy_graph_data(triples, doc_id, chunk_data=chunk_data)

        nodes: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        tenant_id_str = str(self._tenant_id) if self._tenant_id else None

        # 构建 DocumentChunk 节点 + Document → Chunk HAS_CHUNK 边
        chunk_node_ids: set[str] = set()
        if chunk_data:
            for chunk_id, chunk_text, chunk_title_path, _ in chunk_data:
                if not chunk_id or chunk_id in chunk_node_ids:
                    continue
                chunk_node_ids.add(chunk_id)
                chunk_node: dict[str, Any] = {
                    "label": "DocumentChunk",
                    "id": chunk_id,
                    "doc_id": doc_id,
                    "text": chunk_text[:500],  # 截断存储，支持溯源回看
                    "title_path": chunk_title_path,
                }
                if tenant_id_str:
                    chunk_node["tenant_id"] = tenant_id_str
                nodes.append(chunk_node)
                # Document → DocumentChunk HAS_CHUNK
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": "DocumentChunk",
                    "to_id": chunk_id,
                    "type": "HAS_CHUNK",
                })

        for subject, predicate, obj in triples:
            # 归一化三元组
            normalized = EntityRegistry.normalize_triple(subject, predicate, obj)

            s_label = normalized.subject_type.value
            o_label = normalized.object_type.value
            s_id = normalized.subject_canonical
            o_id = normalized.object_canonical
            rel_type = normalized.predicate_standard.value

            # 实体节点（带 tenant_id 补齐租户隔离）
            s_node = {
                "label": s_label,
                "id": s_id,
                "name": s_id,
                "entity_type": s_label.lower(),
            }
            o_node = {
                "label": o_label,
                "id": o_id,
                "name": o_id,
                "entity_type": o_label.lower(),
            }
            if tenant_id_str:
                s_node["tenant_id"] = tenant_id_str
                o_node["tenant_id"] = tenant_id_str
            nodes.append(s_node)
            nodes.append(o_node)

            # 实体间关系（标准英文关系类型）
            relationships.append({
                "from_label": s_label,
                "from_id": s_id,
                "to_label": o_label,
                "to_id": o_id,
                "type": rel_type,
            })

            # MENTIONS: Chunk → Entity（有 chunk 溯源）或 Document → Entity（无 chunk 回退）
            if chunk_data:
                for chunk_id, _, _, chunk_triples in chunk_data:
                    # 该 chunk 是否提及此三元组的 subject 或 object
                    triple_in_chunk = any(
                        t[0] == subject and t[2] == obj
                        for t in chunk_triples
                    )
                    if triple_in_chunk:
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": s_label,
                            "to_id": s_id,
                            "type": "MENTIONS",
                        })
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": o_label,
                            "to_id": o_id,
                            "type": "MENTIONS",
                        })
            else:
                # 回退: 无 chunk 信息时，Document → Entity MENTIONS
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": s_label,
                    "to_id": s_id,
                    "type": "MENTIONS",
                })
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": o_label,
                    "to_id": o_id,
                    "type": "MENTIONS",
                })

        return nodes, relationships

    def _build_legacy_graph_data(
        self,
        triples: list[tuple[str, str, str]],
        doc_id: str,
        chunk_data: list[tuple[str, str, str, list[tuple[str, str, str]]]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """降级构建 — EntityRegistry 不可用时使用原有逻辑。

        溯源链路与 _build_normalized_graph_data 一致:
        Document → HAS_CHUNK → DocumentChunk → MENTIONS → Concept
        """
        nodes: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []

        # 构建 DocumentChunk 节点 + HAS_CHUNK 边
        chunk_node_ids: set[str] = set()
        if chunk_data:
            for chunk_id, chunk_text, chunk_title_path, _ in chunk_data:
                if not chunk_id or chunk_id in chunk_node_ids:
                    continue
                chunk_node_ids.add(chunk_id)
                nodes.append({
                    "label": "DocumentChunk",
                    "id": chunk_id,
                    "doc_id": doc_id,
                    "text": chunk_text[:500],
                    "title_path": chunk_title_path,
                })
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": "DocumentChunk",
                    "to_id": chunk_id,
                    "type": "HAS_CHUNK",
                })

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
            rel_type = predicate.upper().replace(" ", "_")
            relationships.append({
                "from_label": "Concept",
                "from_id": subject,
                "to_label": "Concept",
                "to_id": obj,
                "type": rel_type,
            })
            # MENTIONS: Chunk → Concept（有 chunk 溯源）或 Document → Concept（回退）
            if chunk_data:
                for chunk_id, _, _, chunk_triples in chunk_data:
                    triple_in_chunk = any(
                        t[0] == subject and t[2] == obj
                        for t in chunk_triples
                    )
                    if triple_in_chunk:
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": "Concept",
                            "to_id": subject,
                            "type": "MENTIONS",
                        })
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": "Concept",
                            "to_id": obj,
                            "type": "MENTIONS",
                        })
            else:
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
        return nodes, relationships

    # ------------------------------------------------------------------
    # P2-T5: 图谱召回 — 通过实体名查找关联文档
    # ------------------------------------------------------------------

    async def find_related_documents_by_entity(
        self,
        entity_names: list[str],
        max_depth: int = 2,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """通过实体名查找关联文档和证据片段 — 图谱召回专用。

        P2-T5: HybridRetriever._graph_search() 调用此方法，
        通过实体名在图谱中多跳遍历，找到关联的 DocumentChunk 和 Document 节点。

        溯源链路: Entity ← MENTIONS ← DocumentChunk ← HAS_CHUNK ← Document
        - 有 DocumentChunk 节点时返回 chunk 级证据内容（text + title_path）
        - 无 DocumentChunk 时回退到 Document 级（仅返回 doc_id + title）

        Args:
            entity_names: 实体名列表（已归一化的 canonical_name）。
            max_depth: 最大遍历深度（默认 2 跳）。
            max_results: 最大返回结果数。

        Returns:
            关联文档列表 [{"doc_id": ..., "title": ..., "kb_id": ...,
            "chunk_id": ..., "chunk_text": ..., "title_path": ...}, ...]。
            chunk_text 非空时为段落级证据内容，None 时仅有文档标题。
        """
        if not await self._ensure_connected() or not entity_names:
            return []

        results: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        max_depth = max(1, min(int(max_depth), 10))

        def tc(alias: str) -> str:
            if self._tenant_id:
                return f"({alias}.tenant_id IS NULL OR {alias}.tenant_id = $tenant_id)"
            return f"{alias}.tenant_id IS NULL"

        for entity_name in entity_names:
            if len(results) >= max_results:
                break
            try:
                params: dict[str, Any] = {
                    "entity_name": entity_name,
                    "limit": max_results - len(results),
                }
                if self._tenant_id:
                    params["tenant_id"] = str(self._tenant_id)

                # 1. Chunk 级遍历（新结构）:
                #    Entity ← MENTIONS ← DocumentChunk ← HAS_CHUNK → Document
                #    Entity-to-entity 多跳（undirected），扩展召回覆盖
                chunk_query = (
                    "MATCH (n {name: $entity_name}) "
                    f"WHERE {tc('n')} "
                    f"MATCH (n)-[r*0..{max_depth}]-(e) "
                    f"WHERE {tc('e')} "
                    "MATCH (e)<-[:MENTIONS]-(c:DocumentChunk)-[:HAS_CHUNK]-(d:Document) "
                    f"WHERE {tc('d')} "
                    "RETURN DISTINCT "
                    "  d.id as doc_id, d.title as title, d.kb_id as kb_id, "
                    "  c.id as chunk_id, c.text as chunk_text, c.title_path as title_path "
                    "LIMIT $limit"
                )
                async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                    result = await session.run(chunk_query, **params)
                    records = await result.data()

                for record in records:
                    doc_id = record.get("doc_id", "")
                    if doc_id and doc_id not in seen_doc_ids:
                        seen_doc_ids.add(doc_id)
                        results.append({
                            "doc_id": doc_id,
                            "title": record.get("title", ""),
                            "kb_id": record.get("kb_id"),
                            "chunk_id": record.get("chunk_id"),
                            "chunk_text": record.get("chunk_text"),
                            "title_path": record.get("title_path"),
                        })

                # 2. Document 级回退（旧结构无 DocumentChunk）:
                #    Entity ← MENTIONS ← Document
                if len(results) < max_results:
                    params["limit"] = max_results - len(results)
                    doc_query = (
                        "MATCH (n {name: $entity_name}) "
                        f"WHERE {tc('n')} "
                        f"MATCH (n)-[r*0..{max_depth}]-(e) "
                        f"WHERE {tc('e')} "
                        "MATCH (e)<-[:MENTIONS]-(d:Document) "
                        f"WHERE {tc('d')} "
                        "AND NOT (d)-[:HAS_CHUNK]->() "
                        "RETURN DISTINCT "
                        "  d.id as doc_id, d.title as title, d.kb_id as kb_id "
                        "LIMIT $limit"
                    )
                    async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                        result = await session.run(doc_query, **params)
                        records = await result.data()

                    for record in records:
                        doc_id = record.get("doc_id", "")
                        if doc_id and doc_id not in seen_doc_ids:
                            seen_doc_ids.add(doc_id)
                            results.append({
                                "doc_id": doc_id,
                                "title": record.get("title", ""),
                                "kb_id": record.get("kb_id"),
                                "chunk_id": None,
                                "chunk_text": None,
                                "title_path": None,
                            })

            except Exception as exc:
                logger.warning(
                    "graph.find_related_docs_by_entity_error",
                    entity=entity_name,
                    error=str(exc),
                )
                continue

        return results[:max_results]

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
                # 防注入：标签校验（LLM 提取的标签可能含非法字符）
                try:
                    label = _validate_cypher_ident(label)
                except ValueError as exc:
                    logger.error("graph.batch_import_invalid_label", error=str(exc))
                    continue
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

                # 防注入：标签与关系类型校验
                try:
                    from_label = _validate_cypher_ident(from_label)
                    to_label = _validate_cypher_ident(to_label)
                    rel_type = _validate_cypher_ident(rel_type, "relationship type")
                except ValueError as exc:
                    logger.error("graph.batch_import_invalid_rel", error=str(exc))
                    continue

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
        chunks: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """单文档批量建图 — 文档入库时的便捷入口。

        创建：
        1. Document 节点（含 doc_id, title, kb_id）
        2. DocumentChunk 节点 + HAS_CHUNK 边（当 chunks 提供时，支持段落级溯源）
        3. 三元组对应的 Concept 节点 + 关系
        4. MENTIONS 关系：Chunk → Concept（有 chunks 时）或 Document → Concept（无 chunks 时）

        Args:
            doc_id: 文档 ID。
            title: 文档标题。
            content: 文档纯文本内容。
            triples: 预提取的三元组列表（None 则仅创建 Document 节点）。
            kb_id: 所属知识库 ID。
            chunks: 预分块的 chunk 列表 [{"id": ..., "content": ..., "title_path": ...}, ...]，
                    提供时构建 DocumentChunk 节点并使用段落级 MENTIONS 溯源。

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

        # 构建 DocumentChunk 节点 + HAS_CHUNK 边
        chunk_ids: list[str] = []
        if chunks:
            for chunk in chunks:
                chunk_id = chunk.get("id", "") or ""
                if not chunk_id:
                    continue
                chunk_ids.append(chunk_id)
                nodes.append({
                    "label": "DocumentChunk",
                    "id": chunk_id,
                    "doc_id": doc_id,
                    "text": (chunk.get("content", "") or "")[:500],
                    "title_path": chunk.get("title_path", "") or "",
                })
                relationships.append({
                    "from_label": "Document",
                    "from_id": doc_id,
                    "to_label": "DocumentChunk",
                    "to_id": chunk_id,
                    "type": "HAS_CHUNK",
                })

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
                # MENTIONS: Chunk → Concept（有 chunk 时）或 Document → Concept（无 chunk 时）
                if chunk_ids:
                    for chunk_id in chunk_ids:
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": "Concept",
                            "to_id": subject,
                            "type": "MENTIONS",
                        })
                        relationships.append({
                            "from_label": "DocumentChunk",
                            "from_id": chunk_id,
                            "to_label": "Concept",
                            "to_id": obj,
                            "type": "MENTIONS",
                        })
                else:
                    # 无 chunk 信息时，Document → Concept MENTIONS
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
    # P3: 图片节点管理
    # ------------------------------------------------------------------

    async def add_image_nodes(
        self,
        doc_id: str,
        images: list[tuple[bytes, str]],
    ) -> int:
        """P3: 批量创建图片节点并关联到文档。

        为文档中的每张图片创建 Image 节点（含 VLM 描述），
        并建立 Document → Image 的 CONTAINS 关系。
        图片节点支持图谱可视化中展示文档包含的图片资源。

        优雅降级：Neo4j 不可用时返回 0，不影响主流程。

        Args:
            doc_id: 文档 ID。
            images: 图片数据列表 [(图片二进制, VLM描述), ...]。

        Returns:
            成功创建的图片节点数量。
        """
        if not images:
            return 0

        import hashlib

        nodes: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        tenant_id_str = str(self._tenant_id) if self._tenant_id else None

        for img_bytes, desc in images:
            # 用图片内容的 SHA256 前 16 位作为唯一 ID，避免重复
            img_hash = hashlib.sha256(img_bytes).hexdigest()[:16]
            img_id = f"img:{doc_id}:{img_hash}"

            node: dict[str, Any] = {
                "label": "Image",
                "id": img_id,
                "doc_id": doc_id,
                "description": desc or "[图片内容]",
                "content_type": "image",
                "size_bytes": len(img_bytes),
            }
            if tenant_id_str:
                node["tenant_id"] = tenant_id_str
            nodes.append(node)

            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "Image",
                "to_id": img_id,
                "type": "CONTAINS",
            })

        try:
            result = await self.batch_import_graph(nodes, relationships)
            created = result.get("nodes_created", 0)
            logger.info(
                "graph.image_nodes_added",
                doc_id=doc_id,
                image_count=created,
            )
            return created
        except Exception as exc:
            logger.warning("graph.image_nodes_failed", doc_id=doc_id, error=str(exc))
            return 0

    async def get_document_images(
        self,
        doc_id: str,
    ) -> list[dict[str, Any]]:
        """P3: 查询文档包含的所有图片节点。

        通过 Document → Image 的 CONTAINS 关系查找。

        Args:
            doc_id: 文档 ID。

        Returns:
            图片节点列表 [{"id", "description", "size_bytes"}, ...]。
        """
        if not await self._ensure_connected():
            return []
        try:
            query = (
                "MATCH (d:Document {id: $doc_id})-[:CONTAINS]->(img:Image) "
                "RETURN img.id as id, img.description as description, "
                "img.size_bytes as size_bytes "
                "ORDER BY img.id"
            )
            async with self._driver.session(database=settings.NEO4J_DATABASE) as session:
                result = await session.run(query, doc_id=doc_id)
                records = await result.data()
            return [
                {
                    "id": r["id"],
                    "description": r.get("description", ""),
                    "size_bytes": r.get("size_bytes", 0),
                }
                for r in records
            ]
        except Exception as e:
            logger.error("graph.get_images_error", doc_id=doc_id, error=str(e))
            return []

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
