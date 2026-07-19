"""
S3 兼容对象存储客户端 — 文档/图片上传与下载。

封装 ``boto3`` SDK,提供异步友好的 ``upload_file`` / ``download_file`` 接口。
首次调用时自动创建默认 bucket(幂等),避免部署后手动初始化。

设计要点:
    - 标准 S3 API — 使用 boto3 的公开 S3 接口,不依赖任何厂商私有方法,
      可无缝对接 MinIO / RustFS / AWS S3 / 阿里云 OSS 等任何 S3 兼容存储;
    - 同步 SDK 包装为 async — boto3 是同步的,用 ``asyncio.to_thread``
      包装避免阻塞事件循环;
    - 懒初始化 — 首次调用时创建 boto3 client 实例和 bucket;
    - 优雅降级 — ``boto3`` 包未安装时抛 ``ImportError``,调用方已有 fallback 逻辑。

兼容性说明:
    - 原模块名为 ``minio_client``,保留以避免破坏现有 ``from app.utils.minio_client import ...``
      调用方代码;内部实现已从 minio SDK 私有方法切换为 boto3 标准 S3 API。
    - 返回的 URL 前缀仍为 ``minio://`` 以保持与数据库中已有 file_path 记录兼容。
    - 公开接口签名与返回格式完全保持向后兼容。

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
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# 懒初始化的 boto3 S3 client 实例
_client: Any = None
_initialized_buckets: set[str] = set()

# boto3 client 默认 region(MinIO / RustFS 等 S3 兼容存储通常用 us-east-1)
_DEFAULT_REGION = "us-east-1"


def _get_client() -> Any:
    """获取 boto3 S3 client 实例(懒初始化)。

    使用标准 S3 API,可对接任意 S3 兼容存储(MinIO / RustFS / AWS S3)。

    Returns:
        boto3 S3 client 实例。

    Raises:
        ImportError: ``boto3`` 包未安装。
    """
    global _client
    if _client is not None:
        return _client

    import boto3  # type: ignore[import-untyped]

    endpoint = settings.MINIO_ENDPOINT
    access_key = settings.MINIO_ACCESS_KEY
    secret_key = settings.MINIO_SECRET_KEY

    # 规范化 endpoint 为完整 URL(boto3 需要带协议的 endpoint_url)
    if not endpoint.startswith(("http://", "https://")):
        # 默认 MinIO/RustFS 用 9000 端口,HTTPS 判断交给配置(无协议默认 http)
        endpoint = f"http://{endpoint}"

    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=_DEFAULT_REGION,
        # S3 兼容存储通常需要 path-style(避免 virtual-hosted-style DNS 解析问题)
        config=boto3.session.Config(s3={"addressing_style": "path"}),
    )
    log.info("s3.client_initialized", endpoint=endpoint)
    return _client


def _ensure_bucket_sync(bucket: str) -> None:
    """确保 bucket 存在(幂等,首次调用时创建)。

    Args:
        bucket: bucket 名称。
    """
    if bucket in _initialized_buckets:
        return

    client = _get_client()
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        # bucket 不存在,创建它(us-east-1 无需 CreateBucketConfiguration)
        client.create_bucket(Bucket=bucket)
        log.info("s3.bucket_created", bucket=bucket)
    _initialized_buckets.add(bucket)


# ======================================================================
# 基础操作
# ======================================================================


async def upload_file(
    bucket: str,
    object_name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """上传文件到 S3 兼容存储。

    将同步 SDK 调用包装为 async,避免阻塞事件循环。
    首次调用时自动创建 bucket(幂等)。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径(如 ``kb1/doc.pdf``)。
        data: 文件内容字节。
        content_type: MIME 类型。

    Returns:
        可访问的 URL 路径(``minio://{bucket}/{object_name}``)。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 上传失败(网络错误、权限不足等)。
    """
    def _upload() -> str:
        client = _get_client()
        _ensure_bucket_sync(bucket)
        client.put_object(
            Bucket=bucket,
            Key=object_name,
            Body=data,
            ContentType=content_type,
        )
        log.debug(
            "s3.uploaded",
            bucket=bucket,
            object_name=object_name,
            size=len(data),
        )
        # 保留 minio:// 前缀以兼容数据库中已有 file_path 记录
        return f"minio://{bucket}/{object_name}"

    return await asyncio.to_thread(_upload)


async def download_file(bucket: str, object_name: str) -> bytes:
    """从 S3 兼容存储下载文件。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。

    Returns:
        文件内容字节。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 下载失败。
    """
    def _download() -> bytes:
        client = _get_client()
        response = client.get_object(Bucket=bucket, Key=object_name)
        try:
            return response["Body"].read()
        finally:
            # 显式关闭流,释放连接
            response["Body"].close()

    return await asyncio.to_thread(_download)


async def delete_file(bucket: str, object_name: str) -> None:
    """从 S3 兼容存储删除文件。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 删除失败。
    """
    def _delete() -> None:
        client = _get_client()
        client.delete_object(Bucket=bucket, Key=object_name)
        log.debug("s3.deleted", bucket=bucket, object_name=object_name)

    await asyncio.to_thread(_delete)


async def file_exists(bucket: str, object_name: str) -> bool:
    """检查文件是否存在。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。

    Returns:
        True 如果文件存在。

    Raises:
        ImportError: ``boto3`` 包未安装。
    """
    def _exists() -> bool:
        client = _get_client()
        try:
            client.head_object(Bucket=bucket, Key=object_name)
            return True
        except Exception:
            # head_object 对 404 抛 ClientError;其他错误也视为不存在
            return False

    return await asyncio.to_thread(_exists)


# ======================================================================
# P2-A 多段上传(Multipart Upload)— 突破 50MB 限制,支持 GB 级视频
#
# 全部使用 S3 标准 API,与具体存储引擎解耦:
#   - create_multipart_upload  → 初始化,返回 UploadId
#   - upload_part              → 上传分片,返回 ETag
#   - complete_multipart_upload → 合并分片
#   - abort_multipart_upload   → 取消上传,清理分片
#   - list_parts               → 列出已上传分片(断点续传)
#   - list_multipart_uploads   → 列出进行中的上传(孤儿清理)
# ======================================================================


def _strip_etag_quotes(etag: Any) -> str:
    """去除 boto3 返回 ETag 的双引号(S3 协议 quirk)。

    boto3 的 ETag 通常带双引号(如 ``"abc123"``),统一 strip 以保持
    与 minio SDK 原返回格式一致,避免下游对账逻辑歧义。

    用于:
        - ``upload_part`` 返回值规范化(存入 localStorage / 前端对账)
        - ``list_parts`` 返回值规范化(断点续传比对)

    Args:
        etag: 原始 ETag 值(boto3 返回的字符串)。

    Returns:
        去除引号后的 ETag 字符串。
    """
    if not etag:
        return ""
    s = str(etag)
    # 去除首尾双引号(boto3 会带引号,minio SDK 不带)
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s


def _ensure_etag_quotes(etag: Any) -> str:
    """确保 ETag 带双引号 — 符合 S3 CompleteMultipartUpload 规范。

    S3 协议规范要求 CompleteMultipartUpload 请求体的 ETag 字段为 RFC 2616
    entity-tag,必须带双引号。AWS S3 严格服务端不带引号会返回 InvalidPart;
    MinIO 宽松接受两种格式,但为兼容 AWS S3 / RustFS 等严格实现,统一加引号。

    用于:
        - ``complete_multipart_upload`` 提交给 S3 前的格式规范化

    与 ``_strip_etag_quotes`` 配对使用:
        - 内部对账比较用去引号格式(保证两侧一致)
        - 提交给 S3 用带引号格式(符合规范)

    Args:
        etag: ETag 值(可能带引号也可能不带)。

    Returns:
        带双引号的 ETag 字符串。
    """
    if not etag:
        return ""
    s = str(etag)
    # 已带引号则原样返回,否则加引号
    if s.startswith('"') and s.endswith('"'):
        return s
    return f'"{s}"'


async def init_multipart_upload(
    bucket: str,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """初始化多段上传 — 返回 upload_id(P2-A)。

    使用 S3 标准 ``create_multipart_upload`` API,upload_id 用于后续上传分片和合并。
    前端发起上传时调用此接口,拿到 upload_id 后逐片上传。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。
        content_type: MIME 类型。

    Returns:
        upload_id — 多段上传会话 ID。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 初始化失败。
    """
    def _init() -> str:
        client = _get_client()
        _ensure_bucket_sync(bucket)
        response = client.create_multipart_upload(
            Bucket=bucket,
            Key=object_name,
            ContentType=content_type,
        )
        upload_id = response["UploadId"]
        log.info(
            "s3.multipart_init",
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
    """上传单个分片 — 返回分片 ETag(P2-A)。

    分片编号从 1 开始(S3 协议约定),最大 10000。
    每片大小建议 5MB-100MB,最后一片可以小于 5MB。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
        part_number: 分片编号(1-10000)。
        data: 分片内容字节。

    Returns:
        ``{"part_number": int, "etag": str}`` — 合并时需要。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 上传失败。
    """
    def _upload() -> dict[str, Any]:
        client = _get_client()
        response = client.upload_part(
            Bucket=bucket,
            Key=object_name,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        etag = _strip_etag_quotes(response.get("ETag", ""))
        log.debug(
            "s3.part_uploaded",
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
    """合并分片 — 完成多段上传(P2-A)。

    所有分片上传完成后调用,S3 兼容存储将分片合并为完整对象。
    parts 列表必须按 part_number 升序排列(调用方已在 complete 端点排序)。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
        parts: ``[{"part_number": 1, "etag": "..."}, ...]`` —
            内部会转换为 boto3 要求的 ``PartNumber`` / ``ETag`` 格式。

    Returns:
        可访问的 URL 路径(``minio://{bucket}/{object_name}``)。

    Raises:
        ImportError: ``boto3`` 包未安装。
        Exception: 合并失败。
    """
    def _complete() -> str:
        client = _get_client()
        # 转换为 boto3 要求的 MultipartUpload 结构(PascalCase)
        # 关键:ETag 必须带双引号才符合 S3 CompleteMultipartUpload 规范
        # (RFC 2616 entity-tag)。AWS S3 严格服务端不带引号会返回 InvalidPart。
        # 内部对账用去引号格式(_strip_etag_quotes),提交给 S3 用带引号格式。
        boto_parts = [
            {
                "PartNumber": p["part_number"],
                "ETag": _ensure_etag_quotes(p["etag"]),
            }
            for p in parts
        ]
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=object_name,
            UploadId=upload_id,
            MultipartUpload={"Parts": boto_parts},
        )
        log.info(
            "s3.multipart_completed",
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
    """取消多段上传 — 清理已上传的分片(P2-A)。

    用户取消上传时调用,S3 兼容存储删除该 upload_id 下所有已上传分片,
    释放存储空间。失败时仅记录日志,不抛异常(幂等)。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。
    """
    def _abort() -> None:
        client = _get_client()
        try:
            client.abort_multipart_upload(
                Bucket=bucket,
                Key=object_name,
                UploadId=upload_id,
            )
            log.info(
                "s3.multipart_aborted",
                bucket=bucket,
                object_name=object_name,
                upload_id=upload_id,
            )
        except Exception as exc:
            log.warning(
                "s3.abort_failed",
                upload_id=upload_id,
                error=str(exc),
            )

    await asyncio.to_thread(_abort)


async def list_parts(
    bucket: str,
    object_name: str,
    upload_id: str,
) -> list[dict[str, Any]]:
    """列出已上传的分片 — 用于断点续传(P2-A)。

    前端中断后重新上传时,先调用此接口查询已上传分片,
    跳过已传的分片,只上传缺失的部分。

    Args:
        bucket: bucket 名称。
        object_name: 对象存储路径。
        upload_id: 多段上传会话 ID。

    Returns:
        ``[{"part_number": 1, "etag": "...", "size": 10485760}, ...]``
        失败时返回空列表(允许继续上传)。
    """
    def _list() -> list[dict[str, Any]]:
        client = _get_client()
        try:
            response = client.list_parts(
                Bucket=bucket,
                Key=object_name,
                UploadId=upload_id,
            )
            parts = []
            for p in response.get("Parts", []) or []:
                parts.append({
                    "part_number": p.get("PartNumber"),
                    "etag": _strip_etag_quotes(p.get("ETag", "")),
                    "size": p.get("Size"),
                })
            return parts
        except Exception as exc:
            log.warning("s3.list_parts_failed", error=str(exc))
            return []

    return await asyncio.to_thread(_list)


async def list_multipart_uploads(bucket: str) -> list[dict[str, Any]]:
    """列出所有进行中的多段上传 — 用于孤儿分片清理(P1 加固)。

    定时清理任务调用此接口扫描 S3 兼容存储中所有未 complete/abort 的多段上传,
    对超过阈值的调用 ``abort_multipart_upload`` 释放存储空间。

    Args:
        bucket: bucket 名称。

    Returns:
        ``[{"upload_id": "xxx", "object_name": "kb1/title", "initiated": "2026-01-01T00:00:00Z"}, ...]``
        失败时返回空列表(不阻断清理流程)。
    """
    def _list_uploads() -> list[dict[str, Any]]:
        client = _get_client()
        try:
            response = client.list_multipart_uploads(Bucket=bucket)
            uploads = []
            for u in response.get("Uploads", []) or []:
                uploads.append({
                    "upload_id": u.get("UploadId"),
                    "object_name": u.get("Key"),
                    "initiated": u.get("Initiated"),
                })
            return uploads
        except Exception as exc:
            log.warning("s3.list_uploads_failed", error=str(exc))
            return []

    return await asyncio.to_thread(_list_uploads)
