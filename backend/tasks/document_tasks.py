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

# VLM 并发控制 — 关键帧描述并发上限，防止打满 VLM 服务
_VLM_SEMAPHORE_LIMIT: int = 3

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

    # P1: 收集解析过程中的警告信息（用于摘要响应）
    warnings: list[str] = []

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        # 1. 解析文档内容
        parse_failed = False
        try:
            parsed_text = await _parse_document(doc)
        except Exception as exc:
            logger.warning("document.parse_failed", doc_id=doc_id, error=str(exc))
            parsed_text = doc.content_text or ""
            parse_failed = True
            warnings.append(f"解析异常: {str(exc)[:200]}")

        # 检测旧格式降级提示
        if doc.doc_type in ("doc", "ppt"):
            warnings.append(
                f"旧格式 .{doc.doc_type} 不支持解析，请转换为 .{doc.doc_type}x 后重新上传"
            )

        # 2. 更新纯文本内容（检索用）
        if parsed_text and parsed_text != doc.content_text:
            doc.content_text = parsed_text
            await session.flush()

        # 3. 分块 — 视频/音频文档走专用分块，其他走 SemanticChunker 四级策略
        doc_type = doc.doc_type or "md"
        if doc_type in _VIDEO_TYPES or doc_type in _AUDIO_TYPES:
            # 视频/音频文档：ASR 转写片段 + 关键帧 VLM 描述 → 语义分块
            chunk_objects = await _chunk_video_document(doc, parsed_text)
        else:
            chunk_objects = _chunk_document(parsed_text, doc_type)
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
            warnings.append(f"向量化失败（已降级为空向量）: {str(exc)[:200]}")

        # 5. 索引（延迟导入，构建全文索引和向量索引）
        try:
            await _build_indexes(doc_id, chunk_objects, chunks, embeddings)
        except Exception as exc:
            logger.warning("document.index_failed", doc_id=doc_id, error=str(exc))
            warnings.append(f"索引构建失败: {str(exc)[:200]}")

        # 6. 根据密级决定发布路径：
        #    confidential/secret → 待审核（审核通过后发布）
        #    public/internal → 直接发布
        classification = doc.classification or "internal"
        needs_review = classification in _REQUIRES_REVIEW

        if needs_review:
            doc.status = "pending_review"
            await session.commit()

            # 提交审核流程 — 审核通过后触发文档发布
            try:
                await _submit_for_audit(doc_id, doc.owner_id)
                logger.info("document.audit_submitted", doc_id=doc_id)
            except Exception as exc:
                logger.warning("document.audit_submit_failed", doc_id=doc_id, error=str(exc))
                warnings.append(f"审核流程提交失败: {str(exc)[:200]}")
        else:
            doc.status = "published"
            await session.commit()

        # 7. 链式触发文档智能处理（摘要/标签/分类/行动项）
        try:
            from tasks.intelligence_tasks import process_intelligence
            process_intelligence.delay(doc_id)
            logger.info("document.intelligence_triggered", doc_id=doc_id)
        except Exception as exc:
            logger.warning("document.intelligence_trigger_failed", doc_id=doc_id, error=str(exc))

        final_status = "pending_review" if needs_review else "published"

        # P1: 推断解析状态 — 有内容且无致命错误为 parsed，有警告为 partial
        if parse_failed and not parsed_text.strip():
            parse_status = "failed"
        elif warnings:
            parse_status = "partial"
        else:
            parse_status = "parsed"

        return {
            "doc_id": doc_id,
            "status": "success",
            "chunk_count": len(chunks),
            "embedding_count": len(embeddings),
            "doc_status": final_status,
            "chunk_strategies": list(set(c.chunk_strategy for c in chunk_objects)),
            # P1: 摘要响应字段
            "warnings": warnings,
            "parse_status": parse_status,
            "char_count": len(parsed_text),
        }


async def _parse_document(doc: Any) -> str:
    """解析文档内容 — 优先 Docling 统一解析，降级到原有专用解析器。

    解析优先级：
        1. Docling 统一解析器（PDF/DOCX/PPTX/XLSX/HTML/图片/音频）
        2. 原有专用解析器（pymupdf/python-docx/python-pptx/openpyxl）
        3. 旧格式兜底提示（.doc/.ppt）
        4. 视频专用管线（ffmpeg→ASR + 关键帧 VLM，Docling 不支持视频）

    Args:
        doc: Document ORM 实例。

    Returns:
        解析后的纯文本内容。
    """
    # 如果已有纯文本内容，直接返回
    if doc.content_text:
        return doc.content_text

    doc_type = doc.doc_type or "md"

    # 视频不走 Docling（Docling 不支持视频），直接走专用管线
    if doc_type in _VIDEO_TYPES:
        return await _parse_video(doc)

    # .doc/.ppt 旧格式 — Docling 也无法解析，返回降级提示
    if doc_type in ("doc", "ppt"):
        return _legacy_format_fallback(doc, doc_type)

    # 1. 优先尝试 Docling 统一解析
    docling_result = await _try_docling_parse(doc, doc_type)
    if docling_result is not None:
        return docling_result

    # 2. 降级到原有专用解析器
    if doc_type == "pdf":
        return await _parse_pdf(doc)
    elif doc_type == "docx":
        return await _parse_docx(doc)
    elif doc_type in ("xlsx", "xls"):
        return await _parse_xlsx(doc)
    elif doc_type == "html":
        return _parse_html(doc)
    elif doc_type == "pptx":
        return await _parse_pptx(doc)
    elif doc_type in _AUDIO_TYPES:
        return await _parse_audio(doc)
    else:
        # Markdown 或纯文本，直接返回
        return doc.content_html or doc.content_text or ""


async def _try_docling_parse(doc: Any, doc_type: str) -> str | None:
    """尝试用 Docling 解析文档 — 成功返回 Markdown，不适用返回 None。

    Docling 可用且支持该类型时，调用 DoclingParser.parse()。
    解析结果非空则返回；解析失败或 Docling 不可用则返回 None，
    由调用方降级到原有解析器。

    Args:
        doc: Document ORM 实例。
        doc_type: 文档类型。

    Returns:
        Markdown 文本（成功）或 None（需降级）。
    """
    if not doc.file_path:
        return None

    try:
        from app.document.factory import get_parser_with_fallback

        parser, parser_type = get_parser_with_fallback(doc_type)
        if parser is None or parser_type != "docling":
            return None

        result = await parser.parse(doc.file_path)
        if result and result.strip():
            logger.info(
                "document.docling_parsed",
                doc_id=str(getattr(doc, "id", "")),
                doc_type=doc_type,
                content_len=len(result),
            )
            return result

        # Docling 返回空 — 降级
        logger.info("document.docling_empty_fallback", doc_type=doc_type)
        return None

    except Exception as exc:
        logger.warning("document.docling_failed_fallback", doc_type=doc_type, error=str(exc))
        return None


# 旧格式兜底提示文本
_LEGACY_FORMAT_HINTS: dict[str, str] = {
    "doc": (
        "[格式提示] 此文件为 .doc 旧格式（Word 97-2003），"
        "当前解析器仅支持 .docx（OOXML）格式。"
        "请将文件另存为 .docx 格式后重新上传。"
    ),
    "ppt": (
        "[格式提示] 此文件为 .ppt 旧格式（PowerPoint 97-2003），"
        "当前解析器仅支持 .pptx（OOXML）格式。"
        "请将文件另存为 .pptx 格式后重新上传。"
    ),
}


def _legacy_format_fallback(doc: Any, fmt: str) -> str:
    """旧格式兜底 — 返回提示文本而非空字符串。

    python-docx / python-pptx 只支持 OOXML 格式，
    .doc / .ppt 旧二进制格式无法解析。

    Args:
        doc: Document ORM 实例。
        fmt: 旧格式标识（"doc" 或 "ppt"）。

    Returns:
        降级提示文本。如果 doc 已有 content_text 则优先返回。
    """
    hint = _LEGACY_FORMAT_HINTS.get(fmt, "")
    logger.warning(
        "document.legacy_format",
        fmt=fmt,
        doc_id=str(getattr(doc, "id", "")),
        file_path=getattr(doc, "file_path", ""),
    )

    # 如果已有 content_text（可能由前端预提取），拼接到提示后
    existing = doc.content_text or ""
    if existing and existing.strip():
        return f"{existing}\n\n{hint}"
    return hint


async def _parse_pdf(doc: Any) -> str:
    """解析 PDF 文档 — 优先使用增强解析器（表格 + 图片 VLM），降级为纯文本。

    增强解析器（app/document/pdf_parser.py）支持：
    - 表格提取为 HTML <table> 标签（pymupdf find_tables）；
    - 内嵌图片 VLM 描述（get_images → VLM.understand）。

    pymupdf 未安装或增强解析器不可用时优雅降级为纯文本提取。
    """
    if not doc.file_path:
        return ""

    # 优先尝试增强解析器
    try:
        from app.document import get_parser

        parser = get_parser("pdf")
        if parser is not None:
            result = await parser.parse(doc.file_path)
            if result and result.strip():
                return result
            # 增强解析器返回空，降级到纯文本
    except Exception as exc:
        logger.warning("pdf.enhanced_parse_failed", error=str(exc))

    # 降级：纯文本提取
    try:
        import fitz  # pymupdf

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


async def _parse_pptx(doc: Any) -> str:
    """解析 PPTX 文档 — 使用增强解析器（文本 + 表格 + 图片 VLM）。

    每个幻灯片输出为 <h2>标题</h2> + 内容，chunker 按 slide 分块。
    python-pptx 未安装时优雅降级为返回 content_text。
    """
    if not doc.file_path:
        return ""

    try:
        from app.document import get_parser

        parser = get_parser("pptx")
        if parser is not None:
            result = await parser.parse(doc.file_path)
            if result and result.strip():
                return result
    except Exception as exc:
        logger.warning("pptx.parse_failed", error=str(exc))

    # 降级
    return doc.content_text or ""


async def _parse_docx(doc: Any) -> str:
    """解析 DOCX 文档 — 优先使用增强解析器（表格 + 图片 VLM），降级为纯文本。

    增强解析器（app/document/docx_parser.py）支持：
    - 表格提取为 HTML <table> 标签；
    - 内嵌图片 VLM 描述。

    python-docx 未安装或增强解析器不可用时优雅降级为纯文本提取。
    """
    if not doc.file_path:
        return ""

    # 优先尝试增强解析器
    try:
        from app.document import get_parser

        parser = get_parser("docx")
        if parser is not None:
            result = await parser.parse(doc.file_path)
            if result and result.strip():
                return result
    except Exception as exc:
        logger.warning("docx.enhanced_parse_failed", error=str(exc))

    # 降级：纯文本提取
    try:
        from docx import Document as DocxDocument

        docx_doc = DocxDocument(doc.file_path)
        paragraphs = [p.text for p in docx_doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        logger.warning("docx.parse_skipped", reason="python-docx not installed")
        return doc.content_text or ""
    except Exception as exc:
        logger.warning("docx.parse_error", error=str(exc))
        return doc.content_text or ""


async def _parse_xlsx(doc: Any) -> str:
    """解析 XLSX 文档 — 使用增强解析器（每 sheet 转 HTML 表格）。

    增强解析器（app/document/xlsx_parser.py）支持：
    - 每个 sheet 输出为 <h2>sheet 名</h2> 标题 + HTML <table>；
    - 第一行视为表头，其余为数据行；
    - 配置控制行数/sheet 数量上限。

    openpyxl 未安装或增强解析器不可用时优雅降级为 content_text。
    """
    if not doc.file_path:
        return ""

    # 优先尝试增强解析器
    try:
        from app.document import get_parser

        parser = get_parser("xlsx")
        if parser is not None:
            result = await parser.parse(doc.file_path)
            if result and result.strip():
                return result
    except Exception as exc:
        logger.warning("xlsx.enhanced_parse_failed", error=str(exc))

    # 降级：返回 content_text
    logger.warning("xlsx.parse_skipped", reason="openpyxl not installed or parse failed")
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


# ------------------------------------------------------------------
# 视频处理 — ASR 转写 + 关键帧 VLM 描述
# ------------------------------------------------------------------

# 视频文件后缀集合
_VIDEO_TYPES: set[str] = {"video", "mp4", "avi", "mov", "mkv"}

# 独立音频文件后缀集合 — 通过 ASR 转写为文本，复用视频分块管线
_AUDIO_TYPES: set[str] = {"audio", "mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}


async def _parse_video(doc: Any) -> str:
    """解析视频文档 — 提取音轨 → ASR 转写 → 返回纯文本。

    视频处理流水线（P0 + P1）：
        1. ffmpeg 提取音轨为 16kHz mono WAV；
        2. ASR 引擎转写为带时间戳的文本段；
        3. （P1）ffmpeg 抽取关键帧 → VLM 生成画面描述；
        4. 合并转写文本为纯文本返回（供 content_text 存储）。

    视频分块（chunk_video_transcript）在 _process_document_async 中
    根据转写片段单独处理，不走普通 _chunk_document。

    延迟导入所有外部依赖，优雅降级。

    Args:
        doc: Document ORM 实例（file_path 指向视频文件）。

    Returns:
        ASR 转写的纯文本（各段拼接），失败时返回空字符串。
    """
    if not doc.file_path:
        logger.warning("video.no_file_path", doc_id=str(getattr(doc, "id", "")))
        return doc.content_text or ""

    try:
        from app.video import get_video_processor
        from app.asr import get_asr_provider

        # 1. 提取音轨
        processor = get_video_processor()
        wav_path = await processor.extract_audio(doc.file_path)
        if not wav_path:
            logger.warning("video.audio_extract_failed", file_path=doc.file_path)
            return doc.content_text or ""

        # 2. ASR 转写
        try:
            asr = get_asr_provider()
            segments = await asr.transcribe(wav_path, language="zh")
        except Exception as exc:
            logger.warning("video.asr_failed", error=str(exc))
            segments = []
        finally:
            # 清理临时音频文件
            import os
            if os.path.exists(wav_path):
                os.remove(wav_path)

        if not segments:
            logger.warning("video.no_transcript", doc_id=str(getattr(doc, "id", "")))
            return doc.content_text or ""

        # 3. 合并转写文本
        text_parts = [seg.text for seg in segments if seg.text.strip()]
        transcript_text = "\n".join(text_parts)

        logger.info(
            "video.transcribed",
            segments=len(segments),
            text_len=len(transcript_text),
        )
        return transcript_text

    except ImportError:
        logger.warning("video.deps_not_installed")
        return doc.content_text or ""
    except Exception as exc:
        logger.warning("video.parse_error", error=str(exc))
        return doc.content_text or ""


async def _parse_audio(doc: Any) -> str:
    """解析独立音频文档 — 转换为 WAV → ASR 转写 → 返回纯文本。

    音频处理流程：
        1. ffmpeg 将音频转换为 16kHz mono WAV（ASR 标准输入格式）；
        2. ASR 引擎转写为带时间戳的文本段；
        3. 合并转写文本返回（供 content_text 存储）。

    音频分块复用 _chunk_video_document（按时间窗口合并 ASR 片段），
    无关键帧提取步骤（音频无视频流，关键帧提取自动跳过）。

    延迟导入所有外部依赖，优雅降级。

    Args:
        doc: Document ORM 实例（file_path 指向音频文件）。

    Returns:
        ASR 转写的纯文本，失败时返回 content_text 或空字符串。
    """
    if not doc.file_path:
        logger.warning("audio.no_file_path", doc_id=str(getattr(doc, "id", "")))
        return doc.content_text or ""

    # 检查配置开关
    from app.config import get_settings

    settings = get_settings()
    if not getattr(settings, "AUDIO_ASR_ENABLED", True):
        logger.info("audio.asr_disabled_by_config")
        return doc.content_text or ""

    try:
        from app.video import get_video_processor
        from app.asr import get_asr_provider

        # 1. 转换为 WAV（VideoProcessor.extract_audio 对音频输入同样适用）
        processor = get_video_processor()
        wav_path = await processor.extract_audio(doc.file_path)
        if not wav_path:
            logger.warning("audio.convert_failed", file_path=doc.file_path)
            return doc.content_text or ""

        # 2. ASR 转写
        try:
            asr = get_asr_provider()
            segments = await asr.transcribe(wav_path, language="zh")
        except Exception as exc:
            logger.warning("audio.asr_failed", error=str(exc))
            segments = []
        finally:
            # 清理临时 WAV 文件
            import os
            if os.path.exists(wav_path):
                os.remove(wav_path)

        if not segments:
            logger.warning("audio.no_transcript", doc_id=str(getattr(doc, "id", "")))
            return doc.content_text or ""

        # 3. 合并转写文本
        text_parts = [seg.text for seg in segments if seg.text.strip()]
        transcript_text = "\n".join(text_parts)

        logger.info(
            "audio.transcribed",
            segments=len(segments),
            text_len=len(transcript_text),
        )
        return transcript_text

    except ImportError:
        logger.warning("audio.deps_not_installed")
        return doc.content_text or ""
    except Exception as exc:
        logger.warning("audio.parse_error", error=str(exc))
        return doc.content_text or ""


async def _extract_keyframe_descriptions(
    video_path: str,
) -> list[dict[str, Any]]:
    """提取关键帧并使用 VLM 生成画面描述 — P1。

    流程：ffmpeg 抽帧 → VLM.understand() 逐帧描述 → 返回描述列表。

    Args:
        video_path: 视频文件路径。

    Returns:
        关键帧描述列表，每项格式::

            {"timestamp": 30.0, "description": "幻灯片显示三层架构图"}

        失败时返回空列表。
    """
    if not video_path:
        return []

    try:
        from app.video import get_video_processor

        processor = get_video_processor()
        keyframes = await processor.extract_keyframes(video_path)
        if not keyframes:
            return []

        # 使用 VLM 并发描述关键帧 — Semaphore(3) 防止打满 VLM 服务
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            semaphore = asyncio.Semaphore(_VLM_SEMAPHORE_LIMIT)

            async def describe_keyframe(kf: Any) -> dict[str, Any] | None:
                """并发描述单个关键帧 — 异常时返回 None，不中断整体。"""
                try:
                    import os
                    if not os.path.exists(kf.image_path):
                        logger.warning(
                            "video.keyframe_file_missing",
                            image_path=kf.image_path,
                        )
                        return None

                    with open(kf.image_path, "rb") as f:
                        img_bytes = f.read()

                    async with semaphore:
                        desc = await vlm.understand(
                            image=img_bytes,
                            prompt="请用一句话描述这个视频帧的画面内容，重点关注图表、文字和关键信息。",
                            mime_type="image/png",
                        )
                    if desc and desc.strip():
                        return {
                            "timestamp": kf.timestamp,
                            "description": desc.strip(),
                        }
                    return None
                except Exception as exc:
                    logger.warning(
                        "video.keyframe_vlm_failed",
                        timestamp=kf.timestamp,
                        error=str(exc),
                    )
                    return None

            tasks = [describe_keyframe(kf) for kf in keyframes]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            descriptions: list[dict[str, Any]] = []
            for result in results:
                if isinstance(result, dict):
                    descriptions.append(result)
                elif isinstance(result, Exception):
                    logger.warning("video.keyframe_gather_error", error=str(result))

            logger.info("video.keyframes_described", count=len(descriptions))
            return descriptions

        except ImportError:
            logger.warning("video.vlm_not_available")
            return []

    except Exception as exc:
        logger.warning("video.keyframe_extract_error", error=str(exc))
        return []


async def _chunk_video_document(doc: Any, transcript_text: str) -> list[Chunk]:
    """视频/音频文档专用分块 — ASR 转写片段 + 关键帧 VLM 描述 → 语义分块。

    流程：
        1. 重新执行 ASR 转写获取带时间戳的片段（_parse_video/_parse_audio
           只返回纯文本，此处需要片段级时间戳用于分块）；
        2. （仅视频）提取关键帧 VLM 描述；音频文件无视频流，自动跳过；
        3. 调用 SemanticChunker.chunk_video_transcript() 分块。

    如果 ASR 不可用，降级为普通文本分块。

    Args:
        doc: Document ORM 实例。
        transcript_text: _parse_video 返回的转写纯文本。

    Returns:
        Chunk 对象列表。
    """
    if not transcript_text or not transcript_text.strip():
        return []

    # 尝试获取 ASR 片段（需要时间戳）
    segments: list[dict[str, Any]] = []
    try:
        from app.video import get_video_processor
        from app.asr import get_asr_provider

        if doc.file_path:
            processor = get_video_processor()
            wav_path = await processor.extract_audio(doc.file_path)
            if wav_path:
                try:
                    asr = get_asr_provider()
                    segs = await asr.transcribe(wav_path, language="zh")
                    segments = [s.to_dict() for s in segs]
                finally:
                    import os
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
    except Exception as exc:
        logger.warning("video.chunk_asr_failed", error=str(exc))

    # 如果拿不到 ASR 片段，降级为普通文本分块
    if not segments:
        logger.info("video.chunk_fallback_to_text", reason="no ASR segments")
        return _chunk_document(transcript_text, "txt")

    # P1: 提取关键帧 VLM 描述
    keyframe_descs: list[dict[str, Any]] = []
    try:
        if doc.file_path:
            keyframe_descs = await _extract_keyframe_descriptions(doc.file_path)
    except Exception as exc:
        logger.warning("video.keyframe_desc_failed", error=str(exc))

    # 视频语义分块
    chunker = SemanticChunker()
    chunks = chunker.chunk_video_transcript(segments, keyframe_descs)

    logger.info(
        "video.chunked",
        segments=len(segments),
        chunks=len(chunks),
        keyframes=len(keyframe_descs),
    )
    return chunks


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

    注意：视频文档的分块在 _process_video_pipeline 中通过
    chunker.chunk_video_transcript() 处理，不走本函数。

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


# ------------------------------------------------------------------
# 审核流程串联
# ------------------------------------------------------------------

# 需要人工审核的密级 — confidential 和 secret 必须审核，
# public 和 internal 可直接发布。
_REQUIRES_REVIEW: set[str] = {"confidential", "secret"}


async def _submit_for_audit(doc_id: str, owner_id: Any) -> None:
    """提交文档审核 — 创建 AuditFlow 记录。

    此函数在文档处理完成后调用，将文档纳入审核流程。
    审核通过后由 AuditService.approve 触发 _publish_document。

    Args:
        doc_id: 文档 ID（UUID 字符串）。
        owner_id: 文档所有者 ID（用于填充 submitter_id）。
    """
    from app.database import async_session_factory
    from app.models.audit import AuditFlow
    from app.repositories.base import BaseRepository

    async with async_session_factory() as session:
        repo = BaseRepository(AuditFlow, session)
        await repo.create(
            resource_type="document",
            resource_id=uuid.UUID(doc_id),
            submitter_id=owner_id,
            priority="normal",
        )
        await session.commit()


async def _publish_document(doc_id: str) -> None:
    """发布文档 — 审核通过后调用，将状态设为 published。

    此函数由 AuditService.approve 触发，完成审核通过后的发布动作：
    1. 更新文档状态为 published；

    Args:
        doc_id: 文档 ID（UUID 字符串）。
    """
    from app.database import async_session_factory
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            logger.warning("document.publish_not_found", doc_id=doc_id)
            return

        doc.status = "published"
        await session.commit()
        logger.info("document.published", doc_id=doc_id)


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

    # 构建向量索引（通过 VectorStoreBase 适配器，默认 OpenSearch k-NN，可选 Milvus）
    try:
        await _build_vector_index(doc_id, chunk_objects, embeddings)
    except Exception as exc:
        logger.warning("vector.index_failed", doc_id=doc_id, error=str(exc))


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


async def _build_vector_index(
    doc_id: str,
    chunk_objects: list[Chunk],
    embeddings: list[list[float]],
) -> int:
    """构建向量索引 — 通过 VectorStoreBase 适配器写入向量数据。

    向量后端由 VECTOR_STORE 配置决定：
        - os_knn（默认）：OpenSearch k-NN 引擎
        - milvus：Milvus 向量引擎

    适配器内部处理服务不可用的优雅降级（返回 0）。

    Args:
        doc_id: 文档 ID。
        chunk_objects: Chunk 对象列表（含元数据）。
        embeddings: 向量嵌入列表。

    Returns:
        成功写入的向量数量。
    """
    if not embeddings:
        logger.info("vector.index_skipped", doc_id=doc_id, reason="no embeddings")
        return 0

    try:
        from app.rag.vector_store import get_vector_store

        store = get_vector_store()
        count = await store.upsert(doc_id, chunk_objects, embeddings)
        logger.info(
            "vector.indexed",
            doc_id=doc_id,
            vector_count=count,
            store_type=type(store).__name__,
        )
        return count
    except Exception as exc:
        logger.warning("vector.index_error", doc_id=doc_id, error=str(exc))
        raise


async def _build_milvus_index(
    doc_id: str,
    chunk_objects: list[Chunk],
    embeddings: list[list[float]],
) -> None:
    """[Deprecated] 构建 Milvus 向量索引 — 向后兼容包装器。

    新代码应使用 _build_vector_index() 通过 VectorStoreBase 适配器写入，
    自动根据 VECTOR_STORE 配置选择后端。
    """
    await _build_vector_index(doc_id, chunk_objects, embeddings)
