"""E2 闸门 — shortcut 通道 sources 必须携带资产字段（D10 备注立案）。

红基线契约：ShortcutHandler._handle_search 的 sources 仅
doc_id/title/score/chunk_id，图片/表格资产到不了前端。
修复后契约：复用 D10 enrich_asset_docs 出口副本 — sources 增
chunk_type/asset_url（图片＝下发时 presigned），表格 content＝完整
HTML 不截断，text 形状不变；生成上下文/缓存不得染指签名 URL。

边界：检索/重排/生成替身（本机 OpenSearch/LLM 未跑）；真实签名闸走
本机 MinIO（同 D10）；其余在 get_presigned_url 边界打替身。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import get_settings
from app.utils.sse import SSEEventType


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（既有环境勘误桶）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def _png_bytes() -> bytes:
    import fitz

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
    pix.clear_with(90)
    return pix.tobytes("png")


def _table_html() -> str:
    rows = "".join(
        f"<tr><td>指标{i}</td><td>数值{i * 111}</td><td>说明文本{i}</td></tr>"
        for i in range(12)
    )
    html = f"<table><tr><th>指标</th><th>数值</th><th>说明</th></tr>{rows}</table>"
    assert len(html) > 200
    return html


def _candidate(chunk_type: str | None = None, **kw) -> dict:
    doc = {
        "doc_id": "doc-1",
        "kb_id": "kb-1",
        "chunk_id": f"chunk-{chunk_type or 'text'}",
        "title": "架构文档",
        "content": "正文摘要",
        "score": 0.9,
        "classification": "internal",
    }
    if chunk_type:
        doc["chunk_type"] = chunk_type
    doc.update(kw)
    return doc


def _mock_signer(monkeypatch, url: str = "https://signed.example/x"):
    monkeypatch.setattr(
        "app.utils.minio_client.get_presigned_url", AsyncMock(return_value=url)
    )


def _make_handler(candidates, *, rerank_fail=False, gen_spy=None):
    from app.intent.shortcut_handler import ShortcutHandler

    handler = ShortcutHandler()

    class _Retriever:
        async def search(self, *a, **kw):
            return [dict(c) for c in candidates]

    class _Reranker:
        async def rerank(self, query, documents, top_k):
            if rerank_fail:
                raise RuntimeError("reranker down")
            return [
                {
                    "index": i,
                    "score": d.get("score", 0.0),
                    "content": d.get("content", ""),
                }
                for i, d in enumerate(documents)
            ]

    class _Generator:
        async def generate(self, **kwargs):
            if gen_spy is not None:
                gen_spy.append(kwargs.get("retrieved_docs"))
            yield "答"

    handler._get_retriever = lambda: _Retriever()
    handler._get_reranker = lambda: _Reranker()
    handler._get_generator = lambda: _Generator()
    return handler


async def _run_search(handler, permission_filter=None) -> list[dict]:
    events = []
    async for ev in handler._handle_search(
        "架构是什么",
        user=MagicMock(),
        db=MagicMock(),
        tenant_id=None,
        kb_ids=["kb-1"],
        memory_context="",
        permission_filter=permission_filter,
    ):
        events.append(ev)
    sources_events = [
        e for e in events if getattr(e, "event", None) == SSEEventType.SOURCES
    ]
    assert len(sources_events) == 1
    return sources_events[0].data["sources"]


class TestE2ShortcutAssets:
    async def test_table_source_full_html(self, monkeypatch):
        _mock_signer(monkeypatch)
        html = _table_html()
        # content 与 asset_ref 刻意不同源 — asset_ref 才是权威源
        cand = _candidate("table", content="表格摘要", asset_ref=html)
        handler = _make_handler([cand])
        sources = await _run_search(handler)
        s = sources[0]
        assert s["chunk_type"] == "table"
        assert s["content"] == html, "表格 HTML 必须完整下发（不截断、不取摘要）"
        assert s["asset_url"] is None

    async def test_image_source_presigned_wiring(self, monkeypatch):
        _mock_signer(monkeypatch)
        cand = _candidate("image", asset_ref="minio://bkt/e2/a.png", content="图片描述")
        handler = _make_handler([cand])
        sources = await _run_search(handler)
        s = sources[0]
        assert s["chunk_type"] == "image"
        assert s["asset_url"] == "https://signed.example/x"

    async def test_text_source_shape_unchanged(self, monkeypatch):
        """正向证人：text 条目保持原形状（无 content 键），仅增资产标记。"""
        _mock_signer(monkeypatch)
        handler = _make_handler([_candidate()])
        sources = await _run_search(handler)
        s = sources[0]
        assert s["chunk_type"] == "text"
        assert s["asset_url"] is None
        assert "content" not in s
        assert s["doc_id"] == "doc-1" and s["chunk_id"] == "chunk-text"

    async def test_generation_context_not_polluted(self, monkeypatch):
        """签名 URL 只在下发出口解析 — 生成上下文保留 minio:// 规范地址。"""
        _mock_signer(monkeypatch)
        gen_spy: list = []
        ref = "minio://bkt/e2/a.png"
        cand = _candidate("image", asset_ref=ref)
        handler = _make_handler([cand], gen_spy=gen_spy)
        await _run_search(handler)
        assert len(gen_spy) == 1
        gen_doc = gen_spy[0][0]
        assert gen_doc["asset_ref"] == ref
        assert "asset_url" not in gen_doc

    async def test_permission_denied_empty_sources(self, monkeypatch):
        """正向证人：权限过滤清空候选 → sources 空（既有安全契约不回归）。"""
        _mock_signer(monkeypatch)
        handler = _make_handler([_candidate("image", asset_ref="minio://b/a.png")])

        async def deny(docs):
            return []

        sources = await _run_search(handler, permission_filter=deny)
        assert sources == []

    async def test_rerank_failure_fallback_keeps_assets(self, monkeypatch):
        _mock_signer(monkeypatch)
        cand = _candidate("table", content="摘要", asset_ref=_table_html())
        handler = _make_handler([cand], rerank_fail=True)
        sources = await _run_search(handler)
        assert sources[0]["chunk_type"] == "table"
        assert sources[0]["content"] == _table_html()


class TestE2RealMinIO:
    async def test_real_presigned_url_accessible(self):
        from app.utils.minio_client import delete_file, upload_file

        bucket = get_settings().MINIO_BUCKET
        obj = f"e2-gate/{uuid.uuid4().hex}.png"
        data = _png_bytes()
        ref = await upload_file(
            bucket=bucket, object_name=obj, data=data, content_type="image/png"
        )
        try:
            handler = _make_handler([_candidate("image", asset_ref=ref)])
            sources = await _run_search(handler)
            url = sources[0]["asset_url"]
            assert url.startswith("http://")
            assert "X-Amz-Signature=" in url or "Signature=" in url
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(url)
            assert resp.status_code == 200
            assert resp.content == data
        finally:
            await delete_file(bucket, obj)
