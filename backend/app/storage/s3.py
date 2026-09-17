"""
S3 / MinIO 对象存储 — 单一职责：通过 boto3 实现 StorageBackend 契约。

兼容 AWS S3 与 MinIO（S3_ENDPOINT 指向 MinIO 时走自定义 endpoint）。
boto3 为同步 SDK，方法经 ``asyncio.to_thread`` 包装避免阻塞事件循环。
"""

from __future__ import annotations

from typing import BinaryIO

from app.config import get_settings
from app.storage.base import StorageBackend, StorageError, StorageObject
from app.utils.logger import get_logger

log = get_logger(__name__)

try:  # 依赖可选：未安装 boto3 时 factory 注册期降级
    import boto3
    from botocore.client import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment,misc]
    BotoCoreError = ClientError = Exception  # type: ignore[assignment,misc]


class S3StorageBackend(StorageBackend):
    """S3 / MinIO 兼容对象存储。"""

    backend_id: str = "s3"
    display_name: str = "S3 / MinIO 对象存储"

    def __init__(
        self,
        endpoint: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
    ) -> None:
        if boto3 is None:
            raise RuntimeError("boto3 未安装，无法使用 S3StorageBackend")
        settings = get_settings()
        self._endpoint = endpoint or settings.S3_ENDPOINT or None
        self._bucket = bucket or settings.S3_BUCKET
        self._region = region or settings.S3_REGION
        kwargs: dict = {"region_name": self._region}
        if self._endpoint:
            kwargs["endpoint_url"] = self._endpoint
        if access_key or settings.S3_ACCESS_KEY:
            kwargs["aws_access_key_id"] = access_key or settings.S3_ACCESS_KEY
            kwargs["aws_secret_access_key"] = secret_key or settings.S3_SECRET_KEY
        if self._endpoint:
            kwargs["config"] = BotoConfig(signature_version="s3v4")
        self._client = boto3.client("s3", **kwargs)
        self._bucket_ready = False

    async def _ensure_bucket(self) -> None:
        """幂等创建 bucket（不存在时）。"""
        if self._bucket_ready:
            return
        try:
            await asyncio_to_thread(
                self._client.head_bucket, Bucket=self._bucket
            )
        except Exception:
            try:
                await asyncio_to_thread(self._client.create_bucket, Bucket=self._bucket)
            except Exception as exc:
                raise StorageError(f"S3 bucket 创建失败: {exc}") from exc
        self._bucket_ready = True

    async def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str | None = None,
        overwrite: bool = True,
    ) -> str:
        del overwrite  # S3 PutObject 天然覆盖
        await self._ensure_bucket()
        extra: dict = {}
        if content_type:
            extra["ContentType"] = content_type
        try:
            await asyncio_to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                **extra,
            )
            return key
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 写入失败 {key!r}: {exc}") from exc

    async def get(self, key: str) -> StorageObject:
        try:
            resp = await asyncio_to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
            body = resp["Body"].read()
            return StorageObject(
                key=key,
                data=body,
                content_type=resp.get("ContentType"),
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 读取失败 {key!r}: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio_to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 删除失败 {key!r}: {exc}") from exc

    async def exists(self, key: str) -> bool:
        try:
            await asyncio_to_thread(
                self._client.head_object, Bucket=self._bucket, Key=key
            )
            return True
        except ClientError:
            return False
        except BotoCoreError:
            return False


def asyncio_to_thread(fn, *args, **kwargs):
    """薄封装 — 避免本模块顶层依赖 asyncio（保持导入轻量）。"""
    import asyncio

    return asyncio.to_thread(fn, *args, **kwargs)
