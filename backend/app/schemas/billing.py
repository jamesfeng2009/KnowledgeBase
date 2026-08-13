"""
P2 补充 Schema — 单一职责：为缺少 Response Schema 的模型补充 Pydantic 响应模型。

补全的 Schema：
- NotificationResponse: 通知响应
- KnowledgeGapResponse: 知识缺口响应
- DocumentActionResponse: 文档行动项响应
- SearchLogResponse: 搜索日志响应
- MemoryFactResponse: 记忆事实响应
- UsageRecordResponse: 用量记录响应（P1-5 配套）
- SubscriptionResponse: 订阅响应（P1-6 配套）

同时补充 updated_at 字段到以下 Schema（通过各自 schema 文件修改）：
- ConversationResponse
- MessageResponse
- CommentResponse
- FeedbackResponse
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# P2: 缺失的 Response Schema 补全
# ======================================================================


class NotificationResponse(BaseModel):
    """通知响应 Schema — 补全模型缺少的 Response。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="通知 ID")
    user_id: uuid.UUID = Field(..., description="接收用户 ID")
    notification_type: str = Field(..., description="通知类型: personal_digest/document_change/gap_alert")
    title: str = Field(..., description="通知标题")
    content: str = Field(..., description="通知内容")
    doc_id: uuid.UUID | None = Field(default=None, description="关联文档 ID")
    is_read: bool = Field(default=False, description="是否已读")
    read_at: datetime | None = Field(default=None, description="已读时间")
    tenant_id: uuid.UUID | None = Field(default=None, description="租户 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class KnowledgeGapResponse(BaseModel):
    """知识缺口响应 Schema — 补全模型缺少的 Response。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="缺口 ID")
    question: str = Field(..., description="缺口问题描述")
    frequency: int = Field(default=1, ge=1, description="出现频次")
    status: str = Field(
        default="open", description="状态: open/analyzing/resolved/ignored"
    )
    suggested_doc_title: str | None = Field(
        default=None, description="建议文档标题"
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class DocumentActionResponse(BaseModel):
    """文档行动项响应 Schema — 补全模型缺少的 Response。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="行动项 ID")
    doc_id: uuid.UUID = Field(..., description="文档 ID")
    user_id: uuid.UUID = Field(..., description="负责人 ID")
    action_type: str = Field(..., description="行动类型: todo/followup/decision")
    description: str = Field(..., description="行动描述")
    due_date: datetime | None = Field(default=None, description="截止时间")
    status: str = Field(default="pending", description="状态: pending/completed")
    tenant_id: uuid.UUID | None = Field(default=None, description="租户 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class SearchLogResponse(BaseModel):
    """搜索日志响应 Schema — 补全模型缺少的 Response。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="日志 ID")
    user_id: uuid.UUID | None = Field(default=None, description="用户 ID（匿名为空）")
    query: str = Field(..., description="搜索关键词")
    source: str = Field(..., description="搜索源: knowledge_base/oa/erp/crm")
    result_count: int = Field(default=0, ge=0, description="返回结果数")
    clicked: bool = Field(default=False, description="是否点击了结果")
    clicked_doc_id: uuid.UUID | None = Field(
        default=None, description="点击的文档 ID"
    )
    tenant_id: uuid.UUID | None = Field(default=None, description="租户 ID")
    created_at: datetime = Field(..., description="创建时间")


class MemoryFactResponse(BaseModel):
    """记忆事实响应 Schema — 补全模型缺少的 Response。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="事实 ID")
    user_id: uuid.UUID = Field(..., description="用户 ID")
    category: str = Field(..., description="事实类别")
    fact_text: str = Field(..., description="事实文本")
    fact_key: str | None = Field(default=None, description="事实键")
    fact_value: str | None = Field(default=None, description="事实值")
    is_active: bool = Field(default=True, description="是否有效")
    expires_at: datetime | None = Field(default=None, description="过期时间")
    tenant_id: uuid.UUID | None = Field(default=None, description="租户 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


# ======================================================================
# P1-5/P1-6 配套 Schema
# ======================================================================


class UsageRecordResponse(BaseModel):
    """用量记录响应 Schema — P1-5 配套。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="记录 ID")
    tenant_id: uuid.UUID = Field(..., description="租户 ID")
    user_id: uuid.UUID = Field(..., description="用户 ID")
    model: str = Field(..., description="使用的模型")
    input_tokens: int = Field(default=0, ge=0, description="输入 token")
    output_tokens: int = Field(default=0, ge=0, description="输出 token")
    cost_cents: int = Field(default=0, ge=0, description="成本（分）")
    request_type: str = Field(..., description="请求类型: chat/embed/rerank/vision")
    duration_ms: int = Field(default=0, ge=0, description="请求耗时（毫秒）")
    success: bool = Field(default=True, description="是否成功")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
    created_at: datetime = Field(..., description="创建时间")


class SubscriptionResponse(BaseModel):
    """订阅响应 Schema — P1-6 配套。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="订阅 ID")
    tenant_id: uuid.UUID = Field(..., description="租户 ID")
    plan: str = Field(..., description="套餐: free/pro/enterprise")
    status: str = Field(
        default="active", description="状态: active/cancelled/expired/past_due"
    )
    billing_cycle: str = Field(
        default="monthly", description="计费周期: monthly/yearly"
    )
    seats: int = Field(default=1, ge=1, description="席位数")
    started_at: datetime = Field(..., description="开始时间")
    ended_at: datetime | None = Field(default=None, description="结束时间")
    cancelled_at: datetime | None = Field(default=None, description="取消时间")
    auto_renew: bool = Field(default=True, description="是否自动续费")
    price: int = Field(default=0, ge=0, description="价格（分）")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


# ======================================================================
# P0 计费 + 配额闭环 Schema
# ======================================================================


class PlanInfo(BaseModel):
    """套餐信息 Schema — P0-3 套餐展示。"""

    id: str = Field(..., description="套餐 ID: free/pro/enterprise")
    name: str = Field(..., description="套餐名称")
    max_users: int = Field(..., ge=0, description="最大用户数")
    max_storage_bytes: int = Field(..., ge=0, description="最大存储（字节）")
    max_llm_tokens_per_month: int = Field(..., ge=0, description="LLM 月配额（token）")
    price_cents: int = Field(..., ge=0, description="价格（分/月）")


class PlanStatus(BaseModel):
    """租户计划状态 Schema — 到期/欠费停服判定与前端展示。"""

    plan: str = Field(..., description="套餐 ID")
    status: str = Field(..., description="订阅状态: active/cancelled/expired/past_due")
    expired_at: datetime | None = Field(default=None, description="到期时间")
    usable: bool = Field(..., description="是否可用（false 表示到期/欠费停服）")


class UsageAggregate(BaseModel):
    """用量/账单聚合 Schema — P0-4。"""

    llm_tokens: int = Field(..., ge=0, description="当月 LLM token 用量")
    llm_limit: int = Field(..., ge=0, description="LLM 月配额上限")
    llm_used_pct: float = Field(..., ge=0, description="LLM 配额使用百分比")
    cost_cents: int = Field(..., ge=0, description="估算成本（分）")
    cost_limit: int = Field(..., ge=0, description="套餐价格（分/月）")
    user_count: int = Field(..., ge=0, description="当前用户数")
    user_limit: int = Field(..., ge=0, description="用户数上限")
    storage_bytes: int = Field(..., ge=0, description="已用存储（字节）")
    storage_limit: int = Field(..., ge=0, description="存储上限（字节）")
