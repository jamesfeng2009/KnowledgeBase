#!/usr/bin/env python
"""评测候选池 contexts 回填 — 用真实检索器为既有评测查询预置检索上下文。

将 eval_datasets/*.jsonl（query + kb_ids 格式）转换为 build_eval_datasets.py
所需的候选池格式（case_id + query + contexts）：对每条查询调用 Retriever.search
真实混合检索（Milvus 向量 + OpenSearch 全文），取 top-K chunk 内容作为 contexts。

D_sel rollout 控制变量要求新旧指引共享同一上下文 — contexts 预置后固定，
进化循环不再触达检索层。

用法::

    cd backend && python scripts/backfill_eval_contexts.py \
        --inputs eval_datasets/kg_ablation.jsonl \
        --out eval_cases/candidates.jsonl --top-k 3

输入行格式（兼容 # 注释头）::

    {"query": "...", "kb_ids": ["..."], "expected_doc_ids": ["..."]}

输出行格式::

    {"case_id": "kg_001", "query": "...", "contexts": ["...", "..."]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.rag.retriever import HybridRetriever  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填评测候选池 contexts")
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=["eval_datasets/kg_ablation.jsonl"],
        help="输入评测查询 JSONL（兼容 # 注释行）",
    )
    parser.add_argument(
        "--out", default="eval_cases/candidates.jsonl", help="候选池输出路径"
    )
    parser.add_argument("--top-k", type=int, default=3, help="每查询检索 chunk 数")
    parser.add_argument(
        "--prefix", default="kg", help="case_id 前缀（如 kg / fb）"
    )
    return parser.parse_args()


def load_queries(paths: list[Path]) -> list[dict]:
    """加载查询行（跳过 # 注释与空行）。

    兼容两种输入：
    - 评测查询格式：{"query": ..., "kb_ids": [...], "expected_doc_ids": [...]}
    - export_eval_cases.py 导出格式：{"question": ..., "citations": [...]}
      （kb_ids 从 citations.kb_id 推导，缺失时全局检索）
    """
    queries: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"[跳过] {path.name}:{idx} JSON 解析失败 {exc}")
                    continue
                query = str(raw.get("query") or raw.get("question") or "").strip()
                if not query:
                    continue
                if query in seen:  # 交付链导出可能含重复提问
                    continue
                seen.add(query)
                kb_ids = [str(k) for k in raw.get("kb_ids", []) if k]
                if not kb_ids:
                    kb_ids = [
                        str(c.get("kb_id"))
                        for c in raw.get("citations", [])
                        if isinstance(c, dict) and c.get("kb_id")
                    ]
                queries.append(
                    {
                        "query": query,
                        "kb_ids": kb_ids,
                        "expected_doc_ids": [
                            str(d) for d in raw.get("expected_doc_ids", []) if d
                        ],
                    }
                )
    return queries


async def main() -> int:
    args = parse_args()
    inputs = [
        p if p.is_absolute() else _BACKEND_ROOT / p for p in map(Path, args.inputs)
    ]
    queries = load_queries(inputs)
    if not queries:
        print("错误：输入为空（无有效查询行）")
        return 1

    out_path = (
        Path(args.out) if Path(args.out).is_absolute() else _BACKEND_ROOT / args.out
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    retriever = HybridRetriever()
    hits = 0
    empty = 0
    written = 0

    with out_path.open("w", encoding="utf-8") as f:
        for idx, item in enumerate(queries, start=1):
            query = item["query"]
            try:
                results = await retriever.search(
                    query, kb_ids=item["kb_ids"] or None, top_k=args.top_k
                )
            except Exception as exc:  # noqa: BLE001 — 单条失败不阻断整批
                print(f"[失败] {args.prefix}_{idx:03d} 检索异常：{exc}")
                continue

            # 去重后取 chunk 内容（检索器可能多路返回同一 chunk）
            contexts: list[str] = []
            seen_chunks: set[str] = set()
            retrieved_doc_ids: list[str] = []
            for r in results:
                chunk_id = str(r.get("chunk_id", ""))
                content = str(r.get("content", "")).strip()
                if not content or chunk_id in seen_chunks:
                    continue
                seen_chunks.add(chunk_id)
                contexts.append(content)
                doc_id = str(r.get("doc_id", ""))
                if doc_id and doc_id not in retrieved_doc_ids:
                    retrieved_doc_ids.append(doc_id)

            if not contexts:
                empty += 1
                print(f"[空结果] {args.prefix}_{idx:03d} {query[:30]}")
                continue

            # 质量观测：期望文档是否被召回（不阻断，仅统计）
            expected = set(item["expected_doc_ids"])
            hit = bool(expected & set(retrieved_doc_ids)) if expected else None
            if hit:
                hits += 1
            mark = (
                "hit" if hit else "MISS"
                if hit is not None
                else "n/a"
            )
            print(
                f"{args.prefix}_{idx:03d} [{mark}] {query[:30]} → "
                f"{len(contexts)} chunks, docs={retrieved_doc_ids}"
            )

            record = {
                "case_id": f"{args.prefix}_{idx:03d}",
                "query": query,
                "contexts": contexts,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(
        f"\n完成：{written}/{len(queries)} 条写入 {out_path}"
        f"（召回命中 {hits}，空结果 {empty}）"
    )
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
