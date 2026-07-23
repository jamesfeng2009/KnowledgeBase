"""
智能测试平台 Celery 任务 — 异步执行需求提取 / 用例生成 / 计划编排。

将耗时的 LLM 调用从 HTTP 请求中剥离，避免请求超时：
    extract_requirements_task  — 从 PRD/UI 稿异步提取需求点
    generate_test_cases_task   — 基于需求点异步生成测试用例
    orchestrate_test_plan_task — 异步 AI 编排测试计划

在 ``celery_app`` 不可用时（如开发环境）优雅降级，仅输出告警日志。
"""

from __future__ import annotations

import asyncio
import uuid

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    """在同步 Celery 任务中执行异步协程。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ======================================================================
# 异步业务逻辑
# ======================================================================


async def _extract_requirements(
    project_id: str,
    doc_id: str,
    target_categories: list[str] | None = None,
    tenant_id: str | None = None,
) -> dict:
    """异步执行需求提取 — 调用 RequirementAnalysisService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.testing import RequirementAnalysisService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("testing.llm_unavailable", error=str(exc))
            return {
                "project_id": project_id,
                "doc_id": doc_id,
                "status": "skipped",
                "reason": "llm_unavailable",
            }

        service = RequirementAnalysisService(llm, db, tenant_id=tid)
        try:
            result = await service.extract_requirements(
                project_id, doc_id, target_categories
            )
            await db.commit()
            logger.info(
                "testing.requirements_extracted",
                project_id=project_id,
                doc_id=doc_id,
                count=len(result),
            )
            return {
                "project_id": project_id,
                "doc_id": doc_id,
                "status": "success",
                "requirements": result,
                "count": len(result),
            }
        except Exception as exc:
            await db.rollback()
            logger.error(
                "testing.requirements_extract_failed",
                project_id=project_id,
                doc_id=doc_id,
                error=str(exc),
            )
            return {
                "project_id": project_id,
                "doc_id": doc_id,
                "status": "failed",
                "error": str(exc),
            }


async def _generate_test_cases(
    requirement_id: str,
    context_doc_ids: list[str] | None = None,
    test_type: str | None = None,
    max_cases: int = 5,
    tenant_id: str | None = None,
) -> dict:
    """异步执行用例生成 — 调用 TestCaseGenerationService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.testing import TestCaseGenerationService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("testing.llm_unavailable", error=str(exc))
            return {
                "requirement_id": requirement_id,
                "status": "skipped",
                "reason": "llm_unavailable",
            }

        service = TestCaseGenerationService(llm, db, tenant_id=tid)
        try:
            result = await service.generate_cases(
                requirement_id,
                context_doc_ids=context_doc_ids,
                test_type=test_type,
                max_cases=max_cases,
            )
            await db.commit()
            logger.info(
                "testing.cases_generated",
                requirement_id=requirement_id,
                count=len(result),
            )
            return {
                "requirement_id": requirement_id,
                "status": "success",
                "cases": result,
                "count": len(result),
            }
        except Exception as exc:
            await db.rollback()
            logger.error(
                "testing.cases_generation_failed",
                requirement_id=requirement_id,
                error=str(exc),
            )
            return {
                "requirement_id": requirement_id,
                "status": "failed",
                "error": str(exc),
            }


async def _orchestrate_test_plan(
    plan_id: str,
    node_count: int = 3,
    consider_dependencies: bool = True,
    tenant_id: str | None = None,
) -> dict:
    """异步执行计划编排 — 调用 TestOrchestrationService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.testing import TestOrchestrationService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("testing.llm_unavailable", error=str(exc))
            return {
                "plan_id": plan_id,
                "status": "skipped",
                "reason": "llm_unavailable",
            }

        service = TestOrchestrationService(llm, db, tenant_id=tid)
        try:
            plan = await service.orchestrate(
                uuid.UUID(plan_id),
                node_count=node_count,
                consider_dependencies=consider_dependencies,
            )
            await db.commit()
            logger.info(
                "testing.plan_orchestrated",
                plan_id=plan_id,
                node_count=node_count,
            )
            return {
                "plan_id": str(plan.id),
                "name": plan.name,
                "status": plan.status,
                "execution_strategy": plan.execution_strategy,
                "ai_orchestration": plan.ai_orchestration,
            }
        except Exception as exc:
            await db.rollback()
            logger.error(
                "testing.plan_orchestration_failed",
                plan_id=plan_id,
                error=str(exc),
            )
            return {
                "plan_id": plan_id,
                "status": "failed",
                "error": str(exc),
            }


# ======================================================================
# Celery 任务定义 — 延迟导入 celery_app 避免循环依赖
# ======================================================================

try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.testing_tasks.extract_requirements_task")
    def extract_requirements_task(
        project_id: str,
        doc_id: str,
        target_categories: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        """异步从 PRD/UI 稿提取原子需求点。

        读取知识库文档内容，调用 LLM 分析并拆分为原子需求点，
        持久化到 TestRequirement 表。LLM 不可用时优雅降级。

        Args:
            project_id: 测试项目 ID（UUID 字符串）。
            doc_id: 来源文档 ID（知识库 Document UUID 字符串）。
            target_categories: 可选，指定提取的需求分类。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            处理结果摘要，含 status / count / requirements 等字段。
        """
        logger.info(
            "testing.extract_requirements_task_started",
            project_id=project_id,
            doc_id=doc_id,
        )
        try:
            result = _run_async(
                _extract_requirements(
                    project_id, doc_id, target_categories, tenant_id
                )
            )
            logger.info(
                "testing.extract_requirements_task_completed",
                project_id=project_id,
                doc_id=doc_id,
                status=result.get("status"),
            )
            return result
        except Exception as exc:
            logger.error(
                "testing.extract_requirements_task_failed",
                project_id=project_id,
                doc_id=doc_id,
                error=str(exc),
            )
            return {
                "project_id": project_id,
                "doc_id": doc_id,
                "status": "failed",
                "error": str(exc),
            }

    @celery_app.task(name="tasks.testing_tasks.generate_test_cases_task")
    def generate_test_cases_task(
        requirement_id: str,
        context_doc_ids: list[str] | None = None,
        test_type: str | None = None,
        max_cases: int = 5,
        tenant_id: str | None = None,
    ) -> dict:
        """异步基于需求点 + 上下文文档生成测试用例。

        读取需求点详情和上下文文档（技术方案/接口文档），调用 LLM 生成
        覆盖功能、接口、边界和异常场景的测试用例，持久化到 TestCase 表。
        LLM 不可用时优雅降级。

        Args:
            requirement_id: 需求点 ID（UUID 字符串）。
            context_doc_ids: 可选，额外上下文文档 ID 列表。
            test_type: 可选，指定测试类型。
            max_cases: 最大生成用例数，默认 5。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            处理结果摘要，含 status / count / cases 等字段。
        """
        logger.info(
            "testing.generate_test_cases_task_started",
            requirement_id=requirement_id,
        )
        try:
            result = _run_async(
                _generate_test_cases(
                    requirement_id,
                    context_doc_ids=context_doc_ids,
                    test_type=test_type,
                    max_cases=max_cases,
                    tenant_id=tenant_id,
                )
            )
            logger.info(
                "testing.generate_test_cases_task_completed",
                requirement_id=requirement_id,
                status=result.get("status"),
            )
            return result
        except Exception as exc:
            logger.error(
                "testing.generate_test_cases_task_failed",
                requirement_id=requirement_id,
                error=str(exc),
            )
            return {
                "requirement_id": requirement_id,
                "status": "failed",
                "error": str(exc),
            }

    @celery_app.task(name="tasks.testing_tasks.orchestrate_test_plan_task")
    def orchestrate_test_plan_task(
        plan_id: str,
        node_count: int = 3,
        consider_dependencies: bool = True,
        tenant_id: str | None = None,
    ) -> dict:
        """异步 AI 编排测试计划。

        调用 LLM 分析计划中的测试用例，生成最优执行顺序、节点分配和
        依赖关系方案，存入 TestPlan.ai_orchestration。LLM 不可用时优雅降级。

        Args:
            plan_id: 测试计划 ID（UUID 字符串）。
            node_count: 执行节点数量，默认 3。
            consider_dependencies: 是否考虑用例依赖关系，默认 True。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            处理结果摘要，含 status / ai_orchestration 等字段。
        """
        logger.info(
            "testing.orchestrate_test_plan_task_started",
            plan_id=plan_id,
            node_count=node_count,
        )
        try:
            result = _run_async(
                _orchestrate_test_plan(
                    plan_id,
                    node_count=node_count,
                    consider_dependencies=consider_dependencies,
                    tenant_id=tenant_id,
                )
            )
            logger.info(
                "testing.orchestrate_test_plan_task_completed",
                plan_id=plan_id,
                status=result.get("status"),
            )
            return result
        except Exception as exc:
            logger.error(
                "testing.orchestrate_test_plan_task_failed",
                plan_id=plan_id,
                error=str(exc),
            )
            return {
                "plan_id": plan_id,
                "status": "failed",
                "error": str(exc),
            }

except ImportError:
    logger.warning("testing.celery_not_available")
