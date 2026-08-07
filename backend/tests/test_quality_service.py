"""QualityService doc_id 维度评分测试 — P0 质量评分冗余列直查与旧链路兜底。

验证点：
- 直查路径：带 doc_id 的反馈被计入 citation_accuracy / feedback_score；
- 区分度：有正面反馈的文档与无反馈文档得分不同；
- 兜底路径：doc_id 为 NULL 的旧反馈经 related_message_id → Message.sources 命中；
- 兜底不命中：引用来源指向其他文档时不计入，返回中性分 0.5。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.quality_service import QualityService


def _exec_result(*, scalars: list | None = None, rows: list | None = None) -> MagicMock:
    """构造 db.execute 的返回值（区分 scalars().all() 与 all() 两种消费方式）。"""
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    result.all.return_value = rows or []
    return result


def _feedback(
    *,
    type: str = "praise",
    status: str = "resolved",
    doc_id=None,
    related_message_id=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        type=type,
        status=status,
        doc_id=doc_id,
        related_message_id=related_message_id,
    )


def _service_with_execute(side_effect: list) -> QualityService:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=side_effect)
    return QualityService(db=db)


# ======================================================================
# 直查路径（doc_id 冗余列）
# ======================================================================


class TestDocIdDirectPath:
    """Feedback.doc_id 直查 — 新数据不再依赖 Message.sources 链路。"""

    @pytest.mark.asyncio
    async def test_direct_feedbacks_counted_in_citation(self) -> None:
        """带 doc_id 的 praise/complaint 反馈计入引用准确率。"""
        doc_id = uuid4()
        feedbacks = [
            _feedback(type="praise", doc_id=doc_id),
            _feedback(type="praise", doc_id=doc_id),
            _feedback(type="complaint", doc_id=doc_id),
        ]
        service = _service_with_execute([
            _exec_result(scalars=feedbacks),  # doc_id 直查
            _exec_result(scalars=[]),  # 旧链路：无 doc_id IS NULL 数据
        ])

        score = await service._score_citation(doc_id)

        # 2 praise / (2 praise + 1 complaint) = 2/3
        assert score == pytest.approx(2 / 3)

    @pytest.mark.asyncio
    async def test_direct_feedbacks_counted_in_feedback_score(self) -> None:
        """带 doc_id 的反馈按 resolved/closed 占比计入用户反馈分。"""
        doc_id = uuid4()
        feedbacks = [
            _feedback(status="resolved", doc_id=doc_id),
            _feedback(status="closed", doc_id=doc_id),
            _feedback(status="open", doc_id=doc_id),
            _feedback(status="processing", doc_id=doc_id),
        ]
        service = _service_with_execute([
            _exec_result(scalars=feedbacks),
            _exec_result(scalars=[]),
        ])

        score = await service._score_feedback(doc_id)

        # (resolved + closed) / total = 2/4
        assert score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_scores_distinguish_docs(self) -> None:
        """不同文档得分有区分度：有正面反馈的文档高于无反馈文档（中性 0.5）。"""
        doc_praised = uuid4()
        doc_silent = uuid4()
        service_praised = _service_with_execute([
            _exec_result(scalars=[_feedback(type="praise", doc_id=doc_praised)]),
            _exec_result(scalars=[]),
        ])
        service_silent = _service_with_execute([
            _exec_result(scalars=[]),
            _exec_result(scalars=[]),
        ])

        praised_score = await service_praised._score_citation(doc_praised)
        silent_score = await service_silent._score_citation(doc_silent)

        assert praised_score == pytest.approx(1.0)
        assert silent_score == pytest.approx(0.5)
        assert praised_score > silent_score

    @pytest.mark.asyncio
    async def test_other_doc_feedback_not_counted(self) -> None:
        """其他文档的反馈（doc_id 不同）不会被直查命中 — 由 SQL 过滤，结果为中性分。"""
        doc_id = uuid4()
        # 直查返回空（SQL 层已按 doc_id 过滤），旧链路也无数据
        service = _service_with_execute([
            _exec_result(scalars=[]),
            _exec_result(scalars=[]),
            _exec_result(scalars=[]),
            _exec_result(scalars=[]),
        ])

        assert await service._score_citation(doc_id) == pytest.approx(0.5)
        assert await service._score_feedback(doc_id) == pytest.approx(0.5)


# ======================================================================
# 兼容兜底路径（doc_id IS NULL 的旧数据）
# ======================================================================


class TestLegacyFallbackPath:
    """旧数据兜底 — related_message_id → Message.sources 链路仍生效。"""

    @pytest.mark.asyncio
    async def test_legacy_feedback_matched_via_sources(self) -> None:
        """doc_id 为 NULL 的旧反馈，其关联消息引用来源包含目标文档时被计入。"""
        doc_id = uuid4()
        msg_id = uuid4()
        legacy_fb = _feedback(type="praise", related_message_id=msg_id)
        service = _service_with_execute([
            _exec_result(scalars=[]),  # 直查：无新数据
            _exec_result(scalars=[legacy_fb]),  # 旧链路：命中待扫描反馈
            _exec_result(rows=[(msg_id, [{"doc_id": str(doc_id), "title": "引用卡片"}])]),
        ])

        score = await service._score_citation(doc_id)

        assert score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_legacy_feedback_other_doc_not_matched(self) -> None:
        """旧反馈的引用来源指向其他文档时不计入，返回中性分。"""
        doc_id = uuid4()
        msg_id = uuid4()
        legacy_fb = _feedback(type="praise", related_message_id=msg_id)
        service = _service_with_execute([
            _exec_result(scalars=[]),
            _exec_result(scalars=[legacy_fb]),
            _exec_result(rows=[(msg_id, [{"doc_id": str(uuid4())}])]),
        ])

        score = await service._score_citation(doc_id)

        assert score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_direct_and_legacy_merged(self) -> None:
        """直查与旧链路结果合并计分（两条链路按 doc_id IS NULL 互斥，无重复计数）。"""
        doc_id = uuid4()
        msg_id = uuid4()
        direct_fb = _feedback(type="praise", status="resolved", doc_id=doc_id)
        legacy_fb = _feedback(
            type="complaint", status="open", related_message_id=msg_id
        )
        service = _service_with_execute([
            _exec_result(scalars=[direct_fb]),
            _exec_result(scalars=[legacy_fb]),
            _exec_result(rows=[(msg_id, [{"doc_id": str(doc_id)}])]),
        ])

        citation = await service._score_citation(doc_id)

        # 1 praise / (1 praise + 1 complaint) = 0.5
        assert citation == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_legacy_query_skipped_when_no_candidates(self) -> None:
        """无 doc_id IS NULL 的旧反馈时，不再查询 messages 表。"""
        doc_id = uuid4()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _exec_result(scalars=[_feedback(doc_id=doc_id)]),
            _exec_result(scalars=[]),
        ])
        service = QualityService(db=db)

        feedbacks = await service._load_doc_feedbacks(doc_id)

        assert len(feedbacks) == 1
        # 仅 2 次查询：直查 + 旧链路候选扫描（无候选则提前返回）
        assert db.execute.await_count == 2
