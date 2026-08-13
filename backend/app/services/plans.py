"""
套餐定义 — 集中管理 SaaS 各套餐的配额上限与价格。

单一职责：作为套餐配额与价格的单一事实来源，供计费服务（BillingService）
做配额强制、套餐切换与价格展示使用。禁止在业务代码中硬编码配额数值。

配额维度：
    - max_users:        最大用户数（注册/邀请时强制）
    - max_storage_bytes: 最大存储（字节，上传时强制，基于 MinIO/文档 file_size）
    - max_llm_tokens_per_month: LLM 月配额（token，chat 前基于 UsageRecord 强制）
    - price_cents:      套餐价格（分/月，支付接入后用于账单展示）
"""

from __future__ import annotations

# 1 GB / 1 MB 基准
_GB = 1024**3

# 套餐表 — key 与 Tenant.plan / Subscription.plan 取值保持一致
PLANS: dict[str, dict] = {
    "free": {
        "name": "免费版",
        "max_users": 3,
        "max_storage_bytes": 1 * _GB,           # 1 GB
        "max_llm_tokens_per_month": 500_000,    # 50 万 token / 月
        "price_cents": 0,
    },
    "pro": {
        "name": "专业版",
        "max_users": 20,
        "max_storage_bytes": 20 * _GB,          # 20 GB
        "max_llm_tokens_per_month": 5_000_000,  # 500 万 token / 月
        "price_cents": 9900,                    # ¥99 / 月
    },
    "enterprise": {
        "name": "企业版",
        "max_users": 500,
        "max_storage_bytes": 500 * _GB,         # 500 GB
        "max_llm_tokens_per_month": 50_000_000,  # 5000 万 token / 月
        "price_cents": 49900,                   # ¥499 / 月
    },
}

# 默认套餐（租户自助开通时使用）
DEFAULT_PLAN = "free"

# 有效套餐 ID 集合
PLAN_IDS: set[str] = set(PLANS.keys())


def get_plan(plan: str | None) -> dict:
    """获取套餐定义，未知套餐回退到免费版（安全兜底，避免越权配额）。

    Args:
        plan: 套餐 ID（free/pro/enterprise）。

    Returns:
        套餐配置 dict。
    """
    return PLANS.get(plan or "", PLANS[DEFAULT_PLAN])


def is_valid_plan(plan: str) -> bool:
    """检查套餐 ID 是否有效。"""
    return plan in PLAN_IDS
