"""约束打标流水线测试 — Phase 2（GAP-3 写入侧）。

覆盖：
    Stage A   _prefilter_paragraphs 正则预筛（命中/跳过/截断/limit）
    Stage B   _extract_batch 结构化抽取校验（index 边界/severity 白名单/
              置信度截断/非约束丢弃/非数组返回）
    分流      _save_rules 置信度三分流（≥AUTO active / [REVIEW,AUTO)
              pending_review / <REVIEW 丢弃）+ 同段去重 + 版本链回填
    粗标      _sync_doc_role（constraint_source / normal 回落）
    流水线    extract_constraints 开关短路 / 端到端（mock LLM）
    人审      ConstraintReviewService approve / reject / 非法动作 / 不存在

mock 策略：FakeSession 替换 AsyncSession（execute/add/flush no-op），
_llm_generate / _resolve_constraint_llm mock — 不依赖真实 PG / LLM。
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Mock celery（测试环境未安装）— 与 test_constraint_channel.py 同款
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.config import get_settings
from app.models.constraint import ConstraintRule
from app.models.knowledge import Document
from app.services.constraint_review_service import ConstraintReviewService
from app.services.doc_intelligence_service import (
    DocIntelligenceService,
    _CONSTRAINT_BATCH_SIZE,
    _PARA_MAX_CHARS,
    _PARA_MIN_CHARS,
)

KB_ID = uuid4()
DOC_ID = uuid4()

# 测试条款样本（含 Stage A 命中模式 + 达到最小段长）
_CONSTRAINT_PARA = "所有单笔金额超过5000元的报销必须经过部门总监与财务双签审批，严禁拆单规避。"
_NORMAL_PARA = "公司成立于2015年，总部位于上海，主要业务为企业知识管理平台的研发与销售。"


# ======================================================================
# Fake 基础设施
# ======================================================================


class _FakeScalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _FakeResult:
    def __init__(self, scalars: list | None = None) -> None:
        self._scalars = scalars or []

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._scalars)


class _FakeSession:
    """execute 按队列返回；add/flush no-op — 足够支撑打标流水线。"""

    def __init__(self, queue: list[_FakeResult] | None = None) -> None:
        self._queue = queue or []
        self.added: list = []

    async def execute(self, stmt: Any) -> _FakeResult:
        return self._queue.pop(0) if self._queue else _FakeResult()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, queue: list[_FakeResult] | None = None) -> None:
        self.queue = queue or []

    def __call__(self) -> "_FakeSession":
        return _FakeSession(self.queue)


def _make_doc(content: str = _CONSTRAINT_PARA) -> Document:
    return Document(
        id=DOC_ID, kb_id=KB_ID, content_text=content, doc_role="normal"
    )


def _make_service(session: _FakeSession | None = None) -> DocIntelligenceService:
    return DocIntelligenceService(
        llm=MagicMock(), db=session or _FakeSession(), tenant_id=None
    )


def _extract_item(
    index: int = 0,
    rule_text: str = _CONSTRAINT_PARA,
    severity: str = "block",
    confidence: float = 0.95,
) -> dict[str, Any]:
    return {
        "index": index,
        "rule_text": rule_text,
        "severity": severity,
        "trigger_entities": ["报销"],
        "trigger_domains": ["finance"],
        "confidence": confidence,
        "normalized": {"statement": rule_text[:30]},
    }


# ======================================================================
# Stage A — 正则预筛
# ======================================================================


class TestPrefilterParagraphs:
    """Stage A 纯逻辑 — 零 LLM 候选召回。"""

    def test_constraint_paragraph_matched(self) -> None:
        result = DocIntelligenceService._prefilter_paragraphs(
            _CONSTRAINT_PARA, limit=10
        )
        assert len(result) == 1
        assert "双签" in result[0]

    def test_normal_paragraph_skipped(self) -> None:
        result = DocIntelligenceService._prefilter_paragraphs(
            _NORMAL_PARA, limit=10
        )
        assert result == []

    def test_short_paragraph_skipped(self) -> None:
        """低于最小段长的碎片跳过（即使含命中词）。"""
        result = DocIntelligenceService._prefilter_paragraphs("严禁拆单", limit=10)
        assert result == []

    def test_limit_enforced(self) -> None:
        """超 limit 的候选段截断（防长文档成本失控）。"""
        content = "\n".join([_CONSTRAINT_PARA] * 50)
        result = DocIntelligenceService._prefilter_paragraphs(content, limit=3)
        assert len(result) == 3

    def test_long_paragraph_truncated(self) -> None:
        """超长段落截断至 _PARA_MAX_CHARS。"""
        para = "必须" + "x" * 5000
        result = DocIntelligenceService._prefilter_paragraphs(para, limit=10)
        assert len(result) == 1
        assert len(result[0]) == _PARA_MAX_CHARS

    def test_multiple_patterns_matched(self) -> None:
        content = f"{_CONSTRAINT_PARA}\n\n{_NORMAL_PARA}\n\n采购金额不得低于200元需三家中标比价，一律留档备查。"
        result = DocIntelligenceService._prefilter_paragraphs(content, limit=10)
        assert len(result) == 2


# ======================================================================
# Stage B — 结构化抽取校验
# ======================================================================


class TestExtractBatch:
    """Stage B 返回值校验 — 非法项丢弃，合法项归一化。"""

    @pytest.mark.asyncio
    async def test_valid_items_normalized(self) -> None:
        service = _make_service()
        llm_json = (
            '[{"index": 0, "is_constraint": true, "rule_text": "条款A", '
            '"severity": "block", "trigger_entities": ["报销"], '
            '"trigger_domains": ["finance"], "confidence": 1.5, '
            '"normalized": {"statement": "条款A概括"}}]'
        )
        with patch.object(
            service, "_llm_generate", new=AsyncMock(return_value=llm_json)
        ):
            items = await service._extract_batch(MagicMock(), [_CONSTRAINT_PARA])
        assert len(items) == 1
        # 置信度截断到 [0,1]
        assert items[0]["confidence"] == 1.0
        assert items[0]["severity"] == "block"

    @pytest.mark.asyncio
    async def test_invalid_index_dropped(self) -> None:
        """段落编号越界丢弃 — 防错位落库。"""
        service = _make_service()
        llm_json = (
            '[{"index": 5, "is_constraint": true, "rule_text": "条款", '
            '"severity": "warn", "confidence": 0.9}]'
        )
        with patch.object(
            service, "_llm_generate", new=AsyncMock(return_value=llm_json)
        ):
            items = await service._extract_batch(MagicMock(), [_CONSTRAINT_PARA])
        assert items == []

    @pytest.mark.asyncio
    async def test_invalid_severity_dropped(self) -> None:
        service = _make_service()
        llm_json = (
            '[{"index": 0, "is_constraint": true, "rule_text": "条款", '
            '"severity": "critical", "confidence": 0.9}]'
        )
        with patch.object(
            service, "_llm_generate", new=AsyncMock(return_value=llm_json)
        ):
            items = await service._extract_batch(MagicMock(), [_CONSTRAINT_PARA])
        assert items == []

    @pytest.mark.asyncio
    async def test_non_constraint_dropped(self) -> None:
        service = _make_service()
        llm_json = '[{"index": 0, "is_constraint": false}]'
        with patch.object(
            service, "_llm_generate", new=AsyncMock(return_value=llm_json)
        ):
            items = await service._extract_batch(MagicMock(), [_CONSTRAINT_PARA])
        assert items == []

    @pytest.mark.asyncio
    async def test_non_list_response_returns_empty(self) -> None:
        """LLM 返回非数组（如被截断成对象）时安全返回空。"""
        service = _make_service()
        with patch.object(
            service, "_llm_generate", new=AsyncMock(return_value="解析失败")
        ):
            items = await service._extract_batch(MagicMock(), [_CONSTRAINT_PARA])
        assert items == []


# ======================================================================
# 置信度分流 + 版本链 + doc_role
# ======================================================================


class TestSaveRules:
    """_save_rules 三分流与版本链回填。"""

    @pytest.mark.asyncio
    async def test_high_confidence_active(self) -> None:
        settings = get_settings()
        session = _FakeSession()
        service = _make_service(session)
        saved = await service._save_rules(
            _make_doc(), [_extract_item(confidence=settings.CONSTRAINT_AUTO_CONFIDENCE)], []
        )
        assert len(saved) == 1
        assert saved[0]["rule"].status == "active"
        assert session.added[0] is saved[0]["rule"]

    @pytest.mark.asyncio
    async def test_mid_confidence_pending_review(self) -> None:
        """[REVIEW, AUTO) 进人审队列 — pending_review 照常注入，安全优先。"""
        settings = get_settings()
        service = _make_service()
        mid = (settings.CONSTRAINT_REVIEW_CONFIDENCE + settings.CONSTRAINT_AUTO_CONFIDENCE) / 2
        saved = await service._save_rules(_make_doc(), [_extract_item(confidence=mid)], [])
        assert saved[0]["rule"].status == "pending_review"

    @pytest.mark.asyncio
    async def test_low_confidence_dropped(self) -> None:
        settings = get_settings()
        service = _make_service(_FakeSession())
        saved = await service._save_rules(
            _make_doc(),
            [_extract_item(confidence=settings.CONSTRAINT_REVIEW_CONFIDENCE - 0.01)],
            [],
        )
        assert saved == []

    @pytest.mark.asyncio
    async def test_duplicate_paragraph_deduped(self) -> None:
        """同段落重复抽取去重（LLM 偶发一段多条）。"""
        service = _make_service()
        saved = await service._save_rules(
            _make_doc(),
            [_extract_item(index=0), _extract_item(index=0, severity="warn")],
            [],
        )
        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_version_chain_backfill(self) -> None:
        """重打标时旧规则 superseded_by 回填指向新版本。"""
        old_rule = ConstraintRule(
            kb_id=KB_ID,
            document_id=DOC_ID,
            chunk_id=f"{DOC_ID}:para:0",
            rule_text="旧条款",
            normalized={"statement": "旧条款"},
            severity="warn",
            status="retired",
        )
        service = _make_service()
        saved = await service._save_rules(_make_doc(), [_extract_item()], [old_rule])
        assert old_rule.superseded_by == saved[0]["rule"].id

    @pytest.mark.asyncio
    async def test_batch_size_covers_candidates(self) -> None:
        """打包批量应为常数，Stage B 循环按批切分（防长文档单次超长）。"""
        assert _CONSTRAINT_BATCH_SIZE == 8


class TestSyncDocRole:
    """文档级粗标同步。"""

    @pytest.mark.asyncio
    async def test_saved_rules_set_constraint_source(self) -> None:
        doc = _make_doc()
        service = _make_service()
        await service._sync_doc_role(doc, [], [{"rule": MagicMock()}])
        assert doc.doc_role == "constraint_source"

    @pytest.mark.asyncio
    async def test_no_rules_falls_back_normal(self) -> None:
        doc = _make_doc()
        service = _make_service()
        await service._sync_doc_role(doc, [], [])
        assert doc.doc_role == "normal"

    @pytest.mark.asyncio
    async def test_old_active_rules_keep_source(self) -> None:
        """无新规则但旧规则仍在效（如 Stage A 无命中）时保持粗标。"""
        doc = _make_doc()
        old_rule = ConstraintRule(
            kb_id=KB_ID,
            document_id=DOC_ID,
            chunk_id=f"{DOC_ID}:para:0",
            rule_text="条款",
            normalized={"statement": "条款"},
            severity="warn",
            status="active",
        )
        service = _make_service()
        await service._sync_doc_role(doc, [old_rule], [])
        assert doc.doc_role == "constraint_source"


# ======================================================================
# 流水线端到端
# ======================================================================


class TestExtractConstraintsPipeline:
    """extract_constraints 主流程 — mock LLM 边界。"""

    @pytest.mark.asyncio
    async def test_disabled_switch_returns_empty(self) -> None:
        """CONSTRAINT_CLASSIFIER_ENABLED=False 时回退到无自动打标现状。"""
        settings = get_settings()
        with patch.object(
            settings, "CONSTRAINT_CLASSIFIER_ENABLED", False
        ), patch.object(
            DocIntelligenceService, "_retire_doc_rules"
        ) as retire:
            service = _make_service()
            result = await service.extract_constraints(_make_doc())
        assert result == []
        retire.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_to_end_with_mocked_llm(self) -> None:
        settings = get_settings()
        llm_json = (
            '[{"index": 0, "is_constraint": true, "rule_text": "'
            + _CONSTRAINT_PARA[:20]
            + '", "severity": "block", "trigger_entities": ["报销"], '
            '"trigger_domains": ["finance"], "confidence": 0.95, '
            '"normalized": {"statement": "报销双签"}}]'
        )
        session = _FakeSession()
        service = _make_service(session)
        with patch.object(
            DocIntelligenceService, "_resolve_constraint_llm"
        ) as resolve_llm, patch.object(
            service, "_llm_generate", new=AsyncMock(return_value=llm_json)
        ) as gen:
            result = await service.extract_constraints(_make_doc())
        resolve_llm.assert_called_once()
        gen.assert_called_once()
        assert len(result) == 1
        assert result[0]["status"] == "active"
        assert result[0]["severity"] == "block"
        assert result[0]["confidence"] == 0.95
        assert len(session.added) == 1

    @pytest.mark.asyncio
    async def test_no_candidates_skips_llm(self) -> None:
        """Stage A 无命中 → 零 LLM 调用（长文档约 95% 免调用）。"""
        service = _make_service()
        with patch.object(
            DocIntelligenceService, "_resolve_constraint_llm"
        ) as resolve_llm:
            result = await service.extract_constraints(_make_doc(_NORMAL_PARA))
        resolve_llm.assert_not_called()
        assert result == []


# ======================================================================
# 人审闭环 — ConstraintReviewService
# ======================================================================


class _RuleSession(_FakeSession):
    """支持 session.get(ConstraintRule, id) 的 FakeSession。"""

    def __init__(self, rule: ConstraintRule | None) -> None:
        super().__init__()
        self._rule = rule

    async def get(self, model: Any, pk: Any) -> Any:
        return self._rule if model is ConstraintRule else None


def _make_rule(status: str = "pending_review") -> ConstraintRule:
    return ConstraintRule(
        kb_id=KB_ID,
        document_id=DOC_ID,
        chunk_id=f"{DOC_ID}:para:0",
        rule_text=_CONSTRAINT_PARA,
        normalized={"statement": "报销双签"},
        severity="block",
        status=status,
        classifier_confidence=0.7,
    )


class TestConstraintReviewService:
    """人审流转 — approve/reject/非法动作/不存在。"""

    @pytest.mark.asyncio
    async def test_approve_sets_active(self) -> None:
        rule = _make_rule()
        reviewer = uuid4()
        service = ConstraintReviewService(lambda: _RuleSession(rule))
        assert await service.review_rule(
            rule.id, action="approve", reviewer_id=reviewer, comment="条款有效"
        )
        assert rule.status == "active"
        assert rule.reviewed_by == reviewer
        assert rule.reviewed_at is not None
        assert rule.review_comment == "条款有效"

    @pytest.mark.asyncio
    async def test_reject_sets_retired(self) -> None:
        rule = _make_rule()
        service = ConstraintReviewService(lambda: _RuleSession(rule))
        assert await service.review_rule(
            rule.id, action="reject", reviewer_id=uuid4(), comment="误判"
        )
        assert rule.status == "retired"

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self) -> None:
        service = ConstraintReviewService(lambda: _RuleSession(None))
        with pytest.raises(ValueError, match="无效人审动作"):
            await service.review_rule(uuid4(), action="delete", reviewer_id=uuid4())

    @pytest.mark.asyncio
    async def test_nonexistent_rule_returns_false(self) -> None:
        service = ConstraintReviewService(lambda: _RuleSession(None))
        assert not await service.review_rule(
            uuid4(), action="approve", reviewer_id=uuid4()
        )


class TestReviewStats:
    """误判率统计 — FakeSession 队列按查询顺序返回 scalar_one 值。"""

    @pytest.mark.asyncio
    async def test_stats_from_fake_queries(self) -> None:
        class _StatsSession(_FakeSession):
            def __init__(self) -> None:
                super().__init__()
                # 队列顺序对应 get_review_stats 的 4 次聚合查询：
                # group_by 状态计数 / reviewed / misjudged / high_conf_rejected
                # （第 5 次 version_retired 由 execute 弹出下一项）
                self._scalar_queue = [
                    [("active", 8), ("pending_review", 2), ("retired", 3)],
                    10,   # reviewed
                    3,    # misjudged
                    1,    # auto_high_confidence_rejected
                    2,    # version_chain_retired
                ]

            async def execute(self, stmt: Any) -> Any:
                item = self._scalar_queue.pop(0)
                if isinstance(item, list):
                    return _StatsRows(item)
                return _StatsScalar(item)

        class _StatsScalar:
            def __init__(self, value: Any) -> None:
                self._value = value

            def scalar_one(self) -> Any:
                return self._value

        class _StatsRows:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def all(self) -> list:
                return self._rows

        service = ConstraintReviewService(lambda: _StatsSession())
        stats = await service.get_review_stats()
        assert stats["total_rules"] == 13
        assert stats["active"] == 8
        assert stats["pending_review"] == 2
        assert stats["retired"] == 3
        assert stats["reviewed"] == 10
        assert stats["misjudged"] == 3
        assert stats["misjudgment_rate"] == 0.3
        assert stats["auto_high_confidence_rejected"] == 1
        assert stats["version_chain_retired"] == 2
        assert stats["current_thresholds"]["auto_confidence"] > 0

    @pytest.mark.asyncio
    async def test_stats_zero_reviewed_rate_none(self) -> None:
        """无人审记录时误判率为 None（避免除零）。"""

        class _EmptySession(_FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self._scalar_queue = [[], 0, 0, 0, 0]

            async def execute(self, stmt: Any) -> Any:
                item = self._scalar_queue.pop(0)
                if isinstance(item, list):
                    return SimpleRows(item)
                return SimpleScalar(item)

        class SimpleScalar:
            def __init__(self, value: Any) -> None:
                self._value = value

            def scalar_one(self) -> Any:
                return self._value

        class SimpleRows:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def all(self) -> list:
                return self._rows

        service = ConstraintReviewService(lambda: _EmptySession())
        stats = await service.get_review_stats()
        assert stats["total_rules"] == 0
        assert stats["misjudgment_rate"] is None


# ======================================================================
# API 路由注册
# ======================================================================


class TestConstraintApiRegistered:
    """constraints 路由挂载校验 — 不发起真实请求（环境缺 prometheus_client）。"""

    def test_constraints_routes_present(self) -> None:
        from app.api.v1 import api_router

        paths: set[str] = set()

        def _walk(routes: list, prefix: str = "") -> None:
            for route in routes:
                path = getattr(route, "path", None)
                if path:
                    paths.add(prefix + path)
                # FastAPI 新版 include_router 包装为 _IncludedRouter，
                # 子路由挂在其 original_router 下
                sub = getattr(route, "original_router", None)
                if sub is not None:
                    _walk(sub.routes, prefix + (getattr(route, "prefix", "") or ""))

        _walk(api_router.routes)
        assert "/constraints/rules" in paths
        assert any(p.endswith("/constraints/rules/{rule_id}/review") for p in paths)
        assert "/constraints/review-stats" in paths
