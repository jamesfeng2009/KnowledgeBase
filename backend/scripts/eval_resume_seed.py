#!/usr/bin/env python
"""简历指标测试 — 种子数据入库脚本。

用途：将 test_documents/md/ 下 20 个 Markdown 文档走完整入库流水线
（解析 → 语义分块 → 向量化 → OpenSearch/Milvus 索引 → Neo4j 图谱构建），
为四个简历指标的评测准备数据。

用法::

    cd backend && .venv/bin/python scripts/eval_resume_seed.py [--limit N] [--kb-name XXX]

输出：每个文档的处理结果（chunk 数 / 状态 / 警告）+ Neo4j 图谱统计。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.database import async_session_factory  # noqa: E402
from app.models.billing import Tenant  # noqa: E402
from app.models.knowledge import Document, KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

DOC_DIR = Path(_BACKEND_ROOT).parent / "test_documents" / "md"


async def ensure_tenant(session) -> Tenant:
    """获取或创建 pro 套餐租户（pro 默认启用 knowledge_graph 模块）。"""
    from sqlalchemy import select

    name = "简历指标评测租户"
    result = await session.execute(select(Tenant).where(Tenant.name == name))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=name, plan="pro", max_users=100)
        session.add(tenant)
        await session.flush()
        logger.info("seed.tenant_created", tenant_id=str(tenant.id))
    return tenant


async def ensure_user(session, tenant_id) -> User:
    """获取或创建种子用户（admin）。"""
    from sqlalchemy import select

    email = "eval_seed@local.test"
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            hashed_password="seed_placeholder_not_for_login",
            name="评测种子用户",
            role="admin",
            tenant_id=tenant_id,
        )
        session.add(user)
        await session.flush()
        logger.info("seed.user_created", user_id=str(user.id))
    return user


async def ensure_kb(session, owner_id, tenant_id, name: str) -> KnowledgeBase:
    """获取或创建种子知识库（category 保持 NULL，避免 T4 域兜底干扰指标 4）。"""
    from sqlalchemy import select

    result = await session.execute(
        select(KnowledgeBase).where(KnowledgeBase.name == name)
    )
    kb = result.scalar_one_or_none()
    if kb is None:
        kb = KnowledgeBase(
            name=name, owner_id=owner_id, tenant_id=tenant_id, description="简历指标评测种子库"
        )
        session.add(kb)
        await session.flush()
        logger.info("seed.kb_created", kb_id=str(kb.id), name=name)
    return kb


async def create_document(session, kb_id, owner_id, tenant_id, md_path: Path) -> tuple[Document, bool]:
    """获取或创建 Document（幂等：同名复用，重跑管线可重建索引/图谱）。"""
    from sqlalchemy import select

    title = md_path.stem
    content = md_path.read_text(encoding="utf-8")
    result = await session.execute(
        select(Document)
        .where(Document.kb_id == kb_id, Document.title == title)
        .limit(1)
    )
    doc = result.scalars().first()
    if doc is not None:
        # 已存在：刷新内容后重跑（重建索引与图谱），不新建记录
        doc.content_text = content
        doc.status = "processing"
        await session.flush()
        return doc, False
    doc = Document(
        kb_id=kb_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        title=title,
        doc_type="md",
        content_text=content,
        classification="internal",  # internal → 处理完直接 published
        status="processing",
    )
    session.add(doc)
    await session.flush()
    return doc, True


async def neo4j_stats() -> dict:
    """Neo4j 图谱统计（节点/关系分标签计数）。"""
    try:
        from app.services.graph_service import GraphService

        gs = GraphService()
        stats = await gs.get_stats()
        await gs.close()
        return stats if isinstance(stats, dict) else {"raw": str(stats)[:200]}
    except Exception as exc:
        return {"error": str(exc)[:200]}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个文档（0=全部）")
    parser.add_argument("--kb-name", default="简历指标评测库", help="种子知识库名称")
    args = parser.parse_args()

    md_files = sorted(DOC_DIR.glob("*.md"))
    if args.limit > 0:
        md_files = md_files[: args.limit]
    if not md_files:
        print(f"[ERROR] 未找到种子文档: {DOC_DIR}")
        return 1
    print(f"[SEED] 待入库文档 {len(md_files)} 个（目录: {DOC_DIR}）")

    # 1. 建租户 + 用户 + KB + Document 记录
    doc_ids: list[tuple[str, str]] = []
    async with async_session_factory() as session:
        tenant = await ensure_tenant(session)
        user = await ensure_user(session, tenant.id)
        kb = await ensure_kb(session, user.id, tenant.id, args.kb_name)
        for md in md_files:
            doc, created = await create_document(session, kb.id, user.id, tenant.id, md)
            doc_ids.append((str(doc.id), doc.title))
        await session.commit()
        print(f"[SEED] tenant={tenant.name}({tenant.plan}) KB={kb.name} id={kb.id} docs={len(doc_ids)}")

    # 2. 逐文档跑完整处理流水线（分块→向量化→索引→图谱→发布）
    from tasks.document_tasks import _process_document_async

    ok = fail = 0
    total_chunks = 0
    for doc_id, title in doc_ids:
        try:
            result = await _process_document_async(doc_id)
            chunks = result.get("chunk_count", 0)
            status = result.get("doc_status", "?")
            warnings = result.get("warnings") or []
            total_chunks += chunks
            ok += 1
            warn_str = f" warnings={warnings}" if warnings else ""
            print(f"  [{ok:>2}] {title[:36]:<38} chunks={chunks:>3} status={status}{warn_str}")
        except Exception as exc:
            fail += 1
            print(f"  [FAIL] {title[:36]:<38} error={str(exc)[:120]}")

    print(f"\n[SEED] 完成: ok={ok} fail={fail} total_chunks={total_chunks}")

    # 3. Neo4j 图谱统计
    stats = await neo4j_stats()
    print(f"[SEED] Neo4j stats: {stats}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
