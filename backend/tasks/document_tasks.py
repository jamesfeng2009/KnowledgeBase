"""
文档处理任务 — 单一职责：文档解析、分块、向量化、索引的异步流水线。

遵循单一职责：本模块只负责文档处理流水线的任务编排，
不包含文档解析的具体实现（延迟导入第三方库）。
遵循开闭原则：新增文档类型只需扩展 _parse_document 中的分支，
不修改 process_document 主流程。

分块策略：使用 SemanticChunker 的四级优先级分块策略
（Q&A 分块 → 结构化分块 → TextTiling 语义分块 → 固定长度兜底），
不再使用简单的滑动窗口分块。

注意：文档解析依赖 pymupdf / python-docx 等第三方库，
这些库可能未安装，使用延迟导入实现优雅降级。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from celery_app import celery_app

from app.rag.chunker import Chunk, SemanticChunker
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 保留旧常量供向后兼容引用，实际分块已委托给 SemanticChunker
# Deprecated: 使用 SemanticChunker 替代简单滑动窗口
CHUNK_SIZE: int = 500
CHUNK_OVERLAP: int = 50


@celery_app.task(
    name="tasks.document_tasks.process_document",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def process_document(self, doc_id: str) -> dict[str, Any]:
    """文档处理流水线 — 解析 → 分块 → 向量化 → 索引 → 更新状态。

    异步执行文档的完整处理流程，适用于上传后自动处理。
    每个阶段失败时自动重试（最多 3 次）。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        处理结果字典，包含 doc_id、status、chunk_count 等。
    """
    logger.info("document.task_started", doc_id=doc_id)

    try:
        # 在事件循环中执行异步操作
        result = asyncio.run(_process_document_async(doc_id))
        logger.info(
            "document.task_completed",
            doc_id=doc_id,
            status=result.get("status"),
            chunk_count=result.get("chunk_count", 0),
        )
        return result
    except Exception as exc:
        logger.error(
            "document.task_failed",
            doc_id=doc_id,
            error=str(exc),
        )
        # 重试
        raise self.retry(exc=exc)


@celery_app.task(name="tasks.document_tasks.batch_process_documents")
def batch_process_documents(doc_ids: list[str]) -> list[dict[str, Any]]:
    """批量处理文档 — 逐个调用 process_document。

    使用 Celery group/chord 可实现并行处理，此处简化为串行调用。

    Args:
        doc_ids: 文档 ID 列表（UUID 字符串）。

    Returns:
        每个文档的处理结果列表。
    """
    logger.info("document.batch_started", count=len(doc_ids))
    results: list[dict[str, Any]] = []

    for doc_id in doc_ids:
        try:
            # 串行调用单个文档处理任务
            result = process_document.apply(args=[doc_id]).get()
            results.append(result)
        except Exception as exc:
            logger.error(
                "document.batch_item_failed",
                doc_id=doc_id,
                error=str(exc),
            )
            results.append({
                "doc_id": doc_id,
                "status": "failed",
                "error": str(exc),
            })

    logger.info(
        "document.batch_completed",
        total=len(doc_ids),
        success=len([r for r in results if r.get("status") == "success"]),
        failed=len([r for r in results if r.get("status") == "failed"]),
    )
    return results


# ------------------------------------------------------------------
# 异步处理逻辑
# ------------------------------------------------------------------

async def _process_document_async(doc_id: str) -> dict[str, Any]:
    """文档处理异步流水线。

    步骤：
    1. 加载文档（从数据库）；
    2. 解析文档内容（延迟导入第三方库）；
    3. 分块；
    4. 向量化（调用 Embedder）；
    5. 索引（构建全文索引 + 向量索引）；
    6. 更新文档状态。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        处理结果字典。
    """
    from app.database import async_session_factory
    from app.models.knowledge import Document
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        # 1. 解析文档内容
        try:
            parsed_text = await _parse_document(doc)
        except Exception as exc:
            logger.warning("document.parse_failed", doc_id=doc_id, error=str(exc))
            parsed_text = doc.content_text or ""

        # 2. 更新纯文本内容（检索用）
        if parsed_text and parsed_text != doc.content_text:
            doc.content_text = parsed_text
            await session.flush()

        # 3. 分块 — 使用 SemanticChunker 四级优先级策略
        #    （Q&A → 结构化 → TextTiling → 固定长度兜底）
        chunk_objects = _chunk_document(parsed_text, doc.doc_type or "md")
        chunks = [c.content for c in chunk_objects]
        logger.info(
            "document.chunked",
            doc_id=doc_id,
            chunk_count=len(chunks),
            strategies=[c.chunk_strategy for c in chunk_objects],
        )

        # 4. 向量化（延迟导入，外部服务不可用时优雅降级）
        try:
            embeddings = await _generate_embeddings(chunks)
            logger.info(
                "document.embedded",
                doc_id=doc_id,
                embedding_count=len(embeddings),
            )
        except Exception as exc:
            logger.warning("document.embed_failed", doc_id=doc_id, error=str(exc))
            embeddings = []

        # 5. 索引（延迟导入，构建全文索引和向量索引）
        try:
            await _build_indexes(doc_id, chunk_objects, chunks, embeddings)
        except Exception as exc:
            logger.warning("document.index_failed", doc_id=doc_id, error=str(exc))

        # 6. 更新文档状态为已发布
        doc.status = "published"
        await session.commit()

        # 7. 链式触发文档智能处理（摘要/标签/分类/行动项）
        try:
            from tasks.intelligence_tasks import process_intelligence
            process_intelligence.delay(doc_id)
            logger.info("document.intelligence_triggered", doc_id=doc_id)
        except Exception as exc:
            logger.warning("document.intelligence_trigger_failed", doc_id=doc_id, error=str(exc))

        return {
            "doc_id": doc_id,
            "status": "success",
            "chunk_count": len(chunks),
            "embedding_count": len(embeddings),
            "doc_status": "published",
            "chunk_strategies": list(set(c.chunk_strategy for c in chunk_objects)),
        }


async def _parse_document(doc: Any) -> str:
    """解析文档内容 — 根据文档类型选择解析器。

    使用延迟导入，第三方库未安装时优雅降级为直接返回 content_text。

    Args:
        doc: Document ORM 实例。

    Returns:
        解析后的纯文本内容。
    """
    # 如果已有纯文本内容，直接返回
    if doc.content_text:
        return doc.content_text

    # 根据文档类型解析
    doc_type = doc.doc_type or "md"

    if doc_type == "pdf":
        return await _parse_pdf(doc)
    elif doc_type == "docx":
        return await _parse_docx(doc)
    elif doc_type == "html":
        return _parse_html(doc)
    else:
        # Markdown 或纯文本，直接返回
        return doc.content_html or doc.content_text or ""


async def _parse_pdf(doc: Any) -> str:
    """解析 PDF 文档 — 延迟导入 pymupdf。

    库未安装时优雅降级。
    """
    try:
        import fitz  # pymupdf

        if not doc.file_path:
            return ""
        pdf_doc = fitz.open(doc.file_path)
        text_parts: list[str] = []
        for page in pdf_doc:
            text_parts.append(page.get_text())
        pdf_doc.close()
        return "\n".join(text_parts)
    except ImportError:
        logger.warning("pdf.parse_skipped", reason="pymupdf not installed")
        return doc.content_text or ""
    except Exception as exc:
        logger.warning("pdf.parse_error", error=str(exc))
        return doc.content_text or ""


async def _parse_docx(doc: Any) -> str:
    """解析 DOCX 文档 — 延迟导入 python-docx。

    库未安装时优雅降级。
    """
    try:
        from docx import Document as DocxDocument

        if not doc.file_path:
            return ""
        docx_doc = DocxDocument(doc.file_path)
        paragraphs = [p.text for p in docx_doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        logger.warning("docx.parse_skipped", reason="python-docx not installed")
        return doc.content_text or ""
    except Exception as exc:
        logger.warning("docx.parse_error", error=str(exc))
        return doc.content_text or ""


def _parse_html(doc: Any) -> str:
    """解析 HTML 文档 — 去除标签提取纯文本。"""
    try:
        import re

        html = doc.content_html or ""
        # 移除 script 和 style 标签
        clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL)
        # 移除 HTML 标签
        text = re.sub(r"<[^>]+>", "", clean)
        # 压缩空白
        return re.sub(r"\s+", " ", text).strip()
    except Exception as exc:
        logger.warning("html.parse_error", error=str(exc))
        return doc.content_text or ""


def _chunk_document(
    text: str,
    doc_type: str = "md",
    content_type: str = "auto",
) -> list[Chunk]:
    """使用 SemanticChunker 对文档执行四级优先级分块。

    策略优先级：
    0. 内容类型路由（content_type 显式指定时）；
    1. 结构化分块：按 Markdown 标题或 HTML 标签分割（带标题路径锚点）；
    2. 语义分块：TextTiling 相似度算法，在话题边界分割；
    3. 父子索引：小块检索、大块上下文；
    4. 固定长度兜底：512 tokens 固定分割。

    Args:
        text: 待分块的纯文本内容。
        doc_type: 文档类型（md / html / docx / pdf / txt 等）。
        content_type: 内容类型标签（auto / faq / tutorial / specification / report / plain）。

    Returns:
        Chunk 对象列表，每个 Chunk 包含 content、title_path、content_type、
        chunk_strategy 等元数据。
    """
    if not text or not text.strip():
        return []

    chunker = SemanticChunker()
    chunks = chunker.chunk(text, doc_type=doc_type, content_type=content_type)
    logger.info(
        "document.chunk_detail",
        chunk_count=len(chunks),
        strategies=[c.chunk_strategy for c in chunks],
        doc_type=doc_type,
        content_type=content_type,
    )
    return chunks


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """[Deprecated] 简单滑动窗口分块 — 保留供向后兼容。

    新代码应使用 _chunk_document() 接入 SemanticChunker 四级分块策略。
    """
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk.strip())
        start = end - overlap  # 滑动窗口

    return [c for c in chunks if c]


async def _generate_embeddings(chunks: list[str]) -> list[list[float]]:
    """生成文本块的向量嵌入 — 延迟导入 Embedder。

    外部服务不可用时优雅降级，返回空列表。

    Args:
        chunks: 文本块列表。

    Returns:
        向量嵌入列表（每个文本块对应一个向量）。
    """
    if not chunks:
        return []

    from app.llm.embedder import get_embedder

    embedder = get_embedder()
    return await embedder.embed(chunks)


async def _build_indexes(
    doc_id: str,
    chunk_objects: list[Chunk],
    chunks: list[str],
    embeddings: list[list[float]],
) -> None:
    """构建全文索引和向量索引 — 延迟导入。

    传递 Chunk 对象以索引 richer 元数据（title_path、content_type、chunk_strategy）。

    Args:
        doc_id: 文档 ID。
        chunk_objects: Chunk 对象列表（含元数据）。
        chunks: 文本块内容列表（chunk_objects 中提取的 .content）。
        embeddings: 向量嵌入列表。
    """
    # 构建全文索引（OpenSearch）— 传入 Chunk 元数据
    try:
        await _build_opensearch_index(doc_id, chunk_objects)
    except Exception as exc:
        logger.warning("opensearch.index_failed", doc_id=doc_id, error=str(exc))

    # 构建向量索引（Milvus）— 传入 Chunk 元数据
    try:
        await _build_milvus_index(doc_id, chunk_objects, embeddings)
    except Exception as exc:
        logger.warning("milvus.index_failed", doc_id=doc_id, error=str(exc))


async def _build_opensearch_index(doc_id: str, chunk_objects: list[Chunk]) -> None:
    """构建 OpenSearch 全文索引 — 延迟导入。

    存储 Chunk 元数据（title_path、content_type、chunk_strategy）以支持
    检索时按内容类型过滤和标题路径展示。

    库未安装或服务不可用时优雅降级。
    """
    try:
        from opensearchpy import AsyncOpenSearch
        from app.config import get_settings

        settings = get_settings()
        client = AsyncOpenSearch(hosts=[settings.OPENSEARCH_URL])

        index_name = "ekb_documents"
        # 确保索引存在
        if not await client.indices.exists(index=index_name):
            await client.indices.create(
                index=index_name,
                body={
                    "mappings": {
                        "properties": {
                            "doc_id": {"type": "keyword"},
                            "chunk_id": {"type": "keyword"},
                            "parent_id": {"type": "keyword"},
                            "content": {"type": "text", "analyzer": "standard"},
                            "title_path": {"type": "text", "analyzer": "keyword"},
                            "content_type": {"type": "keyword"},
                            "chunk_strategy": {"type": "keyword"},
                            "token_count": {"type": "integer"},
                        }
                    }
                },
            )

        # 批量索引文档块（含元数据）
        for idx, chunk in enumerate(chunk_objects):
            await client.index(
                index=index_name,
                body={
                    "doc_id": doc_id,
                    "chunk_id": chunk.id,
                    "parent_id": chunk.parent_id,
                    "content": chunk.content,
                    "title_path": chunk.title_path,
                    "content_type": chunk.content_type,
                    "chunk_strategy": chunk.chunk_strategy,
                    "token_count": chunk.token_count,
                },
            )
        await client.close()
        logger.info("opensearch.indexed", doc_id=doc_id, chunk_count=len(chunk_objects))
    except ImportError:
        logger.warning("opensearch.skipped", reason="opensearch-py not installed")
    except Exception as exc:
        logger.warning("opensearch.index_error", error=str(exc))
        raise


async def _build_milvus_index(
    doc_id: str,
    chunk_objects: list[Chunk],
    embeddings: list[list[float]],
) -> None:
    """构建 Milvus 向量索引 — 延迟导入。

    存储 Chunk 元数据（title_path、content_type、chunk_strategy）以支持
    向量检索后按内容类型过滤和标题路径展示。

    库未安装或服务不可用时优雅降级。
    """
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
            return

        collection = Collection(collection_name)
        # 插入向量数据（含 Chunk 元数据）
        if embeddings:
            n = len(embeddings)
            chunks_meta = chunk_objects[:n]
            collection.insert([
                [doc_id] * n,                                    # doc_id 列
                [c.id for c in chunks_meta],                     # chunk_id 列
                [c.content for c in chunks_meta],                # content 列
                embeddings,                                      # embedding 列
                [c.title_path for c in chunks_meta],             # title_path 列
                [c.content_type for c in chunks_meta],           # content_type 列
                [c.chunk_strategy for c in chunks_meta],         # chunk_strategy 列
            ])
            collection.load()
        logger.info(
            "milvus.indexed",
            doc_id=doc_id,
            vector_count=len(embeddings),
        )
    except ImportError:
        logger.warning("milvus.skipped", reason="pymilvus not installed")
    except Exception as exc:
        logger.warning("milvus.index_error", error=str(exc))
        raise
