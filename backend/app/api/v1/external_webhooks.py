"""外部平台 Webhook 接收端点 — P1 Webhook 主动同步入口。

接收飞书/Confluence 的文档更新事件，验证签名后异步派发同步任务。

端点设计：
    POST /api/v1/webhooks/external/{adapter_id}

处理流程：
    读取原始 body → 查候选凭证 → 逐个验签 → 以命中的凭证确定租户
        → challenge 应答 / 解析事件 → 幂等去重 → Celery 异步派发 → 立即 200

## 租户身份的可信边界（fail-closed 改造后重写）

旧实现从 query param ``?tenant_id=<uuid>`` 取租户，并把它直接传给 Celery。
那是**客户端可控标识**：虽然随后有 HMAC 验签兜底，但身份来源本身不该由
调用方声明，而且独立 ``async_session_factory`` 没有 GUC，在生产 RLS 下
连凭证表都查不到 —— 端点恒返回 401「未找到凭证」。

现在：

1. 凭证查询放在显式 ``system_scope`` 内（登录前场景，跨租户可审计）；
2. query 里的 tenant_id **降级为可选筛选提示**，只用于缩小候选集，
   绝不作为派发任务的租户身份；
3. 逐个候选做验签，**验签通过的那条凭证的 ``tenant_id`` 才是身份**；
   攻击者伪造 query 只会筛掉自己的凭证，导致验签失败；
4. 未配置签名密钥的凭证默认拒绝（``WEBHOOK_ALLOW_UNSIGNED`` 控制）；
5. 私有部署凭证 tenant_id 为 NULL 时，用 ``WEBHOOK_DEFAULT_TENANT_ID``
   显式兜底；未配置则拒绝派发，不把任务丢进无 GUC 的会话里静默失败。

关键约束：
    - 端点无需认证（外部平台主动调用），安全由签名验证保证
    - 立即返回 200，避免外部平台 5s 超时重试
    - 实际同步由 Celery 任务 async 执行

遵循单一职责：仅做事件接收/验签/派发，不涉及同步逻辑本身。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import normalize_tenant_id, system_scope
from app.database import async_session_factory
from app.models.knowledge import ExternalCredential
from app.schemas.common import ApiResponse
from app.schemas.external_webhook import ChallengeResponse, WebhookAckResponse
from app.services.webhook_event_parser import (
    is_feishu_challenge,
    parse_webhook_event,
)
from app.services.webhook_idempotency import clear_event_mark, is_duplicate_event
from app.services.webhook_signature import verify_webhook_signature
from app.utils.crypto import decrypt_secret
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/external", tags=["external-webhooks"])


@dataclass(frozen=True)
class ResolvedCredential:
    """一条可用的 webhook 凭证 + 它**在数据库里**所属的租户。

    ``tenant_id`` 来自凭证行，不是请求参数 —— 这是本端点唯一的身份来源。
    """

    tenant_id: uuid.UUID | None
    secrets: dict[str, Any]


def _parse_uuid_safe(value: str | None) -> uuid.UUID | None:
    """尽力解析 UUID；非法值返回 None，绝不把 ValueError 抛成 500。"""
    if not value:
        return None
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None


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

    Query 参数（可选，仅作筛选提示）:
        tenant_id: 候选租户筛选，**不是身份凭据**。身份由验签命中的凭证决定。
    """
    # 1. 读取原始 body（签名验证需要未解析的字节）
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError as exc:
        log.warning(
            "webhook.invalid_json",
            adapter_id=adapter_id,
            error=str(exc)[:200],
        )
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # 2. query 里的 tenant_id 只用于缩小候选集，绝不作为派发身份。
    tenant_hint = _parse_uuid_safe(request.query_params.get("tenant_id"))
    if request.query_params.get("tenant_id") and tenant_hint is None:
        log.warning("webhook.invalid_tenant_hint", adapter_id=adapter_id)

    # 3. 飞书 URL 验证 challenge — 直接应答，跳过验签与凭证查询。
    #    注意：这一步不查库、不派发任务，因此不需要租户身份。
    if adapter_id == "feishu":
        challenge = is_feishu_challenge(body)
        if challenge is not None:
            log.info("webhook.feishu_challenge", adapter_id=adapter_id)
            return ApiResponse(
                code=0,
                data=ChallengeResponse(challenge=challenge).model_dump(),
                message="challenge_ok",
            )

    # 4. 查询候选凭证（跨租户，仅 SELECT + 解密）
    candidates = await _get_credentials(adapter_id, tenant_hint=tenant_hint)
    if not candidates:
        log.warning(
            "webhook.no_credentials",
            adapter_id=adapter_id,
            tenant_hint=str(tenant_hint),
        )
        raise HTTPException(
            status_code=401,
            detail="未找到该适配器的凭证，请先配置凭证",
        )

    # 5. 逐个验签；命中的那条凭证决定租户身份
    headers_lower = {k.lower(): v for k, v in request.headers.items()}
    matched: ResolvedCredential | None = None
    for candidate in candidates:
        sig_result = verify_webhook_signature(
            adapter_id=adapter_id,
            body=body_str,
            headers=headers_lower,
            credentials=candidate.secrets,
        )
        if not sig_result.valid:
            continue
        if sig_result.mode == "no_secret" and not get_settings().WEBHOOK_ALLOW_UNSIGNED:
            # 没有签名密钥 = 任何匿名请求都能触发同步。生产必须显式拒绝，
            # 否则「验签决定租户」这条链根本没有起点。
            log.warning(
                "webhook.unsigned_rejected",
                adapter_id=adapter_id,
                tenant_id=str(candidate.tenant_id),
            )
            continue
        if sig_result.mode == "no_secret":
            log.warning(
                "webhook.unsigned_allowed",
                adapter_id=adapter_id,
                tenant_id=str(candidate.tenant_id),
            )
        matched = candidate
        break

    if matched is None:
        log.warning(
            "webhook.signature_invalid",
            adapter_id=adapter_id,
            candidates=len(candidates),
        )
        raise HTTPException(
            status_code=401,
            detail="签名验证失败或该凭证未启用签名校验",
        )

    # 6. 确定派发用的租户：优先凭证行，私有部署才读显式兜底配置。
    tenant_id_str = _resolve_dispatch_tenant(matched.tenant_id)
    if tenant_id_str is None:
        log.warning(
            "webhook.missing_tenant",
            adapter_id=adapter_id,
            reason="credential tenant_id is NULL and WEBHOOK_DEFAULT_TENANT_ID is empty",
        )
        raise HTTPException(
            status_code=401,
            detail="凭证未绑定租户，请配置 WEBHOOK_DEFAULT_TENANT_ID",
        )

    # 7. 解析事件
    parsed = parse_webhook_event(adapter_id, body)
    if parsed is None:
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

    # 8. 幂等去重
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

    # 9. 异步派发 Celery 同步任务
    # P1 修复：派发失败时清除步骤 8 写入的幂等标记，否则外部平台重试会被
    # skipped 挡住，事件在 TTL 窗口内永久丢失。
    from tasks.webhook_tasks import sync_external_document

    try:
        sync_external_document.delay(
            adapter_id=parsed.adapter_id,
            source_doc_id=parsed.source_doc_id,
            tenant_id=tenant_id_str,
        )
    except Exception as exc:
        await clear_event_mark(parsed.event_id)
        log.error(
            "webhook.dispatch_failed_mark_cleared",
            adapter_id=adapter_id,
            source_doc_id=parsed.source_doc_id,
            event_id=parsed.event_id,
            error=str(exc)[:200],
        )
        raise HTTPException(
            status_code=500,
            detail="同步任务派发失败，外部平台重试将重新处理该事件",
        ) from exc
    log.info(
        "webhook.dispatched",
        adapter_id=adapter_id,
        source_doc_id=parsed.source_doc_id,
        event_id=parsed.event_id,
        event_type=parsed.event_type,
        tenant_id=tenant_id_str,
    )

    # 10. 立即返回 200
    return ApiResponse(
        code=0,
        data=WebhookAckResponse(
            status="accepted",
            event_id=parsed.event_id,
            message="事件已接收，异步同步中",
        ).model_dump(),
        message="success",
    )


def _resolve_dispatch_tenant(credential_tenant: uuid.UUID | None) -> str | None:
    """把凭证上的租户转成可派发的 UUID 字符串；无租户时读显式兜底配置。

    返回值一定经过 ``normalize_tenant_id`` 校验，下游 Celery 任务不会再因为
    非法值拿到一个永远匹配不上的 GUC。
    """
    raw = credential_tenant or _parse_uuid_safe(
        get_settings().WEBHOOK_DEFAULT_TENANT_ID
    )
    if raw is None:
        return None
    try:
        return normalize_tenant_id(raw)
    except Exception:  # noqa: BLE001 - 配置写错时按「没有身份」处理，不放行
        return None


# ------------------------------------------------------------------
# 凭证查询辅助
# ------------------------------------------------------------------

async def _get_credentials(
    adapter_id: str,
    *,
    tenant_hint: uuid.UUID | None = None,
) -> list[ResolvedCredential]:
    """读取并解密该适配器的全部可用凭证，附带上它们各自的租户归属。

    外部平台主动调用、无 JWT，这一步必须在显式 ``system_scope`` 内跨租户查询
    （与登录前按邮箱找用户同构）。不用 ``get_db_session`` 是因为它没有身份；
    不用裸 ``async_session_factory`` 是因为 fail-closed 下会查到 0 行。

    ``tenant_hint`` 只是可选的候选集筛选提示，**不参与身份判定**。
    """
    resolved: list[ResolvedCredential] = []
    async with async_session_factory() as session:
        # mypy: AsyncSession 已由 sessionmaker 提供，这里只是显式标注类型
        db: AsyncSession = session
        async with system_scope(db, reason="webhook.resolve_credentials"):
            stmt = select(ExternalCredential).where(
                ExternalCredential.adapter_id == adapter_id,
                ExternalCredential.is_active.is_(True),
            )
            if tenant_hint is not None:
                stmt = stmt.where(ExternalCredential.tenant_id == tenant_hint)
            rows = (await db.execute(stmt)).scalars().all()

        # 解密放在跨租户作用域之外：纯 CPU 操作，不碰 DB，
        # 尽量缩短 '*' 哨兵生效的时间窗。
        for cred in rows:
            try:
                plaintext = decrypt_secret(cred.credentials_encrypted)
                secrets = json.loads(plaintext)
            except Exception as exc:
                log.warning(
                    "webhook.credential_decrypt_failed",
                    adapter_id=adapter_id,
                    tenant_id=str(cred.tenant_id),
                    error=str(exc)[:200],
                )
                continue
            resolved.append(
                ResolvedCredential(tenant_id=cred.tenant_id, secrets=secrets)
            )
    return resolved
