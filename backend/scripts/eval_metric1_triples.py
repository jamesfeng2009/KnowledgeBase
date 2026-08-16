#!/usr/bin/env python
"""指标 1：三元组抽取「规则优先 + LLM 兜底」混合策略收益评测。

测量三个数字：
  A. 规则覆盖率   — 规则提取产出 ≥1 条三元组的 chunk 占比（真实分块 468 chunk）
  B. LLM 调用节省 — 混合策略（llm_fallback_threshold=3，生产默认）相对
                    「每 chunk 纯 LLM」的调用次数降幅（mock 计数，零 API 成本）
  C. 抽样质量对齐 — 随机抽 N 个 chunk 真实调 LLM 抽取，统计规则三元组
                    被 LLM 结果覆盖/重合的比例（成本极低：N 次 qwen-turbo）

用法::

    cd backend && .venv/bin/python scripts/eval_metric1_triples.py [--sample 30] [--no-llm]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from pathlib import Path

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import select  # noqa: E402

from app.database import async_session_factory  # noqa: E402
from app.models.knowledge import Document  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

# 种子 KB ID（eval_resume_seed.py 建的「简历指标评测库」；用名称动态解析）
_SEED_KB_NAME = "简历指标评测库"
_SEED_KB_ID = None


async def load_chunks() -> tuple[list[str], list]:
    """重新加载种子 KB 的文档并分块（分块确定性，与入库时一致；排除历史脏 KB）。"""
    global _SEED_KB_ID
    from tasks.document_tasks import _chunk_document

    from app.models.knowledge import KnowledgeBase

    all_chunks: list = []
    async with async_session_factory() as session:
        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == _SEED_KB_NAME).limit(1)
            )
        ).scalars().first()
        if kb is None:
            raise SystemExit(f"[ERROR] 种子 KB 不存在: {_SEED_KB_NAME}（先跑 eval_resume_seed.py）")
        _SEED_KB_ID = kb.id
        result = await session.execute(
            select(Document)
            .where(Document.status == "published", Document.kb_id == kb.id)
            .order_by(Document.title)
        )
        docs = list(result.scalars().all())
        # 按标题去重（历史重跑可能产生同名文档，取第一条即可）
        seen_titles: set[str] = set()
        unique_docs = []
        for doc in docs:
            if doc.title in seen_titles:
                continue
            seen_titles.add(doc.title)
            unique_docs.append(doc)
        docs = unique_docs
        for doc in docs:
            chunk_objects = _chunk_document(
                doc.content_text or "", doc.doc_type or "md", doc_id=str(doc.id)
            )
            all_chunks.extend(chunk_objects)
    return [str(d.id) for d in docs], all_chunks


def normalize_triple(t: tuple[str, str, str]) -> tuple[str, str, str]:
    """轻量归一化（去空白/小写）用于三元组重合度比较。"""

    def _n(s: str) -> str:
        return "".join(s.split()).lower()

    return (_n(t[0]), _n(t[1]), _n(t[2]))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=30, help="抽样质量对比的 chunk 数（0=跳过）")
    parser.add_argument("--no-llm", action="store_true", help="跳过真实 LLM 抽样")
    args = parser.parse_args()

    from app.services.graph_service import GraphService

    gs = GraphService()

    # ---------- 加载分块 ----------
    doc_ids, chunks = await load_chunks()
    n = len(chunks)
    print(f"[M1] 文档 {len(doc_ids)} 个，chunk {n} 个")

    # ---------- A. 规则覆盖率（真实规则，零 API） ----------
    hit_chunks = 0
    rule_triple_total = 0
    for c in chunks:
        triples = gs._extract_triples_by_rules(c.content or "")
        if triples:
            hit_chunks += 1
            rule_triple_total += len(triples)
    coverage = hit_chunks / n if n else 0.0
    print(f"[M1-A] 规则覆盖率: {hit_chunks}/{n} = {coverage:.1%}（规则三元组共 {rule_triple_total} 条）")

    # ---------- B. LLM 调用节省（mock 计数，不真调 API；按文档分组，对齐生产语义） ----------
    llm_calls = {"count": 0}

    async def _mock_llm(text: str, provider=None):
        llm_calls["count"] += 1
        return [("mock主体", "属于", "mock客体")]

    real_llm = gs._extract_triples_by_llm
    gs._extract_triples_by_llm = _mock_llm
    gs.batch_import_graph = lambda *a, **k: asyncio.sleep(0)  # 不写 Neo4j

    # chunks 按文档分组（生产上 extract_triples_from_chunks 每文档调用一次）
    by_doc: dict[str, list] = {}
    for c in chunks:
        by_doc.setdefault(getattr(c, "doc_id", doc_ids[0]), []).append(c)

    try:
        fake_provider = object()  # 满足 llm_provider 真值判断（mock 替换真正实现）
        # 策略 B（生产现状）：规则优先 + LLM 兜底（threshold=3，全局累计）
        for did, doc_chunks in by_doc.items():
            await gs.extract_triples_from_chunks(
                doc_chunks, doc_id=did, llm_provider=fake_provider,
                use_rules=True, use_llm=True, llm_fallback_threshold=3
            )
        hybrid_calls = llm_calls["count"]

        # 策略 A（纯 LLM 基线）：每个 chunk 都调 LLM
        llm_calls["count"] = 0
        for did, doc_chunks in by_doc.items():
            await gs.extract_triples_from_chunks(
                doc_chunks, doc_id=did, llm_provider=fake_provider,
                use_rules=False, use_llm=True, llm_fallback_threshold=10**9
            )
        pure_llm_calls = llm_calls["count"]
    finally:
        gs._extract_triples_by_llm = real_llm

    saving = 1 - hybrid_calls / pure_llm_calls if pure_llm_calls else 0.0
    print(
        f"[M1-B] LLM 调用({len(by_doc)} 文档): 混合策略 {hybrid_calls} 次 vs 纯 LLM {pure_llm_calls} 次"
        f" → 减少 {saving:.1%}（{pure_llm_calls/len(by_doc):.1f} → {hybrid_calls/len(by_doc):.1f} 次/文档）"
    )

    # ---------- C. 抽样质量对齐（真实 LLM，成本 N 次轻量调用） ----------
    if args.sample > 0 and not args.no_llm:
        from app.llm.factory import get_llm_provider

        random.seed(42)
        sample_chunks = random.sample(chunks, min(args.sample, n))
        provider = get_llm_provider()

        exact = partial = miss = 0
        all_rule = all_llm = all_overlap = 0
        for c in sample_chunks:
            rule_set = {normalize_triple(t) for t in gs._extract_triples_by_rules(c.content or "")}
            llm_set = {normalize_triple(t) for t in await real_llm(c.content or "", provider)}
            overlap = rule_set & llm_set
            all_rule += len(rule_set)
            all_llm += len(llm_set)
            all_overlap += len(overlap)
            if rule_set and rule_set <= llm_set:
                exact += 1
            elif overlap:
                partial += 1
            elif rule_set:
                miss += 1

        sampled = len(sample_chunks)
        print(
            f"[M1-C] 抽样 {sampled} chunk（真实 LLM 对比）："
            f"规则三元组 {all_rule} 条 / LLM 三元组 {all_llm} 条；"
            f"规则完全被 LLM 覆盖 {exact}、部分重合 {partial}、无重合 {miss}"
        )
        if all_rule:
            print(
                f"[M1-C] 聚合重合率: 规则∩LLM {all_overlap}/{all_rule} = {all_overlap/all_rule:.1%}"
                f"（LLM 均产 {all_llm/sampled:.1f} 条/chunk）"
            )

    await gs.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
