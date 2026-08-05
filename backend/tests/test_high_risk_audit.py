"""
高风险拦截审计服务测试 — P1-8。

覆盖：
    1. record_block_audit：block 决策落库（Mock 会话工厂，不依赖真实 DB）；
    2. record_block_audit 异常降级（DB 失败返回 None，不抛出）；
    3. list_audits：分页与复查状态过滤；
    4. review_audit：标记 confirmed/misjudged、记录不存在、无效状态；
    5. get_misjudgment_stats：误判率计算与当前阈值回显；
    6. engine 接线：block 触发审计调度、confirm 设置提示标记。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.models.high_risk import HighRiskAuditRecord
from app.services.high_risk_audit_service import HighRiskAuditService


# ======================================================================
# Fake 会话工厂
# ======================================================================


class FakeResult:
    """模拟 SQLAlchemy Result — 支持 scalar_one / scalars().all() / all()。"""

    def __init__(self, *, scalar: Any = None, rows: list | None = None) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list:
        return self._rows


class FakeSession:
    """模拟 AsyncSession — 记录 add/commit，按队列返回 execute 结果。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits: int = 0
        self.execute_results: list[FakeResult] = []
        self.records: dict[uuid.UUID, HighRiskAuditRecord] = {}
        self.raise_on_commit: bool = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self.raise_on_commit:
            raise RuntimeError("DB connection lost")
        # 模拟 flush：为未赋主键的记录生成 id（真实 PG 在 flush 时应用客户端默认值）
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
        self.commits += 1

    async def get(self, model: Any, rid: uuid.UUID) -> Any:
        return self.records.get(rid)

    async def execute(self, stmt: Any) -> FakeResult:
        return self.execute_results.pop(0)


class FakeSessionFactory:
    """模拟 async_session_factory — 可调用，返回 FakeSession 上下文管理器。"""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSession:
        return self.session


def _make_service(session: FakeSession) -> HighRiskAuditService:
    return HighRiskAuditService(session_factory=FakeSessionFactory(session))


# ======================================================================
# 1. record_block_audit
# ======================================================================


class TestRecordBlockAudit:
    """block 决策落库测试。"""

    @pytest.mark.asyncio
    async def test_record_persists_all_fields(self) -> None:
        session = FakeSession()
        service = _make_service(session)
        user_id = str(uuid.uuid4())

        record_id = await service.record_block_audit(
            query="报销上限是多少",
            answer="报销上限为 99999元",
            session_id="sess-001",
            user_id=user_id,
            tenant_id=None,
            total_count=2,
            unverified_count=1,
            max_risk_level="high",
            items=[{"type": "amount", "value": "99999元", "risk_level": "high"}],
        )

        assert record_id is not None
        assert session.commits == 1
        assert len(session.added) == 1
        record = session.added[0]
        assert record.query == "报销上限是多少"
        assert record.answer_snippet == "报销上限为 99999元"
        assert record.session_id == "sess-001"
        assert record.user_id == uuid.UUID(user_id)
        assert record.max_risk_level == "high"
        assert record.review_status == "pending"
        assert len(record.items) == 1

    @pytest.mark.asyncio
    async def test_answer_truncated_to_2000_chars(self) -> None:
        session = FakeSession()
        service = _make_service(session)

        await service.record_block_audit(
            query="q",
            answer="长答案" * 2000,
            session_id="s",
            total_count=1,
            unverified_count=1,
            max_risk_level="high",
            items=[],
        )

        assert len(session.added[0].answer_snippet) == 2000

    @pytest.mark.asyncio
    async def test_invalid_user_id_degrades_to_none(self) -> None:
        """非法 user_id 不阻断审计（降级为 None 并记录）。"""
        session = FakeSession()
        service = _make_service(session)

        record_id = await service.record_block_audit(
            query="q", answer="a", session_id="s",
            user_id="not-a-uuid",
            total_count=1, unverified_count=1,
            max_risk_level="high", items=[],
        )

        # uuid.UUID("not-a-uuid") 抛异常 → 整体降级返回 None
        assert record_id is None

    @pytest.mark.asyncio
    async def test_db_failure_returns_none_without_raising(self) -> None:
        session = FakeSession()
        session.raise_on_commit = True
        service = _make_service(session)

        record_id = await service.record_block_audit(
            query="q", answer="a", session_id="s",
            total_count=1, unverified_count=1,
            max_risk_level="high", items=[],
        )

        assert record_id is None


# ======================================================================
# 2. list_audits
# ======================================================================


class TestListAudits:
    """审计记录分页查询测试。"""

    @pytest.mark.asyncio
    async def test_list_returns_total_and_items(self) -> None:
        session = FakeSession()
        record = HighRiskAuditRecord(
            query="q", answer_snippet="a", session_id="s",
            total_count=1, unverified_count=1, max_risk_level="high",
            items=[],
        )
        session.execute_results = [
            FakeResult(scalar=1),            # count
            FakeResult(rows=[record]),       # rows
        ]
        service = _make_service(session)

        result = await service.list_audits(review_status="pending")

        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["max_risk_level"] == "high"

    @pytest.mark.asyncio
    async def test_list_db_failure_returns_empty(self) -> None:
        session = FakeSession()
        service = _make_service(session)

        async def broken_execute(stmt: Any) -> FakeResult:
            raise RuntimeError("DB down")

        session.execute = broken_execute  # type: ignore[method-assign]
        result = await service.list_audits()

        assert result == {"total": 0, "items": []}


# ======================================================================
# 3. review_audit
# ======================================================================


class TestReviewAudit:
    """复查标记测试。"""

    @pytest.mark.asyncio
    async def test_review_marks_misjudged(self) -> None:
        session = FakeSession()
        record = HighRiskAuditRecord(
            query="q", answer_snippet="a", session_id="s",
            total_count=1, unverified_count=1, max_risk_level="high",
            items=[],
        )
        session.records[record.id] = record
        service = _make_service(session)
        reviewer = uuid.uuid4()

        ok = await service.review_audit(
            record.id,
            review_status="misjudged",
            reviewer_id=reviewer,
            comment="金额单位换算问题，误拦",
        )

        assert ok is True
        assert record.review_status == "misjudged"
        assert record.reviewed_by == reviewer
        assert record.reviewed_at is not None
        assert record.review_comment == "金额单位换算问题，误拦"
        assert session.commits == 1

    @pytest.mark.asyncio
    async def test_review_nonexistent_returns_false(self) -> None:
        session = FakeSession()
        service = _make_service(session)

        ok = await service.review_audit(
            uuid.uuid4(),
            review_status="confirmed",
            reviewer_id=uuid.uuid4(),
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_invalid_status_raises(self) -> None:
        session = FakeSession()
        service = _make_service(session)

        with pytest.raises(ValueError, match="无效复查状态"):
            await service.review_audit(
                uuid.uuid4(),
                review_status="pending",
                reviewer_id=uuid.uuid4(),
            )


# ======================================================================
# 4. get_misjudgment_stats
# ======================================================================


class TestMisjudgmentStats:
    """误判率统计测试。"""

    @pytest.mark.asyncio
    async def test_stats_with_reviews(self) -> None:
        session = FakeSession()
        session.execute_results = [
            FakeResult(scalar=10),  # total
            FakeResult(rows=[("pending", 6), ("confirmed", 3), ("misjudged", 1)]),
        ]
        service = _make_service(session)

        stats = await service.get_misjudgment_stats()

        assert stats["total_blocks"] == 10
        assert stats["pending"] == 6
        assert stats["confirmed"] == 3
        assert stats["misjudged"] == 1
        assert stats["misjudgment_rate"] == 0.25  # 1 / (3+1)
        assert stats["current_thresholds"]["deviation_low"] == 0.01
        assert stats["current_thresholds"]["deviation_medium"] == 0.10

    @pytest.mark.asyncio
    async def test_stats_no_reviews_rate_is_none(self) -> None:
        session = FakeSession()
        session.execute_results = [
            FakeResult(scalar=2),
            FakeResult(rows=[("pending", 2)]),
        ]
        service = _make_service(session)

        stats = await service.get_misjudgment_stats()

        assert stats["misjudgment_rate"] is None


# ======================================================================
# 5. engine 接线
# ======================================================================


class TestEngineWiring:
    """engine._check_high_risk 三档动作接线测试。"""

    def _make_engine(self) -> Any:
        from app.rag.engine import AgenticRAGEngine
        from tests.test_rag_engine import (
            FakeGenerator,
            FakeLLM,
            FakeMCPClient,
            FakeReranker,
            FakeRetriever,
        )

        return AgenticRAGEngine(
            llm=FakeLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )

    def _make_state(self) -> dict[str, Any]:
        return {
            "query": "报销上限是多少",
            "user_id": None,
            "session_id": "sess-test",
            "retrieved_docs": [{"content": "报销上限为 5000元"}],
        }

    def test_confirm_sets_flag_without_blocking(self) -> None:
        """中风险（偏差 1-10%）→ high_risk_confirm=True，不阻断。"""
        engine = self._make_engine()
        state = self._make_state()

        engine._check_high_risk(
            state, "报销上限为 5250元", state["retrieved_docs"]
        )

        assert state.get("high_risk_confirm") is True
        assert not state.get("high_risk_blocked")
        assert not state.get("low_confidence")

    def test_warn_no_flags(self) -> None:
        """低风险（偏差 <1%）→ 仅记录结果，无阻断/提示标记。"""
        engine = self._make_engine()
        state = self._make_state()

        engine._check_high_risk(
            state, "报销上限为 5040元", state["retrieved_docs"]
        )

        assert not state.get("high_risk_blocked")
        assert not state.get("high_risk_confirm")

    @pytest.mark.asyncio
    async def test_block_schedules_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """block 决策触发审计落库（fire-and-forget）。"""
        engine = self._make_engine()
        state = self._make_state()

        mock_service = AsyncMock()
        mock_service.record_block_audit = AsyncMock(return_value=uuid.uuid4())
        monkeypatch.setattr(
            "app.services.high_risk_audit_service.get_high_risk_audit_service",
            lambda: mock_service,
        )

        engine._check_high_risk(
            state, "报销上限为 6000元", state["retrieved_docs"]
        )

        assert state.get("high_risk_blocked") is True
        assert state.get("low_confidence") is True

        # fire-and-forget 任务需要事件循环让出执行权
        await asyncio.sleep(0.01)
        mock_service.record_block_audit.assert_awaited_once()
        kwargs = mock_service.record_block_audit.await_args.kwargs
        assert kwargs["query"] == "报销上限是多少"
        assert kwargs["session_id"] == "sess-test"
        assert kwargs["max_risk_level"] == "high"
        assert kwargs["total_count"] >= 1
