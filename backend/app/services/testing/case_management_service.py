"""
测试用例管理服务 — 单一职责：测试用例的 CRUD、批量操作与统计。

纯 CRUD 服务，不依赖 LLM。支持手动创建用例（created_by="manual"）、
多维度筛选查询、软删除、批量状态更新以及平台级统计。

关键设计：
    - 软删除：删除操作仅设置 deleted_at，查询统一过滤 deleted_at IS NULL。
    - 用例编号：create_case 时自动生成项目内递增编号（TC-0001）。
    - 统计聚合：get_stats 使用 GROUP BY 聚合各维度计数，并计算执行通过率。

使用方式::

    service = TestCaseManagementService(db)
    case = await service.create_case(project_id, title="登录测试", ...)
    cases, total = await service.list_cases(project_id, status="active", page=1)
    stats = await service.get_stats(project_id)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.testing import (
    TestCase,
    TestExecution,
    TestPlan,
    TestProject,
    TestRequirement,
)
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)


class TestCaseManagementService:
    """测试用例管理服务 — 用例 CRUD、批量操作与平台统计。

    纯 CRUD 服务，不涉及 LLM 调用。通过 ``AsyncSession`` 直接操作 ORM，
    事务由 ``get_db_session`` 依赖统一管理。

    使用方式::

        service = TestCaseManagementService(db)
        case = await service.create_case(project_id, title="登录测试")
        cases, total = await service.list_cases(project_id, status="active")
        stats = await service.get_stats(project_id)
    """

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        """初始化用例管理服务。

        Args:
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_cases(
        self,
        project_id: uuid.UUID,
        status: str | None = None,
        test_type: str | None = None,
        priority: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[TestCase], int]:
        """分页查询用例列表 — 支持多维度筛选与标题关键字搜索。

        所有查询自动过滤软删除记录（deleted_at IS NULL）。

        Args:
            project_id: 项目 ID。
            status: 用例状态过滤（可选）。
            test_type: 测试类型过滤（可选）。
            priority: 优先级过滤（可选）。
            keyword: 标题关键字（ilike 模糊匹配，可选）。
            page: 页码，从 1 开始。
            size: 每页数量。

        Returns:
            ``(cases, total)`` — 当前页用例列表与总记录数。
        """
        # 构建基础查询条件
        conditions = [
            TestCase.project_id == project_id,
            TestCase.deleted_at.is_(None),
        ]
        if status:
            conditions.append(TestCase.status == status)
        if test_type:
            conditions.append(TestCase.test_type == test_type)
        if priority:
            conditions.append(TestCase.priority == priority)
        if keyword:
            conditions.append(TestCase.title.ilike(f"%{keyword}%"))

        # 总数
        count_stmt = select(func.count()).select_from(TestCase).where(*conditions)
        count_stmt = apply_tenant_filter(count_stmt, TestCase, self._tenant_id)
        total = (await self.db.execute(count_stmt)).scalar_one()

        # 分页数据
        offset = (page - 1) * size
        stmt = select(TestCase).where(*conditions)
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        stmt = (
            stmt.order_by(TestCase.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        cases = list(result.scalars().all())
        return cases, total

    async def get_case(self, case_id: uuid.UUID) -> TestCase | None:
        """按 ID 查询用例 — 过滤软删除记录。

        Args:
            case_id: 用例 ID。

        Returns:
            ``TestCase`` 或 ``None``（不存在或已删除时）。
        """
        stmt = select(TestCase).where(
            TestCase.id == case_id,
            TestCase.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # 创建 / 更新 / 删除
    # ------------------------------------------------------------------

    async def create_case(self, project_id: uuid.UUID, **kwargs) -> TestCase:
        """手动创建测试用例 — created_by 固定为 "manual"。

        自动生成项目内递增的用例编号（case_no），格式为 TC-0001。
        其余字段通过 kwargs 传入（title / description / test_steps 等）。

        Args:
            project_id: 项目 ID。
            **kwargs: 用例字段，如 title、description、preconditions、
                test_steps、expected_result、test_type、priority、tags、
                requirement_id 等。

        Returns:
            创建后的 ``TestCase`` 对象。

        Raises:
            ValueError: 缺少 title 字段。
        """
        title = kwargs.get("title")
        if not title:
            raise ValueError("创建用例必须提供 title 字段")

        case_no = await self._generate_case_no(project_id)
        case = TestCase(
            project_id=project_id,
            case_no=case_no,
            created_by="manual",
            **kwargs,
        )
        self.db.add(case)
        await self.db.flush()

        log.info(
            "test_case.created",
            case_id=str(case.id),
            case_no=case_no,
            project_id=str(project_id),
        )
        return case

    async def update_case(self, case_id: uuid.UUID, **kwargs) -> TestCase:
        """更新测试用例 — 仅更新 kwargs 中提供的字段。

        Args:
            case_id: 用例 ID。
            **kwargs: 需更新的字段。

        Returns:
            更新后的 ``TestCase`` 对象。

        Raises:
            ValueError: 用例不存在或已软删除。
        """
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError(f"测试用例不存在: {case_id}")

        if kwargs:
            stmt = update(TestCase).where(TestCase.id == case_id)
            stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
            await self.db.execute(stmt.values(**kwargs))
            await self.db.flush()
            await self.db.refresh(case)

        log.info(
            "test_case.updated",
            case_id=str(case_id),
            fields=list(kwargs.keys()),
        )
        return case

    async def delete_case(self, case_id: uuid.UUID) -> None:
        """软删除测试用例 — 设置 deleted_at，不物理删除。

        Args:
            case_id: 用例 ID。

        Raises:
            ValueError: 用例不存在或已软删除。
        """
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError(f"测试用例不存在: {case_id}")

        stmt = update(TestCase).where(TestCase.id == case_id)
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        await self.db.execute(
            stmt.values(deleted_at=datetime.now(timezone.utc))
        )
        await self.db.flush()

        log.info("test_case.deleted", case_id=str(case_id))

    # ------------------------------------------------------------------
    # 批量操作
    # ------------------------------------------------------------------

    async def batch_update_status(
        self,
        case_ids: list[uuid.UUID],
        status: str,
    ) -> int:
        """批量更新用例状态 — 仅更新未软删除的用例。

        Args:
            case_ids: 用例 ID 列表。
            status: 目标状态。

        Returns:
            实际更新的用例数量。
        """
        if not case_ids:
            return 0

        stmt = (
            update(TestCase)
            .where(
                TestCase.id.in_(case_ids),
                TestCase.deleted_at.is_(None),
            )
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt.values(status=status))
        await self.db.flush()

        updated_count = result.rowcount or 0
        log.info(
            "test_case.batch_updated",
            count=updated_count,
            status=status,
        )
        return updated_count

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    async def get_stats(self, project_id: uuid.UUID | None = None) -> dict:
        """获取测试平台统计数据 — 用例、需求、计划、执行记录的多维度聚合。

        统计内容：
            - total_projects: 项目总数
            - total_requirements: 需求总数
            - total_cases: 用例总数
            - cases_by_status: 按状态分组的用例计数
            - cases_by_type: 按类型分组的用例计数
            - total_plans: 测试计划总数
            - total_executions: 执行记录总数
            - pass_rate: 执行通过率（passed / 总执行数）
            - execution_stats: 按状态分组的执行计数

        Args:
            project_id: 项目 ID（可选）。指定时仅统计该项目数据，
                未指定时统计全平台数据。

        Returns:
            统计数据字典。
        """
        # --- 项目总数 ---
        project_stmt = select(func.count()).select_from(TestProject).where(
            TestProject.deleted_at.is_(None)
        )
        if project_id:
            project_stmt = project_stmt.where(TestProject.id == project_id)
        total_projects = (await self.db.execute(project_stmt)).scalar_one()

        # --- 需求总数 ---
        req_stmt = select(func.count()).select_from(TestRequirement).where(
            TestRequirement.deleted_at.is_(None)
        )
        if project_id:
            req_stmt = req_stmt.where(TestRequirement.project_id == project_id)
        total_requirements = (await self.db.execute(req_stmt)).scalar_one()

        # --- 用例统计 ---
        case_base = [TestCase.deleted_at.is_(None)]
        if project_id:
            case_base.append(TestCase.project_id == project_id)

        total_cases_stmt = (
            select(func.count()).select_from(TestCase).where(*case_base)
        )
        total_cases_stmt = apply_tenant_filter(
            total_cases_stmt, TestCase, self._tenant_id
        )
        total_cases = (
            await self.db.execute(total_cases_stmt)
        ).scalar_one()

        # 按状态分组
        status_stmt = select(TestCase.status, func.count()).where(*case_base)
        status_stmt = apply_tenant_filter(status_stmt, TestCase, self._tenant_id)
        status_stmt = status_stmt.group_by(TestCase.status)
        status_result = await self.db.execute(status_stmt)
        cases_by_status = {row[0]: row[1] for row in status_result}

        # 按类型分组
        type_stmt = select(TestCase.test_type, func.count()).where(*case_base)
        type_stmt = apply_tenant_filter(type_stmt, TestCase, self._tenant_id)
        type_stmt = type_stmt.group_by(TestCase.test_type)
        type_result = await self.db.execute(type_stmt)
        cases_by_type = {row[0]: row[1] for row in type_result}

        # --- 计划总数 ---
        plan_base = [TestPlan.deleted_at.is_(None)]
        if project_id:
            plan_base.append(TestPlan.project_id == project_id)
        total_plans = (
            await self.db.execute(
                select(func.count()).select_from(TestPlan).where(*plan_base)
            )
        ).scalar_one()

        # --- 执行统计 ---
        exec_base: list = []
        if project_id:
            # 通过 plan_id 关联过滤到项目
            exec_base.append(
                TestExecution.plan_id.in_(
                    select(TestPlan.id).where(TestPlan.project_id == project_id)
                )
            )
        total_executions = (
            await self.db.execute(
                select(func.count()).select_from(TestExecution).where(*exec_base)
            )
        ).scalar_one()

        # 按执行状态分组
        exec_status_stmt = (
            select(TestExecution.status, func.count())
            .where(*exec_base)
            .group_by(TestExecution.status)
        )
        exec_status_result = await self.db.execute(exec_status_stmt)
        execution_stats = {row[0]: row[1] for row in exec_status_result}

        # 通过率计算
        passed_count = execution_stats.get("passed", 0)
        pass_rate = (
            round(passed_count / total_executions * 100, 2)
            if total_executions > 0
            else 0.0
        )

        return {
            "total_projects": total_projects,
            "total_requirements": total_requirements,
            "total_cases": total_cases,
            "cases_by_status": cases_by_status,
            "cases_by_type": cases_by_type,
            "total_plans": total_plans,
            "total_executions": total_executions,
            "pass_rate": pass_rate,
            "execution_stats": execution_stats,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _generate_case_no(self, project_id: uuid.UUID) -> str:
        """生成项目内递增的用例编号 — 格式 TC-0001。

        查询当前项目已有用例的最大编号，递增 1。若项目无用例则从 TC-0001 开始。
        编号基于 case_no 的字符串排序（因格式固定为 TC-XXXX，字典序与数值序一致）。

        Args:
            project_id: 项目 ID。

        Returns:
            用例编号字符串，如 ``TC-0001``。
        """
        stmt = select(func.max(TestCase.case_no)).where(
            TestCase.project_id == project_id,
            TestCase.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt)
        max_case_no = result.scalar_one_or_none()

        if max_case_no:
            # 提取数字部分并递增
            try:
                current_num = int(max_case_no.split("-")[-1])
            except (ValueError, IndexError):
                current_num = 0
            next_num = current_num + 1
        else:
            next_num = 1

        return f"TC-{next_num:04d}"
