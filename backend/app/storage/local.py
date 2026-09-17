"""
本地文件系统存储 — 单一职责：把对象落到本地目录（默认/开发/单机模式）。

路径安全：key 仅允许相对路径片段（字母数字 / _ - .），
拒绝绝对路径与 ``..`` 穿越，防止路径注入。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO

from app.config import get_settings
from app.storage.base import StorageBackend, StorageError, StorageObject

# key 允许的字符：字母数字 / _ - . （相对路径片段）
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class LocalStorageBackend(StorageBackend):
    """本地文件系统对象存储。"""

    backend_id: str = "local"
    display_name: str = "本地文件系统"

    def __init__(self, base_dir: str | None = None) -> None:
        settings = get_settings()
        self._base_dir = Path(base_dir or settings.OBJECT_STORE_LOCAL_DIR).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_key(key: str) -> str:
        """校验并规范化对象键，防止路径穿越。"""
        if key.startswith("/") or key.startswith("\\") or ":" in key:
            raise StorageError(f"非法对象键（绝对路径）: {key!r}")
        if not _KEY_RE.match(key) or ".." in key.split("/"):
            raise StorageError(f"非法对象键: {key!r}")
        return key

    def _resolve(self, key: str) -> Path:
        safe = self._validate_key(key)
        path = (self._base_dir / safe).resolve()
        # 双保险：解析后仍须位于 base_dir 内
        if not str(path).startswith(str(self._base_dir)):
            raise StorageError(f"对象键越界: {key!r}")
        return path

    async def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str | None = None,
        overwrite: bool = True,
    ) -> str:
        del content_type  # 本地存储不记录 MIME
        path = self._resolve(key)
        if path.exists() and not overwrite:
            raise StorageError(f"对象已存在且不允许覆盖: {key!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            with open(path, "wb") as fh:
                chunk = data.read(1024 * 1024)
                while chunk:
                    fh.write(chunk)
                    chunk = data.read(1024 * 1024)
        return key

    async def get(self, key: str) -> StorageObject:
        path = self._resolve(key)
        if not path.exists() or not path.is_file():
            raise StorageError(f"对象不存在: {key!r}")
        return StorageObject(key=key, data=path.read_bytes())

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return path.exists() and path.is_file()
