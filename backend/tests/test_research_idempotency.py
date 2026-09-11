"""幂等提交防重复建任务 — 真 PostgreSQL 并发验证。

对应验收："同一次处理被重复提交，只创建一条任务"。
    - 前置查询命中 → 复用原任务（快捷路径）；
    - 两个请求同时查空后并发插入 → 部分唯一索引兜底，只落一条；
    - 同键不同输入 → 409 IDEMPOTENCY_CONFLICT；
    - failed 任务不占键 → 可原键重试；
    - 非幂等约束冲突（外键等）原样上抛，不被翻译成"重复提交"。

并发测试用两个独立连接（同一引擎连接池），并显式安排"双方都查空"的
空档后再放行插入（对应 barrier 语义），不赌机器快慢。
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.models.research import ResearchJob
from app.models.user import User
from app.services.research_job_service import (
    IdempotencyConflictError,
    compute_input_hash,
    create_research_job,
    find_active_job,
    mark_research_job_status,
)
from app.services.submit_idempotency import (
    canonical_input_hash,
    is_unique_violation,
)


# ======================================================================
# 夹具 — 真 PostgreSQL（DATABASE_URL 必须为 postgresql+asyncpg）
# ======================================================================


@pytest_asyncio.fixture(scope="module")
async def pg_engine():
    """模块级引擎 — create_all 幂等建表 + 清空本模块相关表保证可重跑。"""
    from app.models.base import Base
    import app.models  # noqa: F401  确保全部模型注册进 metadata

    engine = create_async_engine(
        os.environ["DATABASE_URL"], echo=False, poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 仅清测试表（ekb_test 专用库），保证固定键可重复运行
        await conn.execute(text("TRUNCATE research_jobs, finetune_dataset_exports"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_maker(pg_engine):
    """会话工厂 — 每次调用产出独立连接的会话（模拟独立请求）。"""
    factory = sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)

    async def _make() -> AsyncSession:
        return factory()

    return _make


@pytest_asyncio.fixture
async def user_a(db_maker) -> SimpleNamespace:
    return await _create_user(db_maker)


@pytest_asyncio.fixture
async def user_b(db_maker) -> SimpleNamespace:
    return await _create_user(db_maker)


async def _create_user(db_maker) -> SimpleNamespace:
    """落一条真实用户行 — research_jobs.user_id 有 FK 约束。"""
    uid = uuid4()
    async with await db_maker() as session:
        session.add(
            User(
                id=uid,
                email=f"idem-{uid}@test.local",
                hashed_password="x",
                name="幂等测试用户",
                role="editor",
            )
        )
        await session.commit()
    return SimpleNamespace(id=uid)


async def _count_jobs(db_maker, *filters) -> int:
    """按过滤条件计数 — 用固定键断言"只落一条"，不受其他测试影响。"""
    async with await db_maker() as session:
        stmt = select(func.count()).select_from(ResearchJob)
        for f in filters:
            stmt = stmt.where(f)
        return (await session.execute(stmt)).scalar_one()


# ======================================================================
# 单元：指纹与约束识别
# ======================================================================


class TestInputHash:
    async def test_kb_ids_order_insensitive(self) -> None:
        """同一份输入换 kb_ids 顺序 → 同一指纹。"""
        h1 = compute_input_hash("调研供应链", ["kb-b", "kb-a"])
        h2 = compute_input_hash("调研供应链", ["kb-a", "kb-b"])
        assert h1 == h2

    async def test_different_goal_different_hash(self) -> None:
        assert compute_input_hash("目标A", None) != compute_input_hash("目标B", None)

    async def test_canonical_hash_stable(self) -> None:
        """canonical_input_hash 键排序 + 紧凑分隔符 — 同内容同指纹。"""
        assert (
            canonical_input_hash({"a": 1, "b": 2})
            == canonical_input_hash({"b": 2, "a": 1})
        )


class TestUniqueViolationDetection:
    def test_matches_only_target_constraint(self) -> None:
        """只认指定约束名 — 其他约束冲突不被翻译成幂等复用。"""
        exc = IntegrityError(
            "INSERT ...",
            {},
            Exception(
                'duplicate key value violates unique constraint '
                '"uq_research_jobs_idem_active"'
            ),
        )
        assert is_unique_violation(exc, "uq_research_jobs_idem_active")
        assert not is_unique_violation(exc, "users_pkey")


# ======================================================================
# 服务层 — 幂等创建（真库）
# ======================================================================


class TestCreateResearchJob:
    async def test_create_then_same_key_reuses(self, db_maker, user_a) -> None:
        """同键同输入重试 → 返回原任务，库里只有一条。"""
        key = "ticket:T-9527:handle:v1"
        ih = compute_input_hash("调研物流时效", ["kb-1"])

        async with await db_maker() as s1:
            job, created = await create_research_job(
                s1, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="调研物流时效", kb_ids=["kb-1"],
            )
            assert created is True
            await s1.commit()

        async with await db_maker() as s2:
            job2, created2 = await create_research_job(
                s2, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="调研物流时效", kb_ids=["kb-1"],
            )
            assert created2 is False
            assert job2.id == job.id

        assert await _count_jobs(db_maker, ResearchJob.idempotency_key == key) == 1

    async def test_same_key_different_input_conflicts(
        self, db_maker, user_a
    ) -> None:
        """同键不同输入 → 409 语义（IdempotencyConflictError），不覆盖原任务。"""
        key = "key:conflict"
        async with await db_maker() as s:
            job, created = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=key,
                input_hash=compute_input_hash("订单 ORD-10086", None),
                goal="订单 ORD-10086", kb_ids=None,
            )
            assert created is True
            await s.commit()

        with pytest.raises(IdempotencyConflictError):
            async with await db_maker() as s2:
                await create_research_job(
                    s2, user_id=user_a.id, tenant_id=None,
                    idempotency_key=key,
                    input_hash=compute_input_hash("订单 ORD-99999", None),
                    goal="订单 ORD-99999", kb_ids=None,
                )

        assert await _count_jobs(db_maker, ResearchJob.idempotency_key == key) == 1
        async with await db_maker() as s3:
            row = await s3.get(ResearchJob, job.id)
            assert row.goal == "订单 ORD-10086"  # 原输入未被覆盖

    async def test_same_key_other_user_creates_own(
        self, db_maker, user_a, user_b
    ) -> None:
        """幂等键按用户隔离 — 不同用户同键各建各的。"""
        key = "shared:key"
        ih = compute_input_hash("同一目标", None)
        async with await db_maker() as s:
            j1, c1 = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="同一目标", kb_ids=None,
            )
            assert c1 is True
            await s.commit()
        async with await db_maker() as s2:
            j2, c2 = await create_research_job(
                s2, user_id=user_b.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="同一目标", kb_ids=None,
            )
            assert c2 is True
            assert j2.id != j1.id
            await s2.commit()
        assert await _count_jobs(db_maker, ResearchJob.idempotency_key == key) == 2

    async def test_without_key_always_creates_new(self, db_maker, user_a) -> None:
        """无幂等键 → 不参与去重（NULL 键在部分唯一索引下互不冲突）。"""
        ih = compute_input_hash("无键提交", None)
        async with await db_maker() as s:
            j1, _ = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=None, input_hash=ih,
                goal="无键提交", kb_ids=None,
            )
            j2, _ = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=None, input_hash=ih,
                goal="无键提交", kb_ids=None,
            )
            await s.commit()
            assert j1.id != j2.id
        assert await _count_jobs(
            db_maker,
            ResearchJob.user_id == user_a.id,
            ResearchJob.goal == "无键提交",
        ) == 2

    async def test_failed_job_releases_key(self, db_maker, user_a) -> None:
        """failed 任务不占键 — 原键重试创建新任务。"""
        key = "key:failed"
        ih = compute_input_hash("会失败的任务", None)
        async with await db_maker() as s:
            job, created = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="会失败的任务", kb_ids=None,
            )
            assert created is True
            await s.commit()

        await mark_research_job_status(str(job.id), "failed", "LLM 超时")

        async with await db_maker() as s2:
            job2, created2 = await create_research_job(
                s2, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="会失败的任务", kb_ids=None,
            )
            assert created2 is True
            assert job2.id != job.id
            await s2.commit()
        assert await _count_jobs(db_maker, ResearchJob.idempotency_key == key) == 2

    async def test_success_job_keeps_key(self, db_maker, user_a) -> None:
        """成功任务继续持有键 — 重试返回原任务（状态 success）。"""
        key = "key:success"
        ih = compute_input_hash("会成功的任务", None)
        async with await db_maker() as s:
            job, _ = await create_research_job(
                s, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="会成功的任务", kb_ids=None,
            )
            await s.commit()
        await mark_research_job_status(str(job.id), "success")

        async with await db_maker() as s2:
            job2, created2 = await create_research_job(
                s2, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="会成功的任务", kb_ids=None,
            )
            assert created2 is False
            assert job2.id == job.id
            assert job2.status == "success"

    async def test_non_idem_constraint_reraised(self, db_maker, user_a) -> None:
        """外键等其他约束冲突 → 原样上抛，不被包装成幂等复用/冲突。"""
        with pytest.raises(IntegrityError) as exc_info:
            async with await db_maker() as s:
                await create_research_job(
                    s, user_id=uuid4(),  # 不存在的用户 → FK 违例
                    tenant_id=None,
                    idempotency_key="key:fk",
                    input_hash=compute_input_hash("坏用户", None),
                    goal="坏用户", kb_ids=None,
                )
        assert not isinstance(exc_info.value, IdempotencyConflictError)

    async def test_concurrent_same_key_creates_single_row(
        self, db_maker, user_a
    ) -> None:
        """并发验证：两个请求都查空后一起插入 → 只落一条，双方拿到同编号。

        安排空档：两个会话各自确认"查无记录"后同步放行创建 ——
        直接复现文章描述的检查-写入窗口，不赌调度快慢。
        """
        key = "ticket:T-0001:handle:v1"
        ih = compute_input_hash("并发重复提交", ["kb-x"])
        checked_empty_1, checked_empty_2 = asyncio.Event(), asyncio.Event()

        async def flow(db: AsyncSession, my_turn, other_turn):
            # 1) 前置查询 —— 两个连接都在对方插入前完成查空
            found = await find_active_job(db, user_a.id, key)
            assert found is None
            # 2) 双方"查空"都到齐才放行创建（barrier 语义）
            my_turn.set()
            await other_turn.wait()
            result = await create_research_job(
                db, user_id=user_a.id, tenant_id=None,
                idempotency_key=key, input_hash=ih,
                goal="并发重复提交", kb_ids=["kb-x"],
            )
            # 3) 赢家立即提交，解除输家在唯一索引上的等待
            await db.commit()
            return result

        s1, s2 = await db_maker(), await db_maker()
        (job_a, created_a), (job_b, created_b) = await asyncio.gather(
            flow(s1, checked_empty_1, checked_empty_2),
            flow(s2, checked_empty_2, checked_empty_1),
        )

        assert created_a != created_b, "必须恰好一个赢家一个回读方"
        winner = job_a if created_a else job_b
        loser = job_b if created_a else job_a
        assert winner.id == loser.id, "双方拿到同一任务编号"
        assert await _count_jobs(db_maker, ResearchJob.idempotency_key == key) == 1


# ======================================================================
# 重建互斥锁 — Redis SETNX（fake redis，不依赖真实实例）
# ======================================================================


class _FakeRedis:
    """极简 async redis 替身 — 记录 set/get/delete。"""

    def __init__(self, store: dict[str, str]):
        self._store = store
        self.calls: list[tuple] = []

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append(("set", key, value, nx, ex))
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        self.calls.append(("get", key))
        return self._store.get(key)

    async def delete(self, key):
        self.calls.append(("delete", key))
        return self._store.pop(key, None)

    async def close(self):
        self.calls.append(("close",))


class TestRebuildLock:
    async def test_acquire_then_duplicate_returns_existing(
        self, monkeypatch
    ) -> None:
        """首个调用抢锁成功；重复调用拿到原 task_id。"""
        from app.services import rebuild_idempotency as mod

        store: dict[str, str] = {}
        monkeypatch.setattr(
            "redis.asyncio.from_url", lambda *a, **k: _FakeRedis(store)
        )

        acquired, existing = await mod.acquire_rebuild_lock("tenant-1", "tid-1")
        assert acquired is True and existing is None

        acquired2, existing2 = await mod.acquire_rebuild_lock("tenant-1", "tid-2")
        assert acquired2 is False
        assert existing2 == "tid-1", "重复提交回读原 task_id"

        # 不同租户互不影响
        acquired3, _ = await mod.acquire_rebuild_lock("tenant-2", "tid-3")
        assert acquired3 is True

    async def test_release_clears_lock(self, monkeypatch) -> None:
        from app.services import rebuild_idempotency as mod

        store: dict[str, str] = {mod.rebuild_lock_key("tenant-1"): "tid-1"}
        monkeypatch.setattr(
            "redis.asyncio.from_url", lambda *a, **k: _FakeRedis(store)
        )
        await mod.release_rebuild_lock("tenant-1")
        acquired, _ = await mod.acquire_rebuild_lock("tenant-1", "tid-9")
        assert acquired is True, "释放后可重新抢锁"


# ======================================================================
# 端点 — POST /research 幂等语义（真库 + dependency_overrides）
# ======================================================================


def _patch_perm_unrestricted():
    """Mock PermissionService — None=不限制（kb_ids 原样通过）。"""
    svc = MagicMock()
    svc.get_accessible_kb_ids = AsyncMock(return_value=None)
    return patch(
        "app.services.permission_service.PermissionService", return_value=svc
    )


def _install_endpoint_overrides(app, db_maker, mock_user):
    """注入真库会话 + 指定用户（替代认证依赖）。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user

    async def override_user():
        return mock_user

    async def override_db():
        session = await db_maker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db


@pytest_asyncio.fixture
async def research_client(db_maker, user_a):
    from app.main import app
    import app.middleware as _mw

    # 重置两级限流器全局 — 幂等测试不应受前置测试（如 test_rate_limiter）
    # 打满的桶影响（否则请求直接 429，掩盖被测的幂等语义）
    _mw._rate_limiter = None
    _mw._tenant_rate_limiter = None
    _install_endpoint_overrides(
        app,
        db_maker,
        SimpleNamespace(
            id=user_a.id, role="editor", is_active=True,
            email="e2e@test.local", name="端点用户",
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestResearchEndpointIdempotency:
    async def test_retry_after_timeout_returns_same_task_id(
        self, research_client, db_maker, user_a
    ) -> None:
        """超时重试：同键同输入 → 返回原 task_id，只派发一次。"""
        key = "ticket:T-100:handle:v1"
        body = {"goal": "重试场景调研", "idempotency_key": key}
        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async"
        ) as mock_dispatch, _patch_perm_unrestricted():
            r1 = await research_client.post("/api/v1/research", json=body)
            assert r1.json()["code"] == 0
            assert r1.json()["data"]["reused"] is False
            first_task_id = r1.json()["data"]["task_id"]
            assert mock_dispatch.call_count == 1

            # 丢弃 r1 返回值，模拟"响应丢了，客户端重试"
            r2 = await research_client.post("/api/v1/research", json=body)

        data2 = r2.json()
        assert data2["code"] == 0
        assert data2["data"]["reused"] is True
        assert data2["data"]["task_id"] == first_task_id
        assert mock_dispatch.call_count == 1, "重试不得再次派发"
        assert await _count_jobs(
            db_maker, ResearchJob.idempotency_key == key
        ) == 1
        assert await _count_jobs(
            db_maker, ResearchJob.user_id == user_a.id
        ) >= 1

    async def test_idempotency_header_supported(self, research_client) -> None:
        """Idempotency-Key 请求头与请求体字段等效。"""
        body = {"goal": "头传键调研"}
        headers = {"Idempotency-Key": "ticket:T-200:handle:v1"}
        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async"
        ), _patch_perm_unrestricted():
            r1 = await research_client.post(
                "/api/v1/research", json=body, headers=headers
            )
            r2 = await research_client.post(
                "/api/v1/research", json=body, headers=headers
            )
        assert r1.json()["data"]["reused"] is False
        assert r2.json()["data"]["reused"] is True
        assert r1.json()["data"]["task_id"] == r2.json()["data"]["task_id"]

    async def test_same_key_different_body_conflict(self, research_client) -> None:
        """同键不同输入 → 409 IDEMPOTENCY_CONFLICT。"""
        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async"
        ), _patch_perm_unrestricted():
            r1 = await research_client.post(
                "/api/v1/research",
                json={"goal": "订单 ORD-10086", "idempotency_key": "k:conflict"},
            )
            r2 = await research_client.post(
                "/api/v1/research",
                json={"goal": "订单 ORD-99999", "idempotency_key": "k:conflict"},
            )
        assert r1.json()["code"] == 0
        assert r2.json()["code"] == 409
        assert r2.json()["data"]["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async def test_without_key_dispatches_each_time(self, research_client) -> None:
        """无键 → 每次都新建（旧行为向后兼容）。"""
        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async"
        ) as mock_dispatch, _patch_perm_unrestricted():
            r1 = await research_client.post(
                "/api/v1/research", json={"goal": "无键第一次"}
            )
            r2 = await research_client.post(
                "/api/v1/research", json={"goal": "无键第一次"}
            )
        assert r1.json()["data"]["reused"] is False
        assert r2.json()["data"]["reused"] is False
        assert r1.json()["data"]["task_id"] != r2.json()["data"]["task_id"]
        assert mock_dispatch.call_count == 2

    async def test_broker_down_rolls_back_and_key_free(
        self, research_client
    ) -> None:
        """派发失败（broker 不可用）→ 回滚不入库，原键可重试。"""
        body = {"goal": "broker 挂了", "idempotency_key": "k:broker-down"}
        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async",
            side_effect=ConnectionError("broker down"),
        ), _patch_perm_unrestricted():
            r1 = await research_client.post("/api/v1/research", json=body)
        assert r1.json()["code"] == 500

        with patch(
            "tasks.deep_research_tasks.deep_research_task.apply_async"
        ) as mock_dispatch, _patch_perm_unrestricted():
            r2 = await research_client.post("/api/v1/research", json=body)
        assert r2.json()["code"] == 0
        assert r2.json()["data"]["reused"] is False, "键未被占用，重试创建成功"
        assert mock_dispatch.call_count == 1


# ======================================================================
# 端点 — finetune 数据集导出幂等（真库）
# ======================================================================


class TestFinetuneExportIdempotency:
    @pytest_asyncio.fixture
    async def admin_client(self, db_maker, user_a):
        from app.main import app
        import app.middleware as _mw

        # 同 research_client — 隔离前置测试残留的限流桶
        _mw._rate_limiter = None
        _mw._tenant_rate_limiter = None
        _install_endpoint_overrides(
            app,
            db_maker,
            SimpleNamespace(
                id=user_a.id, role="admin", is_active=True,
                email="ft@test.local", name="导出管理员",
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            yield c
        app.dependency_overrides.clear()

    @staticmethod
    def _body(**overrides) -> dict:
        body = {
            "dataset_type": "sft",
            "max_classification": "internal",
            "days": 90,
            "min_rating": 4,
            "limit": 10000,
        }
        body.update(overrides)
        return body

    async def test_retry_same_key_returns_same_export(
        self, admin_client, db_maker
    ) -> None:
        """同键同参数重试 → 返回原 export_id/task_id，只 delay 一次。"""
        key = "ft-export:v1"
        body = self._body(idempotency_key=key)
        with patch("tasks.finetune_tasks.build_dataset_task") as mock_task, \
             patch("app.api.v1.finetune.make_version", return_value="v-test-1"):
            mock_task.delay = MagicMock(
                return_value=SimpleNamespace(id="celery-ft-1")
            )
            r1 = await admin_client.post(
                "/api/v1/finetune/datasets/export", json=body
            )
            assert r1.json()["code"] == 0
            first = r1.json()["data"]

            r2 = await admin_client.post(
                "/api/v1/finetune/datasets/export", json=body
            )

        data2 = r2.json()
        assert data2["code"] == 0
        assert data2["data"]["reused"] is True
        assert data2["data"]["export_id"] == first["export_id"]
        assert data2["data"]["task_id"] == first["task_id"]
        assert mock_task.delay.call_count == 1

    async def test_same_key_different_params_conflict(self, admin_client) -> None:
        """同键不同构建参数 → 409 IDEMPOTENCY_CONFLICT。"""
        with patch("tasks.finetune_tasks.build_dataset_task") as mock_task, \
             patch("app.api.v1.finetune.make_version", return_value="v-test-2"):
            mock_task.delay = MagicMock(
                return_value=SimpleNamespace(id="celery-ft-2")
            )
            r1 = await admin_client.post(
                "/api/v1/finetune/datasets/export",
                json=self._body(idempotency_key="ft-export:v2"),
            )
            r2 = await admin_client.post(
                "/api/v1/finetune/datasets/export",
                json=self._body(idempotency_key="ft-export:v2", days=30),
            )
        assert r1.json()["code"] == 0
        assert r2.json()["code"] == 409
        assert r2.json()["data"]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
