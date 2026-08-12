"""外部平台 Webhook 接收端点 — P1 Webhook 主动同步入口。

接收飞书/Confluence 的文档更新事件，验证签名后异步派发同步任务。

端点设计：
    POST /api/v1/webhooks/external/{adapter_id}
        ?tenant_id=<可选>

处理流程（见模块顶部 P1 方案）::
    读取原始 body → 查凭证 → 验签 → challenge 应答
        → 解析事件 → 幂等去重 → Celery 异步派发 → 立即 200

关键约束：
    - 端点无需认证（外部平台主动调用），安全由签名验证保证
    - 立即返回 200，避免外部平台 5s 超时重试
    - 实际同步由 Celery 任务 async 执行
    - 直接使用 async_session_factory（绕过 get_db_session 的租户上下文耦合，
      webhook 由外部平台调用，无 JWT 租户上下文）

遵循单一职责：仅做事件接收/验签/派发，不涉及同步逻辑本身。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.knowledge import ExternalCredential
from app.schemas.common import ApiResponse
from app.schemas.external_webhook import ChallengeResponse, WebhookAckResponse
from app.services.webhook_event_parser import (
    is_feishu_challenge,
    parse_webhook_event,
)
from app.services.webhook_idempotency import is_duplicate_event
from app.services.webhook_signature import verify_webhook_signature
from app.utils.crypto import decrypt_secret
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/external", tags=["external-webhooks"])


@router.post(
    "/{adapter_id}",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
)
async def receive_external_webhook(
    adapter_id: str,
    request: Request,
) -> ApiResponse:
    """接收外部平台 webhook 事件 — 验签后异步同步文档。

    端点无需认证，安全由签名验证保证。立即返回 200，实际同步异步执行。

    Path 参数:
        adapter_id: 适配器 ID（feishu / confluence）。

    Query 参数（可选）:
        tenant_id: 租户 ID（多租户场景，私有部署不传）。
    """
    # 1. 读取原始 body（签名验证需要未解析的字节）
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    # 解析 JSON
    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError as exc:
        log.warning(
            "webhook.invalid_json",
            adapter_id=adapter_id,
            error=str(exc)[:200],
        )
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # 2. 租户 ID（从 query 参数取，多租户场景）
    tenant_id_str = request.query_params.get("tenant_id")
    tenant_id = uuid.UUID(tenant_id_str) if tenant_id_str else None

    # 3. 飞书 URL 验证 challenge — 直接应答，跳过验签
    if adapter_id == "feishu":
        challenge = is_feishu_challenge(body)
        if challenge is not None:
            log.info("webhook.feishu_challenge", adapter_id=adapter_id)
            return ApiResponse(
                code=0,
                data=ChallengeResponse(challenge=challenge).model_dump(),
                message="challenge_ok",
            )

    # 4. 查询凭证 → 解密获取 webhook secret
    credentials = await _get_credentials(adapter_id, tenant_id)
    if credentials is None:
        log.warning(
            "webhook.no_credentials",
            adapter_id=adapter_id,
            tenant_id=str(tenant_id),
        )
        # 无凭证无法验签 — 拒绝（防止未授权 webhook 调用）
        raise HTTPException(
            status_code=401,
            detail="未找到该适配器的凭证，请先配置凭证",
        )

    # 5. 签名验证
    headers_lower = {k.lower(): v for k, v in request.headers.items()}
    sig_result = verify_webhook_signature(
        adapter_id=adapter_id,
        body=body_str,
        headers=headers_lower,
        credentials=credentials,
    )
    if not sig_result.valid:
        log.warning(
            "webhook.signature_invalid",
            adapter_id=adapter_id,
            reason=sig_result.reason,
        )
        raise HTTPException(
            status_code=401,
            detail=f"签名验证失败: {sig_result.reason}",
        )

    # 6. 解析事件
    parsed = parse_webhook_event(adapter_id, body)
    if parsed is None:
        # 非关注事件类型 — 200 应答但不同步（避免外部平台重试）
        log.info(
            "webhook.irrelevant_event",
            adapter_id=adapter_id,
            event_type=body.get("header", {}).get("event_type")
            or body.get("webhookEvent", ""),
        )
        return ApiResponse(
            code=0,
            data=WebhookAckResponse(
                status="ignored",
                message="非关注事件类型，已忽略",
            ).model_dump(),
            message="success",
        )

    # 7. 幂等去重
    is_dup = await is_duplicate_event(parsed.event_id)
    if is_dup:
        log.info(
            "webhook.duplicate_skipped",
            adapter_id=adapter_id,
            event_id=parsed.event_id,
        )
        return ApiResponse(
            code=0,
            data=WebhookAckResponse(
                status="skipped",
                event_id=parsed.event_id,
                message="重复事件，已跳过",
                deduplicated=True,
            ).model_dump(),
            message="success",
        )

    # 8. 异步派发 Celery 同步任务
    from tasks.webhook_tasks import sync_external_document

    sync_external_document.delay(
        adapter_id=parsed.adapter_id,
        source_doc_id=parsed.source_doc_id,
        tenant_id=tenant_id_str,
    )
    log.info(
        "webhook.dispatched",
        adapter_id=adapter_id,
        source_doc_id=parsed.source_doc_id,
        event_id=parsed.event_id,
        event_type=parsed.event_type,
    )

    # 9. 立即返回 200
    return ApiResponse(
        code=0,
        data=WebhookAckResponse(
            status="accepted",
            event_id=parsed.event_id,
            message="事件已接收，异步同步中",
        ).model_dump(),
        message="success",
    )


# ------------------------------------------------------------------
# 凭证查询辅助
# ------------------------------------------------------------------

async def _get_credentials(
    adapter_id: str,
    tenant_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    """从 external_credentials 表读取并解密凭证。

    直接使用 async_session_factory（webhook 无 JWT 租户上下文，
    不能用 get_db_session 依赖）。
    """
    async with async_session_factory() as session:
        stmt = select(ExternalCredential).where(
            ExternalCredential.adapter_id == adapter_id,
            ExternalCredential.is_active.is_(True),
        )
        if tenant_id is not None:
            stmt = stmt.where(ExternalCredential.tenant_id == tenant_id)
        else:
            stmt = stmt.where(ExternalCredential.tenant_id.is_(None))

        result = await session.execute(stmt)
        cred = result.scalar_one_or_none()
        if cred is None:
            return None
        try:
            plaintext = decrypt_secret(cred.credentials_encrypted)
            return json.loads(plaintext)
        except Exception as exc:
            log.warning(
                "webhook.credential_decrypt_failed",
                adapter_id=adapter_id,
                error=str(exc)[:200],
            )
            return None
