"""
测试编排服务 — 单一职责：测试计划的创建、AI 编排与执行记录管理。

通过 LLMProvider 分析计划中的测试用例，生成最优执行顺序、节点分配和
依赖关系方案，实现智能化的测试编排。

关键设计：
    - AI 编排：orchestrate 方法构建 prompt 调用 LLM，综合考虑用例优先级、
      测试类型依赖（如 API 测试先于 UI 测试）、执行节点容量，
      生成 execution_order / node_assignments / dependencies / rationale。
    - JSON 解析容错：LLM 返回可能包含 markdown 代码块，使用 _extract_json
      辅助函数稳健提取 JSON。
    - 执行记录：record_execution 根据状态自动设置 started_at / completed_at。

使用方式::

    service = TestOrchestrationService(llm, db)
    plan = await service.create_plan(project_id, name="v1.0 回归", ...)
    plan = await service.orchestrate(plan_id, node_count=3)
    execution = await service.record_execution(case_id, plan_id, status="passed")
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.testing import TestCase, TestExecution, TestPlan
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

# 需要记录开始时间的执行状态
_RUNNING_STATUSES: frozenset[str] = frozenset({"running", "passed", "failed"})
# 需要记录完成时间的执行状态
_COMPLETED_STATUSES: frozenset[str] = frozenset(
    {"passed", "failed", "blocked", "skipped"}
)


def _extract_json(text: str) -> list | dict:
    """从 LLM 响应中提取 JSON — 处理 markdown 代码块包裹。

    LLM 返回的 JSON 可能被 ```json ... ``` 代码块包裹，也可能夹杂
    额外文本。本函数依次尝试：代码块提取 → 直接解析 → 正则匹配。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析后的 list 或 dict。

    Raises:
        ValueError: 无法从文本中提取有效 JSON。
    """
    # 尝试提取 markdown 代码块中的内容
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1)

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 回退：正则匹配 JSON 数组或对象
    for pattern in [r"\[.*\]", r"\{.*\}"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Failed to parse JSON from LLM response: {text[:200]}")


class TestOrchestrationService:
    """测试编排服务 — 计划创建、AI 编排与执行记录管理。

    通过 ``LLMProvider`` 调用 LLM 生成执行编排方案，通过 ``AsyncSession``
    操作 ORM。事务由 ``get_db_session`` 依赖统一管理。

    使用方式::

        service = TestOrchestrationService(llm, db)
        plan = await service.create_plan(
            project_id, name="v1.0 回归", case_ids=[...],
            execution_strategy="priority_based", user_id=user_id,
        )
        plan = await service.orchestrate(plan_id, node_count=3)
        execution = await service.record_execution(case_id, status="passed")
    """

    def __init__(
        self, llm: LLMProvider, db: AsyncSession, tenant_id: UUID | None = None
    ) -> None:
        """初始化编排服务。

        Args:
            llm: LLM Provider 实例，用于生成 AI 编排方案。
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.llm: LLMProvider = llm
        self.db: AsyncSession = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 计划管理
    # ------------------------------------------------------------------

    async def create_plan(
        self,
        project_id: uuid.UUID,
        name: str,
        description: str | None,
        case_ids: list[str],
        execution_strategy: str,
        user_id: uuid.UUID,
    ) -> TestPlan:
        """创建测试计划 — 初始状态为 draft。

        Args:
            project_id: 项目 ID。
            name: 计划名称。
            description: 计划描述（可选）。
            case_ids: 包含的用例 ID 列表（字符串形式）。
            execution_strategy: 执行策略 — sequential / parallel / priority_based。
            user_id: 创建者 ID。

        Returns:
            创建后的 ``TestPlan`` 对象（status 为 draft）。
        """
        plan = TestPlan(
            project_id=project_id,
            name=name,
            description=description,
            case_ids=case_ids,
            execution_strategy=execution_strategy,
            status="draft",
            created_by=user_id,
            tenant_id=self._tenant_id,  # RLS WITH CHECK 要求写入行携带当前租户 ID
        )
        self.db.add(plan)
        await self.db.flush()

        log.info(
            "test_plan.created",
            plan_id=str(plan.id),
            project_id=str(project_id),
            case_count=len(case_ids),
        )
        return plan

    async def get_plan(self, plan_id: uuid.UUID) -> TestPlan | None:
        """按 ID 查询测试计划 — 过滤软删除记录。

        Args:
            plan_id: 计划 ID。

        Returns:
            ``TestPlan`` 或 ``None``（不存在或已删除时）。
        """
        stmt = select(TestPlan).where(
            TestPlan.id == plan_id,
            TestPlan.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestPlan, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_plans(
        self,
        project_id: uuid.UUID,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[TestPlan], int]:
        """分页查询项目的测试计划 — 按 created_at 降序排列。

        Args:
            project_id: 项目 ID。
            page: 页码，从 1 开始。
            size: 每页数量。

        Returns:
            ``(plans, total)`` — 当前页计划列表与总记录数。
        """
        conditions = [
            TestPlan.project_id == project_id,
            TestPlan.deleted_at.is_(None),
        ]

        count_stmt = select(func.count()).select_from(TestPlan).where(*conditions)
        count_stmt = apply_tenant_filter(count_stmt, TestPlan, self._tenant_id)
        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = select(TestPlan).where(*conditions)
        stmt = apply_tenant_filter(stmt, TestPlan, self._tenant_id)
        stmt = (
            stmt.order_by(TestPlan.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        plans = list(result.scalars().all())
        return plans, total

    # ------------------------------------------------------------------
    # AI 编排
    # ------------------------------------------------------------------

    async def orchestrate(
        self,
        plan_id: uuid.UUID,
        node_count: int = 3,
        consider_dependencies: bool = True,
    ) -> TestPlan:
        """AI 编排测试计划 — 调用 LLM 生成执行顺序与节点分配方案。

        获取计划及其关联用例后，构建 prompt 让 LLM 综合分析用例优先级、
        测试类型依赖（API 测试先于 UI 测试）、执行节点容量，生成：
            - execution_order: 推荐执行顺序的 case_id 列表
            - node_assignments: 节点 ID → 用例 ID 列表映射
            - dependencies: {case_id, depends_on} 依赖对列表
            - rationale: 编排逻辑说明

        编排结果存入 plan.ai_orchestration，计划状态更新为 active。

        Args:
            plan_id: 计划 ID。
            node_count: 执行节点数量，默认 3。
            consider_dependencies: 是否考虑用例依赖关系，默认 True。

        Returns:
            更新后的 ``TestPlan`` 对象（含 ai_orchestration，status 为 active）。

        Raises:
            ValueError: 计划不存在或计划中无用例。
        """
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise ValueError(f"测试计划不存在: {plan_id}")

        # 获取计划关联的用例
        cases = await self._get_plan_cases(plan)
        if not cases:
            raise ValueError(f"测试计划 {plan_id} 中无可用用例")

        # 构建 prompt 并调用 LLM
        prompt = self._build_orchestration_prompt(
            cases=cases,
            plan=plan,
            node_count=node_count,
            consider_dependencies=consider_dependencies,
        )
        full_response = ""
        async for chunk in self.llm.chat(
            [Message(role="system", content=prompt)],
            stream=False,
        ):
            if isinstance(chunk, str):
                full_response += chunk

        # 解析 LLM 返回的 JSON
        orchestration = _extract_json(full_response)

        # 校验必要字段并补全默认值
        orchestration = self._normalize_orchestration(orchestration)

        # 保存编排方案并更新状态
        stmt = update(TestPlan).where(TestPlan.id == plan_id)
        stmt = apply_tenant_filter(stmt, TestPlan, self._tenant_id)
        await self.db.execute(
            stmt.values(
                ai_orchestration=orchestration,
                status="active",
            )
        )
        await self.db.flush()
        await self.db.refresh(plan)

        log.info(
            "test_plan.orchestrated",
            plan_id=str(plan_id),
            node_count=node_count,
            execution_order_len=len(orchestration.get("execution_order", [])),
        )
        return plan

    # ------------------------------------------------------------------
    # 执行记录
    # ------------------------------------------------------------------

    async def record_execution(
        self,
        case_id: uuid.UUID,
        plan_id: uuid.UUID | None = None,
        executor: str = "human",
        status: str = "pending",
        result: str | None = None,
        execution_log: dict | None = None,
        failure_reason: str | None = None,
        duration_seconds: int = 0,
        executor_id: uuid.UUID | None = None,
    ) -> TestExecution:
        """记录用例执行结果 — 根据状态自动设置时间戳。

        - running / passed / failed 状态：设置 started_at
        - passed / failed / blocked / skipped 状态：设置 completed_at

        Args:
            case_id: 用例 ID。
            plan_id: 测试计划 ID（可选）。
            executor: 执行者类型 — human / ai，默认 human。
            status: 执行状态 — pending / running / passed / failed / blocked / skipped。
            result: 执行结果描述（可选）。
            execution_log: 执行日志（JSONB，可选）。
            failure_reason: 失败原因（可选）。
            duration_seconds: 执行耗时（秒），默认 0。
            executor_id: 执行人 ID（人工执行时，可选）。

        Returns:
            创建后的 ``TestExecution`` 对象。
        """
        now = datetime.now(timezone.utc)
        started_at = now if status in _RUNNING_STATUSES else None
        completed_at = now if status in _COMPLETED_STATUSES else None

        execution = TestExecution(
            case_id=case_id,
            plan_id=plan_id,
            executor=executor,
            status=status,
            result=result,
            execution_log=execution_log,
            failure_reason=failure_reason,
            duration_seconds=duration_seconds,
            executor_id=executor_id,
            started_at=started_at,
            completed_at=completed_at,
        )
        self.db.add(execution)
        await self.db.flush()

        log.info(
            "test_execution.recorded",
            execution_id=str(execution.id),
            case_id=str(case_id),
            plan_id=str(plan_id) if plan_id else None,
            status=status,
            executor=executor,
        )
        return execution

    async def list_executions(
        self,
        plan_id: uuid.UUID | None = None,
        case_id: uuid.UUID | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[TestExecution], int]:
        """分页查询执行记录 — 支持按计划或用例过滤。

        Args:
            plan_id: 计划 ID（可选过滤）。
            case_id: 用例 ID（可选过滤）。
            page: 页码，从 1 开始。
            size: 每页数量。

        Returns:
            ``(executions, total)`` — 当前页执行记录列表与总记录数。
        """
        conditions: list = []
        if plan_id:
            conditions.append(TestExecution.plan_id == plan_id)
        if case_id:
            conditions.append(TestExecution.case_id == case_id)

        count_stmt = select(func.count()).select_from(TestExecution)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        count_stmt = apply_tenant_filter(count_stmt, TestExecution, self._tenant_id)
        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = select(TestExecution)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = apply_tenant_filter(stmt, TestExecution, self._tenant_id)
        stmt = stmt.order_by(
            TestExecution.created_at.desc()
        ).offset(offset).limit(size)

        result = await self.db.execute(stmt)
        executions = list(result.scalars().all())
        return executions, total

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get_plan_cases(self, plan: TestPlan) -> list[TestCase]:
        """获取计划关联的用例列表 — 根据 plan.case_ids 查询。

        过滤软删除记录，仅返回有效用例。

        Args:
            plan: 测试计划 ORM 实例。

        Returns:
            用例列表（可能为空）。
        """
        case_ids = plan.case_ids or []
        if not case_ids:
            return []

        # 将字符串 ID 转为 UUID 进行查询
        uuid_ids: list[uuid.UUID] = []
        for cid in case_ids:
            try:
                uuid_ids.append(uuid.UUID(cid) if isinstance(cid, str) else cid)
            except (ValueError, AttributeError):
                log.warning(
                    "test_plan.invalid_case_id",
                    plan_id=str(plan.id),
                    case_id=str(cid),
                )

        if not uuid_ids:
            return []

        stmt = select(TestCase).where(
            TestCase.id.in_(uuid_ids),
            TestCase.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _build_orchestration_prompt(
        cases: list[TestCase],
        plan: TestPlan,
        node_count: int,
        consider_dependencies: bool,
    ) -> str:
        """构建 AI 编排 prompt — 将用例信息结构化描述给 LLM。

        Args:
            cases: 计划关联的用例列表。
            plan: 测试计划 ORM 实例。
            node_count: 执行节点数量。
            consider_dependencies: 是否考虑依赖关系。

        Returns:
            完整的 prompt 字符串。
        """
        # 构建用例信息列表
        case_infos: list[str] = []
        for case in cases:
            case_infos.append(
                f"- case_id: {case.id}, "
                f"标题: {case.title}, "
                f"类型: {case.test_type}, "
                f"优先级: {case.priority}, "
                f"状态: {case.status}"
            )
        cases_text = "\n".join(case_infos)

        dependency_hint = (
            "请考虑用例间的依赖关系（例如 API 测试应先于 UI 测试执行，"
            "功能测试应先于性能测试执行）。"
            if consider_dependencies
            else "无需考虑用例间的依赖关系。"
        )

        return (
            "你是一个专业的测试编排专家。请根据以下测试用例信息，"
            "生成最优的执行编排方案。\n\n"
            f"测试计划: {plan.name}\n"
            f"执行策略: {plan.execution_strategy}\n"
            f"执行节点数量: {node_count}\n"
            f"{dependency_hint}\n\n"
            "用例列表:\n"
            f"{cases_text}\n\n"
            "请综合考虑以下因素：\n"
            "1. 用例优先级（critical > high > normal > low），高优先级用例应优先执行\n"
            "2. 测试类型依赖（如 API 测试先于 UI 测试，功能测试先于性能测试）\n"
            "3. 执行节点容量，将用例合理分配到各节点\n"
            "4. 避免节点间负载不均衡\n\n"
            "请返回 JSON 格式的编排方案，包含以下字段：\n"
            "{\n"
            '  "execution_order": ["case_id_1", "case_id_2", ...],'
            "  // 推荐执行顺序的 case_id 列表\n"
            '  "node_assignments": {"node_1": ["case_id_1", ...],'
            ' "node_2": ["case_id_2", ...]},'
            "  // 节点到用例的分配映射\n"
            '  "dependencies": [{"case_id": "xxx", "depends_on": "yyy"}],'
            "  // 依赖关系对列表\n"
            '  "rationale": "编排逻辑说明"\n'
            "}\n\n"
            "只输出 JSON，不要额外解释。"
        )

    @staticmethod
    def _normalize_orchestration(orchestration: list | dict) -> dict:
        """校验并补全编排方案的必要字段。

        确保返回值为 dict 且包含 execution_order / node_assignments /
        dependencies / rationale 四个字段，缺失字段使用默认值补全。

        Args:
            orchestration: LLM 返回的解析结果（list 或 dict）。

        Returns:
            规范化后的编排方案 dict。
        """
        if not isinstance(orchestration, dict):
            log.warning(
                "test_plan.orchestration_not_dict",
                type=type(orchestration).__name__,
            )
            return {
                "execution_order": [],
                "node_assignments": {},
                "dependencies": [],
                "rationale": "LLM 返回的编排方案格式异常，已使用默认值",
            }

        return {
            "execution_order": orchestration.get("execution_order", []),
            "node_assignments": orchestration.get("node_assignments", {}),
            "dependencies": orchestration.get("dependencies", []),
            "rationale": orchestration.get("rationale", ""),
        }
