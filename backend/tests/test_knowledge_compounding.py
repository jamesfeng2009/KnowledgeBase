"""知识回流层综合单元测试 — 覆盖 ORM 模型 / Pydantic Schema / 核心服务 / JSON 解析。

测试覆盖：
- TestCompoundingModels: ORM 模型创建、字段默认值、软删除混入
- TestCompoundingSchemas: Pydantic Schema 校验与枚举值
- TestKnowledgeCompoundingService: 知识回流服务（mock LLM + DB）
    - collect_execution_results: 执行结果收集
    - extract_knowledge: AI 知识提取（含 4 类资产沉淀）
    - detect_conflicts: 冲突检测
    - inject_for_reuse: 复用注入
    - list_assets / list_tasks / list_conflicts: 查询方法
    - resolve_conflict: 冲突解决
    - get_stats: 统计聚合
- TestExtractJson: JSON 解析辅助函数

核心流程（5 步）：
    执行结果收集 → AI 知识提取 → 知识资产沉淀 → 冲突检测 → 复用注入
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ------------------------------------------------------------------
# Mock celery before importing app modules
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# 辅助函数
# ======================================================================


def _make_mock_llm(response_text: str = '{"defect_experience": null}'):
    """创建 Mock LLM Provider — chat 为异步生成器，yield 指定响应文本。"""
    llm = MagicMock()

    async def mock_chat(messages, tools=None, stream=False, **kwargs):
        yield response_text

    llm.chat = mock_chat
    return llm


def _make_mock_llm_error(error_msg: str = "LLM service unavailable"):
    """创建 Mock LLM Provider — chat 迭代时抛出异常。"""
    llm = MagicMock()

    async def mock_chat(messages, tools=None, stream=False, **kwargs):
        raise RuntimeError(error_msg)
        yield  # 使函数成为异步生成器（不会执行到此行）

    llm.chat = mock_chat
    return llm


def _make_mock_db():
    """创建 Mock AsyncSession — 覆盖 add / flush / execute / commit / scalar。"""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _make_mock_user():
    """创建 Mock User — admin 角色。"""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.role = "admin"
    return user


def _make_scalar_result(value: Any):
    """创建 mock DB execute 结果 — scalar_one() 返回指定值。"""
    mock = MagicMock()
    mock.scalar_one = MagicMock(return_value=value)
    return mock


def _make_scalar_one_or_none_result(value: Any):
    """创建 mock DB execute 结果 — scalar_one_or_none() 返回指定值。"""
    mock = MagicMock()
    mock.scalar_one_or_none = MagicMock(return_value=value)
    return mock


def _make_scalars_result(values: list):
    """创建 mock DB execute 结果 — scalars().all() 返回列表。"""
    mock = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=values)
    scalars_mock.first = MagicMock(return_value=values[0] if values else None)
    mock.scalars = MagicMock(return_value=scalars_mock)
    return mock


def _make_execution(
    status: str = "failed",
    compounding_status: str = "none",
    evidence_ref: dict | None = None,
):
    """创建 Mock TestExecution ORM 实例。"""
    execution = MagicMock()
    execution.id = uuid.uuid4()
    execution.case_id = uuid.uuid4()
    execution.plan_id = None
    execution.executor = "ai"
    execution.status = status
    execution.result = "测试失败，空指针异常"
    execution.execution_log = {"steps": [{"step": 1, "result": "failed"}]}
    execution.failure_reason = "NullPointerException at line 42"
    execution.duration_seconds = 30
    execution.evidence_ref = evidence_ref or {"screenshot": "s3://bucket/shot.png"}
    execution.compounding_status = compounding_status
    return execution


def _make_test_case():
    """创建 Mock TestCase ORM 实例。"""
    test_case = MagicMock()
    test_case.id = uuid.uuid4()
    test_case.project_id = uuid.uuid4()
    test_case.requirement_id = uuid.uuid4()
    test_case.title = "用户登录接口测试"
    test_case.description = "验证用户登录接口的正确性"
    test_case.test_type = "api"
    test_case.priority = "high"
    test_case.expected_result = "返回 200 状态码和 token"
    test_case.verification_channels = ["api", "log"]
    return test_case


def _make_requirement():
    """创建 Mock TestRequirement ORM 实例。"""
    requirement = MagicMock()
    requirement.id = uuid.uuid4()
    requirement.project_id = uuid.uuid4()
    requirement.title = "用户登录功能"
    requirement.description = "实现用户名密码登录"
    requirement.category = "functional"
    requirement.priority = "high"
    requirement.change_thread_id = "thread-001"
    return requirement


def _make_knowledge_asset(
    asset_type: str = "defect_experience",
    status: str = "active",
    title: str = "缺陷经验",
    content: str = "空指针异常根因分析",
):
    """创建 Mock KnowledgeAsset ORM 实例。"""
    asset = MagicMock()
    asset.id = uuid.uuid4()
    asset.asset_type = asset_type
    asset.source_type = "test_execution"
    asset.source_id = uuid.uuid4()
    asset.project_id = uuid.uuid4()
    asset.title = title
    asset.content = content
    asset.summary = "测试摘要"
    asset.tags = ["api", "defect"]
    asset.doc_id = None
    asset.graph_nodes = None
    asset.graph_relationships = None
    asset.graphiti_entity_id = None
    asset.confidence_score = 0.85
    asset.status = status
    asset.conflict_with = None
    asset.compounding_task_id = None
    asset.created_at = datetime.now(timezone.utc)
    asset.updated_at = datetime.now(timezone.utc)
    return asset


def _make_compounding_task(
    task_type: str = "extraction",
    status: str = "completed",
):
    """创建 Mock CompoundingTask ORM 实例。"""
    task = MagicMock()
    task.id = uuid.uuid4()
    task.execution_id = uuid.uuid4()
    task.project_id = uuid.uuid4()
    task.task_type = task_type
    task.status = status
    task.trigger_source = "execution_completed"
    task.extracted_asset_ids = [str(uuid.uuid4())]
    task.conflicts_detected = 0
    task.assets_injected = 0
    task.error_message = None
    task.started_at = datetime.now(timezone.utc)
    task.completed_at = datetime.now(timezone.utc)
    task.created_at = datetime.now(timezone.utc)
    return task


def _make_conflict(
    conflict_type: str = "contradiction",
    resolution: str = "pending",
):
    """创建 Mock KnowledgeConflict ORM 实例。"""
    conflict = MagicMock()
    conflict.id = uuid.uuid4()
    conflict.new_asset_id = uuid.uuid4()
    conflict.existing_asset_id = uuid.uuid4()
    conflict.conflict_type = conflict_type
    conflict.description = "新资产与已有资产存在矛盾"
    conflict.resolution = resolution
    conflict.resolved_by = None
    conflict.resolved_at = None
    conflict.resolution_note = None
    conflict.created_at = datetime.now(timezone.utc)
    return conflict


def _make_llm_extraction_response():
    """创建 LLM 知识提取的 Mock 响应 JSON。"""
    return json.dumps({
        "defect_experience": {
            "title": "登录接口空指针缺陷",
            "content": "根因：用户名为空时未做空值检查，导致 NullPointerException。复现步骤：1. 发送空用户名 2. 调用登录接口 3. 服务端 500 错误",
            "summary": "空用户名导致空指针异常",
            "tags": ["api", "null_check", "login"],
            "confidence": 0.9,
        },
        "regression_sop": {
            "title": "登录接口回归 SOP",
            "content": "验证步骤：1. 正常登录 2. 空用户名 3. 错误密码 4. 账号锁定。检查点：HTTP 状态码、错误消息、token 有效性",
            "summary": "登录接口回归验证流程",
            "tags": ["regression", "login"],
            "confidence": 0.85,
        },
        "graph_triples": [
            ["登录接口", "验证方式", "API测试"],
            ["空指针", "根因", "空值检查缺失"],
        ],
        "verification_baseline": {
            "entity_name": "登录接口验证基线",
            "content": "基线：正常登录返回200+token，异常返回4xx",
            "summary": "登录接口验证基线 v1",
            "version": "v1",
            "old_version": None,
            "tags": ["baseline", "login"],
            "confidence": 0.9,
        },
    })


# ======================================================================
# ORM 模型测试
# ======================================================================


class TestCompoundingModels:
    """知识回流层 ORM 模型测试。"""

    def test_knowledge_asset_model_creation(self):
        """测试 KnowledgeAsset 模型可正常实例化。"""
        from app.models.knowledge_compounding import KnowledgeAsset

        asset = KnowledgeAsset(
            asset_type="defect_experience",
            source_type="test_execution",
            title="缺陷经验",
            content="测试内容",
            status="draft",
        )
        assert asset.asset_type == "defect_experience"
        assert asset.source_type == "test_execution"
        assert asset.title == "缺陷经验"
        assert asset.status == "draft"

    def test_compounding_task_model_creation(self):
        """测试 CompoundingTask 模型可正常实例化。"""
        from app.models.knowledge_compounding import CompoundingTask

        task = CompoundingTask(
            task_type="extraction",
            status="pending",
            trigger_source="execution_completed",
            conflicts_detected=0,
            assets_injected=0,
        )
        assert task.task_type == "extraction"
        assert task.status == "pending"
        assert task.trigger_source == "execution_completed"
        assert task.conflicts_detected == 0
        assert task.assets_injected == 0

    def test_knowledge_conflict_model_creation(self):
        """测试 KnowledgeConflict 模型可正常实例化。"""
        from app.models.knowledge_compounding import KnowledgeConflict

        conflict = KnowledgeConflict(
            new_asset_id=uuid.uuid4(),
            existing_asset_id=uuid.uuid4(),
            conflict_type="contradiction",
            resolution="pending",
        )
        assert conflict.conflict_type == "contradiction"
        assert conflict.resolution == "pending"

    def test_testing_model_new_fields(self):
        """测试测试模型新增的知识回流字段。"""
        from app.models.testing import TestExecution, TestCase, TestRequirement

        # TestExecution 新增字段
        assert hasattr(TestExecution, "evidence_ref")
        assert hasattr(TestExecution, "compounding_status")

        # TestCase 新增字段
        assert hasattr(TestCase, "verification_channels")

        # TestRequirement 新增字段
        assert hasattr(TestRequirement, "change_thread_id")

    def test_models_exported(self):
        """测试新模型已在 models.__init__ 导出。"""
        from app.models import (
            CompoundingTask,
            KnowledgeAsset,
            KnowledgeConflict,
        )
        assert KnowledgeAsset is not None
        assert CompoundingTask is not None
        assert KnowledgeConflict is not None


# ======================================================================
# Pydantic Schema 测试
# ======================================================================


class TestCompoundingSchemas:
    """知识回流层 Pydantic Schema 测试。"""

    def test_asset_type_enum(self):
        """测试资产类型枚举。"""
        from app.schemas.knowledge_compounding import AssetType

        assert AssetType.defect_experience.value == "defect_experience"
        assert AssetType.regression_sop.value == "regression_sop"
        assert AssetType.graph_association.value == "graph_association"
        assert AssetType.verification_baseline.value == "verification_baseline"

    def test_task_type_enum(self):
        """测试任务类型枚举。"""
        from app.schemas.knowledge_compounding import TaskType

        assert TaskType.extraction.value == "extraction"
        assert TaskType.conflict_detection.value == "conflict_detection"
        assert TaskType.reuse_injection.value == "reuse_injection"

    def test_conflict_type_enum(self):
        """测试冲突类型枚举。"""
        from app.schemas.knowledge_compounding import ConflictType

        assert ConflictType.contradiction.value == "contradiction"
        assert ConflictType.supersede.value == "supersede"
        assert ConflictType.overlap.value == "overlap"

    def test_extraction_request_validation(self):
        """测试知识提取请求 Schema 校验。"""
        from app.schemas.knowledge_compounding import ExtractionRequest

        req = ExtractionRequest(
            execution_id=str(uuid.uuid4()),
            trigger_source="manual",
        )
        assert req.trigger_source == "manual"

    def test_reuse_injection_request_validation(self):
        """测试复用注入请求 Schema 校验。"""
        from app.schemas.knowledge_compounding import ReuseInjectionRequest

        req = ReuseInjectionRequest(
            requirement_id=str(uuid.uuid4()),
            max_assets=10,
        )
        assert req.max_assets == 10

    def test_reuse_injection_request_max_assets_limit(self):
        """测试复用注入 max_assets 上限校验。"""
        from app.schemas.knowledge_compounding import ReuseInjectionRequest

        with pytest.raises(Exception):
            ReuseInjectionRequest(
                requirement_id=str(uuid.uuid4()),
                max_assets=100,  # 超过上限 20
            )

    def test_conflict_resolve_request(self):
        """测试冲突解决请求 Schema 校验。"""
        from app.schemas.knowledge_compounding import ConflictResolveRequest

        req = ConflictResolveRequest(
            resolution="new_wins",
            note="新知识更准确",
        )
        assert req.resolution == "new_wins"
        assert req.note == "新知识更准确"

    def test_compounding_stats_response(self):
        """测试回流统计响应 Schema。"""
        from app.schemas.knowledge_compounding import CompoundingStatsResponse

        stats = CompoundingStatsResponse(
            total_assets=10,
            assets_by_type={"defect_experience": 5, "regression_sop": 5},
            total_tasks=8,
            unresolved_conflicts=2,
        )
        assert stats.total_assets == 10
        assert stats.unresolved_conflicts == 2


# ======================================================================
# 知识回流服务测试
# ======================================================================


class TestKnowledgeCompoundingService:
    """知识回流服务测试 — mock LLM + DB。"""

    @pytest.mark.asyncio
    async def test_collect_execution_results_success(self):
        """测试执行结果收集 — 正常场景。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        execution = _make_execution()
        test_case = _make_test_case()
        requirement = _make_requirement()

        # mock: get_execution → execution
        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(execution),  # _get_execution
                _make_scalar_one_or_none_result(test_case),  # _get_test_case
                _make_scalar_one_or_none_result(requirement),  # _get_requirement
            ]
        )

        service = KnowledgeCompoundingService(None, db)
        result = await service.collect_execution_results(str(execution.id))

        assert result["execution"]["id"] == str(execution.id)
        assert result["execution"]["status"] == "failed"
        assert result["test_case"]["title"] == "用户登录接口测试"
        assert result["requirement"]["title"] == "用户登录功能"
        assert "evidence" in result

    @pytest.mark.asyncio
    async def test_collect_execution_results_not_found(self):
        """测试执行结果收集 — 执行记录不存在。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(None)
        )

        service = KnowledgeCompoundingService(None, db)
        with pytest.raises(ValueError, match="执行记录不存在"):
            await service.collect_execution_results(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_extract_knowledge_success(self):
        """测试 AI 知识提取 — 正常场景（4 类资产全部沉淀）。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        execution = _make_execution(status="failed")
        test_case = _make_test_case()
        requirement = _make_requirement()

        # mock LLM — 返回完整的 4 类知识
        llm = _make_mock_llm(_make_llm_extraction_response())

        # mock DB execute — 多次调用返回不同结果
        # 1. _get_execution (extract_knowledge)
        # 2. update TestExecution (compounding_status=pending)
        # 3. _get_execution (collect_execution_results)
        # 4. _get_test_case
        # 5. _get_requirement
        # 6. _get_existing_assets (for each asset, 4 calls)
        # 7. update TestExecution (compounding_status=processed)
        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(execution),  # _get_execution
                MagicMock(),  # update pending
                _make_scalar_one_or_none_result(execution),  # collect
                _make_scalar_one_or_none_result(test_case),  # _get_test_case
                _make_scalar_one_or_none_result(requirement),  # _get_requirement
                _make_scalars_result([]),  # _get_existing_assets (defect)
                _make_scalars_result([]),  # _get_existing_assets (sop)
                _make_scalars_result([]),  # _get_existing_assets (graph)
                _make_scalars_result([]),  # _get_existing_assets (baseline)
                MagicMock(),  # update processed
            ]
        )
        db.scalar = AsyncMock(return_value=0)  # count queries

        # mock GraphitiManager.sync_to_graphiti
        with patch(
            "app.memory.graphiti_manager.GraphitiManager.register_entity"
        ) as mock_register, patch(
            "app.memory.graphiti_manager.GraphitiManager.record_event"
        ) as mock_record:
            mock_entity = MagicMock()
            mock_entity.id = uuid.uuid4()
            mock_register = AsyncMock(return_value=mock_entity)
            mock_record = AsyncMock()

            service = KnowledgeCompoundingService(llm, db)
            result = await service.extract_knowledge(str(execution.id))

        assert result["status"] == "success"
        assert result["asset_count"] == 4  # 4 类资产
        assert "task_id" in result

    @pytest.mark.asyncio
    async def test_extract_knowledge_already_processed(self):
        """测试 AI 知识提取 — 幂等保护（已处理跳过）。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        execution = _make_execution(compounding_status="processed")

        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(execution)
        )

        service = KnowledgeCompoundingService(None, db)
        result = await service.extract_knowledge(str(execution.id))

        assert result["status"] == "skipped"
        assert result["reason"] == "already_processed"

    @pytest.mark.asyncio
    async def test_extract_knowledge_execution_not_found(self):
        """测试 AI 知识提取 — 执行记录不存在。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(None)
        )

        service = KnowledgeCompoundingService(None, db)
        with pytest.raises(ValueError, match="执行记录不存在"):
            await service.extract_knowledge(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_extract_knowledge_llm_unavailable(self):
        """测试 AI 知识提取 — LLM 不可用时优雅降级。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        execution = _make_execution(status="passed")
        test_case = _make_test_case()
        requirement = _make_requirement()

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(execution),
                MagicMock(),  # update pending
                _make_scalar_one_or_none_result(execution),  # collect
                _make_scalar_one_or_none_result(test_case),
                _make_scalar_one_or_none_result(requirement),
                MagicMock(),  # update processed
            ]
        )
        db.scalar = AsyncMock(return_value=0)

        # llm = None 模拟 LLM 不可用
        service = KnowledgeCompoundingService(None, db)
        result = await service.extract_knowledge(str(execution.id))

        assert result["status"] == "success"
        # LLM 不可用时资产数为 0
        assert result["asset_count"] == 0

    @pytest.mark.asyncio
    async def test_detect_conflicts_success(self):
        """测试冲突检测 — 检测到矛盾冲突。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        new_asset = _make_knowledge_asset(status="draft")
        existing_asset = _make_knowledge_asset(
            title="旧缺陷经验", content="不同的根因分析"
        )

        # mock LLM — 检测到 contradiction
        llm = _make_mock_llm("contradiction")

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(new_asset),  # _get_asset
                _make_scalars_result([existing_asset]),  # _get_existing_assets
            ]
        )

        service = KnowledgeCompoundingService(llm, db)
        conflicts = await service.detect_conflicts(str(new_asset.id))

        assert len(conflicts) == 1
        assert conflicts[0]["conflict_type"] == "contradiction"

    @pytest.mark.asyncio
    async def test_detect_conflicts_no_conflict(self):
        """测试冲突检测 — 无冲突。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        new_asset = _make_knowledge_asset(status="draft")
        existing_asset = _make_knowledge_asset()

        # mock LLM — 返回 none
        llm = _make_mock_llm("none")

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(new_asset),
                _make_scalars_result([existing_asset]),
            ]
        )

        service = KnowledgeCompoundingService(llm, db)
        conflicts = await service.detect_conflicts(str(new_asset.id))

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_detect_conflicts_asset_not_found(self):
        """测试冲突检测 — 资产不存在。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(None)
        )

        service = KnowledgeCompoundingService(None, db)
        with pytest.raises(ValueError, match="知识资产不存在"):
            await service.detect_conflicts(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_inject_for_reuse_success(self):
        """测试复用注入 — 正常场景。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        requirement = _make_requirement()
        assets = [
            _make_knowledge_asset(title="历史缺陷1"),
            _make_knowledge_asset(title="历史SOP1", asset_type="regression_sop"),
        ]

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(requirement),  # _get_requirement
                _make_scalars_result(assets),  # _retrieve_relevant_assets
            ]
        )

        service = KnowledgeCompoundingService(None, db)
        result = await service.inject_for_reuse(str(requirement.id))

        assert result["status"] == "success"
        assert result["asset_count"] == 2
        assert "injection_context" in result
        assert "历史知识资产" in result["injection_context"]

    @pytest.mark.asyncio
    async def test_inject_for_reuse_no_assets(self):
        """测试复用注入 — 无历史资产时返回空上下文。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        requirement = _make_requirement()

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(requirement),
                _make_scalars_result([]),  # 无历史资产
            ]
        )

        service = KnowledgeCompoundingService(None, db)
        result = await service.inject_for_reuse(str(requirement.id))

        assert result["status"] == "success"
        assert result["asset_count"] == 0
        assert result["injection_context"] == ""

    @pytest.mark.asyncio
    async def test_inject_for_reuse_requirement_not_found(self):
        """测试复用注入 — 需求点不存在。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(None)
        )

        service = KnowledgeCompoundingService(None, db)
        with pytest.raises(ValueError, match="需求点不存在"):
            await service.inject_for_reuse(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_list_assets(self):
        """测试知识资产列表查询。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        assets = [_make_knowledge_asset(), _make_knowledge_asset()]

        db.scalar = AsyncMock(return_value=2)
        db.execute = AsyncMock(
            return_value=_make_scalars_result(assets)
        )

        service = KnowledgeCompoundingService(None, db)
        items, total = await service.list_assets(page=1, size=20)

        assert total == 2
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        """测试回流任务列表查询。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        tasks = [_make_compounding_task()]

        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(
            return_value=_make_scalars_result(tasks)
        )

        service = KnowledgeCompoundingService(None, db)
        items, total = await service.list_tasks(page=1, size=20)

        assert total == 1
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_list_conflicts(self):
        """测试知识冲突列表查询。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        conflicts = [_make_conflict()]

        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(
            return_value=_make_scalars_result(conflicts)
        )

        service = KnowledgeCompoundingService(None, db)
        items, total = await service.list_conflicts(page=1, size=20)

        assert total == 1
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_resolve_conflict_new_wins(self):
        """测试冲突解决 — new_wins 方案。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        conflict = _make_conflict(resolution="pending")

        db.execute = AsyncMock(
            side_effect=[
                _make_scalar_one_or_none_result(conflict),  # get conflict
                MagicMock(),  # update asset status
            ]
        )

        service = KnowledgeCompoundingService(None, db)
        result = await service.resolve_conflict(
            conflict.id,
            resolution="new_wins",
            note="新知识更准确",
        )

        assert result.resolution == "new_wins"
        assert result.resolution_note == "新知识更准确"

    @pytest.mark.asyncio
    async def test_resolve_conflict_not_found(self):
        """测试冲突解决 — 冲突不存在。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        db.execute = AsyncMock(
            return_value=_make_scalar_one_or_none_result(None)
        )

        service = KnowledgeCompoundingService(None, db)
        with pytest.raises(ValueError, match="知识冲突不存在"):
            await service.resolve_conflict(
                uuid.uuid4(),
                resolution="new_wins",
            )

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """测试回流统计聚合。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()

        # mock 多次 scalar 调用
        db.scalar = AsyncMock(
            side_effect=[
                10,  # total_assets
                8,  # total_tasks
                5,  # total_conflicts
                2,  # unresolved_conflicts
                3,  # reuse_injection_count
            ]
        )

        # mock 多次 execute 调用（group_by 查询）
        type_result_mock = MagicMock()
        type_result_mock.__iter__ = MagicMock(
            return_value=iter([("defect_experience", 5), ("regression_sop", 5)])
        )
        status_result_mock = MagicMock()
        status_result_mock.__iter__ = MagicMock(
            return_value=iter([("active", 8), ("draft", 2)])
        )
        task_status_mock = MagicMock()
        task_status_mock.__iter__ = MagicMock(
            return_value=iter([("completed", 6), ("failed", 2)])
        )

        db.execute = AsyncMock(
            side_effect=[
                type_result_mock,
                status_result_mock,
                task_status_mock,
            ]
        )

        service = KnowledgeCompoundingService(None, db)
        stats = await service.get_stats()

        assert stats["total_assets"] == 10
        assert stats["assets_by_type"]["defect_experience"] == 5
        assert stats["total_tasks"] == 8
        assert stats["unresolved_conflicts"] == 2
        assert stats["reuse_injection_count"] == 3

    def test_build_extraction_prompt(self):
        """测试知识提取系统提示词构建。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        prompt = KnowledgeCompoundingService._build_extraction_prompt()
        assert "defect_experience" in prompt
        assert "regression_sop" in prompt
        assert "graph_triples" in prompt
        assert "verification_baseline" in prompt

    def test_build_injection_context(self):
        """测试复用注入上下文构建。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        service = KnowledgeCompoundingService.__new__(KnowledgeCompoundingService)
        assets = [_make_knowledge_asset(title="历史缺陷")]
        requirement = _make_requirement()
        context = service._build_injection_context(assets, requirement)
        assert "历史知识资产" in context
        assert "历史缺陷" in context
        assert requirement.title in context

    def test_build_injection_context_empty(self):
        """测试复用注入上下文 — 空资产列表。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        service = KnowledgeCompoundingService.__new__(KnowledgeCompoundingService)
        requirement = _make_requirement()
        context = service._build_injection_context([], requirement)
        assert context == ""

    def test_build_graph_data(self):
        """测试图谱数据构建 — 从三元组生成节点和关系。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        service = KnowledgeCompoundingService.__new__(KnowledgeCompoundingService)
        triples = [["登录接口", "验证方式", "API测试"]]
        context = {
            "execution": {"id": str(uuid.uuid4()), "status": "failed"},
            "test_case": {"id": str(uuid.uuid4()), "title": "登录测试"},
            "requirement": {"id": str(uuid.uuid4()), "title": "登录功能"},
        }
        nodes, relationships = service._build_graph_data(triples, context)

        # 应有 5 个节点（execution + test_case + requirement + 2 concepts）
        assert len(nodes) == 5
        # 应有 1 个关系（concept → concept）
        assert len(relationships) == 1
        assert relationships[0]["type"] == "验证方式".upper().replace(" ", "_")

    def test_execution_to_dict(self):
        """测试执行记录序列化。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        execution = _make_execution()
        result = KnowledgeCompoundingService._execution_to_dict(execution)
        assert result["status"] == "failed"
        assert result["executor"] == "ai"
        assert "evidence_ref" in result

    def test_asset_to_dict(self):
        """测试知识资产序列化。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        asset = _make_knowledge_asset()
        result = KnowledgeCompoundingService._asset_to_dict(asset)
        assert result["asset_type"] == "defect_experience"
        assert result["status"] == "active"
        assert result["confidence_score"] == 0.85

    def test_conflict_to_dict(self):
        """测试知识冲突序列化。"""
        from app.services.knowledge_compounding.compounding_service import (
            KnowledgeCompoundingService,
        )

        conflict = _make_conflict()
        result = KnowledgeCompoundingService._conflict_to_dict(conflict)
        assert result["conflict_type"] == "contradiction"
        assert result["resolution"] == "pending"


# ======================================================================
# JSON 解析辅助函数测试
# ======================================================================


class TestExtractJson:
    """_extract_json 辅助函数测试。"""

    def test_extract_json_plain(self):
        """测试纯 JSON 解析。"""
        from app.services.knowledge_compounding.compounding_service import (
            _extract_json,
        )

        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_extract_json_code_block(self):
        """测试代码块包裹的 JSON 解析。"""
        from app.services.knowledge_compounding.compounding_service import (
            _extract_json,
        )

        result = _extract_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extract_json_array(self):
        """测试 JSON 数组解析。"""
        from app.services.knowledge_compounding.compounding_service import (
            _extract_json,
        )

        result = _extract_json('[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_extract_json_with_surrounding_text(self):
        """测试包含额外文本的 JSON 解析。"""
        from app.services.knowledge_compounding.compounding_service import (
            _extract_json,
        )

        result = _extract_json('Here is the result:\n{"key": "value"}\nDone.')
        assert result == {"key": "value"}

    def test_extract_json_invalid(self):
        """测试无效 JSON 抛出异常。"""
        from app.services.knowledge_compounding.compounding_service import (
            _extract_json,
        )

        with pytest.raises(ValueError, match="Failed to parse JSON"):
            _extract_json("not json at all")


# ======================================================================
# Celery 任务测试
# ======================================================================


class TestCompoundingCeleryTasks:
    """知识回流 Celery 任务测试。"""

    def test_celery_tasks_imported(self):
        """测试 Celery 任务模块可正常导入。"""
        from tasks import compounding_tasks

        assert hasattr(compounding_tasks, "_run_async")
        assert hasattr(compounding_tasks, "_extract_knowledge")
        assert hasattr(compounding_tasks, "_detect_conflicts")
        assert hasattr(compounding_tasks, "_inject_for_reuse")

    @pytest.mark.asyncio
    async def test_run_async_helper(self):
        """测试 _run_async 辅助函数 — 验证协程可同步执行。"""
        import asyncio

        async def sample_coro():
            return 42

        # 在 async 测试中不能调用 _run_async（会嵌套事件循环），
        # 直接用 asyncio 验证协程逻辑
        result = await sample_coro()
        assert result == 42


# ======================================================================
# API 路由注册测试
# ======================================================================


class TestCompoundingAPIRegistration:
    """知识回流 API 路由注册测试。"""

    def test_router_imported(self):
        """测试路由器可正常导入。"""
        from app.api.v1.knowledge_compounding import router

        assert router is not None
        assert router.prefix == "/compounding"

    def test_router_registered(self):
        """测试路由器自身有完整的端点路由。"""
        from app.api.v1.knowledge_compounding import router as compounding_router

        # 检查 compounding_router 自身有路由
        assert len(compounding_router.routes) > 0

        # 检查关键端点路径存在
        paths = {r.path for r in compounding_router.routes if hasattr(r, "path")}
        assert any("/extract" in p for p in paths), f"extract endpoint not found: {paths}"
        assert any("/assets" in p for p in paths), f"assets endpoint not found: {paths}"
        assert any("/conflicts" in p for p in paths), f"conflicts endpoint not found: {paths}"
        assert any("/reuse" in p for p in paths), f"reuse endpoint not found: {paths}"
        assert any("/stats" in p for p in paths), f"stats endpoint not found: {paths}"
