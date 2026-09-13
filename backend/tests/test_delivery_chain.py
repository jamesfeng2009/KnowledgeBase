"""交付链视图 API 测试 — P2b：v_delivery_chain 只读端点。

测试覆盖：
- TestRowToDict: 视图行序列化（完整行 / 空值行 / UUID 与列表转换）
- TestListDeliveryChain:
    - 管理员分页查询（count + rows 两次查询，envelope 结构）
    - 过滤条件注入（is_badcase / feedback_type / conversation_id）
    - 非 admin → 403
- TestDeliveryChainDetail:
    - 命中：返回链路 + 工具明细 + 事件节点（±1h 窗口参数）
    - 未命中 → 404
    - 非 admin → 403

mock 风格参照 test_recommendation_tasks.py（httpx ASGI + dependency_overrides），
不依赖真实数据库。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

# ------------------------------------------------------------------
# Mock celery before importing app modules（与 test_distillation_gate 一致）
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    _mock_celery = MagicMock()
    _mock_celery.Celery = MagicMock
    sys.modules["celery"] = _mock_celery

if "celery_app" not in sys.modules:
    _mock_celery_app = MagicMock()
    _mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = _mock_celery_app


ANSWERED_AT = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)


def _make_view_row(**overrides):
    """构造 v_delivery_chain 视图行的替身 — 字段与视图 SQL 输出对齐。"""
    base = dict(
        message_id=uuid4(),
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        conversation_title="交付链测试会话",
        answered_at=ANSWERED_AT,
        user_message_id=uuid4(),
        user_question="公司差旅报销标准是什么？",
        tool_calls=3,
        tool_errors=1,
        tool_names=["search_docs", "sql_query"],
        node_runs=6,
        max_iteration=2,
        total_latency_ms=1200,
        total_tokens=800,
        has_error=False,
        answer_excerpt="公司差旅报销标准：经济舱按实报销，酒店限额 500 元每晚。",
        citations=[{"doc_id": "d1", "title": "差旅制度"}],
        model_used="glm-4",
        answer_tokens=300,
        feedback_count=2,
        feedback_types=["complaint", "praise"],
        is_badcase=True,
        sediment_asset_ids=[uuid4()],
        sediment_doc_ids=[uuid4()],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_mock_db():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar=MagicMock(return_value=0),
            fetchall=MagicMock(return_value=[]),
            fetchone=MagicMock(return_value=None),
        )
    )
    return db


def _make_execute_recorder(outcomes: list):
    """按调用次序返回不同结果的 execute 记录器 — 用于多查询端点与参数断言。"""
    calls: list[tuple[str, dict]] = []
    results = list(outcomes)

    async def _execute(stmt, params=None):
        calls.append((str(stmt), dict(params or {})))
        idx = min(len(calls) - 1, len(results) - 1)
        return results[idx]

    return calls, _execute


# ======================================================================
# 序列化
# ======================================================================


class TestRowToDict:
    def test_full_row(self):
        from app.api.v1.delivery_chain import _row_to_dict

        row = _make_view_row()
        d = _row_to_dict(row)

        assert d["message_id"] == str(row.message_id)
        assert d["conversation_id"] == str(row.conversation_id)
        assert d["tenant_id"] == str(row.tenant_id)
        assert d["answered_at"] == ANSWERED_AT.isoformat()
        assert d["user_question"] == "公司差旅报销标准是什么？"
        assert d["tool_calls"] == 3
        assert d["tool_errors"] == 1
        assert d["tool_names"] == ["search_docs", "sql_query"]
        assert d["total_latency_ms"] == 1200
        assert d["citations"] == [{"doc_id": "d1", "title": "差旅制度"}]
        assert d["feedback_types"] == ["complaint", "praise"]
        assert d["is_badcase"] is True
        assert d["sediment_asset_ids"] == [str(row.sediment_asset_ids[0])]
        assert d["sediment_doc_ids"] == [str(row.sediment_doc_ids[0])]

    def test_null_heavy_row(self):
        from app.api.v1.delivery_chain import _row_to_dict

        row = _make_view_row(
            tenant_id=None,
            user_message_id=None,
            user_question=None,
            tool_calls=None,
            tool_errors=None,
            tool_names=None,
            node_runs=None,
            max_iteration=None,
            total_latency_ms=None,
            total_tokens=None,
            citations=None,
            model_used=None,
            feedback_count=None,
            feedback_types=None,
            is_badcase=None,
            sediment_asset_ids=None,
            sediment_doc_ids=None,
        )
        d = _row_to_dict(row)

        assert d["tenant_id"] is None
        assert d["user_message_id"] is None
        assert d["tool_calls"] == 0
        assert d["tool_errors"] == 0
        assert d["tool_names"] == []
        assert d["node_runs"] == 0
        assert d["max_iteration"] == 0
        assert d["total_latency_ms"] is None
        assert d["total_tokens"] is None
        assert d["citations"] is None
        assert d["feedback_count"] == 0
        assert d["feedback_types"] == []
        assert d["is_badcase"] is False
        assert d["sediment_asset_ids"] == []
        assert d["sediment_doc_ids"] == []


# ======================================================================
# HTTP 端点
# ======================================================================


async def _make_client(db_mock, role: str = "admin") -> httpx.AsyncClient:
    """构建带认证/DB 覆盖的 ASGI 客户端（不触达真实服务）。"""
    from app.database import get_db_session
    from app.deps import get_current_user
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    user = SimpleNamespace(
        id=uuid4(), role=role, is_active=True,
        email="u@test.com", name="测试用户",
    )

    async def override_user():
        return user

    async def override_db():
        yield db_mock

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db_session] = override_db
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    # 交还覆盖清理给调用方：用 yield fixture 语义不便复用，这里手动挂清理
    return client


async def _teardown_client(client: httpx.AsyncClient) -> None:
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


_BASE = "/api/v1/admin/delivery-chain"


class TestListDeliveryChain:
    @pytest.mark.asyncio
    async def test_list_success_and_envelope(self):
        row = _make_view_row()
        count_result = MagicMock(scalar=MagicMock(return_value=1))
        rows_result = MagicMock(fetchall=MagicMock(return_value=[row]))
        db = _make_mock_db()
        _, exec_fn = _make_execute_recorder([count_result, rows_result])
        db.execute = AsyncMock(side_effect=exec_fn)

        client = await _make_client(db)
        try:
            resp = await client.get(_BASE)
        finally:
            await _teardown_client(client)

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["total"] == 1
        assert len(body["data"]["items"]) == 1
        item = body["data"]["items"][0]
        assert item["message_id"] == str(row.message_id)
        assert item["is_badcase"] is True

    @pytest.mark.asyncio
    async def test_list_filters_in_params(self):
        row = _make_view_row()
        count_result = MagicMock(scalar=MagicMock(return_value=1))
        rows_result = MagicMock(fetchall=MagicMock(return_value=[row]))
        db = _make_mock_db()
        calls, exec_fn = _make_execute_recorder([count_result, rows_result])
        db.execute = AsyncMock(side_effect=exec_fn)

        client = await _make_client(db)
        try:
            resp = await client.get(
                _BASE,
                params={
                    "is_badcase": "true",
                    "feedback_type": "complaint",
                    "conversation_id": str(row.conversation_id),
                    "date_from": "2026-09-01T00:00:00Z",
                },
            )
        finally:
            await _teardown_client(client)

        assert resp.status_code == 200
        # 两次查询（count + rows）都携带过滤参数
        assert len(calls) == 2
        for _, params in calls:
            assert params["is_badcase"] is True
            assert params["ftype"] == "complaint"
            assert params["conversation_id"] == str(row.conversation_id)
            assert params["date_from"] is not None

    @pytest.mark.asyncio
    async def test_list_forbidden_for_non_admin(self):
        db = _make_mock_db()
        client = await _make_client(db, role="editor")
        try:
            resp = await client.get(_BASE)
        finally:
            await _teardown_client(client)

        assert resp.status_code == 403


class TestDeliveryChainDetail:
    @pytest.mark.asyncio
    async def test_detail_found_with_process_details(self):
        row = _make_view_row()
        tool_row = SimpleNamespace(
            id=uuid4(),
            run_id=uuid4(),
            span_id=None,
            tool_name="search_docs",
            status="success",
            duration_ms=120,
            error=None,
            result_summary="命中 3 条",
            evidence_ref="doc:d1",
            created_at=ANSWERED_AT - timedelta(minutes=1),
        )
        event_row = SimpleNamespace(
            seq=1,
            event_type="node_end",
            node_name="think",
            iteration=1,
            metadata={"latency_ms": "300", "token_count": "120"},
            created_at=ANSWERED_AT - timedelta(minutes=2),
        )
        main_result = MagicMock(fetchone=MagicMock(return_value=row))
        tool_result = MagicMock(fetchall=MagicMock(return_value=[tool_row]))
        event_result = MagicMock(fetchall=MagicMock(return_value=[event_row]))
        db = _make_mock_db()
        calls, exec_fn = _make_execute_recorder(
            [main_result, tool_result, event_result]
        )
        db.execute = AsyncMock(side_effect=exec_fn)

        client = await _make_client(db)
        try:
            resp = await client.get(f"{_BASE}/{row.message_id}")
        finally:
            await _teardown_client(client)

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["message_id"] == str(row.message_id)
        assert len(data["tool_calls_detail"]) == 1
        assert data["tool_calls_detail"][0]["tool_name"] == "search_docs"
        assert len(data["event_nodes"]) == 1
        assert data["event_nodes"][0]["node_name"] == "think"
        # 明细查询限定在回答时刻 ±1 小时窗口
        window_calls = calls[1:]
        assert len(window_calls) == 2
        for _, params in window_calls:
            assert params["sid"] == str(row.conversation_id)
            assert params["win_start"] == ANSWERED_AT - timedelta(hours=1)
            assert params["win_end"] == ANSWERED_AT + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_detail_404_when_missing(self):
        db = _make_mock_db()  # fetchone → None
        missing_id = uuid4()
        client = await _make_client(db)
        try:
            resp = await client.get(f"{_BASE}/{missing_id}")
        finally:
            await _teardown_client(client)

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_forbidden_for_non_admin(self):
        db = _make_mock_db()
        client = await _make_client(db, role="editor")
        try:
            resp = await client.get(f"{_BASE}/{uuid4()}")
        finally:
            await _teardown_client(client)

        assert resp.status_code == 403


# ======================================================================
# 评测集导出脚本（scripts/export_eval_cases.py）
# ======================================================================


class TestBuildEvalCase:
    """导出脚本的行转换函数 — 纯逻辑单测（不触达 DB）。"""

    def test_build_case_from_view_row(self):
        import importlib.util
        from pathlib import Path

        script = (
            Path(__file__).resolve().parent.parent
            / "scripts" / "export_eval_cases.py"
        )
        spec = importlib.util.spec_from_file_location("export_eval_cases", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        row = _make_view_row()
        case = mod.build_eval_case(row)

        assert case["case_id"] == str(row.message_id)
        assert case["question"] == "公司差旅报销标准是什么？"
        assert "差旅报销标准" in case["answer_excerpt"]
        assert case["citations"] == [{"doc_id": "d1", "title": "差旅制度"}]
        assert case["model_used"] == "glm-4"
        assert case["tool_calls"] == 3
        assert case["tool_errors"] == 1
        assert case["is_badcase"] is True
        assert "complaint" in case["feedback_types"]
        assert case["answered_at"] == ANSWERED_AT.isoformat()

    def test_build_case_minimal_row(self):
        import importlib.util
        from pathlib import Path

        script = (
            Path(__file__).resolve().parent.parent
            / "scripts" / "export_eval_cases.py"
        )
        spec = importlib.util.spec_from_file_location("export_eval_cases", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        row = _make_view_row(
            user_question=None,
            citations=None,
            model_used=None,
            tool_calls=None,
            tool_errors=None,
            feedback_types=None,
            is_badcase=None,
            sediment_asset_ids=None,
            sediment_doc_ids=None,
        )
        case = mod.build_eval_case(row)

        assert case["question"] == ""
        assert case["citations"] == []
        assert case["model_used"] is None
        assert case["tool_calls"] == 0
        assert case["tool_errors"] == 0
        assert case["feedback_types"] == []
        assert case["is_badcase"] is False
