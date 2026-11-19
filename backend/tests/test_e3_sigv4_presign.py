"""E3 验收闸门 — presigned URL 必须是 SigV4（AWS 新区域仅接受 V4）。

契约（task.md E3 默认值）：
    - boto3 client 显式 signature_version="s3v4"；
    - presigned URL 携带 X-Amz-Signature=/X-Amz-Algorithm=AWS4-HMAC-SHA256，
      不得再出现 SigV2 裸键 Signature=/Expires=（按 query 键判定，
      避免 "X-Amz-Signature" 包含子串 "Signature" 的恒真陷阱）；
    - V4 URL 经真实 MinIO 匿名 GET 200、字节一致（全真实，无替身）；
    - expires_in 参数语义不回归（X-Amz-Expires 透传）；
    - 正向证人：V4 client 的常规上传/下载（请求头签名）不回归。

真实 MinIO（docker compose minio 服务，凭据同 backend/.env）。
httpx trust_env=False — 本机 NO_PROXY ::1 环境勘误桶。
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import get_settings
from app.utils import minio_client as mc
from app.utils.minio_client import (
    delete_file,
    download_file,
    get_presigned_url,
    upload_file,
)


@pytest.fixture(autouse=True)
def _fresh_client():
    """强制重建 boto3 client — 懒单例可能已被旧配置初始化。"""
    saved = mc._client
    mc._client = None
    yield
    mc._client = saved


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def _png_bytes() -> bytes:
    import fitz

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
    pix.clear_with(70)
    return pix.tobytes("png")


def _query_keys(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


class TestE3SigV4:
    @pytest.mark.asyncio
    async def test_presigned_url_is_sigv4(self):
        url = await get_presigned_url(
            get_settings().MINIO_BUCKET, f"e3-gate/{uuid.uuid4().hex}.png"
        )
        q = _query_keys(url)
        assert "X-Amz-Signature" in q, "必须是 SigV4 签名"
        assert q.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"]
        assert "Signature" not in q, "不得残留 SigV2 裸键 Signature="
        assert "Expires" not in q, "不得残留 SigV2 裸键 Expires="

    @pytest.mark.asyncio
    async def test_real_v4_url_anonymous_get(self):
        bucket = get_settings().MINIO_BUCKET
        obj = f"e3-gate/{uuid.uuid4().hex}.png"
        data = _png_bytes()
        await upload_file(bucket=bucket, object_name=obj, data=data, content_type="image/png")
        try:
            url = await get_presigned_url(bucket, obj)
            assert "X-Amz-Signature" in _query_keys(url)
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(url)
            assert resp.status_code == 200, "V4 presigned URL 必须可匿名访问"
            assert resp.content == data
        finally:
            await delete_file(bucket, obj)

    @pytest.mark.asyncio
    async def test_expires_in_passthrough(self):
        url = await get_presigned_url(
            get_settings().MINIO_BUCKET, f"e3-gate/{uuid.uuid4().hex}.png", 123
        )
        q = _query_keys(url)
        assert q.get("X-Amz-Expires") == ["123"], "expires_in 必须透传到 V4 签名"

    @pytest.mark.asyncio
    async def test_v4_client_upload_download_witness(self):
        """正向证人：V4 client 常规请求签名上传/下载不回归。"""
        bucket = get_settings().MINIO_BUCKET
        obj = f"e3-gate/{uuid.uuid4().hex}.bin"
        data = _png_bytes()
        ref = await upload_file(bucket=bucket, object_name=obj, data=data)
        try:
            assert ref == f"minio://{bucket}/{obj}"
            assert await download_file(bucket, obj) == data
        finally:
            await delete_file(bucket, obj)

    @pytest.mark.asyncio
    async def test_d10_enrich_real_v4(self):
        """D10 出口链路走真实签名后仍是 V4（接线证人）。"""
        from app.rag.asset_sources import enrich_asset_docs

        bucket = get_settings().MINIO_BUCKET
        obj = f"e3-gate/{uuid.uuid4().hex}.png"
        data = _png_bytes()
        ref = await upload_file(bucket=bucket, object_name=obj, data=data, content_type="image/png")
        try:
            out = await enrich_asset_docs(
                [{"chunk_id": "c1", "chunk_type": "image", "asset_ref": ref, "content": "描述"}]
            )
            url = out[0]["asset_url"]
            assert "X-Amz-Signature" in _query_keys(url)
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(url)
            assert resp.status_code == 200
            assert resp.content == data
        finally:
            await delete_file(bucket, obj)