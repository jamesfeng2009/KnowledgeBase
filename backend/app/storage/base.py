"""
对象存储抽象接口 — 单一职责：定义统一的对象读写契约。

P1 存储抽象层：文档附件、导入文件、导出产物等二进制对象不再直接依赖
本地磁盘路径，统一通过 StorageBackend 存取，可在本地文件系统与
S3/MinIO 兼容对象存储之间切换（环境变量 OBJECT_STORE=local|s3）。

遵循依赖倒置：业务层依赖本抽象，不感知具体存储实现；
遵循开闭原则：新增后端只需继承 StorageBackend 并在 factory 注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


class StorageError(Exception):
    """对象存储操作失败（读写/删除/连接错误统一包装）。"""


@dataclass
class StorageObject:
    """对象读回结果。"""

    key: str
    data: bytes
    content_type: str | None = None


class StorageBackend(ABC):
    """对象存储抽象基类 — put / get / delete / exists 四项契约。"""

    backend_id: str = "base"
    display_name: str = "基础存储"

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str | None = None,
        overwrite: bool = True,
    ) -> str:
        """写入对象，返回可访问的 key/URL。

        Args:
            key: 对象键（如 "docs/2026/09/uuid.pdf"）。
            data: 二进制内容或文件句柄。
            content_type: MIME 类型（可选）。
            overwrite: False 时若 key 已存在则抛 StorageError。

        Returns:
            对象键（与入参一致，便于调用方落库）。
        """
        ...

    @abstractmethod
    async def get(self, key: str) -> StorageObject:
        """读取对象 — 不存在时抛 StorageError。"""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """删除对象 — 不存在时静默成功（幂等）。"""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """判断对象是否存在。"""
        ...
