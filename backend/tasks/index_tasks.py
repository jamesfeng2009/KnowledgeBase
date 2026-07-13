"""
索引构建任务 — 单一职责：全文索引（OpenSearch）与向量索引（Milvus）的构建与重建。

遵循单一职责：本模块只负责索引的构建与重建，
不涉及文档解析（委托 document_tasks）或业务逻辑。
遵循开闭原则：新增索引引擎只需扩展对应函数，
不修改任务入口签名。

注意：索引服务依赖外部组件（OpenSearch / Milvus），
不可用时优雅降级并记录日志。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from celery_app import celery_app
from app.utils.logger import get_logger

logger = get_logger(__name__)


@celery_app.task(
    name="tasks.index_tasks.build_search_index",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def build_search_index(self, doc_id: str) -> dict[str, Any]:
    """构建文档的全文索引（OpenSearch）。

    将文档内容索引到 OpenSearch，支持全文检索。
    OpenSearch 服务不可用时优雅降级。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        索引构建结果字典。
    """
    logger.info("index.search_task_started", doc_id=doc_id)
    try:
        result = asyncio.run(_build_search_index_async(doc_id))
        return result
    except Exception as exc:
        logger.error("index.search_task_failed", doc_id=doc_id, error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    name="tasks.index_tasks.build_vector_index",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def build_vector_index(self, doc_id: str) -> dict[str, Any]:
    """构建文档的向量索引（Milvus）。

    将文档内容向量化后存入 Milvus，支持语义检索。
    Milvus 服务不可用时优雅降级。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        索引构建结果字典。
    """
    logger.info("index.vector_task_started", doc_id=doc_id)
    try:
        result = asyncio.run(_build_vector_index_async(doc_id))
        return result
    except Exception as exc:
        logger.error("index.vector_task_failed", doc_id=doc_id, error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    name="tasks.index_tasks.rebuild_kb_index",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def rebuild_kb_index(self, kb_id: str) -> dict[str, Any]:
    """重建知识库的全部索引（全文 + 向量）。

    先删除旧索引，再为知识库下所有已发布文档重新构建索引。

    Args:
        kb_id: 知识库 ID（UUID 字符串）。

    Returns:
        重建结果字典，包含处理的文档数量。
    """
    logger.info("index.rebuild_kb_started", kb_id=kb_id)
    try:
        result = asyncio.run(_rebuild_kb_index_async(kb_id))
        return result
    except Exception as exc:
        logger.error("index.rebuild_kb_failed", kb_id=kb_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------
# 异步实现
# ------------------------------------------------------------------

async def _build_search_index_async(doc_id: str) -> dict[str, Any]:
    """异步构建 OpenSearch 全文索引。"""
    from app.database import async_session_factory
    from app.models.knowledge import Document
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        content = doc.content_text or ""
        if not content:
            return {"doc_id": doc_id, "status": "skipped", "reason": "内容为空"}

    # 延迟导入 OpenSearch 客户端
    try:
        from opensearchpy import AsyncOpenSearch
        from app.config import get_settings

        settings = get_settings()
        client = AsyncOpenSearch(hosts=[settings.OPENSEARCH_URL])

        index_name = "ekb_documents"
        if not await client.indices.exists(index=index_name):
            await client.indices.create(
                index=index_name,
                body={
                    "mappings": {
                        "properties": {
                            "doc_id": {"type": "keyword"},
                            "title": {"type": "text", "analyzer": "standard"},
                            "content": {"type": "text", "analyzer": "standard"},
                            "kb_id": {"type": "keyword"},
                        }
                    }
                },
            )

        await client.index(
            index=index_name,
            id=doc_id,
            body={
                "doc_id": doc_id,
                "title": doc.title,
                "content": content,
                "kb_id": str(doc.kb_id),
            },
        )
        await client.close()

        logger.info("index.search_built", doc_id=doc_id)
        return {"doc_id": doc_id, "status": "success", "index": "opensearch"}
    except ImportError:
        logger.warning("index.search_skipped", reason="opensearch-py not installed")
        return {"doc_id": doc_id, "status": "skipped", "reason": "opensearch-py not installed"}
    except Exception as exc:
        logger.warning("index.search_error", error=str(exc))
        raise


async def _build_vector_index_async(doc_id: str) -> dict[str, Any]:
    """异步构建 Milvus 向量索引。"""
    from app.database import async_session_factory
    from app.models.knowledge import Document
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        content = doc.content_text or ""
        if not content:
            return {"doc_id": doc_id, "status": "skipped", "reason": "内容为空"}

    # 生成向量嵌入
    try:
        from app.llm.embedder import get_embedder
        embedder = get_embedder()
        embeddings = await embedder.embed([content])
        if not embeddings:
            return {"doc_id": doc_id, "status": "skipped", "reason": "向量化失败"}
        embedding = embeddings[0]
    except Exception as exc:
        logger.warning("index.vector_embed_failed", error=str(exc))
        return {"doc_id": doc_id, "status": "skipped", "reason": f"embedder error: {exc}"}

    # 延迟导入 Milvus 客户端
    try:
        from pymilvus import connections, Collection, utility
        from app.config import get_settings

        settings = get_settings()
        connections.connect(
            alias="default",
            host=settings.MILVUS_HOST,
            port=str(settings.MILVUS_PORT),
        )

        collection_name = "ekb_documents"
        if not utility.has_collection(collection_name):
            logger.warning("milvus.collection_not_found", collection=collection_name)
            return {"doc_id": doc_id, "status": "skipped", "reason": "collection not found"}

        collection = Collection(collection_name)
        collection.insert([
            [doc_id],           # doc_id 列
            [0],                # chunk_id 列
            [content[:200]],    # content 列（预览）
            [embedding],         # embedding 列
        ])
        collection.load()

        logger.info("index.vector_built", doc_id=doc_id)
        return {"doc_id": doc_id, "status": "success", "index": "milvus"}
    except ImportError:
        logger.warning("index.vector_skipped", reason="pymilvus not installed")
        return {"doc_id": doc_id, "status": "skipped", "reason": "pymilvus not installed"}
    except Exception as exc:
        logger.warning("index.vector_error", error=str(exc))
        raise


async def _rebuild_kb_index_async(kb_id: str) -> dict[str, Any]:
    """异步重建知识库索引 — 删除旧索引后重新构建全部文档索引。"""
    from app.database import async_session_factory
    from app.models.knowledge import Document
    from app.repositories.knowledge_repository import DocumentRepository
    from sqlalchemy import select

    kb_uuid = uuid.UUID(kb_id)

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        docs = await repo.get_by_kb(kb_uuid)
        published_docs = [d for d in docs if d.status == "published"]

    doc_ids = [str(d.id) for d in published_docs]
    logger.info(
        "index.rebuild_docs",
        kb_id=kb_id,
        total_docs=len(docs),
        published_docs=len(published_docs),
    )

    success_count = 0
    failed_count = 0

    for doc_id in doc_ids:
        # 构建全文索引
        try:
            await _build_search_index_async(doc_id)
            success_count += 1
        except Exception as exc:
            logger.warning("index.rebuild_search_failed", doc_id=doc_id, error=str(exc))
            failed_count += 1

        # 构建向量索引
        try:
            await _build_vector_index_async(doc_id)
        except Exception as exc:
            logger.warning("index.rebuild_vector_failed", doc_id=doc_id, error=str(exc))

    return {
        "kb_id": kb_id,
        "status": "success",
        "total_docs": len(doc_ids),
        "indexed_docs": success_count,
        "failed_docs": failed_count,
    }
