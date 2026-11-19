"""D10 验收闸门 — sources/引用卡片资产出口＋presigned 下发时解析。

契约（task.md D10 默认值）：
    - sources/引用卡片扩展 chunk_type＋asset_url；
    - 图片 asset_url＝下发时解析的 presigned URL（存储层不存签名 URL）；
    - 表格 content/snippet＝完整 HTML 不截断；
    - 签名失败保留 minio:// 原址、不炸流（正向证人）；
    - text chunk 行为不回归（content[:200]、snippet 截断契约不变）；
    - 资产块不得被父子回溯吞并（检索命中图片块 → 出口仍是图片块）。

真实签名闸门走本机 MinIO（docker compose minio 服务，凭据同 backend/.env），
上传/签名/匿名 GET 全真实；其余闸门在 get_presigned_url 边界打替身。
httpx 客户端一律 trust_env=False —— 本机 NO_PROXY 含 ::1 条目会炸
httpx 环境代理解析（既有环境勘误桶，非产品缺陷）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import get_settings
from app.rag.citation import _SNIPPET_MAX, CitationExtractor
from app.rag.engine import AgenticRAGEngine
from app.rag.retriever import HybridRetriever
from app.utils.minio_client import delete_file, upload_file

# ==================================================================
# 夹具
# ==================================================================

_VLM_DESC = "三层架构示意图：接入层、服务层、存储层"


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
    assert len(html) > 200, "表格样本必须超过 200 字符才能检验不截断契约"
    return html


def _img_doc(ref: str, **kw) -> dict:
    doc = {
        "doc_id": "doc-1",
        "chunk_id": "chunk-img",
        "title": "架构文档 > 总览",
        "content": _VLM_DESC,
        "score": 0.9,
        "chunk_type": "image",
        "asset_ref": ref,
        "parent_id": "chunk-parent",
    }
    doc.update(kw)
    return doc


def _table_doc(**kw) -> dict:
    html = _table_html()
    doc = {
        "doc_id": "doc-1",
        "chunk_id": "chunk-table",
        "title": "架构文档 > 指标",
        # content 与 asset_ref 刻意不同源 — asset_ref 才是完整 HTML 权威源，
        # 出口必须取 asset_ref（断言两侧不许同源）
        "content": "表格数据摘要（完整 HTML 在 asset_ref）",
        "score": 0.8,
        "chunk_type": "table",
        "asset_ref": html,
        "parent_id": "chunk-parent",
    }
    doc.update(kw)
    return doc


def _text_doc(**kw) -> dict:
    doc = {
        "doc_id": "doc-1",
        "chunk_id": "chunk-text",
        "title": "架构文档 > 正文",
        "content": "正文段落。" * 100,
        "score": 0.7,
    }
    doc.update(kw)
    return doc


def _mock_signer(monkeypatch, url: str = "https://signed.example/x") -> AsyncMock:
    sign = AsyncMock(return_value=url)
    monkeypatch.setattr("app.utils.minio_client.get_presigned_url", sign)
    return sign


class _MockComponent:
    async def search(self, *a, **kw):
        return []

    async def rerank(self, *a, **kw):
        return []

    async def generate(self, *a, **kw):
        return ""

    async def call_tool(self, *a, **kw):
        return {}


class _CapturingLLM:
    async def chat(self, messages, stream=False, **kw):
        yield "generate"


def _make_engine() -> AgenticRAGEngine:
    return AgenticRAGEngine(
        llm=_CapturingLLM(),
        mcp_client=_MockComponent(),
        retriever=_MockComponent(),
        reranker=_MockComponent(),
        generator=_MockComponent(),
    )


async def _collect_events(state: dict) -> list:
    engine = _make_engine()
    return [ev async for ev in engine._emit_sources_events(state)]


# ==================================================================
# enrich_asset_docs — 下发时资产解析（不改原 docs）
# ==================================================================


class TestD10Enrich:
    @pytest.mark.asyncio
    async def test_image_asset_url_presigned(self, monkeypatch):
        from app.rag.asset_sources import enrich_asset_docs

        sign = _mock_signer(monkeypatch)
        doc = _img_doc("minio://bkt/d10/a.png")
        out = await enrich_asset_docs([doc])
        assert out[0]["asset_url"] == "https://signed.example/x"
        sign.assert_awaited_once_with("bkt", "d10/a.png", get_settings().MINIO_PRESIGNED_EXPIRES_SECONDS)
        assert "asset_url" not in doc, "不得污染原 docs（快照/缓存不存签名 URL）"

    @pytest.mark.asyncio
    async def test_table_content_full_html(self, monkeypatch):
        from app.rag.asset_sources import enrich_asset_docs

        _mock_signer(monkeypatch)
        html = _table_html()
        out = await enrich_asset_docs([_table_doc()])
        assert out[0]["content"] == html
        assert out[0].get("asset_url") is None

    @pytest.mark.asyncio
    async def test_text_unchanged_witness(self, monkeypatch):
        from app.rag.asset_sources import enrich_asset_docs

        _mock_signer(monkeypatch)
        doc = _text_doc()
        out = await enrich_asset_docs([doc])
        assert out[0]["content"] == doc["content"]
        assert out[0].get("asset_url") is None
        assert out[0].get("chunk_type", "text") == "text"

    @pytest.mark.asyncio
    async def test_presign_failure_keeps_original(self, monkeypatch):
        """正向证人：签名失败保留 minio:// 原址，不抛异常。"""
        from app.rag.asset_sources import enrich_asset_docs

        monkeypatch.setattr(
            "app.utils.minio_client.get_presigned_url",
            AsyncMock(side_effect=RuntimeError("minio down")),
        )
        ref = "minio://bkt/d10/a.png"
        out = await enrich_asset_docs([_img_doc(ref)])
        assert out[0]["asset_url"] == ref

    @pytest.mark.asyncio
    async def test_image_without_ref(self, monkeypatch):
        from app.rag.asset_sources import enrich_asset_docs

        _mock_signer(monkeypatch)
        out = await enrich_asset_docs([_img_doc(None)])
        assert out[0].get("asset_url") is None


# ==================================================================
# 真实 MinIO 签名 — 上传→presign→匿名 GET 全真实
# ==================================================================


class TestD10RealMinIO:
    @pytest.mark.asyncio
    async def test_real_presigned_url_accessible(self):
        from app.rag.asset_sources import enrich_asset_docs

        bucket = get_settings().MINIO_BUCKET
        obj = f"d10-gate/{uuid.uuid4().hex}.png"
        data = _png_bytes()
        ref = await upload_file(
            bucket=bucket, object_name=obj, data=data, content_type="image/png"
        )
        try:
            out = await enrich_asset_docs([_img_doc(ref)])
            url = out[0]["asset_url"]
            assert url.startswith("http://"), "presigned 必须是 HTTP URL"
            # E3 后恒为 SigV4（X-Amz-Signature=）；本闸保留 V2/V4 宽判据
            # 检验「真实签名存在」，V4 专属判据在 test_e3_sigv4_presign.py
            assert "X-Amz-Signature=" in url or "Signature=" in url, "必须带真实签名"
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(url)
            assert resp.status_code == 200, "presigned URL 必须可匿名访问"
            assert resp.content == data, "取回字节必须与上传一致"
        finally:
            await delete_file(bucket, obj)


# ==================================================================
# build_sources_payload — sources 事件契约
# ==================================================================


class TestD10SourcesPayload:
    @pytest.mark.asyncio
    async def test_image_source_fields(self, monkeypatch):
        from app.rag.asset_sources import build_sources_payload, enrich_asset_docs

        _mock_signer(monkeypatch)
        enriched = await enrich_asset_docs([_img_doc("minio://bkt/d10/a.png")])
        sources = build_sources_payload(enriched)
        s = sources[0]
        assert s["id"] == "chunk-img"
        assert s["title"] == "架构文档 > 总览"
        assert s["score"] == 0.9
        assert s["chunk_type"] == "image"
        assert s["asset_url"] == "https://signed.example/x"
        assert s["content"] == _VLM_DESC[:200]

    @pytest.mark.asyncio
    async def test_table_source_not_truncated(self, monkeypatch):
        from app.rag.asset_sources import build_sources_payload, enrich_asset_docs

        _mock_signer(monkeypatch)
        html = _table_html()
        enriched = await enrich_asset_docs([_table_doc()])
        s = build_sources_payload(enriched)[0]
        assert s["chunk_type"] == "table"
        assert s["content"] == html, "表格 HTML 必须完整下发（>200 字符不截断）"
        assert s["asset_url"] is None

    @pytest.mark.asyncio
    async def test_text_source_truncated_witness(self, monkeypatch):
        """正向证人：text 契约不回归 — content[:200]、无资产字段值。"""
        from app.rag.asset_sources import build_sources_payload, enrich_asset_docs

        _mock_signer(monkeypatch)
        doc = _text_doc()
        enriched = await enrich_asset_docs([doc])
        s = build_sources_payload(enriched)[0]
        assert s["content"] == doc["content"][:200]
        assert s["chunk_type"] == "text"
        assert s["asset_url"] is None


# ==================================================================
# 引擎出口 — _emit_sources_events（run() 的 sources/citations 段）
# ==================================================================


class TestD10EngineEmit:
    @pytest.mark.asyncio
    async def test_sources_event_carries_assets(self, monkeypatch):
        _mock_signer(monkeypatch)
        html = _table_html()
        state = {
            "retrieved_docs": [
                _img_doc("minio://bkt/d10/a.png"),
                _table_doc(),
                _text_doc(),
            ],
            "answer": "架构见[1]，指标见[2]。",
        }
        events = await _collect_events(state)
        sources_ev = [e for e in events if e.event == "sources"]
        assert len(sources_ev) == 1
        sources = sources_ev[0].data["sources"]
        assert [s["chunk_type"] for s in sources] == ["image", "table", "text"]
        assert sources[0]["asset_url"] == "https://signed.example/x"
        assert sources[1]["content"] == html
        assert "asset_url" not in state["retrieved_docs"][0], "原 docs 不得被写入签名 URL"

    @pytest.mark.asyncio
    async def test_citations_event_carries_assets(self, monkeypatch):
        _mock_signer(monkeypatch)
        html = _table_html()
        state = {
            "retrieved_docs": [
                _img_doc("minio://bkt/d10/a.png"),
                _table_doc(),
            ],
            "answer": "架构见[1]，指标见[2]。",
        }
        events = await _collect_events(state)
        cit_ev = [e for e in events if e.event == "citations"]
        assert len(cit_ev) == 1
        cards = cit_ev[0].data["citations"]
        assert len(cards) == 2
        assert cards[0]["chunk_type"] == "image"
        assert cards[0]["asset_url"] == "https://signed.example/x"
        assert cards[1]["chunk_type"] == "table"
        assert cards[1]["snippet"] == html, "表格卡片 snippet 必须完整 HTML"
        assert state.get("citations") == cards

    @pytest.mark.asyncio
    async def test_text_only_no_regression(self, monkeypatch):
        _mock_signer(monkeypatch)
        doc = _text_doc()
        state = {
            "retrieved_docs": [doc],
            "answer": "结论见[1]。",
        }
        events = await _collect_events(state)
        sources = [e for e in events if e.event == "sources"][0].data["sources"]
        assert sources[0]["content"] == doc["content"][:200]
        assert sources[0]["chunk_type"] == "text"
        assert sources[0]["asset_url"] is None
        cards = [e for e in events if e.event == "citations"][0].data["citations"]
        assert cards[0]["snippet"] == doc["content"][:_SNIPPET_MAX] + "..."
        assert cards[0]["chunk_type"] == "text"
        assert cards[0]["asset_url"] is None

    @pytest.mark.asyncio
    async def test_empty_docs_no_events(self):
        events = await _collect_events({"retrieved_docs": [], "answer": "x"})
        assert events == []

    @pytest.mark.asyncio
    async def test_presign_failure_stream_intact(self, monkeypatch):
        """正向证人：签名失败不炸流 — sources 照常下发，asset_url 保留原址。"""
        monkeypatch.setattr(
            "app.utils.minio_client.get_presigned_url",
            AsyncMock(side_effect=RuntimeError("minio down")),
        )
        ref = "minio://bkt/d10/a.png"
        state = {
            "retrieved_docs": [_img_doc(ref)],
            "answer": "见图[1]。",
        }
        events = await _collect_events(state)
        sources = [e for e in events if e.event == "sources"][0].data["sources"]
        assert sources[0]["asset_url"] == ref
        assert sources[0]["chunk_type"] == "image"


# ==================================================================
# CitationExtractor — 卡片资产字段＋表格 snippet 不截断
# ==================================================================


class TestD10CitationExtractor:
    def test_card_carries_asset_fields(self):
        extractor = CitationExtractor()
        source = _img_doc("minio://bkt/a.png", asset_url="https://signed.example/x")
        cards = extractor.extract("见[1]", [source])
        assert cards[0]["chunk_type"] == "image"
        assert cards[0]["asset_url"] == "https://signed.example/x"

    def test_table_snippet_full_html(self):
        extractor = CitationExtractor()
        html = _table_html()
        # extract 读的是出口就绪副本（enrich 后 content=完整 HTML）
        cards = extractor.extract("见[1]", [_table_doc(content=html)])
        assert cards[0]["snippet"] == html

    def test_text_snippet_truncated_witness(self):
        extractor = CitationExtractor()
        doc = _text_doc()
        cards = extractor.extract("见[1]", [doc])
        assert cards[0]["snippet"] == doc["content"][:_SNIPPET_MAX] + "..."
        assert cards[0]["chunk_type"] == "text"
        assert cards[0]["asset_url"] is None


# ==================================================================
# 检索侧接线 — 资产块不被父子回溯吞并＋BM25 路携带资产字段
# ==================================================================


class _FakeStore:
    def __init__(self, parents: dict):
        self._parents = parents
        self.fetched: list[list[str]] = []

    async def fetch_by_ids(self, ids):
        self.fetched.append(list(ids))
        return {k: v for k, v in self._parents.items() if k in ids}


def _retriever_with_parents(parents: dict) -> HybridRetriever:
    r = HybridRetriever(vector_store=_FakeStore(parents))
    return r


class TestD10RetrieverAssets:
    @pytest.mark.asyncio
    async def test_asset_chunk_skips_parent_backtrack(self):
        """图片/表格块命中后必须原样保留（内容＋类型），不得回溯成父块正文。"""
        parents = {
            "chunk-parent": {"content": "父块整章原文", "title_path": "父章"}
        }
        r = _retriever_with_parents(parents)
        img = _img_doc("minio://bkt/a.png", chunk_id="chunk-img")
        text_child = _text_doc(chunk_id="chunk-child", parent_id="chunk-parent")
        out = await r._expand_to_parents([img, text_child])
        by_id = {d["chunk_id"]: d for d in out}
        assert "chunk-img" in by_id, "资产块不得被父块去重吞并"
        assert by_id["chunk-img"]["content"] == _VLM_DESC
        assert by_id["chunk-img"]["chunk_type"] == "image"
        assert by_id["chunk-img"]["asset_ref"] == "minio://bkt/a.png"
        # 既有契约：回溯后保留子块 chunk_id，仅替换 content（2026-11-19 校准）
        assert by_id["chunk-child"]["content"] == "父块整章原文"
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_table_chunk_skips_backtrack_too(self):
        parents = {"chunk-parent": {"content": "父块整章原文", "title_path": "父章"}}
        r = _retriever_with_parents(parents)
        table = _table_doc()
        out = await r._expand_to_parents([table])
        assert len(out) == 1
        assert out[0]["chunk_type"] == "table"
        assert out[0]["content"] == table["content"], "回溯不得替换资产块内容"

    @pytest.mark.asyncio
    async def test_text_child_backtrack_witness(self):
        """正向证人：普通子块回溯契约不回归。"""
        parents = {
            "chunk-parent": {"content": "父块整章原文", "title_path": "父章"}
        }
        r = _retriever_with_parents(parents)
        child = _text_doc(chunk_id="chunk-child", parent_id="chunk-parent")
        out = await r._expand_to_parents([child])
        assert len(out) == 1
        assert out[0]["content"] == "父块整章原文"
        assert out[0]["expanded_from_child"] is True

    def test_fulltext_parse_carries_asset_fields(self):
        html = _table_html()
        data = {
            "hits": {
                "hits": [
                    {
                        "_id": "chunk-table",
                        "_score": 1.5,
                        "_source": {
                            "doc_id": "doc-1",
                            "chunk_id": "chunk-table",
                            "content": html,
                            "title_path": "指标",
                            "kb_id": "kb1",
                            "parent_id": "chunk-parent",
                            "chunk_type": "table",
                            "asset_ref": html,
                        },
                    }
                ]
            }
        }
        out = HybridRetriever._parse_opensearch_results(data, 10)
        assert out[0]["chunk_type"] == "table"
        assert out[0]["asset_ref"] == html

    @pytest.mark.asyncio
    async def test_fulltext_source_requests_asset_fields(self, monkeypatch):
        r = HybridRetriever()
        captured: dict = {}

        async def fake_post(url, json=None):
            captured["payload"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"hits": {"hits": []}},
            )

        monkeypatch.setattr(r._http, "post", fake_post)
        await r._fulltext_search("指标", ["kb1"], 10)
        await r.close()
        assert "chunk_type" in captured["payload"]["_source"]
        assert "asset_ref" in captured["payload"]["_source"]