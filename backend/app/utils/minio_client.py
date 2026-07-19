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
        Exception: 删除失败。
    """
    def _exists() -> bool:
        client = _get_client()
        try:
            client.stat_object(bucket, object_name)
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_exists)


# ------------------------------------------------------------------
# P2-A 多段上传（Multipart Upload）— 突破 50MB 限制，支持 GB 级视频
# ------------------------------------------------------------------


async def init_multipart_upload(
    bucket: str,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """初始化多段上传 — 返回 upload_id（P2-A）。

    MinIO 原生多段上传 API，upload_id 用于后续上传分片和合并。
    前端发起上传时调用此接口，拿到 upload_id 后逐片上传。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。
        content_type: MIME 类型。

    Returns:
        upload_id — 多段上传会话 ID。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 初始化失败。
    """
    def _init() -> str:
        client = _get_client()
        _ensure_bucket_sync(bucket)
        upload_id = client._create_multipart_upload(bucket, object_name)
        log.info(
            "minio.multipart_init",
            bucket=bucket,
            object_name=object_name,
            upload_id=upload_id,
        )
        return upload_id

    return await asyncio.to_thread(_init)


async def upload_part(
    bucket: str,
    object_name: str,
    upload_id: str,
    part_number: int,
    data: bytes,
) -> dict[str, Any]:
    """上传单个分片 — 返回分片 ETag（P2-A）。

    分片编号从 1 开始（S3 协议约定），最大 10000。
    每片大小建议 5MB-100MB，最后一片可以小于 5MB。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
        part_number: 分片编号（1-10000）。
        data: 分片内容字节。

    Returns:
        {"part_number": int, "etag": str} — 合并时需要。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 上传失败。
    """
    def _upload() -> dict[str, Any]:
        client = _get_client()
        from io import BytesIO

        result = client._upload_part(
            bucket_name=bucket,
            object_name=object_name,
            upload_id=upload_id,
            part_number=part_number,
            data=BytesIO(data),
            length=len(data),
        )
        etag = result.etag if hasattr(result, "etag") else result
        log.debug(
            "minio.part_uploaded",
            bucket=bucket,
            object_name=object_name,
            part_number=part_number,
            size=len(data),
        )
        return {"part_number": part_number, "etag": etag}

    return await asyncio.to_thread(_upload)


async def complete_multipart_upload(
    bucket: str,
    object_name: str,
    upload_id: str,
    parts: list[dict[str, Any]],
) -> str:
    """合并分片 — 完成多段上传（P2-A）。

    所有分片上传完成后调用，MinIO 将分片合并为完整对象。
    parts 列表必须按 part_number 升序排列。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
        parts: [{"part_number": 1, "etag": "..."}, ...]

    Returns:
        可访问的 URL 路径（``minio://{bucket}/{object_name}``）。

    Raises:
        ImportError: ``minio`` 包未安装。
        Exception: 合并失败。
    """
    def _complete() -> str:
        client = _get_client()
        client._complete_multipart_upload(
            bucket_name=bucket,
            object_name=object_name,
            upload_id=upload_id,
            parts=parts,
        )
        log.info(
            "minio.multipart_completed",
            bucket=bucket,
            object_name=object_name,
            parts=len(parts),
        )
        return f"minio://{bucket}/{object_name}"

    return await asyncio.to_thread(_complete)


async def abort_multipart_upload(
    bucket: str,
    object_name: str,
    upload_id: str,
) -> None:
    """取消多段上传 — 清理已上传的分片（P2-A）。

    用户取消上传时调用，MinIO 删除该 upload_id 下所有已上传分片，
    释放存储空间。失败时仅记录日志，不抛异常（幂等）。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
    """
    def _abort() -> None:
        client = _get_client()
        try:
            client._abort_multipart_upload(
                bucket_name=bucket,
                object_name=object_name,
                upload_id=upload_id,
            )
            log.info(
                "minio.multipart_aborted",
                bucket=bucket,
                object_name=object_name,
                upload_id=upload_id,
            )
        except Exception as exc:
            log.warning(
                "minio.abort_failed",
                upload_id=upload_id,
                error=str(exc),
            )

    await asyncio.to_thread(_abort)


async def list_parts(
    bucket: str,
    object_name: str,
    upload_id: str,
) -> list[dict[str, Any]]:
    """列出已上传的分片 — 用于断点续传（P2-A）。

    前端中断后重新上传时，先调用此接口查询已上传分片，
    跳过已传的分片，只上传缺失的部分。

    Args:
        bucket: MinIO bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。

    Returns:
        [{"part_number": 1, "etag": "...", "size": 10485760}, ...]
        失败时返回空列表（允许继续上传）。
    """
    def _list() -> list[dict[str, Any]]:
        client = _get_client()
        try:
            result = client._list_parts(
                bucket_name=bucket,
                object_name=object_name,
                upload_id=upload_id,
            )
            parts = []
            for p in result or []:
                parts.append({
                    "part_number": p.part_number if hasattr(p, "part_number") else p.get("part_number"),
                    "etag": p.etag if hasattr(p, "etag") else p.get("etag"),
                    "size": p.size if hasattr(p, "size") else p.get("size"),
                })
            return parts
        except Exception as exc:
            log.warning("minio.list_parts_failed", error=str(exc))
            return []

    return await asyncio.to_thread(_list)


async def list_multipart_uploads(bucket: str) -> list[dict[str, Any]]:
    """列出所有进行中的多段上传 — 用于孤儿分片清理（P1 加固）。

    定时清理任务调用此接口扫描 MinIO 中所有未 complete/abort 的多段上传，
    对超过阈值的调用 ``abort_multipart_upload`` 释放存储空间。

    Args:
        bucket: MinIO bucket 名称。

    Returns:
        [{"upload_id": "xxx", "object_name": "kb1/title", "initiated": "2026-01-01T00:00:00Z"}, ...]
        失败时返回空列表（不阻断清理流程）。
    """
    def _list_uploads() -> list[dict[str, Any]]:
        client = _get_client()
        try:
            result = client._list_multipart_uploads(bucket_name=bucket)
            uploads = []
            for u in result or []:
                upload_id = u.upload_id if hasattr(u, "upload_id") else u.get("upload_id")
                obj_name = u.object_name if hasattr(u, "object_name") else u.get("object_name")
                initiated = u.initiated if hasattr(u, "initiated") else u.get("initiated")
                uploads.append({
                    "upload_id": upload_id,
                    "object_name": obj_name,
                    "initiated": initiated,
                })
            return uploads
        except Exception as exc:
            log.warning("minio.list_uploads_failed", error=str(exc))
            return []

    return await asyncio.to_thread(_list_uploads)
