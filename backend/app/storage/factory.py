"""
对象存储工厂 — 单一职责：根据 OBJECT_STORE 配置创建存储后端。

遵循开闭原则：新增后端只需在此注册映射，无需修改调用方。
遵循依赖倒置：调用方通过 get_storage_backend() 获取 StorageBackend，
不感知底层是本地磁盘还是 S3/MinIO。

切换方式（环境变量）::

    OBJECT_STORE=local   # 默认 — 本地文件系统
    OBJECT_STORE=s3      # 可选 — S3 / MinIO 兼容对象存储
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.storage.base import StorageBackend
from app.utils.logger import get_logger

log = get_logger(__name__)

# 后端注册表 — 新增后端只需在此添加映射
_BACKENDS: dict[str, type[StorageBackend]] = {}

try:
    from app.storage.local import LocalStorageBackend

    _BACKENDS["local"] = LocalStorageBackend
except Exception:  # pragma: no cover
    pass

try:
    from app.storage.s3 import S3StorageBackend

    _BACKENDS["s3"] = S3StorageBackend
except Exception:  # pragma: no cover
    pass

_DEFAULT_BACKEND: str = "local"


@lru_cache(maxsize=1)
def get_storage_backend() -> StorageBackend:
    """获取对象存储单例 — 根据 OBJECT_STORE 配置选择后端。"""
    settings = get_settings()
    backend: str = getattr(settings, "OBJECT_STORE", _DEFAULT_BACKEND) or _DEFAULT_BACKEND
    if backend not in _BACKENDS:
        raise ValueError(
            f"不支持的对象存储后端: {backend}，支持选项: {list(_BACKENDS.keys())}"
        )
    store = _BACKENDS[backend]()
    log.info("storage.factory.selected", backend=backend, cls=type(store).__name__)
    return store


def list_storage_backends() -> list[str]:
    """返回支持的对象存储后端列表。"""
    return list(_BACKENDS.keys())


def reset_storage_cache() -> None:
    """重置工厂缓存 — 测试场景下切换后端后需要调用。"""
    get_storage_backend.cache_clear()
