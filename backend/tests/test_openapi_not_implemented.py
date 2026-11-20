"""开放接口「能力未实现」语义测试（不依赖数据库）。

对应 P2：connectors / multimedia 里的预留端点此前返回
``{"code": 0, "data": {...空占位...}, "message": "success"}``。

集成方看到 ``code=0`` 会当成「查询成功但当前没有数据」，于是按空结果写死
下游逻辑（重试策略、缓存空值、告警阈值全部建立在错误前提上）。真实语义是
「这个能力根本还没做」，两者必须可区分。

本文件锁定约定：未实现能力 → HTTP 501 + ``code=501`` + ``data=None``。
真实对接实现后，把 ``not_implemented(...)`` 换成正常返回，这些用例会失败，
提示同步更新对外文档与契约。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.openapi.responses import NOT_IMPLEMENTED_CODE, not_implemented
from app.api.openapi.v1.connectors import router as connectors_router
from app.api.openapi.v1.multimedia import router as multimedia_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    """最小 app —— 只挂两个 openapi router，并伪造 API Key 认证。

    这里只覆盖 ``get_api_key_user``（``require_scope`` 返回的 ``check_scope``
    闭包持有的正是它的原始对象），并给假密钥足够 scope，这样测的是
    「未实现语义」本身，不受鉴权/数据库/限流影响。
    """
    from app.api.openapi.deps import get_api_key_user

    app = FastAPI()
    app.include_router(connectors_router)
    app.include_router(multimedia_router)

    fake_key = {
        "key_id": "test",
        "name": "pytest",
        "scopes": [
            "connector:oa",
            "connector:erp",
            "connector:hr",
            "multimedia:parse",
            "multimedia:vision",
        ],
        "tenant_id": None,
    }

    async def _fake_key_user() -> dict[str, Any]:
        return fake_key

    app.dependency_overrides[get_api_key_user] = _fake_key_user
    return TestClient(app)


def _assert_not_implemented(resp, capability_hint: str) -> None:
    """统一断言：501 + code=501 + data is None + message 说明未实现。"""
    assert (
        resp.status_code == NOT_IMPLEMENTED_CODE
    ), f"预期 501，实际 {resp.status_code}：{resp.text}"
    body = resp.json()
    assert body["code"] == NOT_IMPLEMENTED_CODE
    assert body["data"] is None, "未实现能力不应返回占位 data，避免被当成功"
    assert "尚未实现" in body["message"]
    assert capability_hint in body["message"]


# ---------------------------------------------------------------------------
# 企业连接器
# ---------------------------------------------------------------------------


def test_oa_query_returns_not_implemented(client: TestClient) -> None:
    resp = client.post(
        "/connectors/oa/query",
        json={"bill_no": "OA-2026-001", "system": "default"},
    )
    _assert_not_implemented(resp, "OA")


def test_erp_query_returns_not_implemented(client: TestClient) -> None:
    resp = client.post(
        "/connectors/erp/query",
        json={"module": "sales", "filters": {"region": "east"}},
    )
    _assert_not_implemented(resp, "ERP")


def test_hr_employee_returns_not_implemented(client: TestClient) -> None:
    """注意：不能返回 found=False —— 那会被读成「查无此人」而不是「没这能力」。"""
    resp = client.post("/connectors/hr/employee", json={"employee_id": "E1001"})
    _assert_not_implemented(resp, "员工")


# ---------------------------------------------------------------------------
# 多媒体
# ---------------------------------------------------------------------------


def test_multimedia_parse_returns_not_implemented(client: TestClient) -> None:
    """注意：不能返回 text="" —— 那会被读成「解析成功但内容为空」。"""
    resp = client.post(
        "/multimedia/parse",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    _assert_not_implemented(resp, "文档解析")


def test_multimedia_vision_returns_not_implemented(client: TestClient) -> None:
    resp = client.post(
        "/multimedia/vision",
        json={"image_base64": "aGVsbG8=", "prompt": "这是什么"},
    )
    _assert_not_implemented(resp, "VLM")


def test_multimedia_transcribe_returns_not_implemented(client: TestClient) -> None:
    resp = client.post(
        "/multimedia/transcribe",
        files={"file": ("a.mp3", b"\x00\x01", "audio/mpeg")},
    )
    _assert_not_implemented(resp, "音频转文字")


# ---------------------------------------------------------------------------
# helper 自身
# ---------------------------------------------------------------------------


def test_not_implemented_helper_shapes_response() -> None:
    """helper 单独测：状态码、业务码、data、message 拼接规则。"""
    from starlette.responses import Response

    resp = Response()
    payload = not_implemented(resp, "某能力", hint="需先接入 X")
    assert resp.status_code == NOT_IMPLEMENTED_CODE
    assert payload.code == NOT_IMPLEMENTED_CODE
    assert payload.data is None
    assert "某能力" in payload.message
    assert "需先接入 X" in payload.message


def test_not_implemented_helper_without_hint() -> None:
    from starlette.responses import Response

    payload = not_implemented(Response(), "某能力")
    assert payload.message == "某能力 尚未实现"
