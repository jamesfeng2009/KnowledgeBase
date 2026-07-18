"""
MinIO 对象存储客户端 — 文档/图片上传与下载。

封装 ``minio`` SDK，提供异步友好的 ``upload_file`` / ``download_file`` 接口。
首次调用时自动创建默认 bucket（幂等），避免部署后手动初始化。

设计要点：
    - 同步 SDK 包装为 async — ``minio`` Python SDK 是同步的，用 ``asyncio.to_thread``
      包装避免阻塞事件循环；
    - 懒初始化 — 首次调用时创建 ``Minio`` 客户端实例和 bucket；
    - 优雅降级 — ``minio`` 包未安装时抛 ``ImportError``，调用方已有 fallback 逻辑。

使用方式::

    from app.utils.minio_client import upload_file

    url = await upload_file(
        bucket="ekb-documents",
        object_name="kb1/doc.pdf",
        data=file_bytes,
        content_type="application/pdf",
    )
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# 懒初始化的 Minio 客户端实例
_client: Any = None
_initialized_buckets: set[str] = set()


def _get_client() -> Any:
    """获取 Minio 客户端实例（懒初始化）。

    Returns:
        Minio 客户端实例。

    Raises:
        ImportError: ``minio`` 包未安装。
    """
    global _client
    if _client is not None:
        return _client

    from minio import Minio  # type: ignore[import-untyped]

    endpoint = settings.MINIO_ENDPOINT
    access_key = settings.MINIO_ACCESS_KEY
    secret_key = settings.MINIO_SECRET_KEY
    secure = endpoint.startswith("https://")

    # 去掉协议前缀
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]

    _client = Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
    log.info("minio.client_initialized", endpoint=endpoint)
    return _client


def _ensure_bucket_sync(bucket: str) -> None:
    """确保 bucket 存在（幂等，首次调用时创建）。

    Args:
        bucket: bucket 名称。
    """
    if bucket in _initialized_buckets:
        return

    client = _get_client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        log.info("minio.bucket_created", bucket=bucket)
    _initialized_buckets.add(bucket)


async def upload_file(
    bucket: str,
    object_name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """上传文件到 MinIO。

    将同步 SDK 调用包装为 async，避免阻塞事件循环。
    首次调用时自动创建 bucket（幂等）。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径（如 ``kb1/doc.pdf``）。
        data: 文件内容字节。
        content_type: MIME 类型。

    Returns:
        可访问的 URL 路径（``minio://{bucket}/{object_name}``）。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 上传失败（网络错误、权限不足等）。
    """
    def _upload() -> str:
        client = _get_client()
        _ensure_bucket_sync(bucket)
        client.put_object(
            bucket_name=bucket,
            object_name=object_name,
            data=BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        log.debug(
            "minio.uploaded",
            bucket=bucket,
            object_name=object_name,
            size=len(data),
        )
        return f"minio://{bucket}/{object_name}"

    return await asyncio.to_thread(_upload)


async def download_file(bucket: str, object_name: str) -> bytes:
    """从 MinIO 下载文件。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。

    Returns:
        文件内容字节。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 下载失败。
    """
    def _download() -> bytes:
        client = _get_client()
        response = client.get_object(bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await asyncio.to_thread(_download)


async def delete_file(bucket: str, object_name: str) -> None:
    """从 MinIO 删除文件。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 删除失败。
    """
    def _delete() -> None:
        client = _get_client()
        client.remove_object(bucket, object_name)
        log.debug("minio.deleted", bucket=bucket, object_name=object_name)

    await asyncio.to_thread(_delete)


async def file_exists(bucket: str, object_name: str) -> bool:
    """检查文件是否存在。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。

    Returns:
        True 如果文件存在。

    Raises:
        ImportError: ``minio`` 包未安装。
    """
    def _exists() -> bool:
        client = _get_client()
        try:
            client.stat_object(bucket, object_name)
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_exists)
