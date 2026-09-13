"""Deep Research 结果权威落库（P0-1）+ 卡死补偿扫描（P0-2）— 真 PostgreSQL 验证。

P0-1 验收："Redis 结果键被驱逐/重启丢失后，GET /result 仍能查回报告"：
    - worker 收尾 mark(success, output) — status/output_json/finished_at
      同条 UPDATE 落库，不会出现 "success 配空 output" 中间态；
    - DB success → 直接读 output_json（Redis 丢失不影响）；
    - DB queued + Redis 有终态 → 查询自愈回填 DB 后返回；
    - only_when_queued 守卫 — 回填绝不覆盖已写入的终态；
    - 归属校验 — 非归属者 403，非 UUID / 不存在 404。

P0-2 验收："卡死的 queued 任务被补偿扫描回填或标 failed 释放幂等键"：
    - updated_at 超 2h 且 Redis 有终态 → 回填（救 mark 静默失败）；
    - updated_at 超 2h 且 Redis 无终态 → 标 failed，原键可重试；
    - 新鲜任务（updated_at 刚刷新）不被扫中 — 零误杀。

真库夹具沿用 test_research_idempotency.py 的写法。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.models.research import ResearchJob
from app.models.user import User
from app.services.research_job_service import (
    backfill_result_from_redis,
    compute_input_hash,
    create_research_job,
    mark_research_job_status,
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
        # create_all 不会给已存在的旧表加列 — 幂等补齐 P0-1 新列，
        # 与迁移 d1e2f3a4b5c6 对齐（测试库可能带着旧结构）
        await conn.execute(
            text(
                "ALTER TABLE research_jobs "
                "ADD COLUMN IF NOT EXISTS output_json JSONB, "
                "ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ"
            )
        )
        await conn.execute(text("TRUNCATE research_jobs"))
        await conn.execute(text("TRUNCATE users CASCADE"))
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
async def owner(db_maker) -> SimpleNamespace:
    return await _create_user(db_maker, "owner")


@pytest_asyncio.fixture
async def stranger(db_maker) -> SimpleNamespace:
    return await _create_user(db_maker, "stranger")


async def _create_user(db_maker, tag: str) -> SimpleNamespace:
    """落一条真实用户行 — research_jobs.user_id 有 FK 约束。"""
    uid = uuid4()
    async with await db_maker() as session:
        session.add(
            User(
                id=uid,
                email=f"result-{tag}-{uid}@test.local",
                hashed_password="x",
                name=f"结果测试用户-{tag}",
                role="editor",
            )
        )
        await session.commit()
    return SimpleNamespace(id=uid)


async def _get_job(db_maker, job_id) -> ResearchJob | None:
    async with await db_maker() as session:
        return await session.get(ResearchJob, job_id)


async def _age_job(db_maker, job_id, hours: int = 3) -> None:
    """把任务的 updated_at 拨到 N 小时前 — 模拟"长时间无任何 DB 写"。"""
    async with await db_maker() as session:
        await session.execute(
            text(
                "UPDATE research_jobs SET updated_at = now() - "
                f"interval '{hours} hours' WHERE id = :jid"
            ),
            {"jid": str(job_id)},
        )
        await session.commit()


# ======================================================================
# P0-1 服务层 — 收尾原子落库 + 回填守卫
# ======================================================================


class TestMarkFinalize:
    async def test_success_output_finished_at_same_update(
        self, db_maker, owner
    ) -> None:
        """mark(success, output) — status/output_json/finished_at 同条 UPDATE 落库。"""
        async with await db_maker() as s:
            job, _ = await create_research_job(
                s, user_id=owner.id, tenant_id=None,
                idempotency_key=None,
                input_hash=compute_input_hash("收尾落库", None),
                goal="收尾落库", kb_ids=None,
            )
            await s.commit()

        report = {"summary": "结论", "topics": ["t1", "t2"]}
        await mark_research_job_status(str(job.id), "success", output=report)

        row = await _get_job(db_maker, job.id)
        assert row.status == "success"
        assert row.output_json == report
        assert row.finished_at is not None

    async def test_failed_sets_finished_at_keeps_output_null(
        self, db_maker, owner
    ) -> None:
        """mark(failed) — finished_at 落库，output_json 保持 NULL。"""
        async with await db_maker() as s:
            job, _ = await create_research_job(
                s, user_id=owner.id, tenant_id=None,
                idempotency_key=None,
                input_hash=compute_input_hash("失败收尾", None),
                goal="失败收尾", kb_ids=None,
            )
            await s.commit()

        await mark_research_job_status(str(job.id), "failed", "SHIPMENT_STUB_FAILED")

        row = await _get_job(db_maker, job.id)
        assert row.status == "failed"
        assert row.output_json is None
        assert row.last_error == "SHIPMENT_STUB_FAILED"
        assert row.finished_at is not None

    async def test_backfill_skips_terminal_row(self, db_maker, owner) -> None:
        """only_when_queued 守卫 — 已终态的行绝不被回填覆盖。"""
        async with await db_maker() as s:
            job, _ = await create_research_job(
                s, user_id=owner.id, tenant_id=None,
                idempotency_key=None,
                input_hash=compute_input_hash("终态守卫", None),
                goal="终态守卫", kb_ids=None,
            )
            await s.commit()
        real_report = {"summary": "真实结果"}
        await mark_research_job_status(
            str(job.id), "success", output=real_report
        )

        # 迟到的 Redis 旧失败记录 — 不允许把成功翻成失败
        changed = await backfill_result_from_redis(
            str(job.id), {"status": "failed", "error": "过期失败"}
        )
        row = await _get_job(db_maker, job.id)
        assert changed is False
        assert row.status == "success"
        assert row.output_json == real_report


# ======================================================================
# P0-1 端点 — GET /result DB 权威 + 自愈回填 + 归属校验
# ======================================================================


def _install_overrides(app, db_maker, mock_user):
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
async def api_factory(db_maker):
    """客户端工厂 — 可按不同用户身份创建客户端（测归属校验需两方）。"""
    from app.main import app
    import app.middleware as _mw

    _mw._rate_limiter = None
    _mw._tenant_rate_limiter = None

    made: list[httpx.AsyncClient] = []

    async def _make(user: SimpleNamespace) -> httpx.AsyncClient:
        _install_overrides(app, db_maker, user)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )
        made.append(client)
        return client

    yield _make
    for c in made:
        await c.aclose()
    app.dependency_overrides.clear()


async def _make_job(db_maker, owner, goal: str, key: str | None = None):
    async with await db_maker() as s:
        job, _ = await create_research_job(
            s, user_id=owner.id, tenant_id=None,
            idempotency_key=key,
            input_hash=compute_input_hash(goal, None),
            goal=goal, kb_ids=None,
        )
        await s.commit()
    return job


class TestResultEndpoint:
    async def test_db_authoritative_after_redis_lost(
        self, api_factory, db_maker, owner
    ) -> None:
        """核心验收：Redis 结果键丢失（驱逐/重启）→ 仍从 DB 查回完整报告。"""
        job = await _make_job(db_maker, owner, "Redis 丢失后仍可查")
        report = {"summary": "落库结论", "confidence": 0.9}
        await mark_research_job_status(str(job.id), "success", output=report)

        api = await api_factory(owner)
        with patch(
            "app.services.research_progress.load_result",
            return_value=None,  # 模拟 Redis 结果键被驱逐/TTL 过期
        ):
            resp = await api.get(f"/api/v1/research/{job.id}/result")

        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["status"] == "success"
        assert body["data"]["report"] == report

    async def test_queued_backfills_from_redis_and_returns(
        self, api_factory, db_maker, owner
    ) -> None:
        """自愈：DB 仍 queued（mark 静默失败）+ Redis 有终态 → 回填后返回。"""
        job = await _make_job(db_maker, owner, "mark 静默失败")
        report = {"summary": "自愈回填的结论"}
        redis_data = {"status": "success", "report": report}

        api = await api_factory(owner)
        with patch(
            "app.services.research_progress.load_result",
            return_value=redis_data,
        ):
            resp = await api.get(f"/api/v1/research/{job.id}/result")

        assert resp.json()["data"]["status"] == "success"
        assert resp.json()["data"]["report"] == report

        row = await _get_job(db_maker, job.id)
        assert row.status == "success", "查询触发的回填必须落库"
        assert row.output_json == report

    async def test_queued_without_redis_returns_running(
        self, api_factory, db_maker, owner
    ) -> None:
        """排队/执行中且 Redis 无终态 → running（语义不变）。"""
        job = await _make_job(db_maker, owner, "还在跑")
        api = await api_factory(owner)
        with patch(
            "app.services.research_progress.load_result", return_value=None
        ):
            resp = await api.get(f"/api/v1/research/{job.id}/result")
        assert resp.json()["data"]["status"] == "running"

    async def test_stranger_forbidden_owner_ok(
        self, api_factory, db_maker, owner, stranger
    ) -> None:
        """归属校验：非归属者 403，归属者正常读取。"""
        job = await _make_job(db_maker, owner, "私有调研")
        report = {"summary": "归属者的结论"}
        await mark_research_job_status(str(job.id), "success", output=report)

        owner_api = await api_factory(owner)
        resp_owner = await owner_api.get(f"/api/v1/research/{job.id}/result")
        assert resp_owner.json()["code"] == 0
        assert resp_owner.json()["data"]["report"] == report

        stranger_api = await api_factory(stranger)
        resp_stranger = await stranger_api.get(
            f"/api/v1/research/{job.id}/result"
        )
        assert resp_stranger.json()["code"] == 403

    async def test_missing_or_invalid_id_404(self, api_factory, owner) -> None:
        """非 UUID / 不存在的 task_id → 404，不再回落为 running。"""
        api = await api_factory(owner)
        r1 = await api.get("/api/v1/research/not-a-uuid/result")
        assert r1.json()["code"] == 404
        r2 = await api.get(f"/api/v1/research/{uuid4()}/result")
        assert r2.json()["code"] == 404


# ======================================================================
# P0-2 — 补偿扫描：回填 or 标 failed 释放幂等键
# ======================================================================


class TestRescanStuckResearch:
    async def _scan(self) -> dict:
        from tasks.scheduled_tasks import _rescan_stuck_research_async

        return await _rescan_stuck_research_async()

    async def test_stuck_with_redis_result_backfilled(
        self, db_maker, owner
    ) -> None:
        """卡死 + Redis 有终态 → 回填 DB（救 mark 静默失败）。"""
        job = await _make_job(db_maker, owner, "卡死但有结果")
        await _age_job(db_maker, job.id, hours=3)
        report = {"summary": "扫描回填的结论"}

        with patch(
            "app.services.research_progress.load_result",
            return_value={"status": "success", "report": report},
        ):
            result = await self._scan()

        assert result["backfilled"] >= 1
        row = await _get_job(db_maker, job.id)
        assert row.status == "success"
        assert row.output_json == report
        assert row.finished_at is not None

    async def test_stuck_without_redis_marked_failed_and_key_released(
        self, db_maker, owner
    ) -> None:
        """卡死 + Redis 无终态 → 标 failed 释放幂等键，原键可重试新建。"""
        key = "stuck:ticket:v1"
        job = await _make_job(db_maker, owner, "卡死无结果", key=key)
        await _age_job(db_maker, job.id, hours=3)

        with patch(
            "app.services.research_progress.load_result", return_value=None
        ):
            result = await self._scan()

        assert result["marked_failed"] >= 1
        row = await _get_job(db_maker, job.id)
        assert row.status == "failed"
        assert "重新提交" in (row.last_error or "")

        # 幂等键已释放 — 原键重试可创建新任务
        async with await db_maker() as s:
            job2, created = await create_research_job(
                s, user_id=owner.id, tenant_id=None,
                idempotency_key=key,
                input_hash=compute_input_hash("卡死无结果", None),
                goal="卡死无结果", kb_ids=None,
            )
            await s.commit()
        assert created is True
        assert job2.id != job.id

    async def test_fresh_job_not_scanned(self, db_maker, owner) -> None:
        """新鲜任务（updated_at 刚刷新，含执行中）不被扫中 — 零误杀。"""
        job = await _make_job(db_maker, owner, "刚提交还在执行")

        with patch(
            "app.services.research_progress.load_result", return_value=None
        ) as mock_load:
            result = await self._scan()

        row = await _get_job(db_maker, job.id)
        assert row.status == "queued", "执行中的任务绝不能被标失败"
        mock_load.assert_not_called()
        assert result["candidates"] == 0
