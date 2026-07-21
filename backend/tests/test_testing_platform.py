"""智能测试平台综合单元测试 — 覆盖 ORM 模型 / Pydantic Schema / 五大服务 / JSON 解析辅助函数。

测试覆盖：
- TestModels: ORM 模型创建、字段默认值、软删除混入
- TestSchemas: Pydantic Schema 校验与枚举值
- TestRequirementAnalysisService: 需求提取服务（mock LLM + DB）
- TestTestCaseGenerationService: 用例生成服务（mock LLM + DB）
- TestTestReviewService: 用例评审服务（mock DB + user）
- TestTestCaseManagementService: 用例管理服务（mock DB）
- TestTestOrchestrationService: 测试编排服务（mock LLM + DB）
- TestExtractJson: JSON 解析辅助函数（代码块 / 纯文本 / 混杂文本 / 数组）

核心流程：
    PRD/UI稿 → 需求提取(TestRequirement) → 用例生成(TestCase)
    → 用例评审(TestReview) → 用例管理(TestCase CRUD)
    → AI编排(TestPlan) → 执行记录(TestExecution)
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


def _make_mock_llm(response_text: str = '{"requirements": []}'):
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
    """创建 Mock AsyncSession — 覆盖 add / flush / execute / commit / scalar / refresh。"""
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


def _make_scalars_all_result(items: list):
    """创建 mock DB execute 结果 — scalars().all() 返回指定列表。"""
    mock = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all = MagicMock(return_value=items)
    mock.scalars = MagicMock(return_value=mock_scalars)
    return mock


def _make_scalars_first_result(item: Any):
    """创建 mock DB execute 结果 — scalars().first() 返回指定对象。"""
    mock = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first = MagicMock(return_value=item)
    mock.scalars = MagicMock(return_value=mock_scalars)
    return mock


def _make_iterable_result(rows: list):
    """创建 mock DB execute 结果 — 可直接迭代（用于 GROUP BY 查询的 row 遍历）。"""
    mock = MagicMock()
    mock.__iter__ = MagicMock(return_value=iter(rows))
    return mock


def _make_mock_doc(title: str = "PRD文档", content_text: str = "PRD 内容"):
    """创建 Mock Document ORM 实例。"""
    doc = MagicMock()
    doc.id = uuid.uuid4()
    doc.title = title
    doc.content_text = content_text
    doc.content_html = None
    doc.deleted_at = None
    return doc


def _make_mock_requirement(
    title: str = "登录需求",
    category: str = "functional",
    priority: str = "high",
):
    """创建 Mock TestRequirement ORM 实例。"""
    req = MagicMock()
    req.id = uuid.uuid4()
    req.project_id = uuid.uuid4()
    req.source_doc_id = uuid.uuid4()
    req.title = title
    req.description = "需求详细描述"
    req.category = category
    req.priority = priority
    req.acceptance_criteria = ["验收标准1", "验收标准2"]
    req.source_text = "原始文本片段"
    req.source = "ai_extract"
    req.status = "analyzed"
    req.deleted_at = None
    req.created_at = datetime.now(timezone.utc)
    return req


def _make_mock_test_case(
    title: str = "登录测试用例",
    status: str = "draft",
    test_type: str = "functional",
    priority: str = "normal",
):
    """创建 Mock TestCase ORM 实例。"""
    case = MagicMock()
    case.id = uuid.uuid4()
    case.project_id = uuid.uuid4()
    case.requirement_id = uuid.uuid4()
    case.title = title
    case.description = "用例描述"
    case.preconditions = "前置条件"
    case.test_steps = [{"step_no": 1, "action": "操作", "expected": "预期"}]
    case.expected_result = "预期结果"
    case.test_type = test_type
    case.priority = priority
    case.status = status
    case.tags = ["标签"]
    case.created_by = "ai_generate"
    case.case_no = "TC-0001"
    case.context_doc_ids = []
    case.deleted_at = None
    case.created_at = datetime.now(timezone.utc)
    case.updated_at = datetime.now(timezone.utc)
    return case


def _make_mock_review(status: str = "pending"):
    """创建 Mock TestReview ORM 实例。"""
    review = MagicMock()
    review.id = uuid.uuid4()
    review.case_id = uuid.uuid4()
    review.submitter_id = uuid.uuid4()
    review.reviewer_id = None
    review.status = status
    review.comment = None
    review.suggestions = None
    review.review_summary = None
    review.resolved_at = None
    review.created_at = datetime.now(timezone.utc)
    return review


def _make_mock_plan(name: str = "v1.0 回归计划", case_ids: list | None = None):
    """创建 Mock TestPlan ORM 实例。"""
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.project_id = uuid.uuid4()
    plan.name = name
    plan.description = "计划描述"
    plan.case_ids = case_ids if case_ids else []
    plan.execution_strategy = "priority_based"
    plan.ai_orchestration = None
    plan.status = "draft"
    plan.created_by = uuid.uuid4()
    plan.deleted_at = None
    plan.created_at = datetime.now(timezone.utc)
    plan.updated_at = datetime.now(timezone.utc)
    return plan


# ======================================================================
# ORM 模型测试
# ======================================================================


class TestModels:
    """ORM 模型创建与字段默认值验证。"""

    def test_test_project_creation(self) -> None:
        """TestProject 创建 — 验证表名与关键字段。"""
        from app.models.testing import TestProject

        owner_id = uuid.uuid4()
        project = TestProject(
            name="电商平台测试项目",
            description="覆盖下单、支付、物流全流程",
            owner_id=owner_id,
        )
        assert project.name == "电商平台测试项目"
        assert project.description == "覆盖下单、支付、物流全流程"
        assert project.owner_id == owner_id
        assert TestProject.__tablename__ == "test_projects"

        # 验证关键列存在
        columns = {c.name for c in TestProject.__table__.columns}
        required = {
            "id", "name", "description", "owner_id",
            "prd_doc_ids", "tech_doc_ids", "api_doc_ids",
            "status", "tenant_id", "created_at", "updated_at", "deleted_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_test_requirement_creation(self) -> None:
        """TestRequirement 创建 — 验证表名与关键字段。"""
        from app.models.testing import TestRequirement

        req = TestRequirement(
            project_id=uuid.uuid4(),
            title="用户登录功能需求",
            description="用户可通过邮箱密码登录系统",
        )
        assert req.title == "用户登录功能需求"
        assert TestRequirement.__tablename__ == "test_requirements"

        columns = {c.name for c in TestRequirement.__table__.columns}
        required = {
            "id", "project_id", "source_doc_id", "title", "description",
            "category", "priority", "acceptance_criteria", "source_text",
            "source", "status", "tenant_id", "deleted_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_test_case_creation(self) -> None:
        """TestCase 创建 — 验证字段与列默认值。"""
        from app.models.testing import TestCase

        case = TestCase(
            project_id=uuid.uuid4(),
            title="登录成功测试用例",
        )
        assert case.title == "登录成功测试用例"
        assert TestCase.__tablename__ == "test_cases"

        # 验证列默认值
        assert TestCase.__table__.c.status.default.arg == "draft"
        assert TestCase.__table__.c.test_type.default.arg == "functional"
        assert TestCase.__table__.c.priority.default.arg == "normal"
        assert TestCase.__table__.c.created_by.default.arg == "ai_generate"

        # 验证关键列存在
        columns = {c.name for c in TestCase.__table__.columns}
        required = {
            "id", "project_id", "requirement_id", "title", "description",
            "preconditions", "test_steps", "expected_result",
            "test_type", "priority", "status", "tags",
            "created_by", "context_doc_ids", "case_no", "tenant_id", "deleted_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_test_review_creation(self) -> None:
        """TestReview 创建 — 验证默认值（不含软删除混入）。"""
        from app.models.testing import TestReview

        assert TestReview.__tablename__ == "test_reviews"
        assert TestReview.__table__.c.status.default.arg == "pending"

        # TestReview 不继承 SoftDeleteMixin
        columns = {c.name for c in TestReview.__table__.columns}
        assert "deleted_at" not in columns
        required = {
            "id", "case_id", "submitter_id", "reviewer_id",
            "status", "comment", "suggestions", "review_summary",
            "resolved_at", "created_at", "updated_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_test_plan_creation(self) -> None:
        """TestPlan 创建 — 验证默认值。"""
        from app.models.testing import TestPlan

        assert TestPlan.__tablename__ == "test_plans"
        assert TestPlan.__table__.c.execution_strategy.default.arg == "priority_based"
        assert TestPlan.__table__.c.status.default.arg == "draft"

        columns = {c.name for c in TestPlan.__table__.columns}
        required = {
            "id", "project_id", "name", "description", "case_ids",
            "execution_strategy", "ai_orchestration", "status",
            "created_by", "tenant_id", "deleted_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_test_execution_creation(self) -> None:
        """TestExecution 创建 — 验证默认值。"""
        from app.models.testing import TestExecution

        assert TestExecution.__tablename__ == "test_executions"
        assert TestExecution.__table__.c.executor.default.arg == "human"
        assert TestExecution.__table__.c.status.default.arg == "pending"
        assert TestExecution.__table__.c.duration_seconds.default.arg == 0

        columns = {c.name for c in TestExecution.__table__.columns}
        required = {
            "id", "plan_id", "case_id", "executor_id", "executor",
            "status", "result", "execution_log", "failure_reason",
            "duration_seconds", "started_at", "completed_at",
            "created_at", "updated_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_soft_delete_mixin(self) -> None:
        """SoftDeleteMixin — is_deleted 属性随 deleted_at 变化。"""
        from app.models.testing import TestProject

        project = TestProject(name="测试项目", owner_id=uuid.uuid4())

        # 初始状态 — 未删除
        project.deleted_at = None
        assert project.is_deleted is False

        # 设置 deleted_at 后 — 已删除
        project.deleted_at = datetime.now(timezone.utc)
        assert project.is_deleted is True


# ======================================================================
# Pydantic Schema 测试
# ======================================================================


class TestSchemas:
    """Pydantic Schema 校验测试。"""

    def test_project_create_validation(self) -> None:
        """TestProjectCreate — 正常创建与字段校验。"""
        from app.schemas.testing import TestProjectCreate

        # 正常创建
        project = TestProjectCreate(
            name="测试项目",
            description="项目描述",
            prd_doc_ids=[str(uuid.uuid4())],
        )
        assert project.name == "测试项目"
        assert project.prd_doc_ids is not None
        assert len(project.prd_doc_ids) == 1

        # 空名称应校验失败
        with pytest.raises(Exception):
            TestProjectCreate(name="")

        # 超长名称应校验失败
        with pytest.raises(Exception):
            TestProjectCreate(name="x" * 256)

    def test_case_generate_request_validation(self) -> None:
        """TestCaseGenerateRequest — 默认值与边界校验。"""
        from app.schemas.testing import TestCaseGenerateRequest

        # 最小参数
        req = TestCaseGenerateRequest(requirement_id=str(uuid.uuid4()))
        assert req.max_cases == 5
        assert req.context_doc_ids is None
        assert req.test_type is None

        # 最大用例数边界
        req_max = TestCaseGenerateRequest(
            requirement_id=str(uuid.uuid4()),
            max_cases=20,
        )
        assert req_max.max_cases == 20

        # 超出上限应失败
        with pytest.raises(Exception):
            TestCaseGenerateRequest(
                requirement_id=str(uuid.uuid4()),
                max_cases=21,
            )

        # 低于下限应失败
        with pytest.raises(Exception):
            TestCaseGenerateRequest(
                requirement_id=str(uuid.uuid4()),
                max_cases=0,
            )

    def test_test_step_schema(self) -> None:
        """TestStep — 字段校验。"""
        from app.schemas.testing import TestStep

        step = TestStep(step_no=1, action="输入用户名", expected="显示输入框")
        assert step.step_no == 1
        assert step.action == "输入用户名"
        assert step.expected == "显示输入框"

        # 缺少必填字段应失败
        with pytest.raises(Exception):
            TestStep(step_no=1, action="操作")

    def test_enums(self) -> None:
        """验证所有枚举值与 models 注释一致。"""
        from app.schemas.testing import (
            ExecutionStatus,
            ExecutionStrategy,
            Priority,
            RequirementCategory,
            RequirementStatus,
            TestCaseStatus,
            TestCaseType,
            TestPlanStatus,
            TestReviewStatus,
        )

        # 需求分类
        assert RequirementCategory.functional.value == "functional"
        assert RequirementCategory.non_functional.value == "non_functional"
        assert RequirementCategory.ui.value == "ui"
        assert RequirementCategory.api.value == "api"
        assert RequirementCategory.performance.value == "performance"

        # 需求状态
        assert RequirementStatus.pending.value == "pending"
        assert RequirementStatus.analyzed.value == "analyzed"
        assert RequirementStatus.generating_cases.value == "generating_cases"
        assert RequirementStatus.cases_ready.value == "cases_ready"

        # 用例类型
        assert TestCaseType.functional.value == "functional"
        assert TestCaseType.api.value == "api"
        assert TestCaseType.ui.value == "ui"
        assert TestCaseType.performance.value == "performance"
        assert TestCaseType.security.value == "security"
        assert TestCaseType.compatibility.value == "compatibility"

        # 用例状态
        assert TestCaseStatus.draft.value == "draft"
        assert TestCaseStatus.pending_review.value == "pending_review"
        assert TestCaseStatus.approved.value == "approved"
        assert TestCaseStatus.active.value == "active"
        assert TestCaseStatus.deprecated.value == "deprecated"

        # 评审状态
        assert TestReviewStatus.pending.value == "pending"
        assert TestReviewStatus.approved.value == "approved"
        assert TestReviewStatus.rejected.value == "rejected"

        # 计划状态
        assert TestPlanStatus.draft.value == "draft"
        assert TestPlanStatus.active.value == "active"
        assert TestPlanStatus.completed.value == "completed"
        assert TestPlanStatus.archived.value == "archived"

        # 执行状态
        assert ExecutionStatus.pending.value == "pending"
        assert ExecutionStatus.running.value == "running"
        assert ExecutionStatus.passed.value == "passed"
        assert ExecutionStatus.failed.value == "failed"
        assert ExecutionStatus.blocked.value == "blocked"
        assert ExecutionStatus.skipped.value == "skipped"

        # 执行策略
        assert ExecutionStrategy.sequential.value == "sequential"
        assert ExecutionStrategy.parallel.value == "parallel"
        assert ExecutionStrategy.priority_based.value == "priority_based"

        # 优先级
        assert Priority.low.value == "low"
        assert Priority.normal.value == "normal"
        assert Priority.high.value == "high"
        assert Priority.critical.value == "critical"


# ======================================================================
# 需求分析服务测试
# ======================================================================


class TestRequirementAnalysisService:
    """RequirementAnalysisService 测试 — mock LLM + DB。"""

    @pytest.mark.asyncio
    async def test_extract_requirements_success(self) -> None:
        """成功提取需求 — LLM 返回 JSON 数组，验证 DB add 被调用。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm_response = json.dumps([
            {
                "title": "用户登录功能",
                "description": "用户可通过邮箱密码登录",
                "category": "functional",
                "priority": "high",
                "acceptance_criteria": ["输入正确密码可登录", "密码错误提示"],
                "source_text": "系统应支持用户登录功能",
            },
            {
                "title": "界面响应式设计",
                "description": "页面需适配移动端",
                "category": "ui",
                "priority": "normal",
                "acceptance_criteria": ["移动端布局正常"],
                "source_text": "UI 稿要求响应式",
            },
        ])
        llm = _make_mock_llm(llm_response)
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        # Mock 文档查询
        doc = _make_mock_doc(title="PRD文档", content_text="这是一个PRD文档内容")
        db.execute = AsyncMock(return_value=_make_scalars_first_result(doc))

        project_id = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())

        result = await service.extract_requirements(project_id, doc_id)

        assert len(result) == 2
        assert result[0]["title"] == "用户登录功能"
        assert result[0]["category"] == "functional"
        assert result[0]["priority"] == "high"
        assert result[0]["source"] == "ai_extract"
        assert result[0]["status"] == "analyzed"
        assert result[1]["title"] == "界面响应式设计"
        assert result[1]["category"] == "ui"
        # DB add 被调用 2 次（每个需求一次）
        assert db.add.call_count == 2
        # flush 被调用
        db.flush.assert_called()

    @pytest.mark.asyncio
    async def test_extract_requirements_empty_doc(self) -> None:
        """空文档内容应抛出 ValueError。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        # Mock 空内容文档
        doc = _make_mock_doc(title="空文档", content_text="")
        db.execute = AsyncMock(return_value=_make_scalars_first_result(doc))

        with pytest.raises(ValueError, match="文档内容为空"):
            await service.extract_requirements(str(uuid.uuid4()), str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_extract_requirements_doc_not_found(self) -> None:
        """文档不存在应抛出 ValueError。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        # Mock 文档查询返回 None
        db.execute = AsyncMock(return_value=_make_scalars_first_result(None))

        with pytest.raises(ValueError, match="文档不存在"):
            await service.extract_requirements(str(uuid.uuid4()), str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_extract_requirements_llm_error(self) -> None:
        """LLM 调用异常应透传。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm_error("LLM 连接超时")
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        doc = _make_mock_doc(title="PRD", content_text="PRD 内容")
        db.execute = AsyncMock(return_value=_make_scalars_first_result(doc))

        with pytest.raises(RuntimeError, match="LLM 连接超时"):
            await service.extract_requirements(str(uuid.uuid4()), str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_list_requirements(self) -> None:
        """分页查询需求列表 — 返回 (items, total)。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        req1 = _make_mock_requirement(title="需求1")
        req2 = _make_mock_requirement(title="需求2")

        # db.scalar 返回总数
        db.scalar = AsyncMock(return_value=2)
        # db.execute 返回需求列表
        db.execute = AsyncMock(return_value=_make_scalars_all_result([req1, req2]))

        items, total = await service.list_requirements(uuid.uuid4(), page=1, size=20)

        assert total == 2
        assert len(items) == 2
        assert items[0]["title"] == "需求1"
        assert items[1]["title"] == "需求2"

    @pytest.mark.asyncio
    async def test_get_requirement(self) -> None:
        """按 ID 查询需求点。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        requirement = _make_mock_requirement(title="查询需求")
        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(requirement))

        result = await service.get_requirement(requirement.id)

        assert result is not None
        assert result.title == "查询需求"

    @pytest.mark.asyncio
    async def test_get_requirement_not_found(self) -> None:
        """查询不存在的需求点返回 None。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        result = await service.get_requirement(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_update_requirement(self) -> None:
        """更新需求点 — 验证字段被设置、flush 被调用。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        requirement = _make_mock_requirement(title="原始标题")
        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(requirement))

        result = await service.update_requirement(
            requirement.id,
            title="更新后的标题",
            priority="critical",
            description="更新后的描述",
        )

        assert result.title == "更新后的标题"
        assert result.priority == "critical"
        assert result.description == "更新后的描述"
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_requirement_not_found(self) -> None:
        """更新不存在的需求点应抛出 ValueError。"""
        from app.services.testing.requirement_service import RequirementAnalysisService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = RequirementAnalysisService(llm, db)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        with pytest.raises(ValueError, match="需求点不存在"):
            await service.update_requirement(uuid.uuid4(), title="新标题")


# ======================================================================
# 测试用例生成服务测试
# ======================================================================


class TestTestCaseGenerationService:
    """TestCaseGenerationService 测试 — mock LLM + DB。"""

    @pytest.mark.asyncio
    async def test_generate_cases_success(self) -> None:
        """成功生成测试用例 — LLM 返回 JSON 数组，验证 DB add 与需求状态更新。"""
        from app.services.testing.case_generation_service import TestCaseGenerationService

        llm_response = json.dumps([
            {
                "title": "登录成功测试",
                "description": "输入正确账号密码，验证登录成功",
                "preconditions": "用户已注册",
                "test_steps": [
                    {"step_no": 1, "action": "输入正确邮箱", "expected": "邮箱显示正常"},
                    {"step_no": 2, "action": "输入正确密码", "expected": "密码显示掩码"},
                    {"step_no": 3, "action": "点击登录按钮", "expected": "跳转到首页"},
                ],
                "expected_result": "用户成功登录并跳转首页",
                "test_type": "functional",
                "priority": "high",
                "tags": ["登录", "P0"],
            },
        ])
        llm = _make_mock_llm(llm_response)
        db = _make_mock_db()
        service = TestCaseGenerationService(llm, db)

        requirement = _make_mock_requirement(title="登录需求")

        # db.execute 调用顺序：
        # 1. _get_requirement → scalar_one_or_none → requirement
        # 2. _generate_case_no → scalars().all() → [] (无已有编号)
        # 3. update(TestRequirement status) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(requirement),
            _make_scalars_all_result([]),
            MagicMock(),  # update 语句结果
        ])

        result = await service.generate_cases(str(requirement.id))

        assert len(result) == 1
        assert result[0]["title"] == "登录成功测试"
        assert result[0]["created_by"] == "ai_generate"
        assert result[0]["status"] == "draft"
        assert result[0]["case_no"] is not None
        assert result[0]["test_type"] == "functional"
        assert result[0]["priority"] == "high"
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_case_no(self) -> None:
        """用例编号生成 — 从已有最大编号 +1。"""
        from app.services.testing.case_generation_service import TestCaseGenerationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestCaseGenerationService(llm, db)

        # 已有编号 TC-0001, TC-0003 → 下一个应为 TC-0004
        db.execute = AsyncMock(return_value=_make_scalars_all_result(["TC-0001", "TC-0003"]))

        case_no = await service._generate_case_no(uuid.uuid4())
        assert case_no == "TC-0004"

    @pytest.mark.asyncio
    async def test_generate_case_no_empty(self) -> None:
        """用例编号生成 — 项目无用例时从 TC-0001 开始。"""
        from app.services.testing.case_generation_service import TestCaseGenerationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestCaseGenerationService(llm, db)

        db.execute = AsyncMock(return_value=_make_scalars_all_result([]))

        case_no = await service._generate_case_no(uuid.uuid4())
        assert case_no == "TC-0001"

    @pytest.mark.asyncio
    async def test_generate_cases_with_context_docs(self) -> None:
        """带上下文文档生成用例 — 验证上下文文档查询被调用。"""
        from app.services.testing.case_generation_service import TestCaseGenerationService

        llm_response = json.dumps([
            {
                "title": "API 接口测试",
                "description": "测试登录接口返回值",
                "test_steps": [{"step_no": 1, "action": "发送POST请求", "expected": "返回200"}],
                "expected_result": "接口返回 token",
                "test_type": "api",
                "priority": "high",
                "tags": ["API"],
            },
        ])
        llm = _make_mock_llm(llm_response)
        db = _make_mock_db()
        service = TestCaseGenerationService(llm, db)

        requirement = _make_mock_requirement(title="API需求")
        context_doc = _make_mock_doc(title="接口文档", content_text="POST /api/login 接口定义")

        context_doc_ids = [str(uuid.uuid4())]

        # db.execute 调用顺序：
        # 1. _get_requirement → requirement
        # 2. _get_context_docs → [context_doc]
        # 3. _generate_case_no → [] (无已有编号)
        # 4. update(TestRequirement status) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(requirement),
            _make_scalars_all_result([context_doc]),
            _make_scalars_all_result([]),
            MagicMock(),  # update 语句结果
        ])

        result = await service.generate_cases(
            str(requirement.id),
            context_doc_ids=context_doc_ids,
            test_type="api",
        )

        assert len(result) == 1
        assert result[0]["title"] == "API 接口测试"
        assert result[0]["test_type"] == "api"
        # 验证 db.execute 被调用 4 次（含上下文文档查询）
        assert db.execute.call_count == 4

    @pytest.mark.asyncio
    async def test_generate_cases_requirement_not_found(self) -> None:
        """需求点不存在应抛出 ValueError。"""
        from app.services.testing.case_generation_service import TestCaseGenerationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestCaseGenerationService(llm, db)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        with pytest.raises(ValueError, match="需求点不存在"):
            await service.generate_cases(str(uuid.uuid4()))


# ======================================================================
# 用例评审服务测试
# ======================================================================


class TestTestReviewService:
    """TestReviewService 测试 — mock DB + user。"""

    @pytest.mark.asyncio
    async def test_submit_for_review(self) -> None:
        """提交评审 — 验证评审记录创建、用例状态更新为 pending_review。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        case = _make_mock_test_case(title="待评审用例", status="draft")

        # db.execute 调用顺序：
        # 1. _get_case → case (status=draft)
        # 2. update(TestCase status=pending_review) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(case),
            MagicMock(),  # update 语句结果
        ])

        review = await service.submit_for_review(case.id, comment="请尽快评审")

        assert review.status == "pending"
        assert review.case_id == case.id
        assert review.submitter_id == user.id
        assert review.comment == "请尽快评审"
        db.add.assert_called_once()
        db.flush.assert_called()

    @pytest.mark.asyncio
    async def test_submit_for_review_invalid_status(self) -> None:
        """用例状态不在白名单（如 pending_review）应抛出 ValueError。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        case = _make_mock_test_case(status="pending_review")
        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(case))

        with pytest.raises(ValueError, match="仅"):
            await service.submit_for_review(case.id)

    @pytest.mark.asyncio
    async def test_submit_for_review_case_not_found(self) -> None:
        """用例不存在应抛出 ValueError。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        with pytest.raises(ValueError, match="测试用例不存在"):
            await service.submit_for_review(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_approve_review(self) -> None:
        """通过评审 — 验证评审状态变更为 approved、用例状态变更为 approved。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        review = _make_mock_review(status="pending")

        # db.execute 调用顺序：
        # 1. get_review → review (status=pending)
        # 2. update(TestReview status=approved) → 结果不使用
        # 3. update(TestCase status=approved) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(review),
            MagicMock(),  # update TestReview
            MagicMock(),  # update TestCase
        ])

        result = await service.approve(
            review.id,
            comment="用例质量良好",
            suggestions=[{"type": "improvement", "suggestion": "增加边界用例"}],
        )

        # 验证 db.execute 被调用 3 次
        assert db.execute.call_count == 3
        db.flush.assert_called_once()
        db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_reject_review(self) -> None:
        """驳回评审 — 验证评审状态变更为 rejected、用例状态回退为 draft。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        review = _make_mock_review(status="pending")

        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(review),
            MagicMock(),  # update TestReview
            MagicMock(),  # update TestCase
        ])

        result = await service.reject(
            review.id,
            comment="步骤不够详细",
            suggestions=[{"type": "missing", "suggestion": "补充异常场景"}],
        )

        assert db.execute.call_count == 3
        db.flush.assert_called_once()
        db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_approve_already_resolved(self) -> None:
        """已处理的评审不能再次审批。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        review = _make_mock_review(status="approved")
        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(review))

        with pytest.raises(ValueError, match="评审已处理"):
            await service.approve(review.id)

    @pytest.mark.asyncio
    async def test_get_pending_reviews(self) -> None:
        """查询待评审列表。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        review1 = _make_mock_review(status="pending")
        review2 = _make_mock_review(status="pending")

        # db.execute 调用顺序：
        # 1. count → scalar_one() → 2
        # 2. select → scalars().all() → [review1, review2]
        db.execute = AsyncMock(side_effect=[
            _make_scalar_result(2),
            _make_scalars_all_result([review1, review2]),
        ])

        reviews, total = await service.get_pending_reviews(page=1, size=20)

        assert total == 2
        assert len(reviews) == 2

    @pytest.mark.asyncio
    async def test_get_reviews_by_case(self) -> None:
        """查询某用例的全部评审记录。"""
        from app.services.testing.test_review_service import TestReviewService

        db = _make_mock_db()
        user = _make_mock_user()
        service = TestReviewService(db, user)

        review1 = _make_mock_review(status="approved")
        review2 = _make_mock_review(status="rejected")

        db.execute = AsyncMock(return_value=_make_scalars_all_result([review1, review2]))

        case_id = uuid.uuid4()
        reviews = await service.get_reviews_by_case(case_id)

        assert len(reviews) == 2
        assert reviews[0].status == "approved"
        assert reviews[1].status == "rejected"

    def test_build_review_summary_approved(self) -> None:
        """评审摘要生成 — approved 状态含建议计数。"""
        from app.services.testing.test_review_service import TestReviewService

        summary = TestReviewService._build_review_summary(
            "approved",
            [{"type": "improvement", "suggestion": "增加边界测试"}],
            "通过",
        )
        assert "评审通过" in summary
        assert "1 条建议" in summary
        assert "增加边界测试" in summary

    def test_build_review_summary_rejected(self) -> None:
        """评审摘要生成 — rejected 状态。"""
        from app.services.testing.test_review_service import TestReviewService

        summary = TestReviewService._build_review_summary(
            "rejected",
            None,
            "步骤不完整",
        )
        assert "评审驳回" in summary
        assert "步骤不完整" in summary


# ======================================================================
# 测试用例管理服务测试
# ======================================================================


class TestTestCaseManagementService:
    """TestCaseManagementService 测试 — mock DB。"""

    @pytest.mark.asyncio
    async def test_list_cases_with_filters(self) -> None:
        """分页查询用例列表 — 支持多维度筛选。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        case1 = _make_mock_test_case(title="用例1", status="active", test_type="functional")
        case2 = _make_mock_test_case(title="用例2", status="active", test_type="functional")

        # db.execute 调用顺序：
        # 1. count → scalar_one() → 2
        # 2. select → scalars().all() → [case1, case2]
        db.execute = AsyncMock(side_effect=[
            _make_scalar_result(2),
            _make_scalars_all_result([case1, case2]),
        ])

        cases, total = await service.list_cases(
            uuid.uuid4(),
            status="active",
            test_type="functional",
            priority="normal",
            page=1,
            size=20,
        )

        assert total == 2
        assert len(cases) == 2
        assert cases[0].title == "用例1"

    @pytest.mark.asyncio
    async def test_create_case_manual(self) -> None:
        """手动创建用例 — created_by 固定为 manual。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        # _generate_case_no → scalar_one_or_none → None (无已有编号)
        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        project_id = uuid.uuid4()
        case = await service.create_case(
            project_id,
            title="手动创建用例",
            description="手动创建的测试用例",
            test_type="functional",
            priority="high",
        )

        assert case.title == "手动创建用例"
        assert case.created_by == "manual"
        assert case.case_no == "TC-0001"
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_case_no_title(self) -> None:
        """缺少 title 字段应抛出 ValueError。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        with pytest.raises(ValueError, match="title"):
            await service.create_case(uuid.uuid4(), description="无标题")

    @pytest.mark.asyncio
    async def test_update_case(self) -> None:
        """更新用例字段。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        case = _make_mock_test_case(title="原始标题", status="draft")

        # db.execute 调用顺序：
        # 1. get_case → case
        # 2. update(TestCase) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(case),
            MagicMock(),  # update 语句结果
        ])

        result = await service.update_case(case.id, title="更新标题", status="active")

        db.flush.assert_called()
        db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_case_soft_delete(self) -> None:
        """软删除用例 — 设置 deleted_at，不物理删除。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        case = _make_mock_test_case(title="待删除用例")

        # db.execute 调用顺序：
        # 1. get_case → case
        # 2. update(TestCase deleted_at=...) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(case),
            MagicMock(),  # update 语句结果
        ])

        await service.delete_case(case.id)

        # 验证 flush 被调用（软删除提交）
        db.flush.assert_called_once()
        # 验证 db.execute 被调用 2 次（查询 + 更新）
        assert db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_case_not_found(self) -> None:
        """删除不存在的用例应抛出 ValueError。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        with pytest.raises(ValueError, match="测试用例不存在"):
            await service.delete_case(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_batch_update_status(self) -> None:
        """批量更新用例状态 — 返回更新数量。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        mock_result = MagicMock()
        mock_result.rowcount = 3
        db.execute = AsyncMock(return_value=mock_result)

        case_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        count = await service.batch_update_status(case_ids, "active")

        assert count == 3
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_update_status_empty_ids(self) -> None:
        """空 ID 列表返回 0，不调用 DB。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        count = await service.batch_update_status([], "active")
        assert count == 0
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_stats(self) -> None:
        """获取测试平台统计数据 — 多维度聚合。"""
        from app.services.testing.case_management_service import TestCaseManagementService

        db = _make_mock_db()
        service = TestCaseManagementService(db)

        # db.execute 调用顺序（8 次）：
        # 1. total_projects → scalar_one → 2
        # 2. total_requirements → scalar_one → 5
        # 3. total_cases → scalar_one → 10
        # 4. cases_by_status → 可迭代 → [("draft", 3), ("active", 7)]
        # 5. cases_by_type → 可迭代 → [("functional", 8), ("api", 2)]
        # 6. total_plans → scalar_one → 3
        # 7. total_executions → scalar_one → 20
        # 8. execution_stats → 可迭代 → [("passed", 15), ("failed", 5)]
        db.execute = AsyncMock(side_effect=[
            _make_scalar_result(2),                                        # total_projects
            _make_scalar_result(5),                                        # total_requirements
            _make_scalar_result(10),                                       # total_cases
            _make_iterable_result([("draft", 3), ("active", 7)]),          # cases_by_status
            _make_iterable_result([("functional", 8), ("api", 2)]),        # cases_by_type
            _make_scalar_result(3),                                        # total_plans
            _make_scalar_result(20),                                       # total_executions
            _make_iterable_result([("passed", 15), ("failed", 5)]),        # execution_stats
        ])

        stats = await service.get_stats()

        assert stats["total_projects"] == 2
        assert stats["total_requirements"] == 5
        assert stats["total_cases"] == 10
        assert stats["cases_by_status"]["draft"] == 3
        assert stats["cases_by_status"]["active"] == 7
        assert stats["cases_by_type"]["functional"] == 8
        assert stats["cases_by_type"]["api"] == 2
        assert stats["total_plans"] == 3
        assert stats["total_executions"] == 20
        assert stats["execution_stats"]["passed"] == 15
        assert stats["execution_stats"]["failed"] == 5
        # 通过率 = 15 / 20 * 100 = 75.0
        assert stats["pass_rate"] == 75.0


# ======================================================================
# 测试编排服务测试
# ======================================================================


class TestTestOrchestrationService:
    """TestOrchestrationService 测试 — mock LLM + DB。"""

    @pytest.mark.asyncio
    async def test_create_plan(self) -> None:
        """创建测试计划 — 初始状态为 draft。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        project_id = uuid.uuid4()
        user_id = uuid.uuid4()
        case_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

        plan = await service.create_plan(
            project_id=project_id,
            name="v1.0 回归测试",
            description="全量回归测试计划",
            case_ids=case_ids,
            execution_strategy="priority_based",
            user_id=user_id,
        )

        assert plan.name == "v1.0 回归测试"
        assert plan.status == "draft"
        assert plan.execution_strategy == "priority_based"
        assert plan.project_id == project_id
        assert plan.created_by == user_id
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_orchestrate_success(self) -> None:
        """AI 编排成功 — LLM 返回编排 JSON，验证计划状态更新为 active。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        case1 = _make_mock_test_case(title="用例1", test_type="api", priority="high", status="approved")
        case2 = _make_mock_test_case(title="用例2", test_type="ui", priority="normal", status="approved")

        orchestration_json = json.dumps({
            "execution_order": [str(case1.id), str(case2.id)],
            "node_assignments": {
                "node_1": [str(case1.id)],
                "node_2": [str(case2.id)],
            },
            "dependencies": [
                {"case_id": str(case2.id), "depends_on": str(case1.id)},
            ],
            "rationale": "API 测试优先于 UI 测试",
        })
        llm = _make_mock_llm(orchestration_json)
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        plan = _make_mock_plan(
            name="编排计划",
            case_ids=[str(case1.id), str(case2.id)],
        )

        # db.execute 调用顺序：
        # 1. get_plan → plan
        # 2. _get_plan_cases → [case1, case2]
        # 3. update(TestPlan ai_orchestration + status=active) → 结果不使用
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(plan),
            _make_scalars_all_result([case1, case2]),
            MagicMock(),  # update 语句结果
        ])

        result = await service.orchestrate(plan.id, node_count=2)

        assert db.execute.call_count == 3
        db.flush.assert_called_once()
        db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_orchestrate_plan_not_found(self) -> None:
        """计划不存在应抛出 ValueError。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        db.execute = AsyncMock(return_value=_make_scalar_one_or_none_result(None))

        with pytest.raises(ValueError, match="测试计划不存在"):
            await service.orchestrate(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_orchestrate_no_cases(self) -> None:
        """计划中无可用用例应抛出 ValueError。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        plan = _make_mock_plan(name="空计划", case_ids=[])
        db.execute = AsyncMock(side_effect=[
            _make_scalar_one_or_none_result(plan),
            _make_scalars_all_result([]),
        ])

        with pytest.raises(ValueError, match="无可用用例"):
            await service.orchestrate(plan.id)

    @pytest.mark.asyncio
    async def test_record_execution(self) -> None:
        """记录执行结果 — passed 状态自动设置 started_at 和 completed_at。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        case_id = uuid.uuid4()
        plan_id = uuid.uuid4()

        execution = await service.record_execution(
            case_id=case_id,
            plan_id=plan_id,
            executor="human",
            status="passed",
            result="测试通过",
            duration_seconds=30,
        )

        assert execution.case_id == case_id
        assert execution.plan_id == plan_id
        assert execution.status == "passed"
        assert execution.executor == "human"
        assert execution.duration_seconds == 30
        # passed 状态应同时设置 started_at 和 completed_at
        assert execution.started_at is not None
        assert execution.completed_at is not None
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_execution_pending_no_timestamps(self) -> None:
        """pending 状态不设置 started_at 和 completed_at。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        execution = await service.record_execution(
            case_id=uuid.uuid4(),
            status="pending",
        )

        assert execution.started_at is None
        assert execution.completed_at is None

    @pytest.mark.asyncio
    async def test_list_executions(self) -> None:
        """分页查询执行记录。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        exec1 = MagicMock()
        exec1.status = "passed"
        exec2 = MagicMock()
        exec2.status = "failed"

        # db.execute 调用顺序：
        # 1. count → scalar_one → 2
        # 2. select → scalars().all() → [exec1, exec2]
        db.execute = AsyncMock(side_effect=[
            _make_scalar_result(2),
            _make_scalars_all_result([exec1, exec2]),
        ])

        executions, total = await service.list_executions(
            plan_id=uuid.uuid4(),
            page=1,
            size=20,
        )

        assert total == 2
        assert len(executions) == 2

    @pytest.mark.asyncio
    async def test_list_plans(self) -> None:
        """分页查询测试计划列表。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        llm = _make_mock_llm()
        db = _make_mock_db()
        service = TestOrchestrationService(llm, db)

        plan1 = _make_mock_plan(name="计划1")
        plan2 = _make_mock_plan(name="计划2")

        db.execute = AsyncMock(side_effect=[
            _make_scalar_result(2),
            _make_scalars_all_result([plan1, plan2]),
        ])

        plans, total = await service.list_plans(uuid.uuid4(), page=1, size=20)

        assert total == 2
        assert len(plans) == 2
        assert plans[0].name == "计划1"

    def test_normalize_orchestration_dict(self) -> None:
        """编排方案规范化 — dict 输入补全缺失字段。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        result = TestOrchestrationService._normalize_orchestration({
            "execution_order": ["case-1", "case-2"],
        })
        assert result["execution_order"] == ["case-1", "case-2"]
        assert result["node_assignments"] == {}
        assert result["dependencies"] == []
        assert result["rationale"] == ""

    def test_normalize_orchestration_non_dict(self) -> None:
        """编排方案规范化 — 非 dict 输入返回默认值。"""
        from app.services.testing.orchestration_service import TestOrchestrationService

        result = TestOrchestrationService._normalize_orchestration(["not", "a", "dict"])
        assert result["execution_order"] == []
        assert result["node_assignments"] == {}
        assert result["dependencies"] == []
        assert "格式异常" in result["rationale"]


# ======================================================================
# JSON 解析辅助函数测试
# ======================================================================


class TestExtractJson:
    """_extract_json 辅助函数测试 — 处理 markdown 代码块 / 纯 JSON / 混杂文本 / 数组。"""

    def test_extract_json_plain(self) -> None:
        """纯 JSON 字符串直接解析。"""
        from app.services.testing.requirement_service import _extract_json

        result = _extract_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_extract_json_code_fence(self) -> None:
        """markdown 代码块包裹的 JSON 应正确提取。"""
        from app.services.testing.requirement_service import _extract_json

        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_extract_json_code_fence_no_lang(self) -> None:
        """无语言标识的代码块也应正确提取。"""
        from app.services.testing.requirement_service import _extract_json

        text = '```\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_extract_json_with_text(self) -> None:
        """JSON 前后夹杂文本时应通过正则匹配提取。"""
        from app.services.testing.requirement_service import _extract_json

        text = '这是分析结果：\n{"key": "value", "num": 42}\n以上为提取的内容。'
        result = _extract_json(text)
        assert isinstance(result, dict)
        assert result["key"] == "value"
        assert result["num"] == 42

    def test_extract_json_array(self) -> None:
        """JSON 数组应正确解析。"""
        from app.services.testing.requirement_service import _extract_json

        text = '[{"a": 1}, {"b": 2}, {"c": 3}]'
        result = _extract_json(text)
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0] == {"a": 1}
        assert result[2] == {"c": 3}

    def test_extract_json_array_with_text(self) -> None:
        """夹杂文本的 JSON 数组也应通过正则提取。"""
        from app.services.testing.requirement_service import _extract_json

        text = '以下是测试用例：\n[{"title": "用例1"}, {"title": "用例2"}]\n请参考。'
        result = _extract_json(text)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_extract_json_invalid_raises(self) -> None:
        """无法提取有效 JSON 时应抛出 ValueError。"""
        from app.services.testing.requirement_service import _extract_json

        with pytest.raises(ValueError, match="Failed to parse JSON"):
            _extract_json("这不是JSON，也没有任何JSON结构")

    def test_extract_json_empty_string_raises(self) -> None:
        """空字符串应抛出 ValueError。"""
        from app.services.testing.requirement_service import _extract_json

        with pytest.raises(ValueError):
            _extract_json("")

    def test_extract_json_from_case_generation_service(self) -> None:
        """验证 case_generation_service 中的 _extract_json 行为一致。"""
        from app.services.testing.case_generation_service import _extract_json as _extract_json_cg

        text = '```json\n{"test_cases": [{"title": "用例"}]}\n```'
        result = _extract_json_cg(text)
        assert result == {"test_cases": [{"title": "用例"}]}

    def test_extract_json_from_orchestration_service(self) -> None:
        """验证 orchestration_service 中的 _extract_json 行为一致。"""
        from app.services.testing.orchestration_service import _extract_json as _extract_json_orch

        text = '{"execution_order": ["c1", "c2"], "rationale": "test"}'
        result = _extract_json_orch(text)
        assert result["execution_order"] == ["c1", "c2"]
        assert result["rationale"] == "test"
