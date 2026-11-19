"""D11 存量资产回填 — 对 D6 之前入库的文档强制重解析＋重建资产 chunk。

背景：
    D6 资产化（图片/表格独立资产块）只对新写入路径生效。存量文档的
    content_text 是旧解析产物（图片/表格混在正文里），且 A14 哈希早退
    会让日常重索引直接返回旧文本 — 存量文档永远拿不到资产块。本脚本
    以 PG documents 表为权威源选取目标，逐文档执行：

        1. force_reparse 强制重解析＋重新分块（绕过哈希早退）；
        2. 按 doc_id 删除三个索引的旧条目（全文 / k-NN 向量 / 跨模态）—
           跨模态图片 chunk 是随机 UUID ID，不先删除会累积重复条目；
        3. 重新向量化＋重建索引（含 D6 资产块）。

    不触发审核/发布/智能处理（不走 _finalize），文档状态语义不变。
    幂等：确定性 chunk ID + 先删后建，重复执行安全。

使用方式（backend 目录下）::

    # 预演 — 只列出目标文档，不写入
    python -m scripts.backfill_asset_chunks --dry-run

    # 按知识库回填
    python -m scripts.backfill_asset_chunks --kb-id <UUID>

    # 按租户回填
    python -m scripts.backfill_asset_chunks --tenant-id <UUID>

    # 单文档回填
    python -m scripts.backfill_asset_chunks --doc-id <UUID>

注意：
    - 不做自动全量重索引 — 重解析＋embedding 有成本，必须显式执行。
    - 目标排除：软删除、无源文件（编辑型文档）、视频/音频（ASR 成本）、
      .doc/.ppt（旧格式不支持解析）。
    - VECTOR_STORE=milvus 的部署：本脚本的索引清理仅覆盖 OpenSearch，
      Milvus 存量数据需重新触发文档向量化任务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select

from app.config import get_settings
from app.database import create_task_engine
from app.models.knowledge import Document
from app.utils.logger import get_logger

log = get_logger("scripts.backfill_asset_chunks")

settings = get_settings()

# 视频/音频重解析触发 ASR/VLM 管线，.doc/.ppt 旧格式不支持 — 均排除
_EXCLUDED_DOC_TYPES: frozenset[str] = frozenset(
    {"video", "mp4", "avi", "mov", "mkv",
     "audio", "mp3", "wav", "m4a", "aac", "flac", "ogg", "wma",
     "doc", "ppt"}
)


def _parse_uuid(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise SystemExit(f"参数 {name} 不是合法 UUID: {value}")


async def load_targets(
    kb_id: str | None = None,
    tenant_id: str | None = None,
    doc_id: str | None = None,
) -> list[dict]:
    """从 PG 选取回填目标 — DB 为权威数据源。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engine = create_task_engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            stmt = select(
                Document.id, Document.tenant_id, Document.title, Document.doc_type
            ).where(
                Document.deleted_at.is_(None),
                Document.file_path.isnot(None),
                or_(
                    Document.doc_type.is_(None),
                    Document.doc_type.notin_(_EXCLUDED_DOC_TYPES),
                ),
            )
            if kb_id:
                stmt = stmt.where(Document.kb_id == uuid.UUID(kb_id))
            if tenant_id:
                stmt = stmt.where(Document.tenant_id == uuid.UUID(tenant_id))
            if doc_id:
                stmt = stmt.where(Document.id == uuid.UUID(doc_id))
            rows = (await session.execute(stmt)).all()
            return [
                {
                    "doc_id": str(r[0]),
                    "tenant_id": str(r[1]) if r[1] else None,
                    "title": r[2],
                    "doc_type": r[3],
                }
                for r in rows
            ]
    finally:
        await engine.dispose()


async def _delete_old_entries(doc_id: str) -> None:
    """重建前清理旧索引条目 — 全文/k-NN/跨模态三索引均由生产 helper 覆盖。

    跨模态图片 chunk 用随机 UUID ID（cross_modal_service），upsert 不会
    覆盖旧条目，必须显式 delete_by_query，否则重复回填累积重复图片向量。
    E1 后生产 _delete_document_from_indexes 已含 cross-modal，此处不再补删。
    """
    import tasks.document_tasks as dt

    await dt._delete_document_from_indexes(doc_id)


async def backfill_one(
    doc_id: str, tenant_id: str | None = None, dry_run: bool = False
) -> dict:
    """单文档回填：强制重解析 → 清理旧条目 → 重建索引。"""
    if dry_run:
        return {"doc_id": doc_id, "status": "dry_run"}

    import tasks.document_tasks as dt

    result = await dt._parse_and_chunk_async(
        doc_id, tenant_id=tenant_id, force_reparse=True
    )
    if result.get("status") == "failed":
        return {
            "doc_id": doc_id,
            "status": "failed",
            "error": result.get("error", "解析失败"),
        }

    chunk_objects = result.get("chunk_objects") or []
    await _delete_old_entries(doc_id)
    await dt._build_index_async(
        doc_id, chunk_objects, [c.content for c in chunk_objects]
    )
    assets = sum(
        1 for c in chunk_objects if getattr(c, "chunk_type", "text") in ("image", "table")
    )
    return {
        "doc_id": doc_id,
        "status": "done",
        "chunks": len(chunk_objects),
        "assets": assets,
    }


async def backfill_documents(targets: list[dict], dry_run: bool = False) -> dict:
    """串行执行批次 — 单文档失败隔离，不中断其余文档。

    串行而非常规并发：每个文档要走完整重解析＋embedding 管线，
    并发会同时压垮解析器与向量服务。
    """
    summary = {"done": 0, "failed": 0, "dry_run": 0, "assets": 0}
    for row in targets:
        doc_id = row["doc_id"]
        try:
            result = await backfill_one(doc_id, row.get("tenant_id"), dry_run=dry_run)
        except Exception as exc:
            log.error("backfill.doc_failed", doc_id=doc_id, error=str(exc))
            summary["failed"] += 1
            continue
        status = result["status"]
        if status == "done":
            summary["done"] += 1
            summary["assets"] += result.get("assets", 0)
            log.info(
                "backfill.doc_done",
                doc_id=doc_id,
                chunks=result.get("chunks", 0),
                assets=result.get("assets", 0),
            )
        elif status == "dry_run":
            summary["dry_run"] += 1
        else:
            summary["failed"] += 1
            log.error(
                "backfill.doc_parse_failed", doc_id=doc_id, error=result.get("error")
            )
    return summary


async def main() -> int:
    parser = argparse.ArgumentParser(description="D11 存量文档资产回填")
    parser.add_argument("--kb-id", help="按知识库 UUID 限定范围")
    parser.add_argument("--tenant-id", help="按租户 UUID 限定范围")
    parser.add_argument("--doc-id", help="单文档 UUID")
    parser.add_argument("--dry-run", action="store_true", help="只列出目标不写入")
    args = parser.parse_args()

    kb_id = _parse_uuid(args.kb_id, "--kb-id")
    tenant_id = _parse_uuid(args.tenant_id, "--tenant-id")
    doc_id = _parse_uuid(args.doc_id, "--doc-id")

    targets = await load_targets(kb_id=kb_id, tenant_id=tenant_id, doc_id=doc_id)
    print(f"回填目标文档数: {len(targets)}（dry_run={args.dry_run}）")
    if args.dry_run:
        for row in targets:
            print(f"  {row['doc_id']}  [{row['doc_type']}]  {row['title']}")

    if settings.VECTOR_STORE == "milvus":
        print(
            "警告: VECTOR_STORE=milvus — 本脚本索引清理仅覆盖 OpenSearch，"
            "Milvus 存量数据需重新触发文档向量化任务。"
        )

    summary = await backfill_documents(targets, dry_run=args.dry_run)
    if args.dry_run:
        print(f"dry-run 完成 — 待回填: {summary['dry_run']}")
    else:
        print(
            f"回填完成 — 成功: {summary['done']}, 失败: {summary['failed']}, "
            f"资产块总数: {summary['assets']}"
        )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))