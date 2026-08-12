"""Webhook 端点集成单测 — P1 入站 webhook 全流程。

使用 FastAPI TestClient + mock 隔离 DB / Redis / Celery，验证端点行为：
    - 飞书 challenge 应答
    - 无凭证 → 401
    - 签名验证（真实签名计算）
    - 合法事件 → 派发 Celery + 200 accepted
    - 重复事件 → 200 skipped
    - 非关注事件 → 200 ignored
    - Confluence dev 模式（无 secret）
    - 无效 JSON → 400
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.external_webhooks import router as external_webhooks_router
from app.services.webhook_signature import (
    _compute_confluence_signature,
    _compute_feishu_signature,
)


@pytest.fixture
def app() -> FastAPI:
    """最小 FastAPI app — 仅挂载 webhook router，无中间件干扰。"""
    app = FastAPI()
    app.include_router(external_webhooks_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def feishu_credentials() -> dict:
    return {"app_id": "cli_x", "app_secret": "s", "encrypt_key": "ek-test"}


@pytest.fixture
def confluence_credentials() -> dict:
    # dev 模式：无 webhook_secret
    return {"base_url": "https://x.atlassian.net", "username": "u", "api_token": "t"}


def _feishu_event_body(
    event_type: str = "drive.file.updated_v1",
    file_token: str = "doccnABCD1234",
    event_id: str = "evt-001",
) -> str:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": event_id,
                "event_type": event_type,
                "tenant_key": "t-xxx",
            },
            "event": {"file_token": file_token, "file_type": "docx"},
        }
    )


def _confluence_event_body(
    page_id: int = 123456789, event_id: str = "conf-001"
) -> str:
    return json.dumps(
        {
            "webhookEvent": "page_updated",
            "eventId": event_id,
            "page": {"id": page_id, "version": {"number": 5}},
        }
    )


# ==================================================================
# 飞书 challenge
# ==================================================================

class TestFeishuChallenge:
    """飞书 URL 验证 — 直接应答，跳过验签/凭证。"""

    def test_challenge_returns_challenge(self, client: TestClient) -> None:
        body = json.dumps(
            {
                "challenge": "abc-challenge-value",
                "token": "v",
                "type": "url_verification",
            }
        )
        resp = client.post(
            "/webhooks/external/feishu", content=body, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["challenge"] == "abc-challenge-value"


# ==================================================================
# 凭证缺失 / 签名失败
# ==================================================================

class TestAuthFailures:
    """无凭证 / 签名失败 → 401。"""

    def test_no_credentials_returns_401(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        with patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=_feishu_event_body(),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 401

    def test_invalid_signature_returns_401(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        with patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=feishu_credentials),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=_feishu_event_body(),
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                    "X-Lark-Request-Nonce": "n",
                    "X-Lark-Signature": "sha256=invalid-signature",
                },
            )
        assert resp.status_code == 401
        assert "签名验证失败" in resp.json()["detail"]


# ==================================================================
# 合法事件 → 派发 Celery
# ==================================================================

class TestValidEventDispatch:
    """合法签名 + 合法事件 → 派发 Celery + 200 accepted。"""

    def test_feishu_valid_event_dispatches(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        body = _feishu_event_body(event_id="evt-dispatch-1")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "nonce-xyz"
        sig = _compute_feishu_signature(timestamp, nonce, feishu_credentials["encrypt_key"], body)

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=feishu_credentials),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": sig,
                },
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "accepted"
        assert data["event_id"] == "evt-dispatch-1"
        # Celery 任务被派发
        mock_task.delay.assert_called_once()
        call_kwargs = mock_task.delay.call_args.kwargs
        assert call_kwargs["adapter_id"] == "feishu"
        assert call_kwargs["source_doc_id"] == "doccnABCD1234"

    def test_confluence_dev_mode_dispatches(
        self,
        client: TestClient,
        confluence_credentials: dict,
    ) -> None:
        """Confluence 无 webhook_secret（dev 模式）→ 跳过验签，直接派发。"""
        body = _confluence_event_body(event_id="conf-dispatch-1")

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=confluence_credentials),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                "/webhooks/external/confluence",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "accepted"
        assert data["event_id"] == "conf-dispatch-1"
        mock_task.delay.assert_called_once()
        assert mock_task.delay.call_args.kwargs["source_doc_id"] == "123456789"

    def test_confluence_with_secret_valid_dispatches(
        self,
        client: TestClient,
    ) -> None:
        """Confluence 配置 webhook_secret + 合法签名 → 派发。"""
        secret = "conf-webhook-secret"
        creds = {"base_url": "https://x.atlassian.net", "webhook_secret": secret}
        body = _confluence_event_body(event_id="conf-sig-1")
        sig = _compute_confluence_signature(secret, body)

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=creds),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                "/webhooks/external/confluence",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature": sig,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "accepted"
        mock_task.delay.assert_called_once()


# ==================================================================
# 重复事件 → 跳过
# ==================================================================

class TestDuplicateEvent:
    """幂等去重 — 重复事件返回 skipped。"""

    def test_duplicate_returns_skipped(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        body = _feishu_event_body(event_id="evt-dup-1")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        sig = _compute_feishu_signature(timestamp, nonce, feishu_credentials["encrypt_key"], body)

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=feishu_credentials),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=True),  # 重复
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": sig,
                },
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "skipped"
        assert data["deduplicated"] is True
        # 重复事件不派发 Celery
        mock_task.delay.assert_not_called()


# ==================================================================
# 非关注事件 → ignored
# ==================================================================

class TestIrrelevantEvent:
    """非文档事件（如 contact.user.updated）→ 200 ignored。"""

    def test_irrelevant_event_ignored(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        body = json.dumps(
            {
                "header": {
                    "event_id": "e-irrelevant",
                    "event_type": "contact.user.updated_v3",
                },
                "event": {},
            }
        )
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        sig = _compute_feishu_signature(timestamp, nonce, feishu_credentials["encrypt_key"], body)

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=feishu_credentials),
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": sig,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "ignored"
        mock_task.delay.assert_not_called()


# ==================================================================
# 无效 JSON
# ==================================================================

class TestInvalidBody:
    """无效 JSON body → 400。"""

    def test_invalid_json_returns_400(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        with patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=feishu_credentials),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content="{invalid json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400

    def test_empty_body_rejected(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        """空 body → 视为 {} → 无有效签名 → 401（不引发 500）。"""
        with patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=feishu_credentials),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content="",
                headers={"Content-Type": "application/json"},
            )
        # 空 body → body_str="" → 视为 {} → 签名验证失败 → 401
        assert resp.status_code == 401
