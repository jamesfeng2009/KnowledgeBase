"""Qdrant 向量存储单测 — P1 存储抽象层扩展。

测试策略：
    - 客户端工厂：mock app.rag.vector_store.qdrant_store.QdrantClient，
      验证 __init__ 使用配置 URL / api_key / collection；
    - 契约方法：mock to_thread 内部调用，验证 payload 构造、过滤、
      结果格式与异常降级（不依赖真实 Qdrant 服务）；
    - 纯函数：_validate_key / payload 构造直接用真实实例验证。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.vector_store.qdrant_store import QdrantVectorStore


def _make_chunk(cid: str, content: str = "内容") -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, content=content, kb_id="kb1", title_path="t", parent_id=None
    )


@pytest.fixture
def store() -> QdrantVectorStore:
    with patch("app.rag.vector_store.qdrant_store.QdrantClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        with patch("app.rag.vector_store.qdrant_store.get_settings") as mock_settings:
            s = mock_settings.return_value
            s.QDRANT_URL = "http://qdrant:6333"
            s.QDRANT_API_KEY = "k"
            s.QDRANT_COLLECTION = "ekb_documents"
            st = QdrantVectorStore()
            yield st


class TestQdrantInit:
    def test_init_uses_config(self) -> None:
        with patch("app.rag.vector_store.qdrant_store.QdrantClient") as mock_cls, patch(
            "app.rag.vector_store.qdrant_store.get_settings"
        ) as mock_settings:
            s = mock_settings.return_value
            s.QDRANT_URL = "http://qdrant:6333"
            s.QDRANT_API_KEY = "secret"
            s.QDRANT_COLLECTION = "coll"
            QdrantVectorStore()
            mock_cls.assert_called_once_with(
                url="http://qdrant:6333", api_key="secret"
            )

    def test_missing_dependency_raises(self) -> None:
        with patch("app.rag.vector_store.qdrant_store.QdrantClient", None):
            with pytest.raises(RuntimeError):
                QdrantVectorStore()


class TestQdrantContract:
    @pytest.mark.asyncio
    async def test_search_formats_results(self, store) -> None:  # noqa: ANN001
        point = SimpleNamespace(
            id="p1",
            score=0.92,
            payload={
                "doc_id": "d1", "chunk_id": "c1", "content": "内容",
                "kb_id": "kb1", "title_path": "标题", "parent_id": None,
            },
        )
        resp = SimpleNamespace(points=[point])

        async def _fake_thread(fn, *args, **kwargs):
            return resp

        store._ready = True
        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            results = await store.search([0.1, 0.2], kb_ids=["kb1"], top_k=5)

        assert len(results) == 1
        r = results[0]
        assert r["doc_id"] == "d1"
        assert r["chunk_id"] == "c1"
        assert r["content"] == "内容"
        assert r["score"] == 0.92
        assert r["source"] == "vector"
        assert r["kb_id"] == "kb1"

    @pytest.mark.asyncio
    async def test_search_failure_returns_empty(self, store) -> None:  # noqa: ANN001
        async def _raise(*args, **kwargs):
            raise RuntimeError("qdrant down")

        store._ready = True
        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _raise):
            assert await store.search([0.1]) == []

    @pytest.mark.asyncio
    async def test_upsert_writes_points_with_payload(self, store) -> None:  # noqa: ANN001
        calls: list = []

        async def _fake_thread(fn, *args, **kwargs):
            calls.append(kwargs)
            return None

        store._ready = True
        chunks = [_make_chunk("c1"), _make_chunk("c2")]
        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            n = await store.upsert(
                "d1", chunks, [[0.1], [0.2]], kb_id="kb1",
                doc_meta={"series_id": "s1", "depth": 2, "doc_status": "published"},
            )

        assert n == 2
        upsert_call = next(c for c in calls if "points" in c)
        points = upsert_call["points"]
        assert [p.id for p in points] == ["c1", "c2"]
        assert points[0].payload["doc_id"] == "d1"
        assert points[0].payload["kb_id"] == "kb1"
        assert points[0].payload["series_id"] == "s1"
        assert points[0].payload["depth"] == "2"

    @pytest.mark.asyncio
    async def test_upsert_no_chunks_returns_zero(self, store) -> None:  # noqa: ANN001
        assert await store.upsert("d1", [], []) == 0

    @pytest.mark.asyncio
    async def test_delete_filters_by_doc_id(self, store) -> None:  # noqa: ANN001
        calls: list = []

        async def _fake_thread(fn, *args, **kwargs):
            calls.append(kwargs)
            return None

        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            await store.delete("doc-9")

        delete_call = next(c for c in calls if "points_selector" in c)
        points_selector = delete_call["points_selector"]
        assert points_selector.must[0].key == "doc_id"
        assert points_selector.must[0].match.any == ["doc-9"]

    @pytest.mark.asyncio
    async def test_fetch_by_ids(self, store) -> None:  # noqa: ANN001
        point = SimpleNamespace(
            id="c1",
            payload={"chunk_id": "c1", "content": "内容", "title_path": "t", "doc_id": "d1"},
        )

        async def _fake_thread(fn, *args, **kwargs):
            return [point]

        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            out = await store.fetch_by_ids(["c1"])

        assert out["c1"]["content"] == "内容"
        assert out["c1"]["title_path"] == "t"

    @pytest.mark.asyncio
    async def test_health_check(self, store) -> None:  # noqa: ANN001
        async def _fake_thread(fn, *args, **kwargs):
            return True

        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            assert await store.health_check() is True


class TestQdrantEnsureCollection:
    @pytest.mark.asyncio
    async def test_creates_collection_when_missing(self, store) -> None:  # noqa: ANN001
        calls: list = []

        async def _fake_thread(fn, *args, **kwargs):
            if "vectors_config" in kwargs:
                calls.append("create_collection")
                return None
            # collection_exists(collection_name=...) 或 (collection)
            calls.append("collection_exists")
            return False

        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _fake_thread):
            ok = await store._ensure_collection()

        assert ok is True
        assert store._ready is True
        assert "create_collection" in calls
        assert calls.count("collection_exists") == 1

    @pytest.mark.asyncio
    async def test_ensure_failure_returns_false(self, store) -> None:  # noqa: ANN001
        async def _raise(*args, **kwargs):
            raise RuntimeError("conn refused")

        with patch("app.rag.vector_store.qdrant_store.asyncio.to_thread", _raise):
            assert await store._ensure_collection() is False


class TestQdrantFactory:
    def test_factory_registers_qdrant(self) -> None:
        from app.rag.vector_store.factory import _BACKENDS, get_supported_backends

        assert "qdrant" in _BACKENDS
        assert "qdrant" in get_supported_backends()
        assert _BACKENDS["qdrant"] is QdrantVectorStore

    def test_registry_contains_qdrant(self) -> None:
        from app.llm.registry import get_vector_store_entries

        names = {e.name for e in get_vector_store_entries()}
        assert "qdrant" in names
