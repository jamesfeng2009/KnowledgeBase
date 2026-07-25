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
import json
import uuid
from typing import Any

from celery_app import celery_app

from app.rag.chunker import Chunk, SemanticChunker
from app.utils.logger import get_logger
from app.utils.retry import make_celery_retry_kwargs

logger = get_logger(__name__)

# VLM 并发控制 — 关键帧描述并发上限，防止打满 VLM 服务
_VLM_SEMAPHORE_LIMIT: int = 3

# P1: 解析进度反馈 — Redis key 前缀和 TTL
_PROGRESS_KEY_PREFIX = "ekb:parse_progress:"
_PROGRESS_TTL_SECONDS = 1800  # 30 分钟，与 Celery 软超时对齐


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（用于死信队列时间戳）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _update_parse_progress(
    doc_id: str,
    stage: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
    sub_stage: str = "",
    sub_current: int = 0,
    sub_total: int = 0,
) -> None:
    """将解析进度写入 Redis，供前端轮询展示真实进度。

    P1 增强：替代前端按轮询次数模拟的虚假进度。
    P2 增强：新增 sub_stage/sub_current/sub_total，支持 GB 视频分段 ASR 细粒度进度。

    Args:
        doc_id: 文档 ID。
        stage: 阶段标识，取值：
            - "queued"      已入队
            - "parsing"     解析中（current/total 为页码）
            - "chunking"    分块中
            - "embedding"   向量化中
            - "indexing"    索引构建中
            - "publishing"  发布/审核提交中
            - "done"        完成
            - "failed"      失败
        current: 当前进度（如当前页码）。
        total: 总进度（如总页数）。
        message: 人类可读的提示信息。
        sub_stage: 子阶段标识（P2 新增，如 "asr_segment_3"）。
        sub_current: 子阶段当前进度（如已完成的 ASR 段数）。
        sub_total: 子阶段总进度（如总 ASR 段数）。
    """
    try:
        import json

        import redis

        from app.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        payload = {
            "stage": stage,
            "current": current,
            "total": total,
            "message": message,
            "sub_stage": sub_stage,
            "sub_current": sub_current,
            "sub_total": sub_total,
        }
        client.setex(
            f"{_PROGRESS_KEY_PREFIX}{doc_id}",
            _PROGRESS_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
        client.close()
    except Exception:
        # Redis 不可用时静默降级 — 进度反馈是增强项，不影响主流程
        logger.debug("parse_progress.update_failed", doc_id=doc_id, stage=stage)


def get_parse_progress(doc_id: str) -> dict[str, Any] | None:
    """读取解析进度（供 API 端点调用）。

    Args:
        doc_id: 文档 ID。

    Returns:
        进度字典 {"stage", "current", "total", "message"}，无记录时返回 None。
    """
    try:
        import json

        import redis

        from app.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        raw = client.get(f"{_PROGRESS_KEY_PREFIX}{doc_id}")
        client.close()
        if raw:
            return json.loads(raw)
    except Exception:
        logger.debug("parse_progress.read_failed", doc_id=doc_id)
    return None


# ------------------------------------------------------------------
# Chunk 持久化 — 子 task 跨进程共享 chunk_objects（chord 拆分基础）
# ------------------------------------------------------------------

_CHUNKS_KEY_PREFIX = "ekb:chunks:"
_CHUNKS_TTL_SECONDS = 3600  # 1 小时，chunk 是临时中间态


def _save_chunks_to_redis(doc_id: str, chunk_objects: list[Chunk]) -> bool:
    """将 chunk_objects 序列化存入 Redis，供子 task 跨进程读取。

    chord 拆分后，build_index_task 和 build_graph_task 是独立 Celery task，
    无法通过进程内参数传递 chunk_objects。通过 Redis 共享：
    - 入口 task 调用 _save_chunks_to_redis 存入
    - 子 task 调用 _load_chunks_from_redis 读取
    - TTL 1 小时，自动清理临时数据

    Args:
        doc_id: 文档 ID。
        chunk_objects: Chunk 对象列表。

    Returns:
        True = 成功，False = Redis 不可用（调用方应降级）。
    """
    try:
        import json

        import redis

        from app.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        # 序列化 Chunk dataclass（含 token_count/start_pos/end_pos，
        # 保证下游 build_index_task / build_graph_task 拿到完整元数据）
        payload = json.dumps(
            [
                {
                    "id": c.id,
                    "doc_id": c.doc_id,
                    "content": c.content,
                    "parent_id": c.parent_id,
                    "title_path": c.title_path,
                    "content_type": c.content_type,
                    "chunk_strategy": c.chunk_strategy,
                    "start_pos": c.start_pos,
                    "end_pos": c.end_pos,
                    "token_count": c.token_count,
                }
                for c in chunk_objects
            ],
            ensure_ascii=False,
        )
        client.setex(
            f"{_CHUNKS_KEY_PREFIX}{doc_id}",
            _CHUNKS_TTL_SECONDS,
            payload,
        )
        client.close()
        return True
    except Exception:
        logger.debug("chunks.save_failed", doc_id=doc_id)
        return False


def _load_chunks_from_redis(doc_id: str) -> list[Chunk] | None:
    """从 Redis 反序列化读取 chunk_objects。

    Args:
        doc_id: 文档 ID。

    Returns:
        Chunk 对象列表，Redis 不可用或无数据时返回 None。
    """
    try:
        import json

        import redis

        from app.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        raw = client.get(f"{_CHUNKS_KEY_PREFIX}{doc_id}")
        client.close()
        if not raw:
            return None
        data = json.loads(raw)
        # .get() 带默认值，向后兼容 Redis 中残留的旧格式数据（无位置/token 字段）
        return [
            Chunk(
                id=d["id"],
                doc_id=d["doc_id"],
                content=d["content"],
                parent_id=d.get("parent_id"),
                start_pos=d.get("start_pos", 0),
                end_pos=d.get("end_pos", 0),
                token_count=d.get("token_count", 0),
                title_path=d.get("title_path", ""),
                content_type=d.get("content_type", ""),
                chunk_strategy=d.get("chunk_strategy", ""),
            )
            for d in data
        ]
    except Exception:
        logger.debug("chunks.load_failed", doc_id=doc_id)
        return None


def _cleanup_chunks_redis(doc_id: str) -> None:
    """清理 Redis 中的临时 chunks — finalize 各终态分支共用。

    Redis 不可用时静默降级：临时数据有 TTL（1 小时）兜底自动过期。

    Args:
        doc_id: 文档 ID。
    """
    try:
        import redis

        from app.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.delete(f"{_CHUNKS_KEY_PREFIX}{doc_id}")
        client.close()
    except Exception:
        logger.debug("chunks.cleanup_failed", doc_id=doc_id)


def _count_pages_from_text(text: str, doc_type: str) -> int:
    """从解析后的文本推断文档页数。

    优先统计分页标记 <!-- page: N -->，无标记时按文档类型回退到 <h2> 计数：
    - PDF/DOCX: <h2> 数量（章节标题）
    - PPTX: <h2> 数量（幻灯片标题）
    - XLSX: <h2> 数量（sheet 标题）
    - 其他: 0

    Args:
        text: 解析后的文档文本（HTML 格式）。
        doc_type: 文档类型。

    Returns:
        推断的页数，无法推断时返回 0。
    """
    if not text:
        return 0

    import re

    # 优先统计分页标记 <!-- page: N -->
    page_markers = re.findall(r"<!--\s*page:\s*\d+\s*-->", text)
    if page_markers:
        return len(page_markers)

    # 按文档类型统计 <h2> 标题
    if doc_type in ("pdf", "docx", "pptx", "xlsx"):
        return len(re.findall(r"<h2\b", text, re.IGNORECASE))

    return 0

# 保留旧常量供向后兼容引用，实际分块已委托给 SemanticChunker
# Deprecated: 使用 SemanticChunker 替代简单滑动窗口
CHUNK_SIZE: int = 500
CHUNK_OVERLAP: int = 50


def _send_to_dead_letter(
    task_name: str,
    task_id: str,
    args: tuple,
    kwargs: dict,
    exc: Exception,
) -> None:
    """将重试耗尽的任务发送到死信队列供人工排查。

    死信队列不配置消费者，仅用于保留失败任务信息。
    运维可通过 celery inspect 或 Redis CLI 查看死信队列内容。

    Args:
        task_name: 任务名称（如 tasks.document_tasks.process_document）。
        task_id: Celery 任务 ID。
        args: 任务位置参数。
        kwargs: 任务关键字参数。
        exc: 最终异常。
    """
    try:
        dead_letter_payload = {
            "original_task": task_name,
            "original_task_id": task_id,
            "args": list(args) if args else [],
            "kwargs": kwargs if kwargs else {},
            "error": str(exc)[:500],
            "failed_at": _now_iso(),
        }
        # 发送到 dead_letter 队列（无消费者，仅存储）
        celery_app.send_task(
            name="dead_letter.record",
            args=[dead_letter_payload],
            queue="dead_letter",
        )
        logger.warning(
            "task.dead_lettered",
            task_name=task_name,
            task_id=task_id,
            error=str(exc)[:200],
        )
    except Exception as dl_exc:
        # 死信队列记录失败不应影响主流程
        logger.error(
            "task.dead_letter_failed",
            task_name=task_name,
            task_id=task_id,
            error=str(dl_exc)[:200],
        )


@celery_app.task(
    name="dead_letter.record",
    queue="dead_letter",
    ignore_result=True,
)
def _record_dead_letter(payload: dict) -> None:
    """死信队列消费者 — 仅记录日志，不做任何处理。

    死信队列的目的是保留失败任务信息供人工排查，
    此 task 仅用于让 Celery 不报"unknown task"警告。
    """
    logger.warning(
        "dead_letter.recorded",
        original_task=payload.get("original_task"),
        task_id=payload.get("original_task_id"),
        error=payload.get("error", ""),
    )


@celery_app.task(
    name="tasks.document_tasks.build_index_task",
    queue="indexing",
    bind=True,
    **make_celery_retry_kwargs(),
)
def build_index_task(self, doc_id: str) -> dict[str, Any]:
    """支线 A — 向量化 + OpenSearch 索引（独立 worker，可单独扩容）。

    从 Redis 读取 chunk_objects（入口 task 持久化），执行向量和索引构建。
    与 build_graph_task 并行执行，互不影响。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        索引构建结果。
    """
    logger.info("document.index_task_started", doc_id=doc_id)

    chunk_objects = _load_chunks_from_redis(doc_id)
    if chunk_objects is None:
        logger.warning("document.index_chunks_not_found", doc_id=doc_id)
        return {"doc_id": doc_id, "status": "skipped", "reason": "chunks_not_found"}

    chunks = [c.content for c in chunk_objects]

    _update_parse_progress(
        doc_id, stage="embedding", total=len(chunks),
        message=f"正在向量化 {len(chunks)} 个文本块",
    )

    try:
        result = asyncio.run(_build_index_async(doc_id, chunk_objects, chunks))
        return result
    except Exception as exc:
        logger.error("document.index_task_failed", doc_id=doc_id, error=str(exc))
        if self.request.retries >= self.max_retries:
            _send_to_dead_letter(
                task_name="tasks.document_tasks.build_index_task",
                task_id=self.request.id,
                args=(doc_id,),
                kwargs={},
                exc=exc,
            )
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)[:500], "dead_lettered": True}
        raise self.retry(exc=exc)


@celery_app.task(
    name="tasks.document_tasks.build_graph_task",
    queue="documents",
    bind=True,
    **make_celery_retry_kwargs(),
)
def build_graph_task(self, doc_id: str) -> dict[str, Any]:
    """支线 B — 知识图谱构建（独立 worker，复用 chunk_objects）。

    从 Redis 读取 chunk_objects，调用 GraphService.extract_triples_from_chunks
    抽取三元组写入 Neo4j。与 build_index_task 并行执行。

    知识图谱模块未启用时，入口 task 不投递此 task（零开销）。

    Args:
        doc_id: 文档 ID（UUID 字符串）。

    Returns:
        图谱构建结果。
    """
    logger.info("document.graph_task_started", doc_id=doc_id)

    chunk_objects = _load_chunks_from_redis(doc_id)
    if chunk_objects is None:
        logger.warning("document.graph_chunks_not_found", doc_id=doc_id)
        return {"doc_id": doc_id, "status": "skipped", "reason": "chunks_not_found"}

    _update_parse_progress(
        doc_id, stage="embedding",
        message="正在构建知识图谱（三元组抽取）",
        sub_stage="graph_building",
    )

    try:
        result = asyncio.run(_build_graph_async(doc_id, chunk_objects))
        return result
    except Exception as exc:
        logger.error("document.graph_task_failed", doc_id=doc_id, error=str(exc))
        if self.request.retries >= self.max_retries:
            _send_to_dead_letter(
                task_name="tasks.document_tasks.build_graph_task",
                task_id=self.request.id,
                args=(doc_id,),
                kwargs={},
                exc=exc,
            )
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)[:500], "dead_lettered": True}
        raise self.retry(exc=exc)


@celery_app.task(
    name="tasks.document_tasks.finalize_document_task",
    bind=True,
)
def finalize_document_task(self, results: list, doc_id: str = None) -> dict[str, Any]:
    """chord 回调 — 密级路由 + 审核/发布 + 触发智能处理。

    build_index_task 和 build_graph_task 全部完成后触发。
    根据密级决定发布路径，更新 parse_status，触发智能处理。

    Args:
        results: chord group 中各 task 的返回值列表。
        doc_id: 文档 ID（通过 .s() 传入）。

    Returns:
        最终处理结果。
    """
    # chord 回调的 doc_id 可能在 results 中或作为参数
    if doc_id is None and isinstance(results, list) and results:
        last = results[-1]
        if isinstance(last, dict):
            doc_id = last.get("doc_id")

    if doc_id is None:
        logger.error("document.finalize_no_doc_id", results=results)
        return {"status": "failed", "error": "无法确定 doc_id"}

    logger.info("document.finalize_started", doc_id=doc_id, results=results)

    try:
        result = asyncio.run(_finalize_document_async(doc_id, results))
        return result
    except Exception as exc:
        logger.error("document.finalize_failed", doc_id=doc_id, error=str(exc))
        _update_parse_progress(
            doc_id, stage="failed", message=f"发布失败：{str(exc)[:200]}"
        )
        return {"doc_id": doc_id, "status": "failed", "error": str(exc)[:500]}


@celery_app.task(
    name="tasks.document_tasks.process_document",
    bind=True,
    **make_celery_retry_kwargs(),
)
def process_document(self, doc_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    """文档处理流水线 — 解析 → 分块 → 向量化 → 索引 → 更新状态。

    异步执行文档的完整处理流程，适用于上传后自动处理。
    每个阶段失败时自动重试（最多 3 次）。

    P1-B 幂等性：使用 Redis SETNX 任务锁，同一文档同时只被一个 worker 处理。
    其他 worker 尝试获取锁失败时直接返回，避免重复处理。

    Args:
        doc_id: 文档 ID（UUID 字符串）。
        tenant_id: 租户 ID（多租户隔离，可选）。

    Returns:
        处理结果字典，包含 doc_id、status、chunk_count 等。
    """
    logger.info("document.task_started", doc_id=doc_id)

    # P1-B: 幂等锁 — 同一文档同时只被一个 worker 处理
    # 修复：原实现在 _check_lock() 的 async with 中 return，锁随上下文退出立即释放，
    # 导致 30 分钟处理全程无锁、幂等失效。现改为手动 __aenter__ 获取锁并持有到
    # 任务结束（finally 中 __aexit__ 释放），锁覆盖任务整个执行期；
    # TaskLockContext 的 acquire/release 各自建立独立 Redis 短连接，
    # 跨 asyncio.run() 事件循环安全。
    from app.utils.task_lock import TaskLockContext

    lock_ctx = TaskLockContext("process_document", doc_id)
    try:
        lock_ctx = asyncio.run(lock_ctx.__aenter__())
        if not lock_ctx.acquired:
            logger.info("document.task_skipped_locked", doc_id=doc_id)
            return {
                "doc_id": doc_id,
                "status": "skipped",
                "message": "文档正在被其他 worker 处理，跳过重复执行",
            }
    except Exception as exc:
        logger.warning("document.lock_check_failed", doc_id=doc_id, error=str(exc)[:200])
        # Redis 不可用时降级为无锁模式，继续执行
        lock_ctx = None

    try:
        # P1: 标记任务已入队
        _update_parse_progress(doc_id, stage="queued", message="文档已加入解析队列")

        # P2-D: GB 级视频/音频走专用多任务管线，避免单任务 30 分钟超时
        if _should_use_multipart_pipeline(doc_id):
            logger.info("document.video_multipart_routed", doc_id=doc_id)
            try:
                from tasks.video_tasks import process_video_multipart

                process_video_multipart(doc_id)
                return {
                    "doc_id": doc_id,
                    "status": "processing",
                    "message": "GB 视频已分流到多任务管线（Chord 编排）",
                }
            except Exception as exc:
                logger.exception("document.video_multipart_dispatch_failed", doc_id=doc_id)
                # 分流失败时降级走普通管线
                _update_parse_progress(
                    doc_id, stage="parsing",
                    message=f"GB 视频分流失败，降级普通管线: {exc}",
                )

        try:
            # ① 串行阶段：解析 + 分块（事件循环中执行）
            parse_result = asyncio.run(_parse_and_chunk_async(doc_id))

            if parse_result.get("status") == "failed":
                return parse_result

            chunk_objects = parse_result["chunk_objects"]
            graph_enabled = parse_result.get("graph_enabled", False)

            # ② chunk_objects 持久化到 Redis，供子 task 跨进程读取
            chunks_saved = _save_chunks_to_redis(doc_id, chunk_objects)
            if not chunks_saved:
                # Redis 不可用 → 降级为串行模式（_process_document_async）
                logger.warning("document.chunks_save_failed_fallback", doc_id=doc_id)
                result = asyncio.run(_process_document_async(doc_id))
                return result

            # ③ chord 编排：并行 build_index_task + build_graph_task → finalize
            try:
                from celery import chord, group

                if graph_enabled:
                    # 索引 + 图谱并行
                    chord(
                        group(
                            build_index_task.s(doc_id),
                            build_graph_task.s(doc_id),
                        )
                    )(
                        finalize_document_task.s(doc_id)
                    )
                else:
                    # 仅索引（知识图谱未启用）
                    chord(
                        group(
                            build_index_task.s(doc_id),
                        )
                    )(
                        finalize_document_task.s(doc_id)
                    )
                logger.info("document.chord_dispatched", doc_id=doc_id, graph_enabled=graph_enabled)
                return {
                    "doc_id": doc_id,
                    "status": "processing",
                    "chunk_count": len(chunk_objects),
                    "message": "已分发到 chord 并行管线（索引+图谱）",
                }
            except ImportError:
                # Celery 未安装 → 降级串行（串行路径不消费 Redis 暂存，先清理避免孤儿数据）
                logger.warning("document.celery_not_installed_fallback", doc_id=doc_id)
                _cleanup_chunks_redis(doc_id)
                result = asyncio.run(_process_document_async(doc_id))
                return result
            except Exception as exc:
                # chord 分发失败 → 降级串行（串行路径不消费 Redis 暂存，先清理避免孤儿数据）
                logger.exception("document.chord_failed_fallback", doc_id=doc_id)
                _cleanup_chunks_redis(doc_id)
                result = asyncio.run(_process_document_async(doc_id))
                return result
        except Exception as exc:
            logger.error(
                "document.task_failed",
                doc_id=doc_id,
                error=str(exc),
            )
            # 清理 Redis 暂存 chunks，避免任务失败/重试后残留孤儿数据
            # （重试会重新解析并写入；删除不存在的 key 为幂等空操作）
            _cleanup_chunks_redis(doc_id)
            # P1: 标记解析失败
            _update_parse_progress(
                doc_id, stage="failed", message=f"解析失败：{str(exc)[:200]}"
            )
            # 重试 — 耗尽后发送到死信队列
            if self.request.retries >= self.max_retries:
                _send_to_dead_letter(
                    task_name="tasks.document_tasks.process_document",
                    task_id=self.request.id,
                    args=(doc_id,),
                    kwargs={},
                    exc=exc,
                )
                return {
                    "status": "failed",
                    "doc_id": doc_id,
                    "error": str(exc)[:500],
                    "dead_lettered": True,
                }
            raise self.retry(exc=exc)
    finally:
        # 释放任务锁 — 成功/失败/重试均执行；释放失败仅记录日志，
        # 锁会在 TTL（默认 1800s，与任务硬超时对齐）到期后自动释放
        if lock_ctx is not None:
            try:
                asyncio.run(lock_ctx.__aexit__(None, None, None))
            except Exception:
                logger.warning("document.lock_release_failed", doc_id=doc_id)


# ------------------------------------------------------------------
# chord 拆分的异步辅助函数
# ------------------------------------------------------------------


async def _parse_and_chunk_async(doc_id: str) -> dict[str, Any]:
    """chord 入口阶段 — 解析 + 分块 + 检查模块开关。

    从 _process_document_async 提取前半段（加载+解析+分块），
    不包含索引/图谱/发布逻辑。

    Returns:
        {"chunk_objects": [...], "graph_enabled": bool, "parse_warnings": [...]}
        失败时 {"status": "failed", "error": "..."}
    """
    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)
    warnings: list[str] = []

    async with task_db_session() as session:
        # 先无租户过滤地查找文档，获取其 tenant_id
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}
        # 后续操作使用租户感知的仓储，确保多租户数据隔离
        repo = DocumentRepository(session, tenant_id=doc.tenant_id)

        # 1. 解析文档内容
        _update_parse_progress(doc_id, stage="parsing", message="正在解析文档内容")
        parse_failed = False
        extracted_images: list[tuple[bytes, str]] = []
        try:
            parsed_text, extracted_images = await _parse_document(doc)
        except Exception as exc:
            logger.warning("document.parse_failed", doc_id=doc_id, error=str(exc))
            parsed_text = doc.content_text or ""
            parse_failed = True
            warnings.append(f"解析异常: {str(exc)[:200]}")

        if doc.doc_type in ("doc", "ppt"):
            warnings.append(
                f"旧格式 .{doc.doc_type} 不支持解析，请转换为 .{doc.doc_type}x 后重新上传"
            )

        # 2. 更新纯文本内容 + 计算内容哈希（P1-B）
        if parsed_text and parsed_text != doc.content_text:
            doc.content_text = parsed_text
            # P1-B: 计算内容哈希，用于去重和增量更新
            from app.utils.hash import compute_content_hash

            doc.content_hash = compute_content_hash(parsed_text)
            await session.flush()

        # 3. 分块
        _update_parse_progress(doc_id, stage="chunking", message="正在进行语义分块")
        doc_type = doc.doc_type or "md"
        if doc_type in _VIDEO_TYPES or doc_type in _AUDIO_TYPES:
            chunk_objects = await _chunk_video_document(doc, parsed_text)
        else:
            chunk_objects = _chunk_document(parsed_text, doc_type, doc_id=doc_id)
        logger.info(
            "document.chunked",
            doc_id=doc_id,
            chunk_count=len(chunk_objects),
            strategies=[c.chunk_strategy for c in chunk_objects],
        )

        # 4. 检查 knowledge_graph 模块开关
        graph_enabled = False
        try:
            from app.services.tenant_service import TenantService

            tenant_svc = TenantService(session)
            graph_enabled = await tenant_svc.is_module_enabled(
                "knowledge_graph", doc.tenant_id
            )
        except Exception as exc:
            logger.debug(
                "document.graph_module_check_failed",
                doc_id=doc_id, error=str(exc),
            )

        # 5. 持久化 parse 元数据
        doc.parse_status = "partial" if warnings else "parsed"
        doc.parse_warnings = warnings if warnings else None
        doc.char_count = len(parsed_text)
        doc.page_count = _count_pages_from_text(parsed_text, doc_type)
        await session.commit()

        return {
            "chunk_objects": chunk_objects,
            "graph_enabled": graph_enabled,
            "parse_warnings": warnings,
        }


async def _build_index_async(
    doc_id: str, chunk_objects: list[Chunk], chunks: list[str]
) -> dict[str, Any]:
    """chord 支线 A — 向量化 + 索引构建。"""
    warnings: list[str] = []
    try:
        emb = await _generate_embeddings(chunks)
        logger.info("document.embedded", doc_id=doc_id, embedding_count=len(emb))
    except Exception as exc:
        logger.warning("document.embed_failed", doc_id=doc_id, error=str(exc))
        emb = []
        warnings.append(f"向量化失败（已降级为空向量）: {str(exc)[:200]}")

    # P2-Step1: 查询文档所属 kb_id，写入索引供检索端按知识库过滤
    kb_id: str | None = None
    try:
        from app.database import task_db_session
        from app.repositories.knowledge_repository import DocumentRepository

        async with task_db_session() as session:
            repo = DocumentRepository(session)
            doc = await repo.get_by_id(uuid.UUID(doc_id))
            if doc and doc.kb_id:
                kb_id = str(doc.kb_id)
    except Exception as exc:
        logger.debug("document.kb_id_lookup_failed", doc_id=doc_id, error=str(exc)[:200])

    # P2-Step3: 提取图片用于跨模态索引（chord 模式下 images 无法通过参数传递，
    # 此处从文档文件重新提取）
    extracted_images: list[tuple[bytes, str]] = []
    try:
        from app.database import task_db_session
        from app.repositories.knowledge_repository import DocumentRepository

        async with task_db_session() as session:
            repo = DocumentRepository(session)
            doc = await repo.get_by_id(uuid.UUID(doc_id))
            if doc is not None:
                doc_type = doc.doc_type or "md"
                extracted_images = await _extract_images_for_cross_modal(doc, doc_type)
    except Exception as exc:
        logger.debug("document.cross_modal_extract_skipped", doc_id=doc_id, error=str(exc)[:200])

    _update_parse_progress(doc_id, stage="indexing", message="正在构建全文索引和向量索引")
    try:
        await _build_indexes(
            doc_id, chunk_objects, chunks, emb, kb_id=kb_id,
            images=extracted_images if extracted_images else None,
        )
    except Exception as exc:
        logger.warning("document.index_failed", doc_id=doc_id, error=str(exc))
        warnings.append(f"索引构建失败: {str(exc)[:200]}")

    return {"doc_id": doc_id, "status": "done", "index_warnings": warnings}


async def _build_graph_async(doc_id: str, chunk_objects: list[Chunk]) -> dict[str, Any]:
    """chord 支线 B — 知识图谱构建。"""
    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    warnings: list[str] = []
    async with task_db_session() as session:
        # 先无租户过滤地查找文档，获取其 tenant_id
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(uuid.UUID(doc_id))
        if doc is None:
            return {"doc_id": doc_id, "status": "done", "graph_warnings": warnings}
        # 后续操作使用租户感知的仓储，确保多租户数据隔离
        repo = DocumentRepository(session, tenant_id=doc.tenant_id)

        # P3: chord 模式下重新提取图片用于图谱 Image 节点
        graph_images: list[tuple[bytes, str]] = []
        try:
            doc_type = doc.doc_type or "md"
            graph_images = await _extract_images_for_cross_modal(doc, doc_type)
        except Exception as exc:
            logger.debug("document.graph_images_extract_skipped", doc_id=doc_id, error=str(exc)[:200])

        try:
            await _build_knowledge_graph(
                doc_id, chunk_objects, doc,
                images=graph_images if graph_images else None,
            )
        except Exception as exc:
            logger.warning("document.graph_build_failed", doc_id=doc_id, error=str(exc))
            warnings.append(f"知识图谱构建失败: {str(exc)[:200]}")

    return {"doc_id": doc_id, "status": "done", "graph_warnings": warnings}


async def _finalize_document_async(
    doc_id: str, results: list
) -> dict[str, Any]:
    """chord 回调阶段 — 密级路由 + 审核/发布 + 触发智能处理。"""
    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    # 合并子 task 的 warnings
    all_warnings: list[str] = []
    for r in results:
        if isinstance(r, dict):
            all_warnings.extend(r.get("index_warnings", []))
            all_warnings.extend(r.get("graph_warnings", []))

    doc_uuid = uuid.UUID(doc_id)
    async with task_db_session() as session:
        # 先无租户过滤地查找文档，获取其 tenant_id
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}
        # 后续操作使用租户感知的仓储，确保多租户数据隔离
        repo = DocumentRepository(session, tenant_id=doc.tenant_id)

        # 合并解析阶段 warnings
        if doc.parse_warnings:
            all_warnings = list(doc.parse_warnings) + all_warnings

        # 更新 parse_status
        doc.parse_status = "failed" if not doc.content_text else (
            "partial" if all_warnings else "parsed"
        )
        doc.parse_warnings = all_warnings if all_warnings else None

        # 修复：parse_status=failed（无正文内容）时短路发布流程。
        # 失败文档不得进入审核/发布/智能处理，保证 parse_status 与 status 状态一致；
        # 重复 finalize 时该分支幂等（状态保持 failed，不产生任何发布副作用）。
        if doc.parse_status == "failed":
            doc.status = "failed"
            await session.commit()
            _cleanup_chunks_redis(doc_id)
            _update_parse_progress(
                doc_id, stage="failed", message="文档解析失败：无有效正文内容"
            )
            logger.warning(
                "document.finalize_parse_failed",
                doc_id=doc_id,
                warnings=all_warnings,
            )
            return {
                "doc_id": doc_id,
                "status": "failed",
                "error": "文档解析失败：无有效正文内容",
                "warnings": all_warnings,
            }

        # 密级路由
        classification = doc.classification or "internal"
        needs_review = classification in _REQUIRES_REVIEW

        # 幂等：文档已处于发布终态（重复 finalize / chord 重放）时，
        # 不重复提交审核、不重复触发智能处理，仅持久化 parse 元数据后返回。
        if doc.status in ("published", "pending_review"):
            await session.commit()
            _cleanup_chunks_redis(doc_id)
            _update_parse_progress(
                doc_id, stage="done", message=f"文档处理完成：{doc.status}"
            )
            logger.info(
                "document.finalize_idempotent_skip",
                doc_id=doc_id,
                current_status=doc.status,
            )
            return {
                "doc_id": doc_id,
                "status": "success",
                "final_status": doc.status,
                "warnings": all_warnings,
                "idempotent": True,
            }

        if needs_review:
            doc.status = "pending_review"
            await session.commit()
            _update_parse_progress(
                doc_id, stage="publishing", message="文档需审核，正在提交审核流程"
            )
            try:
                await _submit_for_audit(doc_id, doc.owner_id)
                logger.info("document.audit_submitted", doc_id=doc_id)
            except Exception as exc:
                logger.warning("document.audit_submit_failed", doc_id=doc_id, error=str(exc))
        else:
            doc.status = "published"
            await session.commit()

        # 触发智能处理
        try:
            from tasks.intelligence_tasks import process_intelligence
            process_intelligence.delay(doc_id)
            logger.info("document.intelligence_triggered", doc_id=doc_id)
        except Exception as exc:
            logger.warning("document.intelligence_trigger_failed", doc_id=doc_id, error=str(exc))

        # 清理 Redis 中的临时 chunks
        _cleanup_chunks_redis(doc_id)

        final_status = "pending_review" if needs_review else "published"
        _update_parse_progress(
            doc_id, stage="done", message=f"文档处理完成：{final_status}"
        )

        return {
            "doc_id": doc_id,
            "status": "success",
            "final_status": final_status,
            "warnings": all_warnings,
        }


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
    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    # P1: 收集解析过程中的警告信息（用于摘要响应）
    warnings: list[str] = []

    async with task_db_session() as session:
        # 先无租户过滤地查找文档，获取其 tenant_id
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}
        # 后续操作使用租户感知的仓储，确保多租户数据隔离
        repo = DocumentRepository(session, tenant_id=doc.tenant_id)

        # 1. 解析文档内容
        _update_parse_progress(
            doc_id, stage="parsing", message="正在解析文档内容"
        )
        parse_failed = False
        extracted_images: list[tuple[bytes, str]] = []
        try:
            parsed_text, extracted_images = await _parse_document(doc)
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

        # 2. 更新纯文本内容（检索用）+ 计算内容哈希（P1-B）
        if parsed_text and parsed_text != doc.content_text:
            doc.content_text = parsed_text
            # P1-B: 计算内容哈希，用于去重和增量更新
            from app.utils.hash import compute_content_hash

            doc.content_hash = compute_content_hash(parsed_text)
            await session.flush()

        # 3. 分块 — 视频/音频文档走专用分块，其他走 SemanticChunker 四级策略
        _update_parse_progress(
            doc_id, stage="chunking", message="正在进行语义分块"
        )
        doc_type = doc.doc_type or "md"
        if doc_type in _VIDEO_TYPES or doc_type in _AUDIO_TYPES:
            # 视频/音频文档：ASR 转写片段 + 关键帧 VLM 描述 → 语义分块
            chunk_objects = await _chunk_video_document(doc, parsed_text)
        else:
            chunk_objects = _chunk_document(parsed_text, doc_type, doc_id=doc_id)
        chunks = [c.content for c in chunk_objects]
        logger.info(
            "document.chunked",
            doc_id=doc_id,
            chunk_count=len(chunks),
            strategies=[c.chunk_strategy for c in chunk_objects],
        )

        # 4-5. 向量化 + 索引  ‖  知识图谱构建（并行执行）
        #
        # 方向一：分块完成后并行执行两条支线，避免串行等待：
        #   支线 A：向量化 → 索引构建（全文索引 + 向量索引）
        #   支线 B：知识图谱构建（三元组抽取 → Neo4j 写入）
        #
        # 方向二：支线 B 直接复用 chunk_objects，避免重复分块计算。
        # GraphService.extract_triples_from_chunks 从同一批 chunks 抽取三元组。
        #
        # 知识图谱构建受模块开关控制：仅当租户启用 knowledge_graph 模块时执行。
        _update_parse_progress(
            doc_id,
            stage="embedding",
            total=len(chunks),
            message=f"正在并行处理：向量化 {len(chunks)} 个文本块 + 知识图谱构建",
        )

        async def _pipeline_index() -> tuple[list[list[float]], list[str]]:
            """支线 A：向量化 + 索引构建。"""
            try:
                emb = await _generate_embeddings(chunks)
                logger.info(
                    "document.embedded",
                    doc_id=doc_id,
                    embedding_count=len(emb),
                )
            except Exception as exc:
                logger.warning("document.embed_failed", doc_id=doc_id, error=str(exc))
                emb = []
                warnings.append(f"向量化失败（已降级为空向量）: {str(exc)[:200]}")

            _update_parse_progress(
                doc_id, stage="indexing", message="正在构建全文索引和向量索引"
            )
            try:
                await _build_indexes(
                    doc_id, chunk_objects, chunks, emb,
                    kb_id=str(doc.kb_id) if doc.kb_id else None,
                    images=extracted_images if extracted_images else None,
                )
            except Exception as exc:
                logger.warning("document.index_failed", doc_id=doc_id, error=str(exc))
                warnings.append(f"索引构建失败: {str(exc)[:200]}")
            return emb, warnings

        async def _pipeline_graph() -> None:
            """支线 B：知识图谱构建（计算复用 chunk_objects）。"""
            try:
                await _build_knowledge_graph(
                    doc_id, chunk_objects, doc,
                    images=extracted_images if extracted_images else None,
                )
            except Exception as exc:
                logger.warning(
                    "document.graph_build_failed",
                    doc_id=doc_id,
                    error=str(exc),
                )
                warnings.append(f"知识图谱构建失败: {str(exc)[:200]}")

        # 检查租户是否启用 knowledge_graph 模块
        graph_enabled = False
        try:
            from app.services.tenant_service import TenantService

            tenant_svc = TenantService(session)
            graph_enabled = await tenant_svc.is_module_enabled(
                "knowledge_graph", doc.tenant_id
            )
        except Exception as exc:
            logger.debug(
                "document.graph_module_check_failed",
                doc_id=doc_id,
                error=str(exc),
            )

        if graph_enabled:
            # 并行执行两条支线（方向一：asyncio.gather 替代串行）
            embeddings, _ = await asyncio.gather(
                _pipeline_index(),
                _pipeline_graph(),
            )
            embeddings = embeddings[0]
        else:
            # 知识图谱未启用，仅执行索引支线
            embeddings, _ = await _pipeline_index()

        # 6. 根据密级决定发布路径：
        #    confidential/secret → 待审核（审核通过后发布）
        #    public/internal → 直接发布
        classification = doc.classification or "internal"
        needs_review = classification in _REQUIRES_REVIEW

        # P1: 持久化解析元数据（parse_status / parse_warnings / page_count / char_count）
        # 推断解析状态 — 有内容且无致命错误为 parsed，有警告为 partial
        if parse_failed and not parsed_text.strip():
            parse_status = "failed"
        elif warnings:
            parse_status = "partial"
        else:
            parse_status = "parsed"

        doc.parse_status = parse_status
        doc.parse_warnings = warnings if warnings else None
        doc.char_count = len(parsed_text)
        doc.page_count = _count_pages_from_text(parsed_text, doc_type)

        if needs_review:
            doc.status = "pending_review"
            await session.commit()

            # 提交审核流程 — 审核通过后触发文档发布
            _update_parse_progress(
                doc_id,
                stage="publishing",
                message="文档需审核，正在提交审核流程",
            )
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

        # P1: 标记解析完成
        _update_parse_progress(
            doc_id,
            stage="done",
            total=len(chunks),
            message=f"解析完成，共 {len(chunks)} 个分块，状态：{final_status}",
        )

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


async def _parse_document(doc: Any) -> tuple[str, list[tuple[bytes, str]]]:
    """解析文档内容 — 优先 Docling 统一解析，降级到原有专用解析器。

    解析优先级：
        1. Docling 统一解析器（PDF/DOCX/PPTX/XLSX/HTML/图片/音频）
        2. 原有专用解析器（pymupdf/python-docx/python-pptx/openpyxl）
        3. 旧格式兜底提示（.doc/.ppt）
        4. 视频专用管线（ffmpeg→ASR + 关键帧 VLM，Docling 不支持视频）

    P2-Step3: 同时返回文档中提取的图片二进制数据 + VLM 描述，
    供跨模态索引（_build_cross_modal_index）向量化入库。

    Args:
        doc: Document ORM 实例。

    Returns:
        (解析后的纯文本内容, 图片数据列表 [(二进制, VLM描述), ...])
    """
    # 如果已有纯文本内容，直接返回（无图片数据）
    if doc.content_text:
        return doc.content_text, []

    doc_type = doc.doc_type or "md"

    # 视频不走 Docling（Docling 不支持视频），直接走专用管线
    if doc_type in _VIDEO_TYPES:
        text = await _parse_video(doc)
        return text, []

    # .doc/.ppt 旧格式 — Docling 也无法解析，返回降级提示
    if doc_type in ("doc", "ppt"):
        return _legacy_format_fallback(doc, doc_type), []

    # 1. 优先尝试 Docling 统一解析
    docling_result = await _try_docling_parse(doc, doc_type)
    if docling_result is not None:
        # Docling 解析成功 — 尝试提取图片用于跨模态索引
        images = await _extract_images_for_cross_modal(doc, doc_type)
        return docling_result, images

    # 2. 降级到原有专用解析器
    if doc_type == "pdf":
        text = await _parse_pdf(doc)
    elif doc_type == "docx":
        text = await _parse_docx(doc)
    elif doc_type in ("xlsx", "xls"):
        text = await _parse_xlsx(doc)
    elif doc_type == "html":
        text = _parse_html(doc)
    elif doc_type == "pptx":
        text = await _parse_pptx(doc)
    elif doc_type in _AUDIO_TYPES:
        text = await _parse_audio(doc)
    else:
        # Markdown 或纯文本，直接返回
        text = doc.content_html or doc.content_text or ""

    # 非视频/音频文档提取图片用于跨模态索引
    images = await _extract_images_for_cross_modal(doc, doc_type)
    return text, images


async def _extract_images_for_cross_modal(
    doc: Any, doc_type: str
) -> list[tuple[bytes, str]]:
    """从文档中提取图片二进制 + VLM 描述，供跨模态向量化入库。

    P2-Step3: 文档解析阶段图片已被 VLM 转为文本描述内联进正文，
    但图片二进制数据被丢弃。本函数重新提取图片二进制 + VLM 描述，
    传递给 _build_cross_modal_index 进行 jina-clip-v2 向量化。

    优雅降级：任何异常返回空列表，不影响主文档处理流程。

    Args:
        doc: Document ORM 实例。
        doc_type: 文档类型。

    Returns:
        图片数据列表 [(图片二进制, VLM描述), ...]。
    """
    if not doc.file_path:
        return []

    # 跨模态检索未启用时不提取图片（节省资源）
    try:
        from app.config import get_settings

        if not get_settings().CROSS_MODAL_ENABLED:
            return []
    except Exception:
        return []

    images: list[tuple[bytes, str]] = []

    try:
        if doc_type == "pdf":
            from app.document.docling_parser import DoclingParser

            parser = DoclingParser()
            result = await parser._parse_raw(doc.file_path)
            if result is not None:
                pictures = DoclingParser._extract_pictures(result)
                for pic in pictures:
                    images.append((pic["data"], ""))
        elif doc_type in ("docx", "pptx"):
            # DOCX/PPTX: 使用解析器的图片提取能力
            from app.document.factory import get_parser_with_fallback

            parser, parser_type = get_parser_with_fallback(doc_type)
            if parser is not None and hasattr(parser, "extract_raw_images"):
                images = await parser.extract_raw_images(doc.file_path)
    except Exception as exc:
        logger.debug("document.cross_modal_extract_failed", doc_id=str(getattr(doc, "id", "")), error=str(exc)[:200])

    # 对没有描述的图片批量生成 VLM 描述
    if images:
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            enhanced: list[tuple[bytes, str]] = []
            for img_bytes, desc in images:
                if desc:
                    enhanced.append((img_bytes, desc))
                    continue
                try:
                    desc = await vlm.understand(
                        image=img_bytes,
                        prompt="请用一句话描述这张图片的内容，重点关注图表、数据和关键信息。",
                    )
                    enhanced.append((img_bytes, desc or "[图片内容]"))
                except Exception:
                    enhanced.append((img_bytes, "[图片内容]"))
            images = enhanced
        except ImportError:
            # VLM 不可用，使用占位描述
            images = [(b, d or "[图片内容]") for b, d in images]
        except Exception as exc:
            logger.debug("document.cross_modal_vlm_failed", error=str(exc)[:200])
            images = [(b, d or "[图片内容]") for b, d in images]

    return images


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
    """解析 HTML 文档 — 使用 WikiHtmlCleaner 清洗为语义化 HTML。

    升级：从"去全部标签提纯文本"改为"保留 h1-h6/table/ul-ol 结构的语义化 HTML"。
    这样 chunker 的 _split_html 可按标题分块，表格结构也保留。
    """
    try:
        from app.document.wiki_cleaner import clean_wiki_html

        html = doc.content_html or ""
        if not html:
            return doc.content_text or ""
        return clean_wiki_html(html)
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

# P2-D: 超过此大小（MB）的视频/音频走多任务管线，避免单任务超时
_GB_VIDEO_THRESHOLD_MB = 50


def _should_use_multipart_pipeline(doc_id: str) -> bool:
    """判断是否应走 GB 视频多任务管线（P2-D）。

    条件：
        1. doc_type 属于视频/音频类型
        2. file_size 超过 _GB_VIDEO_THRESHOLD_MB（50MB，即超过普通上传限制）

    查询失败时返回 False（降级走普通管线，保证可用性）。

    Args:
        doc_id: 文档 ID。

    Returns:
        True 如果应走多任务管线。
    """
    try:
        import asyncio

        async def _check() -> bool:
            from app.database import task_db_session
            from app.repositories.knowledge_repository import DocumentRepository

            async with task_db_session() as db:
                # 使用 DocumentRepository 替代裸 select，自动应用软删除过滤；
                # 此处按主键查找，无需租户过滤（仅判断文档类型与大小）
                repo = DocumentRepository(db)
                doc = await repo.get_by_id(uuid.UUID(doc_id))
                if not doc:
                    return False

                doc_type = (doc.doc_type or "").lower()
                if doc_type not in _VIDEO_TYPES and doc_type not in _AUDIO_TYPES:
                    return False

                # file_size 以字节存储，转为 MB 比较
                file_size_mb = (doc.file_size or 0) / (1024 * 1024)
                return file_size_mb > _GB_VIDEO_THRESHOLD_MB

        return asyncio.run(_check())
    except Exception:
        logger.debug("video_multipart.check_failed", doc_id=doc_id)
        return False


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
    doc_id: str | None = None,
) -> list[Chunk]:
    """使用 SemanticChunker 对文档执行四级优先级分块。

    策略优先级：
    0. 内容类型路由（content_type 显式指定时）；
    1. 结构化分块：按 Markdown 标题或 HTML 标签分割（带标题路径锚点）；
    2. 语义分块：TextTiling 相似度算法，在话题边界分割；
    3. 父子索引：小块检索、大块上下文；
    4. 固定长度兜底：512 tokens 固定分割。

    P1-B 确定性 ID：传入 doc_id 时启用确定性 chunk ID，支持幂等写入。

    注意：视频文档的分块在 _process_video_pipeline 中通过
    chunker.chunk_video_transcript() 处理，不走本函数。

    Args:
        text: 待分块的纯文本内容。
        doc_type: 文档类型（md / html / docx / pdf / txt 等）。
        content_type: 内容类型标签（auto / faq / tutorial / specification / report / plain）。
        doc_id: 文档 ID（P1-B）。传入时启用确定性 chunk ID。

    Returns:
        Chunk 对象列表，每个 Chunk 包含 content、title_path、content_type、
        chunk_strategy 等元数据。
    """
    if not text or not text.strip():
        return []

    chunker = SemanticChunker()
    chunks = chunker.chunk(text, doc_type=doc_type, content_type=content_type, doc_id=doc_id)
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
    from app.database import task_db_session
    from app.models.audit import AuditFlow
    from app.repositories.base import BaseRepository

    async with task_db_session() as session:
        # AuditFlow 模型无 tenant_id 列，BaseRepository._apply_tenant_filter
        # 会自动跳过租户过滤，无需传入 tenant_id
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
    from app.database import task_db_session
    from app.repositories.knowledge_repository import DocumentRepository

    doc_uuid = uuid.UUID(doc_id)

    async with task_db_session() as session:
        # 先无租户过滤地查找文档，获取其 tenant_id
        repo = DocumentRepository(session)
        doc = await repo.get_by_id(doc_uuid)
        if doc is None:
            logger.warning("document.publish_not_found", doc_id=doc_id)
            return
        # 后续操作使用租户感知的仓储，确保多租户数据隔离
        repo = DocumentRepository(session, tenant_id=doc.tenant_id)

        doc.status = "published"
        await session.commit()
        logger.info("document.published", doc_id=doc_id)


async def _build_indexes(
    doc_id: str,
    chunk_objects: list[Chunk],
    chunks: list[str],
    embeddings: list[list[float]],
    kb_id: str | None = None,
    images: list[tuple[bytes, str]] | None = None,
) -> None:
    """构建全文索引和向量索引 — 延迟导入。

    传递 Chunk 对象以索引 richer 元数据（title_path、content_type、chunk_strategy）。

    Args:
        doc_id: 文档 ID。
        chunk_objects: Chunk 对象列表（含元数据）。
        chunks: 文本块内容列表（chunk_objects 中提取的 .content）。
        embeddings: 向量嵌入列表。
        kb_id: 文档所属知识库 ID — 写入索引供检索端按知识库过滤。
        images: P2-Step3 图片数据列表 [(二进制, VLM描述), ...] —
            CROSS_MODAL_ENABLED 时向量化入库，实现跨模态检索。
    """
    # 构建全文索引（OpenSearch）— 传入 Chunk 元数据
    try:
        await _build_opensearch_index(doc_id, chunk_objects, kb_id=kb_id)
    except Exception as exc:
        logger.warning("opensearch.index_failed", doc_id=doc_id, error=str(exc))

    # 构建向量索引（通过 VectorStoreBase 适配器，默认 OpenSearch k-NN，可选 Milvus）
    try:
        await _build_vector_index(doc_id, chunk_objects, embeddings, kb_id=kb_id)
    except Exception as exc:
        logger.warning("vector.index_failed", doc_id=doc_id, error=str(exc))

    # P2-Step3: 跨模态图片向量索引（CROSS_MODAL_ENABLED 时生效）
    if images:
        try:
            await _build_cross_modal_index(doc_id, kb_id=kb_id, images=images)
        except Exception as exc:
            logger.warning("cross_modal.index_failed", doc_id=doc_id, error=str(exc))


async def _build_knowledge_graph(
    doc_id: str,
    chunk_objects: list[Chunk],
    doc: Any,
    images: list[tuple[bytes, str]] | None = None,
) -> int:
    """构建知识图谱 — 从 chunk_objects 提取三元组并写入 Neo4j。

    方向二：计算复用 — 直接使用文档处理流水线已分块的 chunk_objects，
    避免重复分块计算。GraphService.extract_triples_from_chunks 从同一批
    chunks 抽取三元组，与 _build_indexes 共享分块结果。

    P3: 同时创建 Image 图片节点，建立 Document → Image 的 CONTAINS 关系。

    降级策略：
        - Neo4j 不可用：GraphService 内部降级到 PostgreSQL 全文检索
        - LLM 不可用：仅使用规则提取（正则匹配，零成本）
        - 模块未启用：调用方应先检查 graph_enabled，此处不重复检查

    Args:
        doc_id: 文档 ID。
        chunk_objects: Chunk 对象列表（计算复用）。
        doc: Document ORM 实例（用于获取 tenant_id 等元数据）。
        images: P3 图片数据列表 [(二进制, VLM描述), ...] — 创建 Image 节点。

    Returns:
        提取的三元组数量。
    """
    try:
        from app.services.graph_service import get_graph_service
    except ImportError:
        logger.info("document.graph_service_unavailable", doc_id=doc_id)
        return 0

    try:
        service = get_graph_service()
    except Exception as exc:
        logger.info(
            "document.graph_service_init_failed",
            doc_id=doc_id,
            error=str(exc),
        )
        return 0

    # 获取 LLM Provider（可选，不可用时仅用规则提取）
    llm_provider = None
    try:
        from app.llm.factory import get_llm_provider

        llm_provider = get_llm_provider()
    except Exception:
        pass  # LLM 不可用时仅用规则提取

    # 方向二：从 chunk_objects 提取三元组（计算复用）
    triples = await service.extract_triples_from_chunks(
        chunks=chunk_objects,
        doc_id=doc_id,
        llm_provider=llm_provider,
    )

    # 失效推荐缓存（文档图谱已更新）
    try:
        await service.invalidate_recommend_cache(doc_id)
    except Exception as exc:
        logger.debug(
            "document.graph_cache_invalidate_failed",
            doc_id=doc_id,
            error=str(exc),
        )

    # P3: 创建图片节点 — 将文档中的图片作为 Image 节点写入图谱
    if images:
        try:
            img_count = await service.add_image_nodes(doc_id, images)
            if img_count > 0:
                logger.info("document.graph_images_added", doc_id=doc_id, count=img_count)
        except Exception as exc:
            logger.debug(
                "document.graph_images_failed",
                doc_id=doc_id,
                error=str(exc)[:200],
            )

    logger.info(
        "document.graph_built",
        doc_id=doc_id,
        triples_count=len(triples),
        chunk_count=len(chunk_objects),
    )
    return len(triples)


async def _build_opensearch_index(
    doc_id: str, chunk_objects: list[Chunk], kb_id: str | None = None
) -> None:
    """构建 OpenSearch 全文索引 — 延迟导入。

    存储 Chunk 元数据（title_path、content_type、chunk_strategy）以支持
    检索时按内容类型过滤和标题路径展示。

    P2-Step1: 写入 kb_id 字段，与检索端 kb_id 过滤对齐。

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
                            "kb_id": {"type": "keyword"},
                            "content_type": {"type": "keyword"},
                            "chunk_strategy": {"type": "keyword"},
                            "token_count": {"type": "integer"},
                        }
                    }
                },
            )

        # 批量索引文档块（含元数据）— 单次 bulk 请求替代逐 chunk 的 N 次 HTTP
        # 往返，降低索引构建阶段的网络开销（O(n) 次调用 → 1 次）。
        # 以确定性 chunk.id 作为 OpenSearch _id — bulk index action 指定 _id 即
        # upsert 语义，重复任务执行覆盖同一文档而非产生重复条目（幂等）。
        if chunk_objects:
            ndjson_lines: list[str] = []
            for chunk in chunk_objects:
                ndjson_lines.append(
                    json.dumps({"index": {"_index": index_name, "_id": chunk.id}})
                )
                doc_body: dict[str, Any] = {
                    "doc_id": doc_id,
                    "chunk_id": chunk.id,
                    "parent_id": chunk.parent_id,
                    "content": chunk.content,
                    "title_path": chunk.title_path,
                    "kb_id": kb_id or getattr(chunk, "kb_id", None) or "",  # C5 fix: 不回退 doc_id，避免知识库过滤失效
                    "content_type": chunk.content_type,
                    "chunk_strategy": chunk.chunk_strategy,
                    "token_count": chunk.token_count,
                }
                ndjson_lines.append(json.dumps(doc_body, ensure_ascii=False))
            response = await client.bulk(body="\n".join(ndjson_lines) + "\n")
            if response.get("errors"):
                failed = sum(
                    1
                    for item in response.get("items", [])
                    if item.get("index", {}).get("error")
                )
                logger.warning(
                    "opensearch.bulk_partial_error", doc_id=doc_id, failed_count=failed
                )
                raise RuntimeError(
                    f"OpenSearch bulk 索引部分失败: {failed}/{len(chunk_objects)}"
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
    kb_id: str | None = None,
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
        kb_id: 文档所属知识库 ID — 写入向量库供检索端按知识库过滤。

    Returns:
        成功写入的向量数量。
    """
    if not embeddings:
        logger.info("vector.index_skipped", doc_id=doc_id, reason="no embeddings")
        return 0

    try:
        from app.rag.vector_store import get_vector_store

        store = get_vector_store()
        count = await store.upsert(doc_id, chunk_objects, embeddings, kb_id=kb_id)
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


async def _build_cross_modal_index(
    doc_id: str,
    kb_id: str | None = None,
    images: list[tuple[bytes, str]] | None = None,
) -> int:
    """P2-Step3: 构建跨模态图片向量索引 — 将图片直接向量化入库。

    当 CROSS_MODAL_ENABLED=True 且文档包含图片时，使用 jina-clip-v2
    将图片嵌入到与文本相同的向量空间，实现文本→图片跨模态检索。

    Args:
        doc_id: 文档 ID。
        kb_id: 文档所属知识库 ID。
        images: 图片数据列表 [(图片二进制, VLM描述), ...]。

    Returns:
        成功写入的图片向量数量。
    """
    if not images:
        return 0

    try:
        from app.services.cross_modal_service import CrossModalService

        service = CrossModalService()
        if not service.is_enabled():
            return 0
        count = await service.embed_and_store_images(doc_id, kb_id, images)
        logger.info("cross_modal.indexed", doc_id=doc_id, image_count=count)
        return count
    except Exception as exc:
        logger.warning("cross_modal.index_failed", doc_id=doc_id, error=str(exc))
        return 0


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
