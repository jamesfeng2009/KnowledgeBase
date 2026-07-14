"""
向量存储适配器层 — 支持多后端切换（OpenSearch k-NN / Milvus）。

适配器模式：
    - 默认使用 OpenSearch k-NN（< 500 万向量场景，运维简单，与 BM25 共享集群）；
    - 可选 Milvus（> 500 万向量的大型企业，专用向量引擎）；
    - 通过 VECTOR_STORE 环境变量切换（os_knn / milvus）。

对外暴露 VectorStoreBase 抽象接口与 get_vector_store() 工厂函数，
业务层（检索器 / 文档处理）仅依赖抽象，不感知具体后端实现。

典型用法::

    from app.rag.vector_store import get_vector_store

    store = get_vector_store()
    results = await store.search(query_vec, kb_ids=[...], top_k=20)
    count = await store.upsert(doc_id, chunks, embeddings)
"""

from __future__ import annotations

from app.rag.vector_store.base import VectorStoreBase
from app.rag.vector_store.factory import get_vector_store

__all__ = [
    "VectorStoreBase",
    "get_vector_store",
]
