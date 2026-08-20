"""P4 公网混合检索 — 双源归一化合并排序（POC 骨架）。

只取理念、不动技术栈：本模块为纯函数，无框架依赖，可独立单测。

设计背景（docs/P4_Web_Hybrid_Retrieval_Design.md §6.2）：
    内部向量分（余弦 0~1）与公网搜索分（提供商另一套量纲）不能直接比大小，
    必须先各自 min-max 归一化，再对内部分 × BOOST（体现"内部默认更可信"），
    随后做"保底配额 + 剩余预算全局竞争"，避免固定配额使 boost 形同虚设。
"""

from __future__ import annotations

from typing import TypedDict


class MergedSource(TypedDict):
    """合并排序后的统一条目（internal / web 对齐格式）。"""

    source_type: str          # "internal" | "web"
    title: str
    url_or_doc_path: str      # internal=库内文档路径，web=URL
    snippet: str
    score: float              # 归一化（internal 已乘 boost）后的统一分数


def _min_max_normalize(scores: list[float]) -> list[float]:
    """min-max 归一化到 [0,1]；全等列表返回全 0，避免除以 0。"""
    if not scores:
        return []
    low, high = min(scores), max(scores)
    if high == low:
        return [0.0] * len(scores)
    return [(s - low) / (high - low) for s in scores]


def _uniform_entry(
    hit: dict, source_type: str, normalized_score: float
) -> MergedSource:
    title = hit.get("title") or (hit.get("metadata") or {}).get("title", "")
    return {
        "source_type": source_type,
        "title": title,
        "url_or_doc_path": (
            hit.get("url") or hit.get("url_or_doc_path") or hit.get("doc_id") or ""
        ),
        "snippet": (hit.get("snippet") or hit.get("content") or "")[:200],
        "score": normalized_score,
    }


def merge_and_rank(
    internal_hits: list[dict],
    web_hits: list[dict],
    *,
    k_internal: int = 5,
    k_web: int = 5,
    boost: float = 1.2,
    min_internal: int = 2,
    min_web: int = 2,
    total_budget: int = 6,
) -> list[MergedSource]:
    """内部 + 公网双源，归一化 → 内部 × boost → 保底 + 竞争合并排序。

    参数含义见配置表（docs §8）：
        k_internal/k_web      每源配额上限
        boost                 内部可信加权
        min_internal/min_web  每源保底（保证两个来源证据都出现）
        total_budget          单子课题引用总预算

    hit 字段：
        internal: {"title", "doc_id"/"url_or_doc_path", "content", "score"}
        web:      {"title", "url", "snippet", "score"}
    """
    internal_norm = _min_max_normalize(
        [float(h.get("score", 0.0)) for h in internal_hits]
    )
    web_norm = _min_max_normalize([float(h.get("score", 0.0)) for h in web_hits])

    ranked = [
        _uniform_entry(h, "internal", s * boost)
        for h, s in zip(internal_hits, internal_norm)
    ] + [
        _uniform_entry(h, "web", s)
        for h, s in zip(web_hits, web_norm)
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)

    internal_sorted = [r for r in ranked if r["source_type"] == "internal"]
    web_sorted = [r for r in ranked if r["source_type"] == "web"]

    # 第一刀：每源保底
    picked = internal_sorted[:min_internal] + web_sorted[:min_web]
    # 第二刀：剩余预算在双源候选池里按加权分全局竞争（尊重每源上限）
    flex_pool = internal_sorted[min_internal:k_internal] + web_sorted[min_web:k_web]
    flex_pool.sort(key=lambda x: x["score"], reverse=True)
    picked += flex_pool[: max(0, total_budget - len(picked))]
    picked.sort(key=lambda x: x["score"], reverse=True)
    return picked