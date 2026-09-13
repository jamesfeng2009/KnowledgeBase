"""沉淀前置闸门测试 — 覆盖产出查重 / 支持度统计 / D3 自动审批门槛。

测试覆盖：
- TestSimilarity: lexical_similarity / cosine_similarity 边界
- TestBuildSupportSummary: 加权口径（praise=1, accepted=2）与用户并集
- TestCheckBeforeExtract: 提取前廉价查重（重复资产拦截 / 正常放行 / 支持度携带）
- TestCheckAfterExtract: 提取后权威查重（无文档放行 / 嵌入不可用 fail-open /
  重复拦截 / 不同放行）
- TestEmbedCached: 标题嵌入 TTL 缓存（第二次命中不重复调用 embedder）
- TestSubmitForReviewD3: 自动审批支持度门槛
    - distinct_users=1 → 人工审批（quality 0.99 也不自动发布）+ low_support 风险
    - distinct_users=2 → 自动通过
    - support=None → 旧行为（自动通过）
    - support_evidence 落库
- TestGateInExtractPath: 闸门接入 extract_from_chat_feedback 的 skip 留痕
"""
from __future__ import annotations

import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings

# ------------------------------------------------------------------
# Mock celery before importing app modules（与 test_knowledge_compounding 一致）
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


def _make_mock_db():
    """创建 Mock AsyncSession — execute 返回可迭代空结果（fetchall→[]）。"""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
    db.commit = AsyncMock()
    db.scalar = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _make_db_with_rows(rows_by_call: list[list]):
    """创建按调用次序返回不同行的 Mock DB — 用于闸门多查询场景。"""
    db = _make_mock_db()
    results = [MagicMock(fetchall=MagicMock(return_value=rows)) for rows in rows_by_call]
    db.execute = AsyncMock(side_effect=results)
    return db


def _make_asset(confidence: float = 0.95):
    """创建沉淀资产 Mock — title/content 无 PII。"""
    return MagicMock(
        id=uuid.uuid4(),
        doc_id=uuid.uuid4(),
        title="公司差旅报销标准是什么？",
        content="经济舱按实报销，酒店限额 500 元每晚。",
        confidence_score=confidence,
        status="pending_review",
    )


# ======================================================================
# 相似度工具
# ======================================================================


class TestSimilarity:
    def test_lexical_identical(self):
        from app.services.knowledge_compounding.distillation_gate import (
            lexical_similarity,
        )

        assert lexical_similarity("公司差旅报销标准？", "公司差旅报销标准") == 1.0

    def test_lexical_empty(self):
        from app.services.knowledge_compounding.distillation_gate import (
            lexical_similarity,
        )

        assert lexical_similarity("", "abc") == 0.0
        assert lexical_similarity("abc", "") == 0.0

    def test_lexical_similar_vs_different(self):
        from app.services.knowledge_compounding.distillation_gate import (
            lexical_similarity,
        )

        similar = lexical_similarity(
            "公司差旅报销标准是什么", "公司差旅报销的标准是什么"
        )
        different = lexical_similarity("公司差旅报销标准", "今天天气怎么样")
        assert similar > 0.8
        assert different < 0.3

    def test_cosine_identical_and_orthogonal(self):
        from app.services.knowledge_compounding.distillation_gate import (
            cosine_similarity,
        )

        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_zero_vector_and_dim_mismatch(self):
        from app.services.knowledge_compounding.distillation_gate import (
            cosine_similarity,
        )

        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
        assert cosine_similarity([], []) == 0.0


# ======================================================================
# 支持度摘要（D1 加权口径）
# ======================================================================


class TestBuildSupportSummary:
    def test_weighting_praise_1_accept_2(self):
        from app.services.knowledge_compounding.distillation_gate import (
            _build_support_summary,
        )

        summary = _build_support_summary(
            praise_events=[{"user_id": "u2"}, {"user_id": "u3"}],
            accept_events=[{"user_id": "u4"}],
            user_id="u1",
        )
        assert summary["praise_count"] == 3  # 自身 1 + 相似 2
        assert summary["accept_count"] == 1
        assert summary["support_count"] == 3 + 2 * 1
        assert summary["distinct_users"] == 4

    def test_self_only(self):
        from app.services.knowledge_compounding.distillation_gate import (
            _build_support_summary,
        )

        summary = _build_support_summary(
            praise_events=[], accept_events=[], user_id="u1"
        )
        assert summary["praise_count"] == 1
        assert summary["accept_count"] == 0
        assert summary["support_count"] == 1
        assert summary["distinct_users"] == 1

    def test_no_user_id(self):
        from app.services.knowledge_compounding.distillation_gate import (
            _build_support_summary,
        )

        summary = _build_support_summary(
            praise_events=[], accept_events=[], user_id=None
        )
        assert summary["distinct_users"] == 0


# ======================================================================
# 提取前检查
# ======================================================================


class TestCheckBeforeExtract:
    @pytest.mark.asyncio
    async def test_proceed_no_assets(self):
        """无既有资产 → 放行，support 携带（Mock DB 空结果 → 仅自身信号）。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        gate = DistillationGate(_make_mock_db(), tenant_id=None)
        verdict = await gate.check_before_extract(
            question_hint="公司差旅报销标准？",
            source_type="chat_feedback",
            source_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        assert verdict["action"] == "proceed"
        assert verdict["reason"] is None
        assert verdict["support"]["praise_count"] == 1
        assert verdict["support"]["distinct_users"] == 1

    @pytest.mark.asyncio
    async def test_skip_duplicate_asset(self):
        """与既有 chat_faq 资产标题高度相似 → skip（duplicate_asset）。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        asset_row = (uuid.uuid4(), "公司差旅报销标准是什么？")
        # 调用次序：好评支持度 → 采纳支持度 → 资产查重
        gate = DistillationGate(
            _make_db_with_rows([ [], [], [asset_row] ]), tenant_id=None
        )
        verdict = await gate.check_before_extract(
            question_hint="公司差旅报销标准是什么？",  # 归一化后与标题一致 → 1.0
            source_type="chat_feedback",
            source_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        assert verdict["action"] == "skip"
        assert verdict["reason"].startswith("duplicate_asset:")

    @pytest.mark.asyncio
    async def test_proceed_different_asset_title(self):
        """资产标题不同主题 → 放行。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        asset_row = (uuid.uuid4(), "如何申请年假？年假流程说明")
        gate = DistillationGate(
            _make_db_with_rows([ [], [], [asset_row] ]), tenant_id=None
        )
        verdict = await gate.check_before_extract(
            question_hint="公司差旅报销标准？",
            source_type="chat_feedback",
            source_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        assert verdict["action"] == "proceed"


# ======================================================================
# 提取后权威查重
# ======================================================================


class TestCheckAfterExtract:
    @pytest.mark.asyncio
    async def test_proceed_no_published_docs(self):
        """FAQ KB 无已发布文档 → 放行（不调用 embedder）。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        gate = DistillationGate(_make_db_with_rows([[]]), tenant_id=None)
        verdict = await gate.check_after_extract(
            question="公司差旅报销标准？", target_kb_id=uuid.uuid4()
        )
        assert verdict["action"] == "proceed"

    @pytest.mark.asyncio
    async def test_fail_open_when_embedder_unavailable(self):
        """嵌入初始化失败 → fail-open 放行（不阻塞沉淀）。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        doc_row = (uuid.uuid4(), "已发布的相似问题标题")
        gate = DistillationGate(_make_db_with_rows([[doc_row]]), tenant_id=None)
        with patch(
            "app.llm.embedder.get_embedder",
            side_effect=RuntimeError("embedder down"),
        ):
            verdict = await gate.check_after_extract(
                question="公司差旅报销标准？", target_kb_id=uuid.uuid4()
            )
        assert verdict["action"] == "proceed"

    @pytest.mark.asyncio
    async def test_skip_duplicate_existing(self):
        """与已发布文档标题嵌入高度相似 → skip（duplicate_existing:doc_id）。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        doc_id = uuid.uuid4()
        doc_row = (doc_id, "已发布的问题")
        gate = DistillationGate(_make_db_with_rows([[doc_row]]), tenant_id=None)

        vec = [0.1, 0.2, 0.3]
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[vec, vec])  # 问题与标题同向量
        with patch(
            "app.llm.embedder.get_embedder", return_value=embedder
        ):
            verdict = await gate.check_after_extract(
                question="公司差旅报销标准？", target_kb_id=uuid.uuid4()
            )
        assert verdict["action"] == "skip"
        assert verdict["reason"] == f"duplicate_existing:{doc_id}"

    @pytest.mark.asyncio
    async def test_proceed_different_embedding(self):
        """嵌入不同 → 放行。"""
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        doc_row = (uuid.uuid4(), "已发布的问题")
        gate = DistillationGate(_make_db_with_rows([[doc_row]]), tenant_id=None)

        embedder = MagicMock()
        embedder.embed = AsyncMock(
            return_value=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        )
        with patch(
            "app.llm.embedder.get_embedder", return_value=embedder
        ):
            verdict = await gate.check_after_extract(
                question="公司差旅报销标准？", target_kb_id=uuid.uuid4()
            )
        assert verdict["action"] == "proceed"


# ======================================================================
# 标题嵌入缓存
# ======================================================================


class TestEmbedCached:
    @pytest.mark.asyncio
    async def test_title_embedding_cached(self):
        """同一标题第二次查询不再调用 embedder（TTL 缓存命中）。"""
        from app.services.knowledge_compounding import distillation_gate

        # 清空缓存，避免测试间串扰
        distillation_gate._TITLE_EMBED_CACHE.clear()
        doc_row = (uuid.uuid4(), "缓存测试标题XYZ")
        # 两次 check_after_extract 各 1 次文档查询
        gate = distillation_gate.DistillationGate(
            _make_db_with_rows([[doc_row], [doc_row]]), tenant_id=None
        )

        embedder = MagicMock()
        embedder.embed = AsyncMock(
            side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
        )
        with patch(
            "app.llm.embedder.get_embedder", return_value=embedder
        ):
            await gate.check_after_extract(
                question="全新问题甲", target_kb_id=uuid.uuid4()
            )
            first_calls = embedder.embed.call_count

            await gate.check_after_extract(
                question="全新问题乙", target_kb_id=uuid.uuid4()
            )
        # 第二次仅嵌入新问题（1 条），标题命中缓存
        assert first_calls == 1
        assert embedder.embed.call_count == 2
        distillation_gate._TITLE_EMBED_CACHE.clear()


# ======================================================================
# D3 自动审批支持度门槛
# ======================================================================


class TestSubmitForReviewD3:
    @pytest.mark.asyncio
    async def test_low_support_blocks_auto_approve(self):
        """distinct_users=1 → 人工审批（quality 0.99 也不自动发布）+ low_support 风险。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        service = KnowledgeApprovalService(_make_mock_db(), tenant_id=None)
        support = {
            "support_count": 1,
            "praise_count": 1,
            "accept_count": 0,
            "distinct_users": 1,
            "window_days": 30,
        }
        approval = await service.submit_for_review(
            asset=_make_asset(confidence=0.99),
            doc_id=uuid.uuid4(),
            kb_id=uuid.uuid4(),
            conflict_count=0,
            support=support,
        )
        assert approval.auto_approved is False
        assert approval.status == "pending"
        risk_types = [r["type"] for r in approval.auto_detected_risks]
        assert "low_support" in risk_types
        assert approval.support_evidence == support

    @pytest.mark.asyncio
    async def test_multi_user_support_auto_approves(self):
        """distinct_users=2 → 满足 D3 → 自动通过。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        service = KnowledgeApprovalService(_make_mock_db(), tenant_id=None)
        support = {
            "support_count": 3,
            "praise_count": 3,
            "accept_count": 0,
            "distinct_users": 2,
            "window_days": 30,
        }
        approval = await service.submit_for_review(
            asset=_make_asset(confidence=0.95),
            doc_id=uuid.uuid4(),
            kb_id=uuid.uuid4(),
            conflict_count=0,
            support=support,
        )
        assert approval.auto_approved is True
        assert approval.status == "approved"
        assert approval.support_evidence == support

    @pytest.mark.asyncio
    async def test_no_support_keeps_old_behavior(self):
        """support=None（闸门关闭）→ 旧行为：高质量自动通过。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        service = KnowledgeApprovalService(_make_mock_db(), tenant_id=None)
        approval = await service.submit_for_review(
            asset=_make_asset(confidence=0.95),
            doc_id=uuid.uuid4(),
            kb_id=uuid.uuid4(),
            conflict_count=0,
            support=None,
        )
        assert approval.auto_approved is True
        assert approval.support_evidence is None


# ======================================================================
# 闸门接入提取路径
# ======================================================================


class TestGateInExtractPath:
    @pytest.mark.asyncio
    async def test_extract_skipped_by_gate_with_trace(self):
        """提取前闸门拦截 → task.status=skipped 且留痕 gate:duplicate_asset:*。"""
        from app.models.knowledge_compounding import CompoundingTask
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        service = KnowledgeCompoundingService(MagicMock(), db)
        feedback_id = uuid.uuid4()
        kb_id = uuid.uuid4()
        added_tasks: list = []
        db.add = MagicMock(side_effect=lambda obj: added_tasks.append(obj))

        with patch.object(
            # P3: 闸门直沉路径测试 — 显式关闭候选池（默认开启会先行入池返回 queued）
            get_settings(),
            "CHAT_FAQ_CANDIDATE_POOL_ENABLED",
            False,
        ), patch.object(
            service, "_get_asset_by_source", new=AsyncMock(return_value=None)
        ), patch.object(
            service, "_load_chat_feedback_context",
            new=AsyncMock(return_value={
                "feedback_id": str(feedback_id),
                "user_id": uuid.uuid4(),
                "conversation_id": str(uuid.uuid4()),
                "user_query": "公司差旅报销标准？",
                "assistant_answer": "经济舱按实报销...",
                "feedback_content": "回答很详细",
            }),
        ), patch(
            "app.services.knowledge_compounding.compounding_service.DistillationGate"
        ) as MockGate:
            MockGate.return_value.check_before_extract = AsyncMock(
                return_value={
                    "action": "skip",
                    "reason": f"duplicate_asset:{uuid.uuid4()}",
                    "support": {
                        "support_count": 1,
                        "praise_count": 1,
                        "accept_count": 0,
                        "distinct_users": 1,
                        "window_days": 30,
                    },
                }
            )
            result = await service.extract_from_chat_feedback(feedback_id, kb_id)

        assert result["status"] == "skipped"
        assert result["reason"] == "gate_duplicate"
        tasks = [t for t in added_tasks if isinstance(t, CompoundingTask)]
        assert len(tasks) == 1
        assert tasks[0].status == "skipped"
        assert tasks[0].error_message.startswith("gate:duplicate_asset:")

    @pytest.mark.asyncio
    async def test_extract_passes_support_to_review(self):
        """闸门放行时支持度证据传入 submit_for_review。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        service = KnowledgeCompoundingService(MagicMock(), db)
        asset_mock = MagicMock(id=uuid.uuid4(), doc_id=uuid.uuid4())
        support_expected = {
            "support_count": 2,
            "praise_count": 2,
            "accept_count": 0,
            "distinct_users": 2,
            "window_days": 30,
        }

        with patch.object(
            # P3: 闸门直沉路径测试 — 显式关闭候选池
            get_settings(),
            "CHAT_FAQ_CANDIDATE_POOL_ENABLED",
            False,
        ), patch.object(
            service, "_get_asset_by_source", new=AsyncMock(return_value=None)
        ), patch.object(
            service, "_load_chat_feedback_context",
            new=AsyncMock(return_value={
                "feedback_id": str(uuid.uuid4()),
                "user_id": uuid.uuid4(),
                "conversation_id": str(uuid.uuid4()),
                "user_query": "公司差旅报销标准？",
                "assistant_answer": "经济舱按实报销...",
                "feedback_content": "回答很详细",
            }),
        ), patch(
            "app.services.knowledge_compounding.compounding_service.DistillationGate"
        ) as MockGate, patch.object(
            service, "_llm_extract_faq",
            new=AsyncMock(return_value={
                "question": "公司差旅报销标准是什么？",
                "answer": "经济舱按实报销...",
                "tags": [],
                "confidence": 0.9,
            }),
        ), patch.object(
            service, "_precipitate_faq_asset", new=AsyncMock(return_value=asset_mock)
        ), patch.object(
            service, "_detect_conflicts_for_assets", new=AsyncMock(return_value=[])
        ), patch(
            "app.services.knowledge_approval_service.KnowledgeApprovalService.submit_for_review",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_submit:
            MockGate.return_value.check_before_extract = AsyncMock(
                return_value={
                    "action": "proceed",
                    "reason": None,
                    "support": support_expected,
                }
            )
            MockGate.return_value.check_after_extract = AsyncMock(
                return_value={"action": "proceed", "reason": None}
            )
            result = await service.extract_from_chat_feedback(
                uuid.uuid4(), uuid.uuid4()
            )

        assert result["status"] == "success"
        assert mock_submit.call_args.kwargs["support"] == support_expected
