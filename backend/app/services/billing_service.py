"""
计费服务 — SaaS 多租户计费与配额闭环的核心服务。

单一职责：封装租户自助开通、运行时配额强制、订阅生命周期与用量聚合，
使 API 层与业务层不直接操作 Tenant / Subscription / UsageRecord 表结构。

功能覆盖（P0 计费 + 配额闭环）：
    - P0-1 租户自助开通：provision_tenant 注册时自动建租户 + 默认订阅
    - P0-2 运行时配额强制：
        - 用户数（check_user_quota，注册/邀请时调用）
        - 存储（check_storage_quota，上传时基于文档 file_size 汇总）
        - LLM 月配额（check_llm_monthly_quota，chat 前基于 UsageRecord 汇总）
    - P0-3 订阅生命周期：套餐切换、取消、续费、到期/欠费停服判定
    - P0-4 用量/账单聚合：get_usage_aggregate / get_llm_month_usage

配额隔离：所有配额查询都显式按 tenant_id 过滤，杜绝跨租户数据泄漏。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Subscription, Tenant, UsageRecord
from app.models.knowledge import Document
from app.models.user import User
from app.services.plans import DEFAULT_PLAN, get_plan, is_valid_plan

# 订阅状态（与 Subscription.status 一致）
SUB_ACTIVE = "active"
SUB_CANCELLED = "cancelled"
SUB_EXPIRED = "expired"
SUB_PAST_DUE = "past_due"

ACTIVE_SUB_STATUSES = {SUB_ACTIVE}


class QuotaExceededError(Exception):
    """配额超限异常 — 携带配额维度与当前/上限值用于友好提示。"""

    def __init__(
        self,
        dimension: str,
        used: int,
        limit: int,
        message: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        msgs = {
            "users": "用户数已达上限，请升级套餐或删除闲置账号",
            "storage": "存储空间不足，请升级套餐或清理文档",
            "llm": "本月 LLM 用量已达套餐上限，请升级套餐或下月再试",
            "subscription": "订阅已到期或欠费，请续费后继续使用",
        }
        return msgs.get(self.dimension, "配额超限")


class BillingService:
    """计费服务 — 封装租户/订阅/用量领域逻辑。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ==================================================================
    # P0-1 租户自助开通
    # ==================================================================

    async def provision_tenant(
        self,
        name: str,
        domain: str | None = None,
        plan: str = DEFAULT_PLAN,
    ) -> Tenant:
        """创建租户并自动挂载默认订阅（租户自助开通）。

        事务说明：调用方与用户创建共用同一 DB 会话，由外层统一 commit。

        Args:
            name: 租户名称（通常取注册用户姓名或邮箱前缀）。
            domain: 租户域名（可选）。
            plan: 套餐（默认 free）。

        Returns:
            新建的 Tenant 实例。
        """
        plan_cfg = get_plan(plan)
        tenant = Tenant(
            name=name,
            domain=domain,
            plan=plan,
            max_users=plan_cfg["max_users"],
            max_storage=plan_cfg["max_storage_bytes"],
            settings={"enabled_modules": None},  # 交由 TenantService 按套餐默认填充
        )
        self._db.add(tenant)
        await self._db.flush()  # 生成 tenant.id，供订阅外键引用

        now = datetime.now(timezone.utc)
        subscription = Subscription(
            tenant_id=tenant.id,
            plan=plan,
            status=SUB_ACTIVE,
            billing_cycle="monthly",
            seats=1,
            started_at=now,
            auto_renew=True,
            price=plan_cfg["price_cents"],
            metadata_={"source": "self_register"},
        )
        self._db.add(subscription)
        await self._db.flush()
        return tenant

    # ==================================================================
    # 配额查询辅助
    # ==================================================================

    async def get_tenant(self, tenant_id: uuid.UUID | None) -> Tenant | None:
        """获取租户（含到期时间等配额相关字段）。"""
        if tenant_id is None:
            return None
        stmt = select(Tenant).where(
            Tenant.id == tenant_id,
            Tenant.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def _get_active_subscription(
        self, tenant_id: uuid.UUID
    ) -> Subscription | None:
        """获取租户当前活跃订阅（active），无则返回 None。"""
        stmt = (
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status == SUB_ACTIVE,
            )
            .order_by(Subscription.created_at.desc())
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def _effective_plan(self, tenant: Tenant) -> dict:
        """确定租户生效套餐配额。

        优先取订阅套餐；无活跃订阅或租户套餐已过期时，按租户当前 plan
        计费（free 兜底，避免越权配额）。到期判定在 get_plan_status 处理。
        """
        plan_cfg = get_plan(tenant.plan)
        # 若订阅已变更，以订阅为准
        sub = await self._get_active_subscription(tenant.id)
        if sub is not None:
            plan_cfg = get_plan(sub.plan)
        return plan_cfg

    # ==================================================================
    # P0-2 运行时配额强制
    # ==================================================================

    async def get_user_count(self, tenant_id: uuid.UUID) -> int:
        """统计租户当前活跃用户数。"""
        stmt = select(func.count(User.id)).where(
            User.tenant_id == tenant_id,
            User.deleted_at.is_(None),
            User.is_active.is_(True),
        )
        return int(await self._db.scalar(stmt) or 0)

    async def check_user_quota(self, tenant_id: uuid.UUID) -> bool:
        """检查用户数配额（注册/邀请时调用）。

        返回 True 表示允许新增用户；超限抛出 QuotaExceededError。
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return True
        plan_cfg = await self._effective_plan(tenant)
        current = await self.get_user_count(tenant_id)
        if current >= plan_cfg["max_users"]:
            raise QuotaExceededError("users", current, plan_cfg["max_users"])
        return True

    async def get_storage_usage(self, tenant_id: uuid.UUID) -> int:
        """统计租户已用存储（字节）。

        基于 Document.file_size 求和（上传时写入真实文件大小），
        file_size 为空时回退到 content_text 长度近似估算。
        """
        sum_file = select(
            func.coalesce(func.sum(Document.file_size), 0)
        ).where(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
        )
        used = int(await self._db.scalar(sum_file) or 0)

        # 回退估算：无 file_size 的文档按 content_text 长度 + 每条 500KB 兜底
        sum_text = select(
            func.coalesce(func.sum(func.length(Document.content_text)), 0)
        ).where(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
            Document.file_size.is_(None),
        )
        text_len = int(await self._db.scalar(sum_text) or 0)
        used += text_len

        count_no_size = select(func.count(Document.id)).where(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
            Document.file_size.is_(None),
        )
        no_size_count = int(await self._db.scalar(count_no_size) or 0)
        used += no_size_count * 500 * 1024
        return used

    async def check_storage_quota(
        self, tenant_id: uuid.UUID, additional_bytes: int = 0
    ) -> bool:
        """检查存储配额（上传前调用）。

        additional_bytes 为本次新增文件的字节数，用于预检是否超出上限。

        Returns:
            True 表示允许上传；超限抛出 QuotaExceededError。
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return True
        plan_cfg = await self._effective_plan(tenant)
        used = await self.get_storage_usage(tenant_id)
        if used + additional_bytes > plan_cfg["max_storage_bytes"]:
            raise QuotaExceededError(
                "storage", used + additional_bytes, plan_cfg["max_storage_bytes"]
            )
        return True

    async def get_llm_month_usage(
        self, tenant_id: uuid.UUID, month_start: datetime | None = None
    ) -> int:
        """统计租户当月 LLM token 用量（input + output）。

        Args:
            tenant_id: 租户 ID。
            month_start: 月起始时间（默认当月 1 日 0 点 UTC）。

        Returns:
            当月累计 token 数。
        """
        if month_start is None:
            now = datetime.now(timezone.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stmt = select(
            func.coalesce(
                func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
            )
        ).where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.created_at >= month_start,
        )
        return int(await self._db.scalar(stmt) or 0)

    async def check_llm_monthly_quota(
        self, tenant_id: uuid.UUID, additional_tokens: int = 0
    ) -> bool:
        """检查 LLM 月配额（chat 前调用）。

        Returns:
            True 表示允许继续；超限抛出 QuotaExceededError。
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return True
        plan_cfg = await self._effective_plan(tenant)
        used = await self.get_llm_month_usage(tenant_id)
        if used + additional_tokens >= plan_cfg["max_llm_tokens_per_month"]:
            raise QuotaExceededError("llm", used, plan_cfg["max_llm_tokens_per_month"])
        return True

    # ==================================================================
    # P0-3 订阅生命周期（手动开通过渡）
    # ==================================================================

    async def get_subscription(self, tenant_id: uuid.UUID) -> Subscription | None:
        """获取租户当前订阅（取最新一条，含非 active 状态用于展示）。"""
        stmt = (
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def switch_plan(
        self,
        tenant_id: uuid.UUID,
        plan: str,
        manual: bool = True,
    ) -> Subscription:
        """套餐切换（手动开通过渡：管理员开通后立即生效）。

        规则：
        - 校验套餐 ID 合法；
        - 将当前活跃订阅置为 expired（历史记录保留）；
        - 新建目标套餐的活跃订阅，并同步更新租户配额字段。

        Args:
            tenant_id: 租户 ID。
            plan: 目标套餐（free/pro/enterprise）。
            manual: 是否手动开通（占位，为支付接入预留）。

        Returns:
            新建的活跃订阅。
        """
        if not is_valid_plan(plan):
            raise ValueError(f"未知的套餐: {plan}")

        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            raise ValueError("租户不存在")

        plan_cfg = get_plan(plan)

        # 关闭旧的活跃订阅
        current = await self._get_active_subscription(tenant_id)
        now = datetime.now(timezone.utc)
        if current is not None:
            current.status = SUB_EXPIRED
            current.ended_at = now

        # 创建新订阅
        sub = Subscription(
            tenant_id=tenant_id,
            plan=plan,
            status=SUB_ACTIVE,
            billing_cycle="monthly",
            seats=1,
            started_at=now,
            auto_renew=True,
            price=plan_cfg["price_cents"],
            metadata_={"source": "manual_activation" if manual else "payment"},
        )
        self._db.add(sub)

        # 同步租户配额字段
        tenant.plan = plan
        tenant.max_users = plan_cfg["max_users"]
        tenant.max_storage = plan_cfg["max_storage_bytes"]
        await self._db.flush()
        return sub

    async def cancel_subscription(self, tenant_id: uuid.UUID) -> Subscription | None:
        """取消订阅（停服：到期不再续费）。

        取消后订阅状态置为 cancelled，租户到期时间置为当前时间，
        后续配额强制在下次访问时生效（到期判定见 get_plan_status）。
        """
        sub = await self._get_active_subscription(tenant_id)
        if sub is None:
            return None
        now = datetime.now(timezone.utc)
        sub.status = SUB_CANCELLED
        sub.auto_renew = False
        sub.cancelled_at = now
        sub.ended_at = now

        tenant = await self.get_tenant(tenant_id)
        if tenant is not None:
            tenant.expired_at = now
        await self._db.flush()
        return sub

    async def reactivate_subscription(self, tenant_id: uuid.UUID) -> Subscription | None:
        """恢复订阅（续费后重新激活为 free 套餐）。

        手动开通过渡：欠费/到期后，管理员恢复时默认回到 free 套餐
        并清除租户过期标记。
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            raise ValueError("租户不存在")
        now = datetime.now(timezone.utc)
        plan_cfg = get_plan(DEFAULT_PLAN)

        sub = Subscription(
            tenant_id=tenant_id,
            plan=DEFAULT_PLAN,
            status=SUB_ACTIVE,
            billing_cycle="monthly",
            seats=1,
            started_at=now,
            auto_renew=True,
            price=plan_cfg["price_cents"],
            metadata_={"source": "reactivate"},
        )
        self._db.add(sub)
        tenant.expired_at = None
        await self._db.flush()
        return sub

    async def get_plan_status(
        self, tenant_id: uuid.UUID
    ) -> dict[str, Any]:
        """计算租户当前计划状态（用于到期/欠费停服判定与前端展示）。

        Returns:
            dict: plan / status / expired_at / usable。
            usable=False 表示到期或欠费，应拒绝计费型功能调用。
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return {"plan": DEFAULT_PLAN, "status": SUB_EXPIRED, "expired_at": None, "usable": False}

        sub = await self.get_subscription(tenant_id)
        status = sub.status if sub else SUB_EXPIRED
        now = datetime.now(timezone.utc)

        usable = True
        expired_at = tenant.expired_at
        if status in (SUB_EXPIRED, SUB_PAST_DUE, SUB_CANCELLED):
            usable = False
        if status == SUB_ACTIVE and expired_at is not None and expired_at <= now:
            # 活跃订阅但租户到期时间已过 → 停服
            status = SUB_EXPIRED
            usable = False

        return {
            "plan": tenant.plan,
            "status": status,
            "expired_at": expired_at,
            "usable": usable,
        }

    async def require_usable(self, tenant_id: uuid.UUID) -> None:
        """确保租户处于可用状态，否则抛出 QuotaExceededError（停服）。"""
        status = await self.get_plan_status(tenant_id)
        if not status["usable"]:
            raise QuotaExceededError("subscription", 1, 1)

    # ==================================================================
    # P0-4 用量/账单聚合
    # ==================================================================

    async def get_usage_aggregate(
        self,
        tenant_id: uuid.UUID,
        month_start: datetime | None = None,
    ) -> dict[str, Any]:
        """聚合租户当月用量与账单信息。

        Returns:
            dict 包含：
            - llm_tokens / llm_limit / llm_used_pct（LLM 月配额使用情况）
            - cost_cents / cost_limit（套餐价格，账单）
            - user_count / user_limit
            - storage_bytes / storage_limit
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return {
                "llm_tokens": 0, "llm_limit": 0, "llm_used_pct": 0,
                "cost_cents": 0, "cost_limit": 0,
                "user_count": 0, "user_limit": 0,
                "storage_bytes": 0, "storage_limit": 0,
            }

        plan_cfg = await self._effective_plan(tenant)
        llm_used = await self.get_llm_month_usage(tenant_id, month_start)
        user_count = await self.get_user_count(tenant_id)
        storage_used = await self.get_storage_usage(tenant_id)

        llm_limit = plan_cfg["max_llm_tokens_per_month"]
        cost_cents = self._estimate_llm_cost_cents(llm_used)
        return {
            "llm_tokens": llm_used,
            "llm_limit": llm_limit,
            "llm_used_pct": round(llm_used / llm_limit * 100, 2) if llm_limit else 0,
            "cost_cents": cost_cents,
            "cost_limit": plan_cfg["price_cents"],
            "user_count": user_count,
            "user_limit": plan_cfg["max_users"],
            "storage_bytes": storage_used,
            "storage_limit": plan_cfg["max_storage_bytes"],
        }

    @staticmethod
    def _estimate_llm_cost_cents(tokens: int) -> int:
        """估算用量对应的成本（分）— 用于账单展示的粗略估算。

        采用统一单价基准（约 ¥0.03 / 千 token），后续支付接入后
        由真实成本核算替代。
        """
        return int(tokens / 1000 * 3)
