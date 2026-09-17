"""对象存储抽象包 — P1 存储抽象层（本地文件系统 / S3·MinIO 兼容）。"""

from app.storage.base import (
    StorageBackend,
    StorageError,
    StorageObject,
)
from app.storage.factory import get_storage_backend, list_storage_backends

__all__ = [
    "StorageBackend",
    "StorageError",
    "StorageObject",
    "get_storage_backend",
    "list_storage_backends",
]
