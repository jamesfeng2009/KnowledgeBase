"""
智能测试平台服务包 — 单一职责：测试平台全部业务服务。

遵循单一职责：本文件仅做导出，不包含业务逻辑。
遵循依赖倒置：API 层通过 Service 接口访问业务逻辑，
不直接操作 Repository，实现业务逻辑与数据访问解耦。

核心流程：
    PRD/UI稿 → 需求提取(RequirementAnalysisService)
    → 用例生成(TestCaseGenerationService)
    → 用例评审(TestReviewService)
    → 用例管理(TestCaseManagementService)
    → AI 编排(TestOrchestrationService)

使用方式::

    from app.services.testing import RequirementAnalysisService, TestCaseGenerationService
    from app.llm.factory import get_llm_provider

    async def extract_endpoint(db: AsyncSession = Depends(get_db_session)):
        llm = get_llm_provider()
        service = RequirementAnalysisService(llm, db)
        requirements = await service.extract_requirements(project_id, doc_id)
"""

from app.services.testing.case_generation_service import TestCaseGenerationService
from app.services.testing.case_management_service import TestCaseManagementService
from app.services.testing.orchestration_service import TestOrchestrationService
from app.services.testing.requirement_service import RequirementAnalysisService
from app.services.testing.test_review_service import TestReviewService

__all__ = [
    "RequirementAnalysisService",
    "TestCaseGenerationService",
    "TestReviewService",
    "TestCaseManagementService",
    "TestOrchestrationService",
]
