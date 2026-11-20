"""E1 闸门 — 生产删除路径必须清理 cross-modal 索引。

红基线契约：_delete_document_from_indexes 只删 ekb_documents/ekb_knn_vectors，
归档/下架后 cross-modal 图片向量残留（随机 UUID ID，upsert 不覆盖）。
修复后契约：三索引同 loop 尽力删除，单索引失败隔离；D11 回填脚本不再
单独补删 cross-modal（生产 helper 已覆盖，防重复删除）。

边界：httpx.AsyncClient.post 捕获替身（本机 OpenSearch 未跑）；
NO_PROXY delenv 为既有环境勘误校准（2026-11-19），非产品缺陷。
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（既有环境勘误桶，httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture
def recorded_posts(monkeypatch):
    """记录 (url, payload) 的 httpx post 替身。"""
    posts: list[tuple[str, dict]] = []

    async def recorder_post(self, url, **kwargs):
        posts.append((url, kwargs.get("json")))
        resp = MagicMock()
        resp.json.return_value = {"deleted": 1}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", recorder_post)
    return posts


def _cross_modal_index() -> str:
    return get_settings().OPENSEARCH_CROSS_MODAL_INDEX


class TestE1DeleteCoverage:
    async def test_delete_covers_three_indexes(self, recorded_posts):
        from tasks.document_tasks import _delete_document_from_indexes

        doc_id = str(uuid.uuid4())
        await _delete_document_from_indexes(doc_id)

        urls = [url for url, _ in recorded_posts]
        for index in ("ekb_documents", "ekb_knn_vectors", _cross_modal_index()):
            assert any(
                f"/{index}/_delete_by_query" in url for url in urls
            ), f"缺少索引 {index} 的清理: {urls}"

    async def test_delete_payload_term_doc_id(self, recorded_posts):
        from tasks.document_tasks import _delete_document_from_indexes

        doc_id = str(uuid.uuid4())
        await _delete_document_from_indexes(doc_id)

        cm_posts = [
            payload
            for url, payload in recorded_posts
            if f"/{_cross_modal_index()}/_delete_by_query" in url
        ]
        assert len(cm_posts) == 1
        assert cm_posts[0] == {"query": {"term": {"doc_id": doc_id}}}

    async def test_single_index_failure_isolated(self, monkeypatch, recorded_posts):
        """k-NN 索引删除炸不得吞掉其余索引的清理。"""
        from tasks.document_tasks import _delete_document_from_indexes

        async def failing_post(self, url, **kwargs):
            if "/ekb_knn_vectors/" in url:
                raise RuntimeError("knn 索引不可达")
            return await recorder_post_original(self, url, **kwargs)

        recorder_post_original = httpx.AsyncClient.post
        monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)

        doc_id = str(uuid.uuid4())
        await _delete_document_from_indexes(doc_id)

        urls = [url for url, _ in recorded_posts]
        assert any("/ekb_documents/_delete_by_query" in u for u in urls)
        assert any(f"/{_cross_modal_index()}/_delete_by_query" in u for u in urls)


class TestE1Wiring:
    def test_cleanup_task_calls_helper(self, monkeypatch):
        """归档/下架清理任务 → helper 接线证人（同步降级路径真实执行）。"""
        import tasks.document_tasks as dt

        called: list[str] = []

        async def spy_helper(doc_id):
            called.append(doc_id)

        monkeypatch.setattr(dt, "_delete_document_from_indexes", spy_helper)
        doc_id = str(uuid.uuid4())
        dt.cleanup_document_indexes(doc_id)
        assert called == [doc_id]

    async def test_backfill_no_duplicate_cross_modal_delete(
        self, monkeypatch, recorded_posts
    ):
        """D11 脚本经生产 helper 删三索引，cross-modal 不得重复删。

        红基线按设计绿（脚本自补一次）；修复若忘删脚本补删块则红。
        """
        import scripts.backfill_asset_chunks as bf

        doc_id = str(uuid.uuid4())
        await bf._delete_old_entries(doc_id)

        cm_deletes = [
            url
            for url, _ in recorded_posts
            if f"/{_cross_modal_index()}/_delete_by_query" in url
        ]
        assert len(cm_deletes) == 1, f"cross-modal 重复删除: {cm_deletes}"
        urls = [url for url, _ in recorded_posts]
        for index in ("ekb_documents", "ekb_knn_vectors"):
            assert any(f"/{index}/_delete_by_query" in u for u in urls)
