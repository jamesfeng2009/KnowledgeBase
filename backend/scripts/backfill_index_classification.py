"""P0 密级下推 — 存量向量索引回填 classification 字段。

背景：
    密级下推（召回层 terms 过滤）要求索引文档携带 classification 字段。
    新写入路径（tasks/document_tasks._build_doc_meta）已带该字段，但存量
    索引文档缺失 — strict 模式下会因字段缺失被召回层静默排除。本脚本
    按 PG documents 表为权威源，对 OpenSearch 索引执行 _update_by_query
    补写 classification（只改元数据字段，不动 embedding，无需重新解析）。

覆盖索引：
    - 文本向量 + BM25 索引（settings.OPENSEARCH_INDEX）
    - 跨模态图片索引（settings.OPENSEARCH_CROSS_MODAL_INDEX）

使用方式（backend 目录下）::

    # 预演 — 只统计不写入
    python -m scripts.backfill_index_classification --dry-run

    # 执行回填
    python -m scripts.backfill_index_classification

    # 回填完成后开启 strict 模式（config: CLASSIFICATION_PUSHDOWN_STRICT=true）

注意：
    - VECTOR_STORE=milvus 的部署不支持 update_by_query，本脚本跳过 Milvus
      并输出提示 — Milvus 存量数据需通过重新触发文档向量化任务覆盖。
    - 幂等：重复执行安全（相同值重复写入无副作用）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import create_task_engine
from app.models.knowledge import Document
from app.utils.logger import get_logger

log = get_logger("scripts.backfill_classification")

# _update_by_query 并发上限 — 避免压垮 OpenSearch
_CONCURRENCY: int = 5

settings = get_settings()


async def _load_doc_classifications() -> list[tuple[str, str]]:
    """从 PG 加载全量 (doc_id, classification) — DB 为权威数据源。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engine = create_task_engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Document.id, Document.classification).where(
                    Document.deleted_at.is_(None),
                )
            )
            rows = result.all()
            return [(str(row[0]), row[1] or "internal") for row in rows]
    finally:
        await engine.dispose()


async def _backfill_one(
    client: httpx.AsyncClient,
    index: str,
    doc_id: str,
    classification: str,
    dry_run: bool,
) -> tuple[str, int]:
    """对单个文档在指定索引执行 update_by_query。

    Returns:
        (doc_id, updated_count) — updated 为该索引中实际更新的文档数
        （文本索引通常为 1，未入库文档为 0）。
    """
    if dry_run:
        return doc_id, -1

    url = f"{settings.OPENSEARCH_URL}/{index}/_update_by_query"
    payload = {
        "query": {"term": {"doc_id": doc_id}},
        "script": {
            "source": "ctx._source.classification = params.c",
            "params": {"c": classification},
        },
    }
    resp = await client.post(url, json=payload)
    resp.raise_for_status()
    updated = int(resp.json().get("updated", 0))
    return doc_id, updated


async def main() -> int:
    parser = argparse.ArgumentParser(description="回填索引 classification 字段")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--skip-cross-modal", action="store_true", help="跳过跨模态索引")
    args = parser.parse_args()

    pairs = await _load_doc_classifications()
    log.info("backfill.loaded_from_pg", total=len(pairs), dry_run=args.dry_run)
    print(f"PG 文档总数: {len(pairs)}（dry_run={args.dry_run}）")

    if settings.VECTOR_STORE == "milvus":
        print(
            "警告: VECTOR_STORE=milvus — 本脚本仅覆盖 OpenSearch 索引，"
            "Milvus 存量数据需重新触发文档向量化任务。"
        )

    indexes = [settings.OPENSEARCH_INDEX]
    if not args.skip_cross_modal:
        indexes.append(settings.OPENSEARCH_CROSS_MODAL_INDEX)

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    total_updated = 0
    total_missing = 0  # 索引中不存在（未入库/已清理）的文档数

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def _run(index: str, doc_id: str, classification: str) -> None:
            nonlocal total_updated, total_missing
            async with semaphore:
                try:
                    _, updated = await _backfill_one(
                        client, index, doc_id, classification, args.dry_run
                    )
                except Exception as exc:
                    log.error(
                        "backfill.doc_failed", index=index, doc_id=doc_id, error=str(exc)
                    )
                    return
                if updated == 0:
                    total_missing += 1
                elif updated > 0:
                    total_updated += updated

        tasks = [
            _run(index, doc_id, classification)
            for index in indexes
            for doc_id, classification in pairs
        ]
        await asyncio.gather(*tasks)

    if args.dry_run:
        print(f"dry-run 完成 — 待回填文档对: {len(pairs) * len(indexes)}")
    else:
        print(f"回填完成 — 索引更新条数: {total_updated}, 索引未命中: {total_missing}")
        print(
            "后续步骤: 确认无失败日志后，将环境变量 "
            "CLASSIFICATION_PUSHDOWN_STRICT=true 开启 strict 密级下推。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
