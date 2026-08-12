"""Webhook 事件解析器 — 统一解析飞书/Confluence 事件为 ParsedWebhookEvent。

各平台 webhook 事件格式不同，本模块提供统一解析接口，输出标准事件对象
供 webhook 端点消费。

支持的事件来源：
    - 飞书 (feishu): event v2 格式，``drive.file.updated_v1`` / ``created_v1``
      事件中 ``event.file_token`` 即文档 token（source_doc_id）。
    - Confluence: ``page_updated`` / ``page_created`` 事件，
      ``page.id`` 即页面 ID（source_doc_id）。

遵循单一职责：仅做事件解析与字段提取，不涉及签名验证或 DB 访问。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ParsedWebhookEvent:
    """统一 webhook 事件 — 解析后的标准结构。

    Attributes:
        adapter_id: 来源适配器 ID（feishu / confluence）。
        source_doc_id: 外部平台文档 ID（飞书 doc_token / Confluence pageId）。
        event_id: 事件唯一标识（用于幂等去重）。
        event_type: 事件类型（drive.file.updated_v1 / page_updated 等）。
        tenant_key: 租户标识（飞书 tenant_key，可选）。
        raw: 原始事件 dict（供调试/日志）。
    """

    adapter_id: str
    source_doc_id: str
    event_id: str
    event_type: str
    tenant_key: str | None = None
    raw: dict[str, Any] | None = None


# 飞书关注的事件类型 — 仅文档内容变更/创建触发同步
_FEISHU_RELEVANT_EVENTS: frozenset[str] = frozenset({
    "drive.file.updated_v1",
    "drive.file.created_v1",
    "drive.file.title_updated_v1",
})

# Confluence 关注的事件类型
_CONFLUENCE_RELEVANT_EVENTS: frozenset[str] = frozenset({
    "page_updated",
    "page_created",
    "page_restored",
})


# ==================================================================
# 飞书事件解析
# ==================================================================

def parse_feishu_event(body: dict[str, Any]) -> ParsedWebhookEvent | None:
    """解析飞书 event v2 事件。

    飞书事件格式::

        {
          "schema": "2.0",
          "header": {
            "event_id": "xxx",
            "event_type": "drive.file.updated_v1",
            "tenant_key": "xxx"
          },
          "event": {
            "file_token": "doccnXXXXXX",
            "file_type": "docx",
            "file_name": "..."
          }
        }

    Args:
        body: 解析后的 JSON dict。

    Returns:
        ParsedWebhookEvent 或 None（非关注事件 / 格式不符）。
    """
    header = body.get("header") or {}
    event_type = header.get("event_type", "")

    # 仅处理关注的事件类型
    if event_type not in _FEISHU_RELEVANT_EVENTS:
        log.debug(
            "webhook.feishu_irrelevant_event",
            event_type=event_type,
        )
        return None

    event = body.get("event") or {}
    file_token = event.get("file_token", "")
    if not file_token:
        log.warning(
            "webhook.feishu_missing_file_token",
            event_type=event_type,
            event_id=header.get("event_id"),
        )
        return None

    return ParsedWebhookEvent(
        adapter_id="feishu",
        source_doc_id=file_token,
        event_id=header.get("event_id", ""),
        event_type=event_type,
        tenant_key=header.get("tenant_key"),
        raw=body,
    )


def is_feishu_challenge(body: dict[str, Any]) -> str | None:
    """检测飞书 URL 验证 challenge 请求。

    飞书配置 webhook URL 时会发送::

        {"challenge": "xxx", "token": "...", "type": "url_verification"}

    端点需原样返回 ``{"challenge": "xxx"}``。

    Returns:
        challenge 值（若是验证请求），否则 None。
    """
    if body.get("type") == "url_verification":
        return body.get("challenge", "")
    return None


# ==================================================================
# Confluence 事件解析
# ==================================================================

def parse_confluence_event(body: dict[str, Any]) -> ParsedWebhookEvent | None:
    """解析 Confluence webhook 事件。

    Confluence Cloud webhook 格式::

        {
          "webhookEvent": "page_updated",
          "eventId": "xxx",  // Cloud 有 eventId
          "page": {
            "id": "123456789",
            "title": "...",
            "version": {"number": 5}
          }
        }

    Confluence Server/DC 可能无 eventId，此时从事件体合成。
    """
    event_type = body.get("webhookEvent", "") or body.get("event", {}).get(
        "eventType", ""
    )

    if event_type not in _CONFLUENCE_RELEVANT_EVENTS:
        log.debug(
            "webhook.confluence_irrelevant_event",
            event_type=event_type,
        )
        return None

    page = body.get("page") or {}
    # page.id 可能是 int 或 str
    page_id = str(page.get("id", "")) if page.get("id") else ""
    if not page_id:
        log.warning(
            "webhook.confluence_missing_page_id",
            event_type=event_type,
        )
        return None

    # eventId 优先用 Cloud 提供的，缺失时合成（防重复）
    event_id = body.get("eventId", "") or _synthesize_confluence_event_id(
        event_type, page_id, body.get("timestamp", "")
    )

    return ParsedWebhookEvent(
        adapter_id="confluence",
        source_doc_id=page_id,
        event_id=event_id,
        event_type=event_type,
        tenant_key=None,
        raw=body,
    )


def _synthesize_confluence_event_id(
    event_type: str, page_id: str, timestamp: Any
) -> str:
    """为缺少 eventId 的 Confluence 事件合成唯一 ID。

    用 event_type + page_id + timestamp 取 SHA-256，保证幂等性
    （同一事件多次推送合成相同 ID）。
    """
    raw = f"confluence:{event_type}:{page_id}:{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ==================================================================
# 统一解析入口
# ==================================================================

def parse_webhook_event(
    adapter_id: str, body: dict[str, Any]
) -> ParsedWebhookEvent | None:
    """按 adapter_id 分发到对应解析器。

    Args:
        adapter_id: 适配器 ID（feishu / confluence）。
        body: 解析后的 JSON dict。

    Returns:
        ParsedWebhookEvent 或 None（非关注事件 / 不支持的平台）。
    """
    if adapter_id == "feishu":
        return parse_feishu_event(body)
    if adapter_id == "confluence":
        return parse_confluence_event(body)
    log.warning("webhook.unsupported_parser", adapter_id=adapter_id)
    return None
