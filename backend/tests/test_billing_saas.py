"""
SaaS 计费 + 配额闭环测试（P0-1 ~ P0-4）。

覆盖：
1. 套餐定义（plans.py）— 配额与价格、回退逻辑
2. BillingService 租户自助开通（P0-1 provision_tenant）
3. 运行时配额强制（P0-2）：用户数 / 存储 / LLM 月配额
4. 订阅生命周期（P0-3）：套餐切换、取消、恢复、到期/欠费停服判定
5. 用量/账单聚合（P0-4）
6. AuthService.register 自动建租户 + 管理员

说明：本批单测不连接真实数据库，通过 FakeSession 与 mock 隔离 DB，
避免依赖本地 PostgreSQL 实例（项目硬约束仍为 PostgreSQL，仅测试隔离）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.billing import Subscription, Tenant
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.billing_service import (
    BillingService,
    QuotaExceededError,
    SUB_ACTIVE,
    SUB_CANCELLED,
    SUB_EXPIRED,
)
from app.services.plans import (
    DEFAULT_PLAN,
    PLANS,
    get_plan,
    is_valid_plan,
)


# ==================================================================
# 工具：FakeSession — 记录 add/flush，execute/scalar 返回空
# ==================================================================


class _FakeScalars:
    def first(self):
        return None

    def all(self):
        return []


class _FakeResult:
    def scalars(self):
        return _FakeScalars()

    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def all(self):
        return []


class FakeSession:
    """轻量 FakeSession — 支持 add/flush/execute/scalar 的基本形态。"""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        # 模拟 SQLAlchemy 在 INSERT flush 时为 UUID 主键生成 id
        for obj in self.added:
            if getattr(obj, "id", None) is None and hasattr(obj, "id"):
                obj.id = uuid.uuid4()
        self.flushed = list(self.added)

    async def execute(self, stmt):
        return _FakeResult()

    async def scalar(self, stmt):
        return None

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _make_tenant(plan: str = DEFAULT_PLAN, **kwargs) -> Tenant:
    cfg = get_plan(plan)
    return Tenant(
        id=uuid.uuid4(),
        name="测试租户",
        plan=plan,
        max_users=cfg["max_users"],
        max_storage=cfg["max_storage_bytes"],
        **kwargs,
    )


# ==================================================================
# 1. 套餐定义
# ==================================================================


class TestPlans:
    def test_plans_has_all_required(self):
        """包含 free/pro/enterprise 三个套餐。"""
        for pid in ("free", "pro", "enterprise"):
            assert pid in PLANS

    def test_free_price_zero(self):
        """免费版价格为 0。"""
        assert PLANS["free"]["price_cents"] == 0

    def test_pro_limits_gt_free(self):
        """pro 配额高于 free。"""
        assert PLANS["pro"]["max_users"] > PLANS["free"]["max_users"]
        assert (
            PLANS["pro"]["max_storage_bytes"] > PLANS["free"]["max_storage_bytes"]
        )
        assert (
            PLANS["pro"]["max_llm_tokens_per_month"]
            > PLANS["free"]["max_llm_tokens_per_month"]
        )

    def test_enterprise_limits_gt_pro(self):
        """enterprise 配额高于 pro。"""
        assert PLANS["enterprise"]["max_users"] > PLANS["pro"]["max_users"]
        assert (
            PLANS["enterprise"]["max_storage_bytes"]
            > PLANS["pro"]["max_storage_bytes"]
        )

    def test_get_plan_unknown_falls_back_to_free(self):
        """未知套餐回退到 free（安全兜底）。"""
        cfg = get_plan("nonexistent")
        assert cfg["name"] == PLANS[DEFAULT_PLAN]["name"]

    def test_is_valid_plan(self):
        """is_valid_plan 校验。"""
        assert is_valid_plan("free")
        assert is_valid_plan("pro")
        assert is_valid_plan("enterprise")
        assert not is_valid_plan("gold")


# ==================================================================
# 2. BillingService — 租户自助开通（P0-1）
# ==================================================================


class TestProvisionTenant:
    async def test_creates_tenant_and_subscription(self):
        """创建租户并自动挂载默认订阅。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = await service.provision_tenant(name="张三", domain="zhangsan.io")

        assert tenant.id is not None
        assert tenant.plan == DEFAULT_PLAN
        # 自动挂载订阅
        subs = [o for o in session.added if isinstance(o, Subscription)]
        assert len(subs) == 1
        assert subs[0].tenant_id == tenant.id
        assert subs[0].status == SUB_ACTIVE
        assert subs[0].price == get_plan(DEFAULT_PLAN)["price_cents"]

    async def test_provision_specific_plan(self):
        """可指定套餐开通。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = await service.provision_tenant(name="企业", plan="pro")
        cfg = get_plan("pro")
        assert tenant.max_users == cfg["max_users"]
        assert tenant.max_storage == cfg["max_storage_bytes"]


# ==================================================================
# 3. 运行时配额强制（P0-2）
# ==================================================================


class TestQuotaEnforcement:
    async def test_check_user_quota_ok(self):
        """用户数未超限返回 True。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_user_count", AsyncMock(return_value=1)
            ):
                assert await service.check_user_quota(tenant.id) is True

    async def test_check_user_quota_exceeded(self):
        """用户数超限抛出 QuotaExceededError。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        limit = get_plan("free")["max_users"]
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_user_count", AsyncMock(return_value=limit)
            ):
                with pytest.raises(QuotaExceededError) as exc:
                    await service.check_user_quota(tenant.id)
                assert exc.value.dimension == "users"

    async def test_check_user_quota_no_tenant(self):
        """无租户（私有部署）时返回 True。"""
        session = FakeSession()
        service = BillingService(session)
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=None)):
            assert await service.check_user_quota(uuid.uuid4()) is True

    async def test_check_storage_quota_exceeded(self):
        """存储超限抛出 QuotaExceededError。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        limit = get_plan("free")["max_storage_bytes"]
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_storage_usage", AsyncMock(return_value=limit)
            ):
                with pytest.raises(QuotaExceededError) as exc:
                    await service.check_storage_quota(tenant.id, additional_bytes=1)
                assert exc.value.dimension == "storage"

    async def test_check_llm_quota_ok(self):
        """LLM 月配额未超限返回 True。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("pro")
        limit = get_plan("pro")["max_llm_tokens_per_month"]
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_llm_month_usage", AsyncMock(return_value=limit - 1)
            ):
                assert await service.check_llm_monthly_quota(tenant.id) is True

    async def test_check_llm_quota_exceeded(self):
        """LLM 月配额超限抛出 QuotaExceededError。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        limit = get_plan("free")["max_llm_tokens_per_month"]
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_llm_month_usage", AsyncMock(return_value=limit)
            ):
                with pytest.raises(QuotaExceededError) as exc:
                    await service.check_llm_monthly_quota(tenant.id)
                assert exc.value.dimension == "llm"


# ==================================================================
# 4. 订阅生命周期（P0-3）
# ==================================================================


class TestSubscriptionLifecycle:
    async def test_switch_plan_invalid(self):
        """未知套餐抛出 ValueError。"""
        session = FakeSession()
        service = BillingService(session)
        with pytest.raises(ValueError, match="未知的套餐"):
            await service.switch_plan(uuid.uuid4(), "gold")

    async def test_switch_plan_updates_tenant_and_creates_sub(self):
        """切换套餐创建新订阅并同步租户配额字段。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        old_sub = Subscription(
            tenant_id=tenant.id, plan="free", status=SUB_ACTIVE, started_at=datetime.now(timezone.utc)
        )
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "_get_active_subscription", AsyncMock(return_value=old_sub)
            ):
                sub = await service.switch_plan(tenant.id, "pro", manual=True)

        # 旧订阅置为 expired
        assert old_sub.status == SUB_EXPIRED
        # 新订阅为 pro
        assert sub.plan == "pro"
        assert sub.status == SUB_ACTIVE
        # 租户配额同步
        cfg = get_plan("pro")
        assert tenant.plan == "pro"
        assert tenant.max_users == cfg["max_users"]
        assert tenant.max_storage == cfg["max_storage_bytes"]

    async def test_cancel_subscription_sets_expired(self):
        """取消订阅置为 cancelled 并设置租户到期时间。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("pro")
        sub = Subscription(
            tenant_id=tenant.id, plan="pro", status=SUB_ACTIVE, started_at=datetime.now(timezone.utc)
        )
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "_get_active_subscription", AsyncMock(return_value=sub)
            ):
                result = await service.cancel_subscription(tenant.id)

        assert result is not None
        assert result.status == SUB_CANCELLED
        assert result.auto_renew is False
        assert tenant.expired_at is not None

    async def test_plan_status_usable(self):
        """active 订阅且未到期 → usable=True。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("pro")
        sub = Subscription(
            tenant_id=tenant.id, plan="pro", status=SUB_ACTIVE, started_at=datetime.now(timezone.utc)
        )
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_subscription", AsyncMock(return_value=sub)
            ):
                status = await service.get_plan_status(tenant.id)
        assert status["usable"] is True
        assert status["status"] == SUB_ACTIVE

    async def test_plan_status_expired(self):
        """订阅 expired → usable=False。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("pro")
        sub = Subscription(
            tenant_id=tenant.id, plan="pro", status=SUB_EXPIRED, started_at=datetime.now(timezone.utc)
        )
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_subscription", AsyncMock(return_value=sub)
            ):
                status = await service.get_plan_status(tenant.id)
        assert status["usable"] is False
        assert status["status"] == SUB_EXPIRED

    async def test_plan_status_past_due_blocked(self):
        """past_due（欠费）→ usable=False。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        sub = Subscription(
            tenant_id=tenant.id, plan="free", status="past_due", started_at=datetime.now(timezone.utc)
        )
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_subscription", AsyncMock(return_value=sub)
            ):
                status = await service.get_plan_status(tenant.id)
        assert status["usable"] is False

    async def test_require_usable_raises_on_expired(self):
        """停服时 require_usable 抛出 QuotaExceededError。"""
        session = FakeSession()
        service = BillingService(session)
        with patch.object(
            BillingService,
            "get_plan_status",
            AsyncMock(
                return_value={
                    "plan": "free",
                    "status": SUB_EXPIRED,
                    "expired_at": datetime.now(timezone.utc),
                    "usable": False,
                }
            ),
        ):
            with pytest.raises(QuotaExceededError) as exc:
                await service.require_usable(uuid.uuid4())
            assert exc.value.dimension == "subscription"

    async def test_require_usable_passes_when_active(self):
        """可用租户 require_usable 不抛异常。"""
        session = FakeSession()
        service = BillingService(session)
        with patch.object(
            BillingService,
            "get_plan_status",
            AsyncMock(
                return_value={
                    "plan": "free",
                    "status": SUB_ACTIVE,
                    "expired_at": None,
                    "usable": True,
                }
            ),
        ):
            await service.require_usable(uuid.uuid4())  # 不抛异常


# ==================================================================
# 5. 用量/账单聚合（P0-4）
# ==================================================================


class TestUsageAggregate:
    async def test_get_usage_aggregate(self):
        """聚合返回各配额维度数据。"""
        session = FakeSession()
        service = BillingService(session)
        tenant = _make_tenant("free")
        free = get_plan("free")
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=tenant)):
            with patch.object(
                BillingService, "get_llm_month_usage", AsyncMock(return_value=1000)
            ):
                with patch.object(
                    BillingService, "get_user_count", AsyncMock(return_value=2)
                ):
                    with patch.object(
                        BillingService, "get_storage_usage", AsyncMock(return_value=100)
                    ):
                        agg = await service.get_usage_aggregate(tenant.id)

        assert agg["llm_tokens"] == 1000
        assert agg["llm_limit"] == free["max_llm_tokens_per_month"]
        assert agg["user_count"] == 2
        assert agg["user_limit"] == free["max_users"]
        assert agg["storage_bytes"] == 100
        assert agg["storage_limit"] == free["max_storage_bytes"]
        assert agg["cost_limit"] == free["price_cents"]
        assert agg["llm_used_pct"] > 0

    async def test_get_usage_aggregate_no_tenant(self):
        """无租户返回全 0。"""
        session = FakeSession()
        service = BillingService(session)
        with patch.object(BillingService, "get_tenant", AsyncMock(return_value=None)):
            agg = await service.get_usage_aggregate(uuid.uuid4())
        assert agg["llm_tokens"] == 0
        assert agg["storage_limit"] == 0


# ==================================================================
# 6. AuthService.register 自动建租户（P0-1）
# ==================================================================


class TestRegisterAutoProvision:
    async def test_register_without_tenant_provisions_and_admin(self):
        """无 tenant_id 时自动建租户，首位用户为 admin。"""
        session = FakeSession()
        service = AuthService(session)

        created_user = User(
            id=uuid.uuid4(),
            email="new@example.com",
            hashed_password="x",
            name="新用户",
            role="admin",
            tenant_id=uuid.uuid4(),
        )

        # patch user_repo.create 返回一个带有 tenant_id 的用户
        async def fake_create(**kwargs):
            created_user.tenant_id = kwargs.get("tenant_id")
            created_user.role = kwargs.get("role", "viewer")
            return created_user

        with patch.object(
            service.user_repo, "get_by_email", AsyncMock(return_value=None)
        ):
            with patch.object(
                service.user_repo, "create", side_effect=fake_create
            ):
                with patch.object(
                    BillingService, "provision_tenant", AsyncMock(
                        return_value=Tenant(id=created_user.tenant_id, name="新用户")
                    )
                ) as mock_provision:
                    user = await service.register("new@example.com", "pw", "新用户")

        mock_provision.assert_awaited_once()
        assert user.role == "admin"
        assert user.tenant_id is not None

    async def test_register_with_tenant_checks_user_quota(self):
        """指定 tenant_id 时校验用户数配额，保持 viewer 角色。"""
        session = FakeSession()
        service = AuthService(session)
        tid = uuid.uuid4()
        created_user = User(
            id=uuid.uuid4(),
            email="member@example.com",
            hashed_password="x",
            name="成员",
            role="viewer",
            tenant_id=tid,
        )

        async def fake_create(**kwargs):
            created_user.tenant_id = kwargs.get("tenant_id")
            created_user.role = kwargs.get("role", "viewer")
            return created_user

        with patch.object(
            service.user_repo, "get_by_email", AsyncMock(return_value=None)
        ):
            with patch.object(
                service.user_repo, "create", side_effect=fake_create
            ):
                with patch.object(
                    BillingService, "check_user_quota", AsyncMock(return_value=True)
                ) as mock_quota:
                    user = await service.register(
                        "member@example.com", "pw", "成员", tenant_id=tid
                    )

        mock_quota.assert_awaited_once_with(tid)
        assert user.role == "viewer"
        assert user.tenant_id == tid
