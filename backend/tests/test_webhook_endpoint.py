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
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.external_webhooks import (
    ResolvedCredential,
)
from app.api.v1.external_webhooks import (
    router as external_webhooks_router,
)

TENANT_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
VICTIM_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _resolved(creds: dict, tenant_id=TENANT_UUID) -> list[ResolvedCredential]:
    """凭证查询的返回值：租户归属来自 DB 行，不是请求参数。"""
    return [ResolvedCredential(tenant_id=tenant_id, secrets=creds)]


def _allow_unsigned():
    """本地 dev 模式开关；默认关闭，仅这两个用例显式打开。"""
    return patch(
        "app.api.v1.external_webhooks.get_settings",
        return_value=SimpleNamespace(WEBHOOK_ALLOW_UNSIGNED=True),
    )


from app.services.webhook_signature import (  # noqa: E402  — 有意延迟导入（依赖上方初始化）
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


def _confluence_event_body(page_id: int = 123456789, event_id: str = "conf-001") -> str:
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
            "/webhooks/external/feishu",
            content=body,
            headers={"Content-Type": "application/json"},
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
            new=AsyncMock(return_value=_resolved(feishu_credentials)),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=_feishu_event_body(),
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": str(
                        int(datetime.now(timezone.utc).timestamp())
                    ),
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
        sig = _compute_feishu_signature(
            timestamp, nonce, feishu_credentials["encrypt_key"], body
        )

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(feishu_credentials)),
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
        """Confluence 无 webhook_secret（dev 模式）→ 显式开关打开后才派发。"""
        body = _confluence_event_body(event_id="conf-dispatch-1")

        with (
            _allow_unsigned(),
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(confluence_credentials)),
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
                new=AsyncMock(return_value=_resolved(creds)),
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
        sig = _compute_feishu_signature(
            timestamp, nonce, feishu_credentials["encrypt_key"], body
        )

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(feishu_credentials)),
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
        sig = _compute_feishu_signature(
            timestamp, nonce, feishu_credentials["encrypt_key"], body
        )

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(feishu_credentials)),
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
# P1 修复：派发失败 → 清除幂等标记 + 500
# ==================================================================


class TestDispatchFailureClearsMark:
    """P1 修复：Celery 派发失败时清除幂等标记。

    旧行为：标记先于派发写入，派发失败返回 500 后，外部平台重试会被
    skipped 挡住，事件在 TTL 窗口内永久丢失。
    修复行为：派发失败 → 清除标记 → 返回 500 → 外部平台重试可重新处理。
    """

    def test_feishu_dispatch_failure_clears_mark_and_returns_500(
        self,
        client: TestClient,
        feishu_credentials: dict,
    ) -> None:
        body = _feishu_event_body(event_id="evt-dispatch-fail-1")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        sig = _compute_feishu_signature(
            timestamp, nonce, feishu_credentials["encrypt_key"], body
        )

        mock_clear = AsyncMock(return_value=True)

        with (
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(feishu_credentials)),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.api.v1.external_webhooks.clear_event_mark",
                new=mock_clear,
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            mock_task.delay.side_effect = Exception("broker unavailable")

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

        # 派发失败 → 500（外部平台将重试）
        assert resp.status_code == 500
        # 幂等标记被清除 — 重试不会被 skipped 挡住
        mock_clear.assert_awaited_once_with("evt-dispatch-fail-1")

    def test_confluence_dispatch_failure_clears_mark(
        self,
        client: TestClient,
        confluence_credentials: dict,
    ) -> None:
        body = _confluence_event_body(event_id="conf-dispatch-fail-1")
        mock_clear = AsyncMock(return_value=True)

        with (
            _allow_unsigned(),
            patch(
                "app.api.v1.external_webhooks._get_credentials",
                new=AsyncMock(return_value=_resolved(confluence_credentials)),
            ),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.api.v1.external_webhooks.clear_event_mark",
                new=mock_clear,
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            mock_task.delay.side_effect = Exception("broker unavailable")

            resp = client.post(
                "/webhooks/external/confluence",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 500
        mock_clear.assert_awaited_once_with("conf-dispatch-fail-1")


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
            new=AsyncMock(return_value=_resolved(feishu_credentials)),
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
            new=AsyncMock(return_value=_resolved(feishu_credentials)),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content="",
                headers={"Content-Type": "application/json"},
            )
        # 空 body → body_str="" → 视为 {} → 签名验证失败 → 401
        assert resp.status_code == 401


# ==================================================================
# 租户身份绑定（fail-closed 改造后的回归）
# ==================================================================


class TestTenantBinding:
    """派发用的租户必须来自验签命中的凭证行，不能来自 query param。"""

    @staticmethod
    def _signed_feishu_patch(creds: dict, tenant_id=TENANT_UUID):
        return patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=_resolved(creds, tenant_id=tenant_id)),
        )

    def test_forged_query_tenant_cannot_override_dispatch_tenant(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        """带上受害者租户的 query，派发的仍应是凭证自己的租户。"""
        body = _feishu_event_body(event_id="evt-tenant-forge")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        sig = _compute_feishu_signature(
            timestamp, nonce, feishu_credentials["encrypt_key"], body
        )

        with (
            self._signed_feishu_patch(feishu_credentials),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch("tasks.webhook_tasks.sync_external_document") as mock_task,
        ):
            resp = client.post(
                f"/webhooks/external/feishu?tenant_id={VICTIM_UUID}",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": sig,
                },
            )

        assert resp.status_code == 200
        assert mock_task.delay.call_args.kwargs["tenant_id"] == str(TENANT_UUID)

    def test_unsigned_credentials_rejected_by_default(
        self, client: TestClient, confluence_credentials: dict
    ) -> None:
        """凭证没配 secret = 任何匿名请求都能触发同步，默认必须拒绝。"""
        with patch(
            "app.api.v1.external_webhooks._get_credentials",
            new=AsyncMock(return_value=_resolved(confluence_credentials)),
        ):
            resp = client.post(
                "/webhooks/external/confluence",
                content=_confluence_event_body(event_id="conf-unsigned"),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 401

    def test_null_tenant_credential_needs_explicit_fallback(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        """私有部署凭证 tenant_id 为 NULL 时，未配置兜底就拒绝派发。"""
        body = _feishu_event_body(event_id="evt-null-tenant")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        sig = _compute_feishu_signature(
            timestamp, "n", feishu_credentials["encrypt_key"], body
        )

        with (
            self._signed_feishu_patch(feishu_credentials, tenant_id=None),
            patch(
                "app.api.v1.external_webhooks.get_settings",
                return_value=SimpleNamespace(
                    WEBHOOK_ALLOW_UNSIGNED=True, WEBHOOK_DEFAULT_TENANT_ID=""
                ),
            ),
        ):
            resp = client.post(
                "/webhooks/external/feishu",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": "n",
                    "X-Lark-Signature": sig,
                },
            )
        assert resp.status_code == 401

    def test_explicit_fallback_tenant_is_used_for_private_deploy(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        body = _feishu_event_body(event_id="evt-fallback-tenant")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        sig = _compute_feishu_signature(
            timestamp, "n", feishu_credentials["encrypt_key"], body
        )

        with (
            self._signed_feishu_patch(feishu_credentials, tenant_id=None),
            patch(
                "app.api.v1.external_webhooks.get_settings",
                return_value=SimpleNamespace(
                    WEBHOOK_ALLOW_UNSIGNED=True,
                    WEBHOOK_DEFAULT_TENANT_ID=str(VICTIM_UUID),
                ),
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
                    "X-Lark-Request-Nonce": "n",
                    "X-Lark-Signature": sig,
                },
            )
        assert resp.status_code == 200
        assert mock_task.delay.call_args.kwargs["tenant_id"] == str(VICTIM_UUID)

    def test_invalid_query_tenant_hint_does_not_500(
        self, client: TestClient, feishu_credentials: dict
    ) -> None:
        """非法 tenant_id 只应被忽略，不能变成 500。"""
        body = _feishu_event_body(event_id="evt-bad-hint")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        sig = _compute_feishu_signature(
            timestamp, "n", feishu_credentials["encrypt_key"], body
        )

        with (
            self._signed_feishu_patch(feishu_credentials),
            patch(
                "app.api.v1.external_webhooks.is_duplicate_event",
                new=AsyncMock(return_value=False),
            ),
            patch("tasks.webhook_tasks.sync_external_document"),
        ):
            resp = client.post(
                "/webhooks/external/feishu?tenant_id=not-a-uuid",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": "n",
                    "X-Lark-Signature": sig,
                },
            )
        assert resp.status_code == 200
