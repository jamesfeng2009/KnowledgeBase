#!/usr/bin/env python
"""指标 2：图谱第四路召回对 Recall@5 的增量贡献（A/B 对比评测）。

两组配置（同一批查询、同一索引）：
  A（四路）  GRAPH_SEARCH_ENABLED=true  + ENTITY_REGISTRY_ENABLED=true
             （向量 + BM25（含同义词扩展） + 跨模态 + 图谱多跳）
  B（双路）  GRAPH_SEARCH_ENABLED=false + ENTITY_REGISTRY_ENABLED=false
             （向量 + BM25 原始查询，即图谱功能上线前的基线）

评测集：--build-dataset 时用 LLM 从 20 个种子文档各生成 2 个自然问题
（qwen-turbo，共 20 次调用，成本可忽略），expected_doc_ids = 该文档 UUID。

用法::

    cd backend && .venv/bin/python scripts/eval_metric2_recall.py --build-dataset
    cd backend && .venv/bin/python scripts/eval_metric2_recall.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session_factory  # noqa: E402
from app.models.knowledge import Document, KnowledgeBase  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

KB_NAME = "简历指标评测库"
DATASET_PATH = Path(_BACKEND_ROOT) / "eval_datasets" / "kg_ablation.jsonl"
HARD_DATASET_PATH = Path(_BACKEND_ROOT) / "eval_datasets" / "kg_ablation_hard.jsonl"

# ---------------------------------------------------------------------------
# 领域实体注册（词汇鸿沟设计）：
#   canonical_name = Neo4j Concept 节点精确名（图谱召回按 name 匹配）
#   synonyms = 文档全文中不出现的口语同义词（构建时校验），用于：
#     a) BM25 同义词扩展（A 臂）  b) 实体识别触发图谱第四路
#   模拟生产上"运营注册领域实体"的运营流程。
# ---------------------------------------------------------------------------
ENTITY_CANDIDATES: list[tuple[str, str, list[str]]] = [
    # (canonical=图谱概念名, 语义说明, 口语同义词)
    ("数据持久化", "存储", ["落盘", "存档"]),
    ("架构模式", "架构", ["架构形态", "选型"]),
    ("Docker Compose", "容器部署", ["容器编排", "编排部署"]),
    ("Kubernetes", "容器编排平台", ["K8s", "集群调度"]),
    ("Grafana", "监控可视化", ["可视化面板"]),
    ("Kafka", "消息中间件", ["消息中间件", "消息管道"]),
    ("CI/CD 流水线", "持续集成交付", ["构建发版", "自动发版"]),
    ("LLM", "大语言模型", ["大模型", "生成式模型"]),
    ("多租户", "租户隔离", ["多客户", "客户隔离"]),
    ("数据迁移", "数据搬迁", ["迁库", "搬库"]),
    ("安全加固", "安全防护", ["防护强化"]),
    ("监控告警", "可观测", ["值班告警"]),
]


async def get_seed_kb() -> KnowledgeBase:
    async with async_session_factory() as session:
        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == KB_NAME).limit(1)
            )
        ).scalars().first()
    if kb is None:
        raise SystemExit("[ERROR] 种子 KB 不存在，先跑 eval_resume_seed.py")
    return kb


async def _load_docs(kb_id) -> list[Document]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Document)
            .where(Document.status == "published", Document.kb_id == kb_id)
            .order_by(Document.title)
        )
        return list(result.scalars().all())


async def register_corpus_entities() -> dict[str, list[str]]:
    """校验并注册领域实体 — 三重校验保证评测严谨性：

    1. canonical 在 Neo4j 中是真实 Concept 节点（有 MENTIONS 指向已发布文档）
    2. 同义词在全部文档全文中不出现（真词汇鸿沟，BM25/向量双路天然失效）
    3. 概念关联 ≥1 个已发布文档（图谱召回可达）

    Returns: {canonical: [关联 doc_id 列表]}
    """
    from app.ontology.entity_registry import EntityDefinition, EntityRegistry, EntityType

    kb = await get_seed_kb()
    docs = await _load_docs(kb.id)
    all_text = "\n".join(d.content_text or "" for d in docs)

    from app.config import get_settings

    s = get_settings()
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD))
    entity_docs: dict[str, list[str]] = {}
    registered = 0
    try:
        for canonical, desc, synonyms in ENTITY_CANDIDATES:
            # 校验 2: 词汇鸿沟（同义词不得出现在任何文档中）
            leaked = [syn for syn in synonyms if syn.lower() in all_text.lower()]
            if leaked:
                print(f"  [SKIP] {canonical} — 同义词出现在文档中: {leaked}")
                continue
            # 校验 1+3: 图谱中存在该 Concept 且关联已发布文档
            q = (
                "MATCH (c:Concept {name: $name})<-[:MENTIONS]-(:DocumentChunk)"
                "-[:HAS_CHUNK]-(d:Document {doc_status: 'published'}) "
                "RETURN DISTINCT d.id AS doc_id, d.title AS title LIMIT 5"
            )
            recs = await driver.execute_query(q, name=canonical, database_="neo4j")
            linked = [(r["doc_id"], r["title"]) for r in recs.records]
            if not linked:
                print(f"  [SKIP] {canonical} — 图谱中无关联已发布文档")
                continue
            EntityRegistry.register(EntityDefinition(
                canonical_name=canonical,
                display_name=canonical,
                entity_type=EntityType.CONCEPT,
                synonyms=synonyms,
                description=desc,
            ))
            entity_docs[canonical] = [doc_id for doc_id, _ in linked]
            registered += 1
            print(
                f"  [REG] {canonical:<18} synonyms={synonyms} → "
                f"{[t[:10] for _, t in linked[:2]]}"
            )
    finally:
        await driver.close()
    print(f"[M2] 注册领域实体 {registered}/{len(ENTITY_CANDIDATES)} 个")
    return entity_docs


async def build_hard_dataset() -> None:
    """生成词汇鸿沟难题集 — 双路基线天然失效、图谱实体桥接可救回。

    约束：问题必须包含同义词、严禁出现 canonical 及文档术语。
    expected_doc_ids = 图谱关联的首个文档（多关联时取主文档）。
    """
    from app.llm.factory import get_llm_provider

    kb = await get_seed_kb()
    entity_docs = await register_corpus_entities()
    docs = await _load_docs(kb.id)
    doc_by_id = {str(d.id): d for d in docs}

    provider = get_llm_provider()
    cases: list[dict] = []
    syn_by_canonical = {c: syns for c, _, syns in ENTITY_CANDIDATES}
    for canonical, doc_ids in entity_docs.items():
        doc = doc_by_id.get(doc_ids[0])
        if doc is None:
            continue
        synonyms = syn_by_canonical[canonical]
        syn_str = "/".join(synonyms)
        prompt = (
            f"你是企业知识库的普通员工，对技术术语不熟悉，只会用口语表达。\n"
            f"请围绕文档《{doc.title}》的主题生成 2 个中文提问。\n"
            f"硬性要求：\n"
            f"1. 每个问题必须原样包含「{syn_str}」中的至少一个词；\n"
            f"2. 严禁出现「{canonical}」这个词或其任何子串；\n"
            f"3. 严禁出现文档标题中的技术名词；\n"
            f"4. 问题自然口语化，像不懂技术的人问的；\n"
            f"5. 只输出 JSON 数组，格式 [\"问题1\", \"问题2\"]。\n\n"
            f"文档标题：{doc.title}"
        )
        out = ""
        async for c in provider.chat(
            [{"role": "user", "content": prompt}], stream=False, max_tokens=200
        ):
            out += c if isinstance(c, str) else ""
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            continue
        try:
            questions = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        for q in questions[:2]:
            if not (isinstance(q, str) and q.strip()):
                continue
            q = q.strip()
            # 程序化校验：含同义词、不含 canonical
            has_syn = any(syn.lower() in q.lower() for syn in synonyms)
            if not has_syn or canonical.lower() in q.lower():
                continue
            cases.append({
                "query": q,
                "expected_doc_ids": [str(doc.id)],
                "kb_ids": [str(kb.id)],
                "tags": ["kg_hard", canonical],
            })
        print(f"  [HARD] {canonical:<18} +{sum(1 for c in cases if c['tags'][1]==canonical)} 题")

    HARD_DATASET_PATH.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8"
    )
    print(f"[M2] 难题集写入 {HARD_DATASET_PATH}（{len(cases)} 条）")


async def build_dataset() -> None:
    """用 LLM 从每个文档生成 2 个自然问题（expected=该文档）。"""
    from app.llm.factory import get_llm_provider

    provider = get_llm_provider()
    kb = await get_seed_kb()
    cases: list[dict] = []
    docs = await _load_docs(kb.id)

    seen: set[str] = set()
    for doc in docs:
        if doc.title in seen:
            continue
        seen.add(doc.title)
        text = (doc.content_text or "")[:1500]
        prompt = (
            "基于以下文档内容，生成 2 个企业员工可能会提出的中文问题，"
            "要求：1) 问题自然口语化，不要直接复述文档标题；"
            "2) 一个问具体细节，一个需要理解内容后归纳；"
            "3) 只输出 JSON 数组，格式 [\"问题1\", \"问题2\"]。\n\n"
            f"文档标题：{doc.title}\n\n文档内容：\n{text}"
        )
        out = ""
        async for c in provider.chat(
            [{"role": "user", "content": prompt}], stream=False, max_tokens=200
        ):
            out += c if isinstance(c, str) else ""
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            print(f"  [SKIP] {doc.title[:30]} — LLM 输出无法解析: {out[:60]}")
            continue
        try:
            questions = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        for q in questions[:2]:
            if isinstance(q, str) and q.strip():
                cases.append({
                    "query": q.strip(),
                    "expected_doc_ids": [str(doc.id)],
                    "kb_ids": [str(kb.id)],
                    "tags": ["kg_ablation", doc.title],
                })
        print(f"  [GEN] {doc.title[:30]:<34} +{min(2, len(questions))} 问题")

    DATASET_PATH.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8"
    )
    print(f"[M2] 数据集写入 {DATASET_PATH}（{len(cases)} 条）")


def recall_at_k(expected: list[str], got: list[str], k: int) -> float:
    topk = {g for g in got[:k]}
    return len(set(expected) & topk) / len(expected) if expected else 0.0


def mrr(expected: list[str], got: list[str]) -> float:
    for i, g in enumerate(got, start=1):
        if g in expected:
            return 1.0 / i
    return 0.0


async def run_arm(retriever, cases: list[dict], kb_id: str, label: str) -> dict:
    recalls, mrrs, graph_hits = [], [], 0
    for case in cases:
        try:
            results = await retriever.search(case["query"], kb_ids=[kb_id], top_k=20)
        except Exception as exc:
            logger.warning("m2.search_error", error=str(exc), query=case["query"][:30])
            results = []
        doc_ids = [r.get("doc_id", "") for r in results]
        recalls.append(recall_at_k(case["expected_doc_ids"], doc_ids, 5))
        mrrs.append(mrr(case["expected_doc_ids"], doc_ids))
        if any(r.get("source") == "graph" for r in results[:5]):
            graph_hits += 1
    n = len(cases) or 1
    return {
        "label": label,
        "recall@5": sum(recalls) / n,
        "mrr": sum(mrrs) / n,
        "graph_in_top5": graph_hits / n,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dataset", action="store_true", help="（重新）生成常规评测数据集")
    parser.add_argument("--build-hard", action="store_true", help="（重新）生成词汇鸿沟难题集")
    args = parser.parse_args()

    if args.build_dataset:
        await build_dataset()
        return 0
    if args.build_hard:
        await build_hard_dataset()
        return 0

    if not DATASET_PATH.exists():
        await build_dataset()
    if not HARD_DATASET_PATH.exists():
        await build_hard_dataset()

    # A 臂前置：注册领域实体（进程内全局），B 臂因 ENTITY_REGISTRY_ENABLED=false 不受影响
    await register_corpus_entities()

    def load(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    easy_cases = load(DATASET_PATH)
    hard_cases = load(HARD_DATASET_PATH)
    kb = await get_seed_kb()
    print(f"[M2] 常规集 {len(easy_cases)} 条 | 难题集 {len(hard_cases)} 条 | KB={kb.name}")

    from app.rag.retriever import HybridRetriever

    settings = get_settings()

    async def arm(graph: bool, entity: bool, label: str, cases: list[dict]) -> dict:
        settings.GRAPH_SEARCH_ENABLED = graph
        settings.ENTITY_REGISTRY_ENABLED = entity
        retriever = HybridRetriever()
        return await run_arm(retriever, cases, str(kb.id), label)

    for set_name, cases in (("常规集（文档术语可直接命中）", easy_cases),
                            ("难题集（词汇鸿沟，仅口语同义词）", hard_cases)):
        # B 先跑（基线），A 后跑 — 每臂新建 retriever，公平对比
        result_b = await arm(graph=False, entity=False, label="B·双路基线(向量+BM25)", cases=cases)
        result_a = await arm(graph=True, entity=True, label="A·四路(+实体扩展+图谱)", cases=cases)

        print(f"\n===== 指标2 · 图谱第四路 A/B 对比 · {set_name} =====")
        for r in (result_b, result_a):
            print(
                f"  {r['label']:<28} Recall@5={r['recall@5']:.4f}  MRR={r['mrr']:.4f}"
                f"  图谱命中Top5占比={r['graph_in_top5']:.2%}"
            )
        delta = result_a["recall@5"] - result_b["recall@5"]
        print(
            f"  Recall@5 增量: {delta:+.4f}（相对提升 {delta/result_b['recall@5']:+.1%}）"
            if result_b["recall@5"] > 0
            else f"  Recall@5 增量: {delta:+.4f}"
        )

    # 恢复默认
    settings.GRAPH_SEARCH_ENABLED = True
    settings.ENTITY_REGISTRY_ENABLED = True
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
