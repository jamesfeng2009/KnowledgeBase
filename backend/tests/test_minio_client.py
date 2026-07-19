"""S3 兼容存储客户端测试 — 验证 upload/download/delete/exists 接口。

使用 Mock 避免真实 S3 连接。
测试覆盖 boto3 标准 S3 API 的封装层。
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


class TestMinioClient:
    """S3 兼容存储客户端测试(原模块名 minio_client 保留兼容)。"""

    def test_upload_file_returns_url(self) -> None:
        """upload_file 返回 minio:// URL(保留前缀兼容数据库记录)。"""
        # 重置懒初始化状态
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = set()

        mock_s3 = MagicMock()
        mock_s3.head_bucket.return_value = {}  # bucket 存在

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.upload_file(
                bucket="ekb-documents",
                object_name="kb1/doc.pdf",
                data=b"test content",
                content_type="application/pdf",
            ))

        assert result == "minio://ekb-documents/kb1/doc.pdf"
        mock_s3.put_object.assert_called_once()
        # 验证 boto3 参数格式
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "ekb-documents"
        assert call_kwargs["Key"] == "kb1/doc.pdf"
        assert call_kwargs["Body"] == b"test content"
        assert call_kwargs["ContentType"] == "application/pdf"

    def test_upload_file_creates_bucket_if_missing(self) -> None:
        """bucket 不存在时(head_bucket 抛异常)自动创建。"""
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = set()

        mock_s3 = MagicMock()
        # head_bucket 抛异常表示 bucket 不存在
        mock_s3.head_bucket.side_effect = Exception("404 NoSuchBucket")

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            asyncio.run(mc.upload_file(
                bucket="new-bucket",
                object_name="test.txt",
                data=b"data",
            ))

        mock_s3.create_bucket.assert_called_once_with(Bucket="new-bucket")

    def test_upload_file_bucket_cached(self) -> None:
        """bucket 创建后缓存,不重复检查。"""
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = {"ekb-documents"}  # 已缓存

        mock_s3 = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            asyncio.run(mc.upload_file(
                bucket="ekb-documents",
                object_name="test.txt",
                data=b"data",
            ))

        # head_bucket 不应被调用(已缓存)
        mock_s3.head_bucket.assert_not_called()
        mock_s3.create_bucket.assert_not_called()

    def test_download_file(self) -> None:
        """download_file 返回文件内容(boto3 get_object 返回 Body 流)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_body = MagicMock()
        mock_body.read.return_value = b"file content"

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.download_file("bucket", "obj"))

        assert result == b"file content"
        mock_body.close.assert_called_once()  # 验证流被关闭

    def test_delete_file(self) -> None:
        """delete_file 调用 boto3 delete_object。"""
        import app.utils.minio_client as mc

        mock_s3 = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            asyncio.run(mc.delete_file("bucket", "obj"))

        mock_s3.delete_object.assert_called_once_with(Bucket="bucket", Key="obj")

    def test_file_exists_true(self) -> None:
        """file_exists 文件存在(head_object 成功)返回 True。"""
        import app.utils.minio_client as mc

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 100}

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.file_exists("bucket", "obj"))

        assert result is True

    def test_file_exists_false(self) -> None:
        """file_exists 文件不存在(head_object 抛异常)返回 False。"""
        import app.utils.minio_client as mc

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = Exception("404 Not Found")

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.file_exists("bucket", "obj"))

        assert result is False

    def test_get_client_lazy_init(self) -> None:
        """_get_client 懒初始化 boto3 client。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_boto3.session.Config.return_value = MagicMock()

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            client = mc._get_client()
            assert client is mock_client

        # 第二次调用不重新创建
        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            client2 = mc._get_client()
            assert client2 is mock_client
            # boto3.client 只调用一次
            assert mock_boto3.client.call_count == 1

    def test_strip_etag_quotes(self) -> None:
        """_strip_etag_quotes 去除 boto3 ETag 的双引号(用于内部对账)。"""
        import app.utils.minio_client as mc

        assert mc._strip_etag_quotes('"abc123"') == "abc123"
        assert mc._strip_etag_quotes("abc123") == "abc123"
        assert mc._strip_etag_quotes("") == ""
        assert mc._strip_etag_quotes(None) == ""

    def test_ensure_etag_quotes(self) -> None:
        """_ensure_etag_quotes 确保 ETag 带双引号(用于提交给 S3 complete)。"""
        import app.utils.minio_client as mc

        # 不带引号 → 加引号
        assert mc._ensure_etag_quotes("abc123") == '"abc123"'
        # 已带引号 → 原样返回
        assert mc._ensure_etag_quotes('"abc123"') == '"abc123"'
        # 空值处理
        assert mc._ensure_etag_quotes("") == ""
        assert mc._ensure_etag_quotes(None) == ""

    def test_init_multipart_upload(self) -> None:
        """init_multipart_upload 调用 boto3 create_multipart_upload。"""
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = {"ekb-documents"}

        mock_s3 = MagicMock()
        mock_s3.create_multipart_upload.return_value = {"UploadId": "upload-xxx"}

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.init_multipart_upload(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                content_type="video/mp4",
            ))

        assert result == "upload-xxx"
        mock_s3.create_multipart_upload.assert_called_once()
        call_kwargs = mock_s3.create_multipart_upload.call_args.kwargs
        assert call_kwargs["Bucket"] == "ekb-documents"
        assert call_kwargs["Key"] == "kb1/video.mp4"
        assert call_kwargs["ContentType"] == "video/mp4"

    def test_upload_part(self) -> None:
        """upload_part 调用 boto3 upload_part,返回去引号的 etag。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        # boto3 返回带引号的 ETag
        mock_s3.upload_part.return_value = {"ETag": '"etag-abc"'}

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.upload_part(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
                part_number=1,
                data=b"chunk-data",
            ))

        assert result == {"part_number": 1, "etag": "etag-abc"}  # 去引号
        call_kwargs = mock_s3.upload_part.call_args.kwargs
        assert call_kwargs["PartNumber"] == 1
        assert call_kwargs["UploadId"] == "upload-xxx"
        assert call_kwargs["Body"] == b"chunk-data"

    def test_complete_multipart_upload(self) -> None:
        """complete_multipart_upload 转换为 boto3 PascalCase 格式,ETag 加回双引号。

        模拟真实断点续传场景:前端从 localStorage 读取的 ETag 不带引号
        (因为 upload_part 返回时已 _strip_etag_quotes 去引号),
        提交给 S3 complete 时必须加回双引号才符合 S3 规范。
        """
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.complete_multipart_upload(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
                parts=[
                    {"part_number": 1, "etag": "etag-1"},  # 不带引号(来自 localStorage)
                    {"part_number": 2, "etag": "etag-2"},
                ],
            ))

        assert result == "minio://ekb-documents/kb1/video.mp4"
        call_kwargs = mock_s3.complete_multipart_upload.call_args.kwargs
        # 验证转换为 boto3 的 PascalCase 格式,且 ETag 加回了双引号
        assert call_kwargs["MultipartUpload"]["Parts"] == [
            {"PartNumber": 1, "ETag": '"etag-1"'},
            {"PartNumber": 2, "ETag": '"etag-2"'},
        ]

    def test_complete_multipart_upload_etag_already_quoted(self) -> None:
        """complete 时 ETag 已带引号则原样提交(幂等,不重复加引号)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            asyncio.run(mc.complete_multipart_upload(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
                parts=[
                    {"part_number": 1, "etag": '"etag-1"'},  # 已带引号
                ],
            ))

        call_kwargs = mock_s3.complete_multipart_upload.call_args.kwargs
        # 已带引号的 ETag 原样提交,不会变成 ""etag-1""
        assert call_kwargs["MultipartUpload"]["Parts"] == [
            {"PartNumber": 1, "ETag": '"etag-1"'},
        ]

    def test_abort_multipart_upload(self) -> None:
        """abort_multipart_upload 调用 boto3 abort_multipart_upload。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            asyncio.run(mc.abort_multipart_upload(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
            ))

        mock_s3.abort_multipart_upload.assert_called_once_with(
            Bucket="ekb-documents",
            Key="kb1/video.mp4",
            UploadId="upload-xxx",
        )

    def test_abort_multipart_upload_failure_no_raise(self) -> None:
        """abort 失败时不抛异常(幂等,仅记录日志)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        mock_s3.abort_multipart_upload.side_effect = Exception("network error")

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            # 不应抛异常
            asyncio.run(mc.abort_multipart_upload(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
            ))

    def test_list_parts(self) -> None:
        """list_parts 返回 snake_case 格式(转换自 boto3 PascalCase)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        mock_s3.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 1, "ETag": '"etag-1"', "Size": 10485760},
                {"PartNumber": 2, "ETag": '"etag-2"', "Size": 5242880},
            ]
        }

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.list_parts(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
            ))

        assert result == [
            {"part_number": 1, "etag": "etag-1", "size": 10485760},
            {"part_number": 2, "etag": "etag-2", "size": 5242880},
        ]

    def test_list_parts_failure_returns_empty(self) -> None:
        """list_parts 失败时返回空列表(允许继续上传)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        mock_s3.list_parts.side_effect = Exception("network error")

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.list_parts(
                bucket="ekb-documents",
                object_name="kb1/video.mp4",
                upload_id="upload-xxx",
            ))

        assert result == []

    def test_list_multipart_uploads(self) -> None:
        """list_multipart_uploads 返回 snake_case 格式。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        mock_s3.list_multipart_uploads.return_value = {
            "Uploads": [
                {"UploadId": "uid-1", "Key": "kb1/video1.mp4", "Initiated": "2026-01-01T00:00:00Z"},
                {"UploadId": "uid-2", "Key": "kb1/video2.mp4", "Initiated": "2026-01-02T00:00:00Z"},
            ]
        }

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.list_multipart_uploads(bucket="ekb-documents"))

        assert result == [
            {"upload_id": "uid-1", "object_name": "kb1/video1.mp4", "initiated": "2026-01-01T00:00:00Z"},
            {"upload_id": "uid-2", "object_name": "kb1/video2.mp4", "initiated": "2026-01-02T00:00:00Z"},
        ]

    def test_list_multipart_uploads_failure_returns_empty(self) -> None:
        """list_multipart_uploads 失败时返回空列表(不阻断清理流程)。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_s3 = MagicMock()
        mock_s3.list_multipart_uploads.side_effect = Exception("network error")

        with patch("app.utils.minio_client._get_client", return_value=mock_s3):
            import asyncio
            result = asyncio.run(mc.list_multipart_uploads(bucket="ekb-documents"))

        assert result == []
