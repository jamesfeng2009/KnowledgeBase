"""
跨模态检索服务 — 单一职责：将图片向量化并入库，支持文本查询检索图片。

P2-Step3: 基于 jina-clip-v2 跨模态嵌入模型，将文档中的图片直接向量化，
与文本向量共享同一向量空间。用户可用文本查询检索到图片内容，
无需仅依赖 VLM 生成的文本描述。

工作流：
    1. 文档解析阶段提取图片二进制数据 + VLM 描述
    2. CrossModalService.embed_and_store_images() 将图片向量化
    3. 图片向量 + VLM 描述作为 content 存入向量库（content_type="image"）
    4. 检索器使用 MultimodalEmbedder 生成查询向量，
       自然命中图片向量（同一嵌入空间）

遵循单一职责：本模块只负责跨模态图片向量化与入库，
不涉及文档解析（document_tasks）和检索（retriever）。
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.rag.chunker import Chunk
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()


class CrossModalService:
    """跨模态检索服务 — 图片向量化 + 入库。

    使用方式::

        service = CrossModalService()
        count = await service.embed_and_store_images(
            doc_id="...",
            kb_id="...",
            images=[(img_bytes, "图表描述"), ...],
        )
    """

    def __init__(self) -> None:
        from app.rag.vector_store import get_vector_store

        self._vector_store = get_vector_store()
        self._mm_embedder: Any | None = None

    def _get_mm_embedder(self) -> Any | None:
        """懒初始化跨模态 Embedder。"""
        if self._mm_embedder is not None:
            return self._mm_embedder
        try:
            from app.llm.multimodal_embedder import get_multimodal_embedder

            self._mm_embedder = get_multimodal_embedder()
        except Exception as exc:
            log.warning("cross_modal.embedder_unavailable", error=str(exc))
        return self._mm_embedder

    async def embed_and_store_images(
        self,
        doc_id: str,
        kb_id: str | None,
        images: list[tuple[bytes, str]],
    ) -> int:
        """将图片批量向量化并写入向量库。

        Args:
            doc_id: 文档 ID。
            kb_id: 所属知识库 ID。
            images: 元组列表 [(图片二进制数据, VLM描述文本), ...]。

        Returns:
            成功写入的图片向量数量。
        """
        if not images:
            return 0

        embedder = self._get_mm_embedder()
        if embedder is None:
            log.info("cross_modal.skipped", reason="embedder not available")
            return 0

        # 提取图片二进制数据
        image_bytes_list = [img[0] for img in images]
        descriptions = [img[1] for img in images]

        # 批量向量化图片
        try:
            embeddings = await embedder.embed_images(image_bytes_list)
        except Exception as exc:
            log.warning("cross_modal.embed_failed", doc_id=doc_id, error=str(exc))
            return 0

        if not embeddings:
            return 0

        # 创建图片 Chunk 对象 — content 为 VLM 描述，content_type 标记为 image
        import uuid as _uuid

        chunks: list[Chunk] = []
        for i, desc in enumerate(descriptions):
            chunk = Chunk(
                id=str(_uuid.uuid4()),
                doc_id=doc_id,
                content=desc or "[图片内容]",
                title_path="[跨模态图片]",
                content_type="image",
                chunk_strategy="cross_modal",
                token_count=len(desc) // 2 if desc else 0,
            )
            chunks.append(chunk)

        # 写入向量库 — 与文本向量在同一索引
        count = await self._vector_store.upsert(doc_id, chunks, embeddings, kb_id=kb_id)
        log.info(
            "cross_modal.stored",
            doc_id=doc_id,
            image_count=count,
            kb_id=kb_id,
        )
        return count

    def is_enabled(self) -> bool:
        """跨模态检索是否启用。"""
        return bool(settings.CROSS_MODAL_ENABLED and settings.JINA_API_KEY)
