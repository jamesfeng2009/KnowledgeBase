"""外部平台 Webhook 请求/响应 Schema — P1 Webhook 主动同步。

单一职责：入参校验与出参序列化，不包含业务逻辑。

请求体不定义为 Pydantic 模型 — 签名验证需要原始 body 字节，
端点通过 ``await request.body()`` 读取原始字节后再解析为 dict。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WebhookAckResponse(BaseModel):
    """Webhook 接收确认响应 — 立即返回 200，异步处理。

    外部平台（飞书/Confluence）要求 webhook 在 5s 内返回 200，
    否则视为失败并重试。因此端点收到事件后立即返回此响应，
    实际同步由 Celery 异步执行。
    """

    status: str = Field(default="accepted", description="接收状态")
    event_id: str | None = Field(default=None, description="事件 ID（用于幂等追踪）")
    message: str = Field(default="事件已接收，异步处理中")
    deduplicated: bool = Field(
        default=False, description="是否为重复事件（已处理过，本次跳过）"
    )


class ChallengeResponse(BaseModel):
    """飞书 URL 验证 challenge 应答。

    飞书配置 webhook URL 时会发送 ``{"challenge": "xxx", ...}`` 验证
    URL 可达性，端点需原样返回 ``{"challenge": "xxx"}``。
    """

    challenge: str = Field(..., description="飞书下发的 challenge 值，原样返回")


class WebhookEventPayload(BaseModel):
    """解析后的 webhook 事件负载（用于日志/调试展示）。"""

    adapter_id: str
    source_doc_id: str
    event_id: str
    event_type: str
    raw: dict[str, Any] = Field(default_factory=dict, description="原始事件体")
