"""
向量存储工厂 — 单一职责：根据配置创建向量存储实例。

遵循开闭原则：新增后端只需在此注册映射，无需修改调用方代码。
遵循依赖倒置：调用方通过 get_vector_store() 获取 VectorStoreBase 实例，
不感知具体实现类。

切换方式（环境变量）::

    VECTOR_STORE=os_knn   # 默认 — OpenSearch k-NN
    VECTOR_STORE=milvus   # 可选 — Milvus 向量引擎
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.rag.vector_store.base import VectorStoreBase
from app.rag.vector_store.milvus_store import MilvusVectorStore
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
from app.utils.logger import get_logger

log = get_logger(__name__)

# 后端注册表 — 新增后端只需在此添加映射
_BACKENDS: dict[str, type[VectorStoreBase]] = {
    "os_knn": OpenSearchVectorStore,
    "milvus": MilvusVectorStore,
}

# 默认后端
_DEFAULT_BACKEND: str = "os_knn"


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStoreBase:
    """获取向量存储单例实例 — 根据 VECTOR_STORE 配置选择后端。

    Returns:
        VectorStoreBase 实例（OpenSearchVectorStore 或 MilvusVectorStore）。

    Raises:
        ValueError: VECTOR_STORE 配置值不在支持列表中时抛出。
    """
    settings = get_settings()
    backend: str = getattr(settings, "VECTOR_STORE", _DEFAULT_BACKEND) or _DEFAULT_BACKEND

    if backend not in _BACKENDS:
        raise ValueError(
            f"不支持的向量存储后端: {backend}，支持选项: {list(_BACKENDS.keys())}"
        )

    store_cls = _BACKENDS[backend]
    store = store_cls()
    log.info("vector_store.factory.selected", backend=backend, store=store_cls.__name__)
    return store


def get_supported_backends() -> list[str]:
    """返回支持的向量存储后端列表。"""
    return list(_BACKENDS.keys())


def reset_vector_store_cache() -> None:
    """重置工厂缓存 — 测试场景下切换后端后需要调用。"""
    get_vector_store.cache_clear()
