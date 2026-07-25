"""
多模态处理 Celery 任务 — 文档入库后处理图片/表格/扫描件。

在 document_tasks.process_document 完成后链式调用：
    1. 提取文档中的所有图片（原始二进制）
    2. 对每张图片调用 VLM 生成描述 + 标签
    3. 将描述文本作为 image_desc chunk 向量化入库

与跨模态向量检索（jina-clip-v2）互补：
    - 跨模态（P0）：图片二进制 → jina-clip-v2 向量（文本查询直接命中图片）
    - VLM 描述（P1）：图片 → VLM 文本描述 → 文本向量（文本查询命中描述文本）
    两条路径互补，提高图片检索召回率。

优雅降级：VLM/Embedder 不可用时跳过图片处理，不影响文档入库。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    """在同步 Celery 任务中执行异步协程。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _process_document_images(doc_id: str) -> dict:
    """处理文档中的所有图片 — VLM 生成描述并索引为文本向量。

    流程：
        1. 从 DB 加载文档，获取 file_path / doc_type / kb_id
        2. 提取文档中的原始图片二进制数据
        3. 对每张图片调用 MultimodalService.process_image() 获取描述 + 标签
        4. 将描述作为 Chunk(content_type="image_desc") 向量化入库

    优雅降级：
        - 文档不存在/无文件路径 → 返回 success(0)
        - 无图片 → 返回 success(0)
        - VLM 不可用 → 跳过该图片
        - Embedder/向量库不可用 → 返回 partial

    Args:
        doc_id: 文档 ID 字符串。

    Returns:
        处理结果统计 {"doc_id", "images_processed", "status"}。
    """
    import uuid

    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    # 1. 从 DB 加载文档元数据（在 session 内提取标量值，避免 DetachedInstanceError）
    file_path: str | None = None
    doc_type: str = "md"
    kb_id: str | None = None

    async with task_db_session() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        file_path = doc.file_path
        doc_type = doc.doc_type or "md"
        kb_id = str(doc.kb_id) if doc.kb_id else None

    if not file_path:
        logger.info("multimodal.no_file_path", doc_id=doc_id)
        return {"doc_id": doc_id, "images_processed": 0, "status": "success"}

    # 2. 提取原始图片
    raw_images = await _extract_raw_images_for_vlm(file_path, doc_type)
    if not raw_images:
        logger.info("multimodal.no_images", doc_id=doc_id, doc_type=doc_type)
        return {"doc_id": doc_id, "images_processed": 0, "status": "success"}

    logger.info("multimodal.images_found", doc_id=doc_id, count=len(raw_images))

    # 3. VLM 处理每张图片 — 生成描述 + 标签
    from app.services.multimodal_service import MultimodalService

    service = MultimodalService()

    from app.rag.chunker import Chunk

    chunks: list[Chunk] = []

    for img_bytes, mime_type in raw_images:
        try:
            result = await service.process_image(img_bytes, mime_type)
            desc = result.get("description", "")
            tags = result.get("tags", [])

            if not desc or not desc.strip():
                continue

            # 构建索引内容：描述 + 标签
            content = desc.strip()
            if tags:
                content += f"\n标签: {', '.join(tags)}"

            chunk = Chunk(
                id=str(uuid.uuid4()),
                doc_id=doc_id,
                content=content,
                title_path="[图片描述]",
                content_type="image_desc",
                chunk_strategy="vlm_image",
                token_count=len(content) // 2,
            )
            chunks.append(chunk)
        except Exception as exc:
            logger.warning(
                "multimodal.image_process_failed",
                doc_id=doc_id,
                error=str(exc)[:200],
            )

    if not chunks:
        logger.info("multimodal.no_descriptions", doc_id=doc_id)
        return {"doc_id": doc_id, "images_processed": 0, "status": "success"}

    # 4. 生成向量并入库
    texts = [c.content for c in chunks]
    try:
        from app.llm.embedder import get_embedder
        from app.rag.vector_store import get_vector_store

        embedder = get_embedder()
        embeddings = await embedder.embed(texts)

        if not embeddings:
            logger.warning("multimodal.embed_empty", doc_id=doc_id)
            return {
                "doc_id": doc_id,
                "images_processed": 0,
                "status": "partial",
                "error": "向量化返回空结果",
            }

        store = get_vector_store()
        count = await store.upsert(doc_id, chunks, embeddings, kb_id=kb_id)

        logger.info(
            "multimodal.task_completed",
            doc_id=doc_id,
            images_processed=count,
        )
        return {
            "doc_id": doc_id,
            "images_processed": count,
            "status": "success",
        }
    except Exception as exc:
        logger.warning(
            "multimodal.embed_failed",
            doc_id=doc_id,
            error=str(exc)[:200],
        )
        return {
            "doc_id": doc_id,
            "images_processed": 0,
            "status": "partial",
            "error": f"向量化/入库失败: {str(exc)[:200]}",
        }


async def _extract_raw_images_for_vlm(
    file_path: str,
    doc_type: str,
) -> list[tuple[bytes, str]]:
    """从文档中提取原始图片二进制数据（不经过 VLM 处理）。

    与 document_tasks._extract_images_for_cross_modal 不同，本函数：
        - 不检查 CROSS_MODAL_ENABLED（始终提取）
        - 不生成 VLM 描述（仅返回原始字节）
        - 供 _process_document_images 进行更丰富的 VLM 处理（描述 + 标签）

    Args:
        file_path: 文档文件路径。
        doc_type: 文档类型（pdf / docx / pptx）。

    Returns:
        [(图片二进制, MIME类型), ...] 列表。
    """
    images: list[tuple[bytes, str]] = []

    try:
        if doc_type == "pdf":
            # PDF: 使用 Docling 提取图片
            from app.document.docling_parser import DoclingParser

            parser = DoclingParser()
            result = await parser._parse_raw(file_path)
            if result is not None:
                pictures = DoclingParser._extract_pictures(result)
                for pic in pictures:
                    images.append(
                        (pic["data"], pic.get("mime_type", "image/png"))
                    )
        elif doc_type in ("docx", "pptx"):
            # DOCX/PPTX: 使用解析器的 extract_raw_images 方法
            from app.document.factory import get_parser_with_fallback

            parser, _ = get_parser_with_fallback(doc_type)
            if parser is not None and hasattr(parser, "extract_raw_images"):
                images = await parser.extract_raw_images(file_path)
    except Exception as exc:
        logger.debug(
            "multimodal.extract_images_failed",
            file_path=file_path,
            error=str(exc)[:200],
        )

    return images


# ------------------------------------------------------------------
# Celery 任务注册
# ------------------------------------------------------------------

try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.multimodal_tasks.process_document_images")
    def process_document_images(doc_id: str) -> dict:
        """文档入库后，处理文档中的所有图片。

        在 document_tasks.process_document 完成后链式调用。
        VLM 不可用时优雅降级，跳过图片处理。
        """
        logger.info("multimodal.task_started", doc_id=doc_id)
        try:
            result = _run_async(_process_document_images(doc_id))
            logger.info("multimodal.task_done", doc_id=doc_id, result=result)
            return result
        except Exception as exc:
            logger.error("multimodal.task_failed", doc_id=doc_id, error=str(exc))
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)}

except ImportError:
    logger.warning("multimodal.celery_not_available")
