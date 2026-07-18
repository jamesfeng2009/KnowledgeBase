"""MinIO 客户端测试 — 验证 upload/download/delete/exists 接口。

使用 Mock 避免真实 MinIO 连接。
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
    """MinIO 客户端测试。"""

    def test_upload_file_returns_url(self) -> None:
        """upload_file 返回 minio:// URL。"""
        # 重置懒初始化状态
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = set()

        mock_minio = MagicMock()
        mock_minio.bucket_exists.return_value = True

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            result = asyncio.run(mc.upload_file(
                bucket="ekb-documents",
                object_name="kb1/doc.pdf",
                data=b"test content",
                content_type="application/pdf",
            ))

        assert result == "minio://ekb-documents/kb1/doc.pdf"
        mock_minio.put_object.assert_called_once()

    def test_upload_file_creates_bucket_if_missing(self) -> None:
        """bucket 不存在时自动创建。"""
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = set()

        mock_minio = MagicMock()
        mock_minio.bucket_exists.return_value = False

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            asyncio.run(mc.upload_file(
                bucket="new-bucket",
                object_name="test.txt",
                data=b"data",
            ))

        mock_minio.make_bucket.assert_called_once_with("new-bucket")

    def test_upload_file_bucket_cached(self) -> None:
        """bucket 创建后缓存，不重复检查。"""
        import app.utils.minio_client as mc
        mc._client = None
        mc._initialized_buckets = {"ekb-documents"}  # 已缓存

        mock_minio = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            asyncio.run(mc.upload_file(
                bucket="ekb-documents",
                object_name="test.txt",
                data=b"data",
            ))

        # bucket_exists 不应被调用（已缓存）
        mock_minio.bucket_exists.assert_not_called()

    def test_download_file(self) -> None:
        """download_file 返回文件内容。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_response = MagicMock()
        mock_response.read.return_value = b"file content"

        mock_minio = MagicMock()
        mock_minio.get_object.return_value = mock_response

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            result = asyncio.run(mc.download_file("bucket", "obj"))

        assert result == b"file content"

    def test_delete_file(self) -> None:
        """delete_file 调用 remove_object。"""
        import app.utils.minio_client as mc

        mock_minio = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            asyncio.run(mc.delete_file("bucket", "obj"))

        mock_minio.remove_object.assert_called_once_with("bucket", "obj")

    def test_file_exists_true(self) -> None:
        """file_exists 文件存在返回 True。"""
        import app.utils.minio_client as mc

        mock_minio = MagicMock()
        mock_minio.stat_object.return_value = MagicMock()

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            result = asyncio.run(mc.file_exists("bucket", "obj"))

        assert result is True

    def test_file_exists_false(self) -> None:
        """file_exists 文件不存在返回 False。"""
        import app.utils.minio_client as mc

        mock_minio = MagicMock()
        mock_minio.stat_object.side_effect = Exception("not found")

        with patch("app.utils.minio_client._get_client", return_value=mock_minio):
            import asyncio
            result = asyncio.run(mc.file_exists("bucket", "obj"))

        assert result is False

    def test_get_client_lazy_init(self) -> None:
        """_get_client 懒初始化。"""
        import app.utils.minio_client as mc
        mc._client = None

        mock_minio_class = MagicMock()
        mock_instance = MagicMock()
        mock_minio_class.return_value = mock_instance

        with patch.dict("sys.modules", {"minio": MagicMock(Minio=mock_minio_class)}):
            client = mc._get_client()
            assert client is mock_instance

        # 第二次调用不重新创建
        with patch.dict("sys.modules", {"minio": MagicMock(Minio=mock_minio_class)}):
            client2 = mc._get_client()
            assert client2 is mock_instance
            # Minio 构造函数只调用一次
            assert mock_minio_class.call_count == 1
