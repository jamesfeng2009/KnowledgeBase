"""对象存储抽象层单测 — P1 存储抽象（本地文件系统 + 工厂）。

S3 后端通过 mock boto3 客户端验证契约（不依赖真实 S3/MinIO）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.storage.base import StorageError
from app.storage.local import LocalStorageBackend


@pytest.fixture
def local(tmp_path) -> LocalStorageBackend:  # noqa: ANN001
    return LocalStorageBackend(base_dir=str(tmp_path / "objects"))


class TestLocalStorage:
    @pytest.mark.asyncio
    async def test_put_get_roundtrip(self, local) -> None:  # noqa: ANN001
        key = await local.put("docs/2026/09/a.pdf", b"pdf-data", "application/pdf")
        assert key == "docs/2026/09/a.pdf"
        obj = await local.get(key)
        assert obj.data == b"pdf-data"
        assert await local.exists(key) is True

    @pytest.mark.asyncio
    async def test_put_file_handle(self, local, tmp_path) -> None:  # noqa: ANN001
        src = tmp_path / "src.bin"
        src.write_bytes(b"x" * 2048)
        with open(src, "rb") as fh:
            await local.put("files/big.bin", fh)
        obj = await local.get("files/big.bin")
        assert len(obj.data) == 2048

    @pytest.mark.asyncio
    async def test_overwrite_false_raises(self, local) -> None:  # noqa: ANN001
        await local.put("dup.txt", b"1")
        with pytest.raises(StorageError):
            await local.put("dup.txt", b"2", overwrite=False)
        assert (await local.get("dup.txt")).data == b"1"

    @pytest.mark.asyncio
    async def test_get_missing_raises(self, local) -> None:  # noqa: ANN001
        with pytest.raises(StorageError):
            await local.get("missing.txt")

    @pytest.mark.asyncio
    async def test_delete_idempotent(self, local) -> None:  # noqa: ANN001
        await local.put("del.txt", b"1")
        await local.delete("del.txt")
        await local.delete("del.txt")  # 不存在时静默
        assert await local.exists("del.txt") is False

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, local) -> None:  # noqa: ANN001
        with pytest.raises(StorageError):
            await local.put("../../etc/passwd", b"x")
        with pytest.raises(StorageError):
            await local.get("a/../b")

    @pytest.mark.asyncio
    async def test_absolute_key_rejected(self, local) -> None:  # noqa: ANN001
        with pytest.raises(StorageError):
            await local.put("/etc/hosts", b"x")


class TestStorageFactory:
    def test_factory_lists_backends(self) -> None:
        from app.storage.factory import list_storage_backends

        assert "local" in list_storage_backends()

    def test_factory_default_local(self) -> None:
        from app.storage.factory import get_storage_backend, reset_storage_cache

        reset_storage_cache()
        with patch("app.storage.factory.get_settings") as mock_settings:
            mock_settings.return_value.OBJECT_STORE = "local"
            backend = get_storage_backend()
            assert backend.backend_id == "local"
        reset_storage_cache()


class TestS3Storage:
    def _make_s3(self, client: MagicMock) -> MagicMock:
        from app.storage.s3 import S3StorageBackend

        with patch("app.storage.s3.boto3") as mock_boto, patch(
            "app.storage.s3.get_settings"
        ) as mock_settings:
            mock_boto.client.return_value = client
            s = mock_settings.return_value
            s.S3_ENDPOINT = "http://minio:9000"
            s.S3_BUCKET = "ekb"
            s.S3_ACCESS_KEY = "ak"
            s.S3_SECRET_KEY = "sk"
            s.S3_REGION = "cn"
            s3 = S3StorageBackend()
        return s3

    @pytest.mark.asyncio
    async def test_put_creates_bucket_then_object(self) -> None:
        client = MagicMock()
        client.head_bucket.side_effect = Exception("not found")
        s3 = self._make_s3(client)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("app.storage.s3.asyncio_to_thread", _to_thread):
            key = await s3.put("d/1.txt", b"hi", "text/plain")

        assert key == "d/1.txt"
        client.create_bucket.assert_called_once_with(Bucket="ekb")
        client.put_object.assert_called_once_with(
            Bucket="ekb", Key="d/1.txt", Body=b"hi", ContentType="text/plain"
        )

    @pytest.mark.asyncio
    async def test_get_and_exists(self) -> None:
        client = MagicMock()
        client.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"data"),
            "ContentType": "text/plain",
        }
        client.head_object.return_value = {}
        s3 = self._make_s3(client)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("app.storage.s3.asyncio_to_thread", _to_thread):
            obj = await s3.get("d/1.txt")
            assert obj.data == b"data"
            assert obj.content_type == "text/plain"
            assert await s3.exists("d/1.txt") is True

    @pytest.mark.asyncio
    async def test_get_missing_raises_storage_error(self) -> None:
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
        )
        s3 = self._make_s3(client)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("app.storage.s3.asyncio_to_thread", _to_thread):
            with pytest.raises(StorageError):
                await s3.get("missing")
