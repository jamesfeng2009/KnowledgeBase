"""GB 级视频处理任务 — Celery Chord 编排（P2-D）。

突破单任务 30 分钟超时限制：将 GB 视频处理拆分为多个子任务，
每个子任务 ≤ 15 分钟，通过 Chord 编排并行执行 + 回调合并。

任务拓扑::

    process_video_multipart (入口, 串行)
        │
        ├── extract_audio  (5-10 分钟)
        │
        ▼
    chord(group(asr_multipart_task, keyframe_task))  (并行)
        │
        ▼
    finalize_video_task  (callback, 合并+分块+向量化+索引)

每个子任务独立进度反馈到 Redis（sub_stage/sub_current/sub_total）。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from celery_app import celery_app
from app.utils.logger import get_logger
from app.utils.retry import make_celery_retry_kwargs

log = get_logger(__name__)

# ASR 分段时长（秒）— 8 分钟 WAV ~24MB < Whisper 25MB 限制
_ASR_SEGMENT_DURATION = 480


# ======================================================================
# 入口任务 — 提取音轨后触发并行 ASR + 关键帧
# ======================================================================


@celery_app.task(name="tasks.video_tasks.process_video_multipart")
def process_video_multipart(doc_id: str) -> None:
    """GB 视频处理入口 — 提取音轨 → 触发并行 ASR + 关键帧 → finalize。

    替代 document_tasks.process_document 对视频/音频文档的处理，
    每个 GB 视频走此专用管线，避免单任务超时。

    流程：
        1. 更新进度 queued
        2. 从 MinIO 下载视频到本地临时文件
        3. ffmpeg 提取音轨 WAV
        4. 用 Celery Chord 编排：并行 ASR + 关键帧 → finalize 回调
    """
    from tasks.document_tasks import _update_parse_progress

    _update_parse_progress(doc_id, "queued", message="GB 视频已入队，准备处理")

    try:
        from celery_app import celery_app
        from celery import chord, group
    except ImportError:
        log.warning("video_tasks.celery_not_installed")
        _process_video_fallback(doc_id)
        return

    # 1. 提取音轨（串行，5-10 分钟）
    wav_path = _extract_audio_stage(doc_id)
    if not wav_path:
        _update_parse_progress(doc_id, "failed", message="音轨提取失败")
        return

    # 2. Chord 编排：并行 ASR + 关键帧 → finalize
    try:
        chord(
            group(
                asr_multipart_task.s(doc_id, wav_path),
                keyframe_task.s(doc_id),
            )
        )(
            finalize_video_task.s(doc_id, wav_path)
        )
        log.info("video_multipart.chord_dispatched", doc_id=doc_id)
    except Exception:
        log.exception("video_multipart.chord_failed", doc_id=doc_id)
        # Chord 失败时降级为串行处理
        _process_video_fallback(doc_id, wav_path)


def _extract_audio_stage(doc_id: str) -> str | None:
    """提取音轨阶段 — 下载视频 + ffmpeg 提取 WAV。

    Args:
        doc_id: 文档 ID。

    Returns:
        WAV 文件路径，失败返回 None。
    """
    from tasks.document_tasks import _update_parse_progress

    _update_parse_progress(
        doc_id, "parsing",
        message="正在提取音轨...",
        sub_stage="extract_audio",
    )

    try:
        import tempfile

        from app.config import get_settings
        from app.utils.minio_client import download_file

        settings = get_settings()

        # 查询文档获取 file_path
        import asyncio

        async def _get_doc() -> Any:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 按主键查找文档，无需租户过滤（仅读取 file_path）
                repo = DocumentRepository(db)
                return await repo.get_by_id(uuid.UUID(doc_id))

        doc = asyncio.run(_get_doc())
        if not doc or not doc.file_path:
            log.warning("video_multipart.no_file_path", doc_id=doc_id)
            return None

        # 从 MinIO 下载视频到临时文件
        # file_path 格式: minio://bucket/object_name
        if doc.file_path.startswith("minio://"):
            parts = doc.file_path[8:].split("/", 1)
            bucket, object_name = parts[0], parts[1]
            video_data = asyncio.run(download_file(bucket, object_name))
        else:
            # 本地文件
            local_path = doc.file_path.replace("local://", "")
            with open(local_path, "rb") as f:
                video_data = f.read()

        # 保存到临时文件
        tmp_video = tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False, prefix="ekb_video_"
        )
        tmp_video.write(video_data)
        tmp_video.close()

        # ffmpeg 提取音轨
        from app.video import get_video_processor

        processor = get_video_processor()
        wav_path = asyncio.run(processor.extract_audio(tmp_video.name))

        # 清理临时视频文件
        try:
            os.remove(tmp_video.name)
        except OSError:
            pass

        if not wav_path:
            log.warning("video_multipart.audio_extract_failed", doc_id=doc_id)
            return None

        _update_parse_progress(
            doc_id, "parsing",
            message="音轨提取完成，准备 ASR 转写",
            sub_stage="extract_audio_done",
            sub_current=1, sub_total=1,
        )
        return wav_path

    except Exception:
        log.exception("video_multipart.extract_audio_error", doc_id=doc_id)
        return None


# ======================================================================
# ASR 分段任务 — 可并行执行
# ======================================================================


@celery_app.task(
    name="tasks.video_tasks.asr_multipart_task",
    **make_celery_retry_kwargs(),
)
def asr_multipart_task(doc_id: str, wav_path: str) -> dict[str, Any]:
    """分段 ASR 转写 — 逐段调用 Whisper，结果存 Redis（P2-D）。

    每段 8 分钟，单段 ASR 1-2 分钟。总时长取决于视频长度。
    进度通过 sub_stage/sub_current/sub_total 反馈。

    Args:
        doc_id: 文档 ID。
        wav_path: WAV 音频文件路径。

    Returns:
        {"doc_id": str, "segments": [...], "text": "..."}
    """
    from tasks.document_tasks import _update_parse_progress

    _update_parse_progress(
        doc_id, "parsing",
        message="ASR 分段转写中...",
        sub_stage="asr_multipart",
        sub_current=0, sub_total=1,
    )

    try:
        from app.asr import get_asr_provider

        asr = get_asr_provider()

        # 进度回调
        def _progress(seg_idx: int, total: int, segments_count: int) -> None:
            _update_parse_progress(
                doc_id, "parsing",
                message=f"ASR 转写 {seg_idx}/{total} 段",
                sub_stage=f"asr_segment_{seg_idx}",
                sub_current=seg_idx, sub_total=total,
            )

        # 调用分段 ASR
        import asyncio

        segments = asyncio.run(
            asr.transcribe_multipart(
                wav_path,
                language="zh",
                segment_duration=_ASR_SEGMENT_DURATION,
                progress_callback=_progress,
            )
        )

        # 序列化 segments
        seg_list = [s.to_dict() for s in segments]
        text = "\n".join(s.text for s in segments if s.text.strip())

        # 存入 Redis 供 finalize 合并
        try:
            import redis

            from app.config import get_settings

            settings = get_settings()
            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            client.setex(
                f"ekb:video_asr:{doc_id}",
                3600,
                json.dumps({"segments": seg_list, "text": text}, ensure_ascii=False),
            )
            client.close()
        except Exception:
            log.debug("video_multipart.asr_redis_failed", doc_id=doc_id)

        _update_parse_progress(
            doc_id, "parsing",
            message=f"ASR 转写完成，共 {len(segments)} 段",
            sub_stage="asr_done",
            sub_current=len(segments), sub_total=len(segments),
        )

        return {"doc_id": doc_id, "segments": seg_list, "text": text}

    except Exception as exc:
        log.exception("video_multipart.asr_failed", doc_id=doc_id)
        _update_parse_progress(
            doc_id, "parsing",
            message=f"ASR 转写失败: {exc}",
            sub_stage="asr_failed",
        )
        return {"doc_id": doc_id, "segments": [], "text": "", "error": str(exc)}


# ======================================================================
# 关键帧提取任务 — 可与 ASR 并行
# ======================================================================


@celery_app.task(name="tasks.video_tasks.keyframe_task")
def keyframe_task(doc_id: str) -> dict[str, Any]:
    """关键帧提取 + VLM 描述（P2-D）。

    与 ASR 并行执行。P2-C 优化后按时长采样，1GB 视频约 24 帧。
    每帧 VLM 描述完成后流式删除 PNG。

    Args:
        doc_id: 文档 ID。

    Returns:
        {"doc_id": str, "keyframes": [...]}
    """
    from tasks.document_tasks import _update_parse_progress

    _update_parse_progress(
        doc_id, "parsing",
        message="关键帧提取中...",
        sub_stage="keyframe_extract",
        sub_current=0, sub_total=1,
    )

    try:
        # 复用 document_tasks._extract_keyframe_descriptions
        from tasks.document_tasks import _extract_keyframe_descriptions

        import asyncio

        # 获取视频文件路径
        from app.models.knowledge import Document  # noqa: F401  # 保留供类型推断
        from sqlalchemy import select  # noqa: F401  # 保留供其他分支使用

        async def _get_doc() -> Any:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 按主键查找文档，无需租户过滤（仅读取 file_path）
                repo = DocumentRepository(db)
                return await repo.get_by_id(uuid.UUID(doc_id))

        doc = asyncio.run(_get_doc())
        if not doc or not doc.file_path:
            return {"doc_id": doc_id, "keyframes": []}

        # 下载视频到临时文件（关键帧提取需要本地文件）
        import tempfile

        from app.utils.minio_client import download_file

        if doc.file_path.startswith("minio://"):
            parts = doc.file_path[8:].split("/", 1)
            video_data = asyncio.run(download_file(parts[0], parts[1]))
            tmp_video = tempfile.NamedTemporaryFile(
                suffix=".mp4", delete=False, prefix="ekb_kf_"
            )
            tmp_video.write(video_data)
            tmp_video.close()
            video_path = tmp_video.name
        else:
            video_path = doc.file_path.replace("local://", "")

        try:
            descriptions = asyncio.run(
                _extract_keyframe_descriptions(video_path)
            )
        finally:
            # 清理临时视频文件
            if doc.file_path.startswith("minio://"):
                try:
                    os.remove(video_path)
                except OSError:
                    pass

        _update_parse_progress(
            doc_id, "parsing",
            message=f"关键帧提取完成，共 {len(descriptions)} 帧",
            sub_stage="keyframe_done",
            sub_current=len(descriptions), sub_total=len(descriptions),
        )

        return {"doc_id": doc_id, "keyframes": descriptions}

    except Exception as exc:
        log.exception("video_multipart.keyframe_failed", doc_id=doc_id)
        _update_parse_progress(
            doc_id, "parsing",
            message=f"关键帧提取失败: {exc}",
            sub_stage="keyframe_failed",
        )
        return {"doc_id": doc_id, "keyframes": [], "error": str(exc)}


# ======================================================================
# Finalize 任务 — 合并 ASR + 关键帧 → 分块 → 向量化 → 索引
# ======================================================================


@celery_app.task(
    name="tasks.video_tasks.finalize_video_task",
    **make_celery_retry_kwargs(),
)
def finalize_video_task(
    asr_keyframe_results: list,
    doc_id: str,
    wav_path: str = "",
) -> None:
    """合并 ASR + 关键帧结果 → 分块 → 向量化 → 索引（P2-D）。

    Chord 的 callback，接收 group 中两个任务的结果列表：
        [asr_multipart_task_result, keyframe_task_result]

    流程：
        1. 从 Redis 读取 ASR 结果（或用参数传入）
        2. 合并 ASR segments + keyframe descriptions
        3. 调用 SemanticChunker.chunk_video_transcript 分块
        4. 向量化 + 索引构建
        5. 更新文档状态为 published
        6. 清理临时文件

    Args:
        asr_keyframe_results: Chord group 结果列表。
        doc_id: 文档 ID。
        wav_path: WAV 文件路径（用于清理）。
    """
    from tasks.document_tasks import _update_parse_progress

    _update_parse_progress(
        doc_id, "chunking",
        message="合并 ASR 结果，开始分块...",
        sub_stage="finalize_start",
    )

    try:
        import asyncio

        # 1. 提取 ASR 和关键帧结果
        asr_result = {}
        keyframe_descriptions: list[dict[str, Any]] = []

        for result in asr_keyframe_results or []:
            if isinstance(result, dict):
                if result.get("segments"):
                    asr_result = result
                elif result.get("keyframes"):
                    keyframe_descriptions = result.get("keyframes", [])

        # Fallback: 从 Redis 读取 ASR 结果
        if not asr_result:
            try:
                import redis

                from app.config import get_settings

                settings = get_settings()
                client = redis.from_url(settings.REDIS_URL, decode_responses=True)
                cached = client.get(f"ekb:video_asr:{doc_id}")
                if cached:
                    asr_result = json.loads(cached)
                client.close()
            except Exception:
                pass

        segments = asr_result.get("segments", [])
        text = asr_result.get("text", "")

        if not segments:
            log.warning("video_multipart.finalize_no_segments", doc_id=doc_id)
            _update_parse_progress(
                doc_id, "failed",
                message="ASR 无转写结果，无法处理",
            )
            return

        # 2. 更新文档 content_text
        async def _update_doc_content() -> None:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 先无租户过滤地查找文档，获取其 tenant_id
                repo = DocumentRepository(db)
                doc = await repo.get_by_id(uuid.UUID(doc_id))
                if doc:
                    # 后续操作使用租户感知的仓储，确保多租户数据隔离
                    repo = DocumentRepository(db, tenant_id=doc.tenant_id)
                    doc.content_text = text
                    doc.content = text
                    await db.commit()

        asyncio.run(_update_doc_content())

        # 3. 分块（复用 document_tasks._chunk_video_document）
        from app.models.knowledge import Document  # noqa: F401  # 保留供类型推断
        from sqlalchemy import select  # noqa: F401  # 保留供其他分支使用

        async def _get_doc() -> Any:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 按主键查找文档，无需租户过滤（仅读取元数据）
                repo = DocumentRepository(db)
                return await repo.get_by_id(uuid.UUID(doc_id))

        doc = asyncio.run(_get_doc())
        if not doc:
            _update_parse_progress(doc_id, "failed", message="文档不存在")
            return

        # 3. 分块 — 修复：原实现调用签名不符的 _chunk_video_document（4 参 vs 真实 2 参）
        # 与不存在的 _embed_and_index，视频文档必失败。现对齐 document_tasks 真实函数：
        # 直接复用 chord 已产出的 ASR segments 与关键帧描述做语义分块
        # （与 _chunk_video_document 末步一致，避免重复 ASR/关键帧提取）。
        from app.rag.chunker import SemanticChunker
        from tasks.document_tasks import _chunk_document

        _update_parse_progress(
            doc_id, "chunking",
            message="视频语义分块中...",
            sub_stage="chunking",
        )

        chunker = SemanticChunker()
        chunk_objects = chunker.chunk_video_transcript(segments, keyframe_descriptions)
        if not chunk_objects:
            # 语义分块无结果时降级为普通文本分块（与 _chunk_video_document 降级策略一致）
            log.info("video_multipart.chunk_fallback_to_text", doc_id=doc_id)
            chunk_objects = _chunk_document(text, "txt")

        # 4. 向量化 + 索引（对齐 document_tasks 真实函数签名：
        #    _generate_embeddings(chunks) + _build_indexes(doc_id, chunk_objects, chunks, embeddings)）
        _update_parse_progress(
            doc_id, "embedding",
            message=f"向量化 {len(chunk_objects)} 个分块...",
            sub_stage="embedding",
        )

        from tasks.document_tasks import _build_indexes, _generate_embeddings

        async def _embed_and_index() -> None:
            chunks = [c.content for c in chunk_objects]
            embeddings = await _generate_embeddings(chunks)
            await _build_indexes(doc_id, chunk_objects, chunks, embeddings)

        asyncio.run(_embed_and_index())

        # 5. 更新文档状态
        _update_parse_progress(
            doc_id, "publishing",
            message="发布文档...",
            sub_stage="publishing",
        )

        async def _publish_doc() -> None:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 先无租户过滤地查找文档，获取其 tenant_id
                repo = DocumentRepository(db)
                doc = await repo.get_by_id(uuid.UUID(doc_id))
                if doc:
                    # 后续操作使用租户感知的仓储，确保多租户数据隔离
                    repo = DocumentRepository(db, tenant_id=doc.tenant_id)
                    doc.status = "published"
                    await db.commit()

        asyncio.run(_publish_doc())

        # 6. 清理临时 WAV 文件
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass

        # 7. 清理 Redis 缓存
        try:
            import redis

            from app.config import get_settings

            settings = get_settings()
            client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            client.delete(f"ekb:video_asr:{doc_id}")
            client.close()
        except Exception:
            pass

        _update_parse_progress(
            doc_id, "done",
            message=f"GB 视频处理完成，共 {len(chunk_objects)} 个分块",
        )

        # 8. 链式触发文档智能处理（摘要/标签/分类）
        try:
            from tasks.intelligence_tasks import process_intelligence

            process_intelligence.delay(doc_id)
        except Exception:
            log.debug("video_multipart.intelligence_trigger_failed", doc_id=doc_id)

    except Exception as exc:
        log.exception("video_multipart.finalize_failed", doc_id=doc_id)
        _update_parse_progress(
            doc_id, "failed",
            message=f"合并处理失败: {exc}",
        )


# ======================================================================
# Fallback — Celery 不可用时串行处理
# ======================================================================


def _process_video_fallback(doc_id: str, wav_path: str = "") -> None:
    """Celery Chord 不可用时降级为串行处理。

    直接调用 asr_multipart_task + keyframe_task + finalize_video_task，
    不并行，但保证功能可用。
    """
    log.info("video_multipart.fallback_serial", doc_id=doc_id)

    if not wav_path:
        wav_path = _extract_audio_stage(doc_id) or ""

    if not wav_path:
        return

    asr_result = asr_multipart_task(doc_id, wav_path)
    kf_result = keyframe_task(doc_id)
    finalize_video_task([asr_result, kf_result], doc_id, wav_path)
