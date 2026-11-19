"""D10: sources/引用卡片资产出口 — presigned 在下发时解析。

存储层/向量库保留 minio:// 规范地址（不过期）；仅在 SSE 出口把图片
asset_ref 签成短期 presigned URL，表格 content 换成完整 HTML（不截断）。
enrich 返回浅拷贝，不污染 state["retrieved_docs"]（快照/缓存不存签名 URL）。
签名失败由 resolve_minio_image_urls 保留原址（A11 契约），不阻断流。
"""

from __future__ import annotations

from typing import Any

from app.document.image_storage import resolve_minio_image_urls

#: 资产块类型 — 检索父子回溯与出口契约共用
ASSET_CHUNK_TYPES = ("image", "table")


async def enrich_asset_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """检索结果 → 出口就绪副本：图片解析 asset_url，表格换完整 HTML。"""
    out: list[dict[str, Any]] = []
    for doc in docs:
        d = dict(doc)
        ctype = d.get("chunk_type") or "text"
        d["chunk_type"] = ctype
        d["asset_url"] = None
        if ctype == "image":
            ref = d.get("asset_ref")
            if ref:
                d["asset_url"] = await resolve_minio_image_urls(ref)
        elif ctype == "table":
            d["content"] = d.get("asset_ref") or d.get("content", "")
        out.append(d)
    return out


def build_sources_payload(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """出口就绪副本 → sources 事件契约（表格不截断，text 保持 [:200]）。"""
    sources: list[dict[str, Any]] = []
    for d in docs:
        ctype = d.get("chunk_type") or "text"
        content = d.get("content", "")
        sources.append(
            {
                "id": d.get("chunk_id", ""),
                "title": d.get("title", d.get("doc_title", "")),
                "content": content if ctype == "table" else content[:200],
                "score": d.get("score", 0.0),
                "chunk_type": ctype,
                "asset_url": d.get("asset_url"),
            }
        )
    return sources