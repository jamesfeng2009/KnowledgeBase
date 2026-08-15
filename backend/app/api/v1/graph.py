"""知识图谱 API — 单一职责：提供图谱可视化、查询、管理的 RESTful 端点。

对应前端 knowledge/graph.astro 页面。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.graph_service import get_graph_service
from app.services.permission_service import PermissionService

router = APIRouter(prefix="/graph", tags=["知识图谱"])


@router.get("/data")
async def get_graph_data(
    node_label: str | None = Query(None, description="节点标签过滤"),
    node_id: str | None = Query(None, description="起始节点 ID（子图模式）"),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """获取图谱可视化数据（节点 + 边）。"""
    service = get_graph_service()
    data = await service.get_graph_data(
        node_label=node_label,
        node_id=node_id,
        limit=limit,
    )
    return ApiResponse(code=0, data=data, message="success")


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: str,
    label: str = Query("Document", description="节点标签"),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """获取节点详情。"""
    service = get_graph_service()
    node = await service.get_node(label, node_id)
    return ApiResponse(code=0, data=node, message="success")


@router.get("/nodes/{node_id}/related")
async def find_related(
    node_id: str,
    label: str = Query("Document"),
    max_depth: int = Query(2, ge=1, le=5),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """查找与指定节点间接相关的节点（多跳图遍历）。"""
    service = get_graph_service()
    nodes = await service.find_related_nodes(label, node_id, max_depth=max_depth)
    return ApiResponse(code=0, data=nodes, message="success")


@router.get("/path")
async def shortest_path(
    from_id: str = Query(..., description="起始节点 ID"),
    to_id: str = Query(..., description="目标节点 ID"),
    from_label: str = Query("Document"),
    to_label: str = Query("Document"),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """查找两个节点间的最短路径。"""
    service = get_graph_service()
    path = await service.shortest_path(from_label, from_id, to_label, to_id)
    return ApiResponse(code=0, data=path, message="success")


@router.get("/stats")
async def get_stats(
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """获取图谱统计信息。"""
    service = get_graph_service()
    stats = await service.get_stats()
    return ApiResponse(code=0, data=stats, message="success")


@router.post("/nodes")
async def create_node(
    label: str = Query(..., description="节点标签"),
    properties: dict = Body(..., description="节点属性"),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """创建图节点（需 admin 权限）。"""
    if user.role != "admin":
        return ApiResponse(code=403, data=None, message="需要管理员权限")
    service = get_graph_service()
    node = await service.create_node(label, properties)
    return ApiResponse(code=0, data=node, message="success")


@router.post("/relationships")
async def create_relationship(
    from_label: str = Query(...),
    from_id: str = Query(...),
    to_label: str = Query(...),
    to_id: str = Query(...),
    rel_type: str = Query(...),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """创建节点间的关系（需 admin 权限）。"""
    if user.role != "admin":
        return ApiResponse(code=403, data=None, message="需要管理员权限")
    service = get_graph_service()
    success = await service.create_relationship(
        from_label, from_id, to_label, to_id, rel_type
    )
    return ApiResponse(code=0, data={"success": success}, message="success")


# ------------------------------------------------------------------
# 关联推荐 — 三级缓存保障（L1 Redis → L2 Neo4j → L3 PG 降级）
# ------------------------------------------------------------------

@router.get("/recommendations/{doc_id}")
async def get_recommendations(
    request: Request,
    doc_id: str,
    top_k: int = Query(5, ge=1, le=20, description="推荐数量"),
    user: User = Depends(require_module("knowledge_graph")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取文档关联推荐 — 用户浏览文档时调用。

    三级保障：
    - L1 Redis 缓存（<5ms，命中率 60%+）
    - L2 Neo4j 2 跳图遍历（<30ms）
    - L3 PostgreSQL 全文检索降级（<200ms）

    结果按用户密级过滤，确保只返回用户可见的文档。
    """
    service = get_graph_service()
    tenant_id = getattr(request.state, "tenant_id", None)
    perm = PermissionService(db, user, tenant_id=tenant_id)
    recommendations = await service.get_related_recommendations(
        doc_id=doc_id,
        user_id=str(user.id),
        top_k=top_k,
        permission_filter=perm.filter_documents,
        db_session=db,
    )
    return ApiResponse(code=0, data=recommendations, message="success")


@router.delete("/recommendations/{doc_id}/cache")
async def invalidate_recommendation_cache(
    doc_id: str,
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """失效指定文档的推荐缓存 — 文档内容变更后调用。

    清除该文档对所有用户的推荐缓存，确保下次请求获取最新推荐。
    """
    service = get_graph_service()
    await service.invalidate_recommend_cache(doc_id)
    return ApiResponse(code=0, data={"invalidated": True}, message="success")


# ------------------------------------------------------------------
# 批量导入 — 文档入库时批量建图
# ------------------------------------------------------------------

@router.post("/batch-import")
async def batch_import(
    nodes: list[dict] = Body(..., description="节点列表"),
    relationships: list[dict] = Body(
        default=[], description="关系列表"
    ),
    batch_size: int = Query(500, ge=1, le=10000),
    user: User = Depends(require_module("knowledge_graph")),
) -> ApiResponse:
    """批量导入图谱数据 — 使用 UNWIND 高效写入。

    需 admin 权限。节点和关系均使用 MERGE（幂等），支持重复导入。

    请求体示例：
    ```json
    {
      "nodes": [
        {"label": "Document", "id": "doc-1", "title": "架构规范"},
        {"label": "Concept", "id": "微服务", "name": "微服务"}
      ],
      "relationships": [
        {"from_label": "Document", "from_id": "doc-1",
         "to_label": "Concept", "to_id": "微服务", "type": "MENTIONS"}
      ]
    }
    ```
    """
    if user.role != "admin":
        return ApiResponse(code=403, data=None, message="需要管理员权限")
    service = get_graph_service()
    result = await service.batch_import_graph(
        nodes=nodes,
        relationships=relationships,
        batch_size=batch_size,
    )
    return ApiResponse(code=0, data=result, message="success")


@router.post("/documents/{doc_id}/build-graph")
async def build_graph_from_document(
    doc_id: str,
    use_rules: bool = Query(True, description="启用规则提取"),
    use_llm: bool = Query(True, description="启用 LLM 兜底提取"),
    user: User = Depends(require_module("knowledge_graph")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """从文档内容构建知识图谱 — 混合三元组提取。

    流程：
    1. 读取文档纯文本内容
    2. 规则提取三元组（快速、免费）
    3. 规则结果不足时 LLM 兜底（准确、有成本）
    4. 批量写入 Neo4j 图谱

    需要 admin 权限。
    """
    if user.role != "admin":
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    from uuid import UUID

    from sqlalchemy import select

    from app.models.knowledge import Document

    # 读取文档内容
    result = await db.execute(
        select(Document).where(Document.id == UUID(doc_id))
    )
    doc = result.scalars().first()
    if not doc:
        return ApiResponse(code=404, data=None, message="文档不存在")

    # 检索不变量 I1：半成品不建图 — draft / pending_review 文档建图后，
    # 图谱节点会被检索路径召回（Cypher 只认 published），从源头杜绝。
    if doc.status != "published":
        return ApiResponse(
            code=400,
            data=None,
            message=f"仅已发布文档可建图（当前状态: {doc.status}）",
        )

    content = doc.content_text or ""
    if not content:
        return ApiResponse(code=400, data=None, message="文档无文本内容")

    # 获取 LLM Provider（可选）
    llm_provider = None
    if use_llm:
        try:
            from app.llm.factory import get_llm_provider

            llm_provider = get_llm_provider()
        except Exception:
            pass  # LLM 不可用时仅用规则提取

    service = get_graph_service()
    triples = await service.extract_triples_from_text(
        text=content,
        doc_id=doc_id,
        llm_provider=llm_provider,
        use_rules=use_rules,
        use_llm=use_llm,
    )

    # 失效推荐缓存（文档图谱已更新）
    await service.invalidate_recommend_cache(doc_id)

    return ApiResponse(
        code=0,
        data={
            "triples_count": len(triples),
            "triples": triples[:20],  # 最多返回 20 条预览
            "used_rules": use_rules,
            "used_llm": use_llm and len(triples) < 3,
        },
        message="success",
    )


@router.post("/backfill-doc-status")
async def backfill_doc_status(
    batch_size: int = Query(500, ge=1, le=5000),
    user: User = Depends(require_module("knowledge_graph")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """回填存量 Document 节点的 doc_status 属性 — 检索不变量 I1 上线后的迁移入口。

    图谱召回的 Cypher 以 ``d.doc_status = 'published'`` 过滤（fail-closed），
    存量节点缺该属性会被过滤（召回升零，不泄漏）。本端点从 DB 查全量
    文档真实状态，批量写入图谱节点属性，恢复已发布文档的图谱召回。

    需要 admin 权限。幂等，可重复执行。
    """
    if user.role != "admin":
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    from sqlalchemy import select

    from app.models.knowledge import Document

    result = await db.execute(select(Document.id, Document.status))
    status_map = {str(row[0]): row[1] for row in result.all() if row[1]}

    service = get_graph_service()
    updated = await service.sync_doc_status(status_map, batch_size=batch_size)
    return ApiResponse(
        code=0,
        data={"total_docs": len(status_map), "nodes_updated": updated},
        message="success",
    )
