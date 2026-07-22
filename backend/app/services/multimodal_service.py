"""
多模态知识处理服务 — 单一职责：利用 VLM 处理文档中的图片/表格/扫描件/白板。

四项多模态能力：
    1. 图片智能解析 — VLM 生成图片描述 → 一并索引
    2. 表格结构化 — VLM 识别行列结构 → JSON
    3. 扫描件 OCR — VLM 识别文字 → 纯文本
    4. 白板拍照入库 — VLM 理解内容 → 会议纪要

优雅降级：VLM 不可用时返回占位文本，不阻塞文档入库流程。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.utils.logger import get_logger
from app.vlm.provider import VisionProvider

logger = get_logger(__name__)


class MultimodalService:
    """多模态知识处理 — 图片/表格/扫描件/白板。

    复用 VLM Provider 抽象层，SaaS 和私有部署统一接口。
    """

    def __init__(
        self, vlm: VisionProvider | None = None, tenant_id: UUID | None = None
    ) -> None:
        self._vlm = vlm
        self._tenant_id = tenant_id

    @property
    def vlm(self) -> VisionProvider:
        """懒加载 VLM Provider。"""
        if self._vlm is None:
            from app.vlm.provider import get_vision_provider
            self._vlm = get_vision_provider()
        return self._vlm

    # ------------------------------------------------------------------
    # 四项多模态能力
    # ------------------------------------------------------------------

    async def process_image(
        self,
        image: bytes,
        mime_type: str = "image/png",
    ) -> dict[str, Any]:
        """图片智能解析 — 生成描述文本用于索引。

        将图片描述文本与图片所在段落一起向量化，
        使搜索能命中图片内容。

        Args:
            image: 图片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            {"description": "...", "tags": ["架构图", "微服务"]}
        """
        prompt = (
            "请描述这张图片的内容，并提取 3-5 个关键词标签。"
            "格式：描述文本\\n标签1,标签2,标签3"
        )
        result = await self._safe_understand(image, prompt, mime_type)
        parts = result.split("\n", 1)
        description = parts[0].strip() if parts else result
        tags: list[str] = []
        if len(parts) > 1:
            tags = [t.strip() for t in parts[1].split(",") if t.strip()]

        logger.info(
            "multimodal.image_processed",
            description_len=len(description),
            tags_count=len(tags),
        )
        return {"description": description, "tags": tags}

    async def process_table(
        self,
        image: bytes,
        mime_type: str = "image/png",
    ) -> list[dict[str, Any]]:
        """表格结构化 — VLM 识别表格行列结构，转为 JSON。

        用于政策费率表、产品参数表等结构化数据检索。

        Args:
            image: 表格图片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            [{"column": "职级", "value": "P7"}, ...]
        """
        prompt = (
            "请识别表格内容，转为 JSON 数组格式。"
            "每行一个对象，列名为 key。只输出 JSON，不要额外解释。"
        )
        result = await self._safe_understand(image, prompt, mime_type)
        parsed = self._parse_json(result, default=[])
        if isinstance(parsed, list):
            return parsed
        return []

    async def process_scanned_pdf(
        self,
        image: bytes,
        mime_type: str = "image/png",
    ) -> str:
        """扫描件 OCR — VLM 识别扫描件文字。

        用于合同、发票等扫描件入库。
        返回纯文本，与文档正文一并分块索引。

        Args:
            image: 扫描件图片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            识别到的纯文本。
        """
        prompt = "请识别图片中的所有文字，按原文输出，不要额外解释。"
        result = await self._safe_understand(image, prompt, mime_type)
        logger.info(
            "multimodal.scanned_pdf_processed",
            text_length=len(result),
        )
        return result

    async def process_whiteboard(
        self,
        image: bytes,
        mime_type: str = "image/png",
    ) -> dict[str, Any]:
        """白板拍照入库 — VLM 理解白板内容，生成会议纪要。

        用于会议场景。

        Args:
            image: 白板照片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            {"summary": "...", "action_items": [...], "key_points": [...]}
        """
        prompt = """请分析白板照片内容，生成会议纪要。格式为 JSON：
{"summary": "会议摘要", "key_points": ["要点1", "要点2"],
 "action_items": [{"assignee": "负责人", "content": "行动内容"}]}
只输出 JSON。"""
        result = await self._safe_understand(image, prompt, mime_type)
        parsed = self._parse_json(result, default={})
        default = {
            "summary": "",
            "key_points": [],
            "action_items": [],
        }
        if isinstance(parsed, dict):
            default.update(parsed)
        return default

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _safe_understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str,
    ) -> str:
        """安全调用 VLM — 异常时返回占位文本，不阻塞流程。"""
        try:
            return await self.vlm.understand(image, prompt, mime_type)
        except Exception as exc:
            logger.warning(
                "multimodal.vlm_failed",
                prompt_len=len(prompt),
                error=str(exc),
            )
            return f"[图像处理失败: {exc}]"

    @staticmethod
    def _parse_json(text: str, default: Any = None) -> Any:
        """安全解析 JSON — 解析失败时返回默认值。

        Args:
            text: 可能包含 JSON 的文本。
            default: 解析失败时的默认返回值。

        Returns:
            解析后的对象或默认值。
        """
        if not text or not text.strip():
            return default
        text = text.strip()
        # 尝试提取 JSON 块（LLM 可能包裹在 markdown 中）
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default
