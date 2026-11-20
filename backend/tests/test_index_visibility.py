"""T2 索引可见性测试 — 派生状态机 + reindex 端点权限/限流。

背景：检索可见性已有双层权威判定（召回层 doc_status + Final Gate DB status），
T2 不新增 DB 字段，而是在 /progress 端点派生 searchable/index_state 语义，
并提供文档级 reindex 重试入口（带权限 + 限流）。

覆盖：
1. _derive_index_state 全部分支（published/failed/pending/draft/indexing + 索引失败警告）；
2. reindex 限流器独立配额（6/分钟，突发 3）；
3. reindex 端点权限拒绝（非所有者/非 admin）与成功路径（mock service）。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 测试环境缺重依赖，按项目惯例 mock
for _m in ("celery", "celery_app", "opensearchpy", "pymilvus"):
    if _m not in sys.modules:
        sys.modules[_m] = MagicMock()

from app.api.v1.documents import _derive_index_state, reindex_document  # noqa: E402

# ------------------------------------------------------------------
# 1. 派生状态机（纯函数，覆盖全部分支）
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,stage,warnings,expect_state,expect_searchable",
    [
        ("published", "done", None, "ready", True),
        ("published", None, [], "ready", True),
        # 索引失败警告 → 即使 published 也不可检索（关键：异常被流水线吞掉的路径）
        ("published", "done", ["索引构建失败: connection refused"], "failed", False),
        ("published", "done", ["解析异常: xxx"], "failed", False),
        ("failed", "failed", ["解析失败"], "failed", False),
        ("pending_review", "publishing", None, "pending", False),
        ("draft", "indexing", None, "indexing", False),
        ("draft", "parsing", None, "indexing", False),
        ("draft", "unknown", None, "pending", False),
        (None, None, None, "pending", False),
    ],
)
def test_derive_index_state(status, stage, warnings, expect_state, expect_searchable):
    result = _derive_index_state(status, stage, warnings)
    assert result["index_state"] == expect_state
    assert result["searchable"] is expect_searchable
    assert result["reason"]  # 永远有中文提示


def test_derive_index_state_indexing_reason_mentions_indexing():
    """明确命中"内容索引中"文案——这是 T2 要消除的体验断层。"""
    result = _derive_index_state("draft", "indexing", None)
    assert "索引中" in result["reason"]


# ------------------------------------------------------------------
# 2. reindex 限流器（独立配额）
# ------------------------------------------------------------------


def test_reindex_limiter_quota(monkeypatch):
    """模块级限流器：burst=3，前 3 次通过，第 4 次拒绝。"""
    from app.api.v1 import documents as doc_mod

    limiter = doc_mod._RateLimiter(per_minute=6, burst=3)
    client = "test-client-quota"
    assert limiter.allow(client) is True
    assert limiter.allow(client) is True
    assert limiter.allow(client) is True
    assert limiter.allow(client) is False  # 突发耗尽


# ------------------------------------------------------------------
# 3. reindex 端点逻辑（直接调用 async 函数，mock 依赖）
# ------------------------------------------------------------------


def _make_request(client_id: str = "u-1") -> MagicMock:
    request = MagicMock()
    request.state = SimpleNamespace(tenant_id=None)
    return request


def _make_user(role: str = "user", owner_id=None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role=role, email="t@t.test")


def _make_doc(owner_id) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        owner_id=owner_id,
        parse_status="failed",
        status="published",
    )


@pytest.mark.asyncio
async def test_reindex_forbidden_for_non_owner():
    """非所有者/非 admin → 403，且不触发重建。"""
    from fastapi import HTTPException

    doc = _make_doc(owner_id=uuid.uuid4())  # owner 是别人
    user = _make_user(role="user")
    service = MagicMock()
    service.permission = MagicMock()
    service.permission.check_write = AsyncMock(return_value=True)
    service.get_document = AsyncMock(return_value=doc)
    service._trigger_reindex = AsyncMock()

    with patch("app.api.v1.documents.KnowledgeService", return_value=service), patch(
        "app.middleware._get_client_id", return_value="forbidden-client"
    ), patch("app.api.v1.documents._reindex_limiter") as limiter:
        limiter.allow.return_value = True
        with pytest.raises(HTTPException) as exc:
            await reindex_document(_make_request(), doc.id, MagicMock(), user)
        assert exc.value.status_code == 403
        service._trigger_reindex.assert_not_awaited()


@pytest.mark.asyncio
async def test_reindex_success_triggers_pipeline():
    """所有者 → 标记 pending + 触发重建，返回 queued。"""
    user = _make_user(role="user")
    doc = _make_doc(owner_id=user.id)
    service = MagicMock()
    service.permission = MagicMock()
    service.permission.check_write = AsyncMock(return_value=True)
    service.get_document = AsyncMock(return_value=doc)
    service._trigger_reindex = AsyncMock()
    db = MagicMock()
    db.commit = AsyncMock()

    with patch("app.api.v1.documents.KnowledgeService", return_value=service), patch(
        "app.middleware._get_client_id", return_value="success-client"
    ), patch("app.api.v1.documents._reindex_limiter") as limiter:
        limiter.allow.return_value = True
        resp = await reindex_document(_make_request(), doc.id, db, user)

    assert resp.code == 0
    assert resp.data["status"] == "queued"
    assert doc.parse_status == "pending"
    db.commit.assert_awaited_once()
    service._trigger_reindex.assert_awaited_once_with(str(doc.id), str(doc.kb_id))


@pytest.mark.asyncio
async def test_reindex_rate_limited():
    """限流拒绝 → 429，不查库不触发。"""
    from fastapi import HTTPException

    user = _make_user(role="admin")
    service = MagicMock()
    with patch("app.api.v1.documents.KnowledgeService", return_value=service), patch(
        "app.middleware._get_client_id", return_value="rl-client"
    ), patch("app.api.v1.documents._reindex_limiter") as limiter:
        limiter.allow.return_value = False
        with pytest.raises(HTTPException) as exc:
            await reindex_document(_make_request(), uuid.uuid4(), MagicMock(), user)
        assert exc.value.status_code == 429
        service.get_document.assert_not_called()
