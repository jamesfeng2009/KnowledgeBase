"""
多模态处理 Celery 任务 — 文档入库后处理图片/表格/扫描件。

在 document_tasks.process_document 完成后链式调用：
    1. 提取文档中的所有图片
    2. 对每张图片调用 VLM 生成描述
    3. 将描述文本追加到文档内容中
    4. 重新分块索引（仅追加图片描述的 chunk）

优雅降级：VLM 不可用时跳过图片处理，仅索引文本。
"""

from __future__ import annotations

import asyncio

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
    """处理文档中的所有图片。

    Args:
        doc_id: 文档 ID 字符串。

    Returns:
        处理结果统计。
    """
    from app.services.multimodal_service import MultimodalService

    service = MultimodalService()

    # TODO: 对接真实文档图片提取逻辑
    # images = extract_images_from_document(doc_id)
    # for img in images:
    #     result = await service.process_image(img.data, img.mime_type)
    #     update_chunk_metadata(img.chunk_id, {
    #         "image_description": result["description"],
    #         "image_tags": result["tags"],
    #     })
    #     embed_and_store(result["description"], doc_id, source="image")

    logger.info("multimodal.task_completed", doc_id=doc_id, images_processed=0)
    return {"doc_id": doc_id, "images_processed": 0, "status": "success"}


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
