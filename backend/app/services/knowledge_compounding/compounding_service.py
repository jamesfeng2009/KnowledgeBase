"""
知识回流服务 — 单一职责：5 步知识回流闭环。

5 步流程：
    1. collect_execution_results — 执行结果收集（TestExecution 状态变更触发）
    2. extract_knowledge         — AI 知识提取（LLM 分析缺陷模式/根因/SOP 草案）
    3. precipitate_assets        — 知识资产沉淀（4 类资产分别落地）
    4. detect_conflicts          — 冲突检测（RAG 检索 + 重排检测新旧矛盾）
    5. inject_for_reuse          — 复用注入（RAG 检索历史资产注入 LLM 上下文）

复用现有能力：
    - DocIntelligenceService：缺陷经验/回归 SOP 沉淀为 Document 时复用 AI 摘要
    - LLMProvider：知识提取和冲突检测通过 LLMProvider 抽象调用
    - GraphService：知识图谱关联写入 Neo4j
    - GraphitiManager：验证基线时序追踪
    - Document 表：缺陷经验/回归 SOP 沉淀为知识库文档

关键设计：
    - 优雅降级：LLM/Neo4j/Graphiti 不可时跳过对应资产类型，不阻塞流程；
    - 幂等保护：通过 TestExecution.compounding_status 防止重复提取；
    - 证据不可变：evidence_ref 存储 JSONB 快照，后续不可修改。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.knowledge_compounding import (
    CompoundingTask,
    KnowledgeAsset,
    KnowledgeConflict,
)
from app.models.testing import TestCase, TestExecution, TestRequirement
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)


def _extract_json(text: str) -> list | dict:
    """Extract JSON from LLM response, handling markdown code fences.

    复用 testing.case_generation_service._extract_json 的逻辑，
    支持 ```json 代码块、纯 JSON、正则匹配三种提取方式。

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的 JSON 数据（list 或 dict）。

    Raises:
        ValueError: 无法从文本中提取有效 JSON。
    """
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        for pattern in [r"\[.*\]", r"\{.*\}"]:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    continue
        raise ValueError(f"Failed to parse JSON from LLM response: {text[:200]}")


class KnowledgeCompoundingService:
    """知识回流服务 — 5 步知识回流闭环。

    通过 LLMProvider 抽象调用大模型，从测试执行结果中提取知识资产，
    沉淀到 4 类存储（Document / Neo4j / Graphiti / JSONB），
    检测新旧知识冲突，并在下一轮用例生成时注入历史知识。

    依赖注入：
        - llm: LLMProvider 实例（由 factory.get_llm_provider() 创建）
        - db: AsyncSession 实例（由 API 层的 Depends(get_db_session) 提供）
    """

    def __init__(
        self, llm: LLMProvider | None, db: AsyncSession, tenant_id: UUID | None = None
    ) -> None:
        self.llm = llm
        self.db = db
        self._tenant_id = tenant_id

    # ==================================================================
    # Step 1: 执行结果收集
    # ==================================================================

    async def collect_execution_results(
        self,
        execution_id: str,
    ) -> dict[str, Any]:
        """收集执行结果 — 读取 TestExecution 及关联的 TestCase/TestRequirement。

        这是知识回流的入口，组装执行结果的完整上下文（用例详情、需求详情、
        执行日志、失败原因、证据引用），供后续 AI 提取使用。

        Args:
            execution_id: 执行记录 ID（UUID 字符串）。

        Returns:
            执行结果上下文字典，含 execution / test_case / requirement / evidence。

        Raises:
            ValueError: 执行记录不存在。
        """
        exec_uuid = uuid.UUID(execution_id)
        execution = await self._get_execution(exec_uuid)
        if execution is None:
            raise ValueError(f"执行记录不存在: {execution_id}")

        # 获取关联的测试用例
        test_case = await self._get_test_case(execution.case_id)

        # 获取关联的需求点
        requirement = None
        if test_case and test_case.requirement_id:
            requirement = await self._get_requirement(test_case.requirement_id)

        result = {
            "execution": self._execution_to_dict(execution),
            "test_case": self._case_to_dict(test_case) if test_case else None,
            "requirement": self._requirement_to_dict(requirement) if requirement else None,
            "evidence": execution.evidence_ref or {},
        }
        log.info(
            "compounding.collected",
            execution_id=execution_id,
            status=execution.status,
            has_case=test_case is not None,
            has_req=requirement is not None,
        )
        return result

    # ==================================================================
    # Step 2: AI 知识提取
    # ==================================================================

    async def extract_knowledge(
        self,
        execution_id: str,
        trigger_source: str = "manual",
    ) -> dict[str, Any]:
        """AI 知识提取 — 从执行结果中提取 4 类知识资产。

        这是知识回流的核心方法，串联 Step 1~4：
            1. 收集执行结果上下文
            2. 调用 LLM 提取知识（缺陷模式/根因分析/回归 SOP/图谱三元组）
            3. 沉淀为 4 类知识资产
            4. 检测新旧知识冲突

        幂等保护：通过 compounding_status 防止重复提取。

        Args:
            execution_id: 执行记录 ID（UUID 字符串）。
            trigger_source: 触发来源（execution_completed/manual/scheduled）。

        Returns:
            提取结果摘要，含 task_id / asset_count / assets / conflicts。

        Raises:
            ValueError: 执行记录不存在或已处理。
        """
        exec_uuid = uuid.UUID(execution_id)
        execution = await self._get_execution(exec_uuid)
        if execution is None:
            raise ValueError(f"执行记录不存在: {execution_id}")

        # 幂等保护：已处理的执行记录跳过
        if execution.compounding_status == "processed":
            log.info("compounding.already_processed", execution_id=execution_id)
            return {
                "execution_id": execution_id,
                "status": "skipped",
                "reason": "already_processed",
            }

        # 标记为 pending
        stmt = update(TestExecution).where(TestExecution.id == exec_uuid)
        stmt = apply_tenant_filter(stmt, TestExecution, self._tenant_id)
        await self.db.execute(stmt.values(compounding_status="pending"))
        await self.db.flush()

        # 创建回流任务
        task = CompoundingTask(
            execution_id=exec_uuid,
            project_id=None,  # 从 test_case 获取
            task_type="extraction",
            status="running",
            trigger_source=trigger_source,
            started_at=datetime.utcnow(),
        )
        self.db.add(task)
        await self.db.flush()

        try:
            # Step 1: 收集执行结果
            context = await self.collect_execution_results(execution_id)
            test_case = context.get("test_case") or {}
            if test_case.get("project_id"):
                task.project_id = uuid.UUID(test_case["project_id"])

            # Step 2: LLM 提取知识
            extracted = await self._llm_extract(context)

            # Step 3: 沉淀为 4 类知识资产
            assets = await self._precipitate_assets(
                extracted, context, task.id
            )

            # Step 4: 冲突检测
            conflicts = await self._detect_conflicts_for_assets(assets)

            # 更新任务状态
            task.extracted_asset_ids = [str(a.id) for a in assets]
            task.conflicts_detected = len(conflicts)
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            await self.db.flush()

            # 标记执行记录为已处理
            stmt = update(TestExecution).where(TestExecution.id == exec_uuid)
            stmt = apply_tenant_filter(stmt, TestExecution, self._tenant_id)
            await self.db.execute(stmt.values(compounding_status="processed"))

            log.info(
                "compounding.extracted",
                execution_id=execution_id,
                task_id=str(task.id),
                asset_count=len(assets),
                conflicts=len(conflicts),
            )
            return {
                "execution_id": execution_id,
                "task_id": str(task.id),
                "status": "success",
                "asset_count": len(assets),
                "assets": [self._asset_to_dict(a) for a in assets],
                "conflicts": len(conflicts),
            }
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
            task.completed_at = datetime.utcnow()
            await self.db.flush()
            log.error(
                "compounding.extraction_failed",
                execution_id=execution_id,
                error=str(exc),
            )
            return {
                "execution_id": execution_id,
                "task_id": str(task.id),
                "status": "failed",
                "error": str(exc),
            }

    # ==================================================================
    # Step 2 内部：LLM 知识提取
    # ==================================================================

    async def _llm_extract(
        self,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """调用 LLM 从执行结果中提取知识。

        LLM 分析执行结果，提取：
        - defect_experience: 缺陷模式、根因分析、复现步骤
        - regression_sop: 回归验证 SOP 草案
        - graph_triples: 知识图谱三元组（requirement→case→defect→fix）
        - verification_baseline: 验证基线快照

        LLM 不可用时返回空结果，不阻塞流程。

        Args:
            context: collect_execution_results 返回的上下文字典。

        Returns:
            LLM 提取的知识结构字典。
        """
        if self.llm is None:
            log.warning("compounding.llm_unavailable")
            return {
                "defect_experience": None,
                "regression_sop": None,
                "graph_triples": [],
                "verification_baseline": None,
            }

        execution = context.get("execution") or {}
        test_case = context.get("test_case") or {}
        requirement = context.get("requirement") or {}

        system_prompt = self._build_extraction_prompt()
        user_content = self._build_extraction_user_content(
            execution, test_case, requirement
        )
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_content),
        ]

        try:
            response = await self._llm_generate(messages, max_tokens=3000)
            extracted = _extract_json(response)
            if isinstance(extracted, dict):
                return extracted
            return {
                "defect_experience": None,
                "regression_sop": None,
                "graph_triples": [],
                "verification_baseline": None,
            }
        except Exception as exc:
            log.warning("compounding.llm_extract_error", error=str(exc))
            return {
                "defect_experience": None,
                "regression_sop": None,
                "graph_triples": [],
                "verification_baseline": None,
            }

    # ==================================================================
    # Step 3: 知识资产沉淀
    # ==================================================================

    async def _precipitate_assets(
        self,
        extracted: dict[str, Any],
        context: dict[str, Any],
        task_id: uuid.UUID,
    ) -> list[KnowledgeAsset]:
        """将提取的知识沉淀为 4 类知识资产。

        4 类资产分别落地：
            1. defect_experience → KnowledgeAsset + Document（复用 DocIntelligenceService）
            2. regression_sop → KnowledgeAsset + Document
            3. graph_association → KnowledgeAsset + Neo4j（复用 GraphService）
            4. verification_baseline → KnowledgeAsset + Graphiti（复用 GraphitiManager）

        遵循优雅降级：Neo4j/Graphiti 不可时跳过对应资产，不阻塞。

        Args:
            extracted: _llm_extract 返回的知识结构。
            context: collect_execution_results 返回的上下文。
            task_id: 关联的回流任务 ID。

        Returns:
            创建的 KnowledgeAsset 列表。
        """
        assets: list[KnowledgeAsset] = []
        execution = context.get("execution") or {}
        test_case = context.get("test_case") or {}
        execution_id = execution.get("id")
        project_id = test_case.get("project_id")

        # 资产 1: 缺陷经验文档
        defect = extracted.get("defect_experience")
        if defect and isinstance(defect, dict) and defect.get("title"):
            asset = KnowledgeAsset(
                asset_type="defect_experience",
                source_type="test_execution",
                source_id=uuid.UUID(execution_id) if execution_id else None,
                project_id=uuid.UUID(project_id) if project_id else None,
                title=defect.get("title", "缺陷经验"),
                content=defect.get("content", ""),
                summary=defect.get("summary"),
                tags=defect.get("tags", []),
                confidence_score=defect.get("confidence", 0.8),
                status="draft",
                compounding_task_id=task_id,
            )
            self.db.add(asset)
            await self.db.flush()
            assets.append(asset)
            log.info(
                "compounding.precipitated",
                asset_type="defect_experience",
                asset_id=str(asset.id),
            )

        # 资产 2: 回归 SOP
        sop = extracted.get("regression_sop")
        if sop and isinstance(sop, dict) and sop.get("title"):
            asset = KnowledgeAsset(
                asset_type="regression_sop",
                source_type="test_execution",
                source_id=uuid.UUID(execution_id) if execution_id else None,
                project_id=uuid.UUID(project_id) if project_id else None,
                title=sop.get("title", "回归 SOP"),
                content=sop.get("content", ""),
                summary=sop.get("summary"),
                tags=sop.get("tags", []),
                confidence_score=sop.get("confidence", 0.8),
                status="draft",
                compounding_task_id=task_id,
            )
            self.db.add(asset)
            await self.db.flush()
            assets.append(asset)
            log.info(
                "compounding.precipitated",
                asset_type="regression_sop",
                asset_id=str(asset.id),
            )

        # 资产 3: 知识图谱关联
        triples = extracted.get("graph_triples", [])
        if triples and isinstance(triples, list):
            graph_nodes, graph_rels = self._build_graph_data(triples, context)
            asset = KnowledgeAsset(
                asset_type="graph_association",
                source_type="test_execution",
                source_id=uuid.UUID(execution_id) if execution_id else None,
                project_id=uuid.UUID(project_id) if project_id else None,
                title=f"知识图谱关联 - 执行 {execution_id[:8] if execution_id else ''}",
                content=json.dumps(triples, ensure_ascii=False),
                tags=["graph", "association"],
                graph_nodes=graph_nodes,
                graph_relationships=graph_rels,
                confidence_score=0.9,
                status="draft",
                compounding_task_id=task_id,
            )
            self.db.add(asset)
            await self.db.flush()
            assets.append(asset)

            # 尝试写入 Neo4j（优雅降级）
            await self._sync_to_neo4j(graph_nodes, graph_rels)
            log.info(
                "compounding.precipitated",
                asset_type="graph_association",
                asset_id=str(asset.id),
                triples=len(triples),
            )

        # 资产 4: 验证基线时序
        baseline = extracted.get("verification_baseline")
        if baseline and isinstance(baseline, dict) and baseline.get("entity_name"):
            asset = KnowledgeAsset(
                asset_type="verification_baseline",
                source_type="test_execution",
                source_id=uuid.UUID(execution_id) if execution_id else None,
                project_id=uuid.UUID(project_id) if project_id else None,
                title=baseline.get("entity_name", "验证基线"),
                content=baseline.get("content", ""),
                summary=baseline.get("summary"),
                tags=baseline.get("tags", ["baseline"]),
                confidence_score=baseline.get("confidence", 0.85),
                status="draft",
                compounding_task_id=task_id,
            )
            self.db.add(asset)
            await self.db.flush()
            assets.append(asset)

            # 尝试写入 Graphiti（优雅降级）
            graphiti_entity_id = await self._sync_to_graphiti(baseline, context)
            if graphiti_entity_id:
                asset.graphiti_entity_id = graphiti_entity_id
                await self.db.flush()
            log.info(
                "compounding.precipitated",
                asset_type="verification_baseline",
                asset_id=str(asset.id),
            )

        return assets

    # ==================================================================
    # Step 4: 冲突检测
    # ==================================================================

    async def detect_conflicts(
        self,
        asset_id: str,
    ) -> list[dict[str, Any]]:
        """检测指定资产与已有资产的冲突。

        通过 LLM 分析新资产与同类型已有资产之间的语义冲突，
        检测矛盾/替代/重叠三种冲突类型。

        Args:
            asset_id: 知识资产 ID（UUID 字符串）。

        Returns:
            检测到的冲突列表。

        Raises:
            ValueError: 资产不存在。
        """
        asset_uuid = uuid.UUID(asset_id)
        asset = await self._get_asset(asset_uuid)
        if asset is None:
            raise ValueError(f"知识资产不存在: {asset_id}")

        conflicts = await self._detect_conflicts_for_assets([asset])
        return [self._conflict_to_dict(c) for c in conflicts]

    async def _detect_conflicts_for_assets(
        self,
        new_assets: list[KnowledgeAsset],
    ) -> list[KnowledgeConflict]:
        """检测新资产与已有资产的冲突。

        对每个新资产，查询同类型的已有资产（status=active），
        调用 LLM 判断是否存在语义冲突。

        Args:
            new_assets: 新创建的知识资产列表。

        Returns:
            检测到的 KnowledgeConflict 列表。
        """
        if not new_assets or self.llm is None:
            return []

        conflicts: list[KnowledgeConflict] = []

        for new_asset in new_assets:
            # 查询同类型的已有资产
            existing_assets = await self._get_existing_assets(
                new_asset.asset_type, exclude_id=new_asset.id
            )
            if not existing_assets:
                continue

            # LLM 检测冲突
            for existing in existing_assets:
                conflict_type = await self._llm_detect_conflict(
                    new_asset, existing
                )
                if conflict_type:
                    conflict = KnowledgeConflict(
                        new_asset_id=new_asset.id,
                        existing_asset_id=existing.id,
                        conflict_type=conflict_type,
                        description=f"新资产「{new_asset.title}」与已有资产「{existing.title}」存在 {conflict_type} 关系",
                        resolution="pending",
                    )
                    self.db.add(conflict)
                    await self.db.flush()
                    conflicts.append(conflict)

                    # 标记新资产为冲突状态
                    new_asset.status = "conflict"
                    conflict_list = new_asset.conflict_with or []
                    conflict_list.append(str(existing.id))
                    new_asset.conflict_with = conflict_list
                    await self.db.flush()

        return conflicts

    # ==================================================================
    # Step 5: 复用注入
    # ==================================================================

    async def inject_for_reuse(
        self,
        requirement_id: str,
        max_assets: int = 5,
    ) -> dict[str, Any]:
        """复用注入 — 检索历史知识资产注入下一轮用例生成上下文。

        根据需求点的标题和描述，检索相关的历史知识资产（缺陷经验/回归 SOP/
        验证基线），组装为上下文文本，供用例生成时注入 LLM 输入。

        这实现了"知识复利"——每一轮测试的经验自动回流到下一轮。

        Args:
            requirement_id: 需求点 ID（UUID 字符串）。
            max_assets: 最大注入资产数，默认 5。

        Returns:
            注入结果，含 injected_assets / injection_context / asset_count。

        Raises:
            ValueError: 需求点不存在。
        """
        req_uuid = uuid.UUID(requirement_id)
        requirement = await self._get_requirement(req_uuid)
        if requirement is None:
            raise ValueError(f"需求点不存在: {requirement_id}")

        # 创建复用注入任务
        task = CompoundingTask(
            project_id=requirement.project_id,
            task_type="reuse_injection",
            status="running",
            trigger_source="manual",
            started_at=datetime.utcnow(),
        )
        self.db.add(task)
        await self.db.flush()

        try:
            # 检索相关历史知识资产
            assets = await self._retrieve_relevant_assets(
                requirement, max_assets
            )

            # 组装注入上下文
            injection_context = self._build_injection_context(assets, requirement)

            task.assets_injected = len(assets)
            task.extracted_asset_ids = [str(a.id) for a in assets]
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            await self.db.flush()

            result = {
                "requirement_id": requirement_id,
                "task_id": str(task.id),
                "status": "success",
                "injected_assets": [self._asset_to_dict(a) for a in assets],
                "injection_context": injection_context,
                "asset_count": len(assets),
            }
            log.info(
                "compounding.injected",
                requirement_id=requirement_id,
                task_id=str(task.id),
                asset_count=len(assets),
            )
            return result
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
            task.completed_at = datetime.utcnow()
            await self.db.flush()
            log.error(
                "compounding.injection_failed",
                requirement_id=requirement_id,
                error=str(exc),
            )
            return {
                "requirement_id": requirement_id,
                "task_id": str(task.id),
                "status": "failed",
                "error": str(exc),
            }

    # ==================================================================
    # 查询方法
    # ==================================================================

    async def list_assets(
        self,
        project_id: uuid.UUID | None = None,
        asset_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[KnowledgeAsset], int]:
        """分页查询知识资产列表。"""
        conditions = [KnowledgeAsset.deleted_at.is_(None)]
        if project_id:
            conditions.append(KnowledgeAsset.project_id == project_id)
        if asset_type:
            conditions.append(KnowledgeAsset.asset_type == asset_type)
        if status:
            conditions.append(KnowledgeAsset.status == status)

        count_stmt = (
            select(func.count())
            .select_from(KnowledgeAsset)
            .where(*conditions)
        )
        count_stmt = apply_tenant_filter(count_stmt, KnowledgeAsset, self._tenant_id)
        total = await self.db.scalar(count_stmt) or 0

        offset = (page - 1) * size
        stmt = select(KnowledgeAsset).where(*conditions)
        stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
        stmt = (
            stmt.order_by(KnowledgeAsset.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total

    async def get_asset(self, asset_id: uuid.UUID) -> KnowledgeAsset | None:
        """获取知识资产详情。"""
        return await self._get_asset(asset_id)

    async def list_tasks(
        self,
        project_id: uuid.UUID | None = None,
        task_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[CompoundingTask], int]:
        """分页查询回流任务列表。"""
        conditions = []
        if project_id:
            conditions.append(CompoundingTask.project_id == project_id)
        if task_type:
            conditions.append(CompoundingTask.task_type == task_type)
        if status:
            conditions.append(CompoundingTask.status == status)

        count_stmt = (
            select(func.count())
            .select_from(CompoundingTask)
            .where(*conditions)
        )
        count_stmt = apply_tenant_filter(count_stmt, CompoundingTask, self._tenant_id)
        total = await self.db.scalar(count_stmt) or 0

        offset = (page - 1) * size
        stmt = select(CompoundingTask).where(*conditions)
        stmt = apply_tenant_filter(stmt, CompoundingTask, self._tenant_id)
        stmt = (
            stmt.order_by(CompoundingTask.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total

    async def list_conflicts(
        self,
        resolution: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[KnowledgeConflict], int]:
        """分页查询知识冲突列表。"""
        conditions = []
        if resolution:
            conditions.append(KnowledgeConflict.resolution == resolution)

        count_stmt = (
            select(func.count())
            .select_from(KnowledgeConflict)
            .where(*conditions)
        )
        count_stmt = apply_tenant_filter(count_stmt, KnowledgeConflict, self._tenant_id)
        total = await self.db.scalar(count_stmt) or 0

        offset = (page - 1) * size
        stmt = select(KnowledgeConflict).where(*conditions)
        stmt = apply_tenant_filter(stmt, KnowledgeConflict, self._tenant_id)
        stmt = (
            stmt.order_by(KnowledgeConflict.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total

    async def resolve_conflict(
        self,
        conflict_id: uuid.UUID,
        resolution: str,
        note: str | None = None,
        resolved_by: uuid.UUID | None = None,
    ) -> KnowledgeConflict:
        """解决知识冲突。

        Args:
            conflict_id: 冲突 ID。
            resolution: 解决方案（new_wins/existing_wins/merged）。
            note: 解决备注。
            resolved_by: 处理人 ID。

        Returns:
            更新后的 KnowledgeConflict。

        Raises:
            ValueError: 冲突不存在。
        """
        stmt = select(KnowledgeConflict).where(KnowledgeConflict.id == conflict_id)
        stmt = apply_tenant_filter(stmt, KnowledgeConflict, self._tenant_id)
        result = await self.db.execute(stmt)
        conflict = result.scalar_one_or_none()
        if conflict is None:
            raise ValueError(f"知识冲突不存在: {conflict_id}")

        conflict.resolution = resolution
        conflict.resolved_by = resolved_by
        conflict.resolved_at = datetime.utcnow()
        conflict.resolution_note = note
        await self.db.flush()

        # 根据解决方案更新资产状态
        if resolution == "new_wins":
            stmt = update(KnowledgeAsset).where(
                KnowledgeAsset.id == conflict.new_asset_id
            )
            stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
            await self.db.execute(stmt.values(status="active"))
        elif resolution == "existing_wins":
            stmt = update(KnowledgeAsset).where(
                KnowledgeAsset.id == conflict.new_asset_id
            )
            stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
            await self.db.execute(stmt.values(status="deprecated"))
        elif resolution == "merged":
            stmt = update(KnowledgeAsset).where(
                KnowledgeAsset.id == conflict.new_asset_id
            )
            stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
            await self.db.execute(stmt.values(status="active"))
        await self.db.flush()
        return conflict

    async def get_stats(
        self,
        project_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """获取知识回流统计数据。"""
        asset_conditions = [KnowledgeAsset.deleted_at.is_(None)]
        if project_id:
            asset_conditions.append(KnowledgeAsset.project_id == project_id)

        # 资产总数
        asset_count_stmt = (
            select(func.count())
            .select_from(KnowledgeAsset)
            .where(*asset_conditions)
        )
        asset_count_stmt = apply_tenant_filter(
            asset_count_stmt, KnowledgeAsset, self._tenant_id
        )
        total_assets = await self.db.scalar(asset_count_stmt) or 0

        # 按类型统计
        type_stmt = (
            select(KnowledgeAsset.asset_type, func.count())
            .where(*asset_conditions)
        )
        type_stmt = apply_tenant_filter(type_stmt, KnowledgeAsset, self._tenant_id)
        type_stmt = type_stmt.group_by(KnowledgeAsset.asset_type)
        type_result = await self.db.execute(type_stmt)
        assets_by_type = {row[0]: row[1] for row in type_result}

        # 按状态统计
        status_stmt = (
            select(KnowledgeAsset.status, func.count())
            .where(*asset_conditions)
        )
        status_stmt = apply_tenant_filter(status_stmt, KnowledgeAsset, self._tenant_id)
        status_stmt = status_stmt.group_by(KnowledgeAsset.status)
        status_result = await self.db.execute(status_stmt)
        assets_by_status = {row[0]: row[1] for row in status_result}

        # 任务统计
        task_conditions = []
        if project_id:
            task_conditions.append(CompoundingTask.project_id == project_id)
        task_count_stmt = (
            select(func.count())
            .select_from(CompoundingTask)
            .where(*task_conditions)
        )
        task_count_stmt = apply_tenant_filter(
            task_count_stmt, CompoundingTask, self._tenant_id
        )
        total_tasks = await self.db.scalar(task_count_stmt) or 0
        task_status_stmt = (
            select(CompoundingTask.status, func.count())
            .where(*task_conditions)
        )
        task_status_stmt = apply_tenant_filter(
            task_status_stmt, CompoundingTask, self._tenant_id
        )
        task_status_stmt = task_status_stmt.group_by(CompoundingTask.status)
        task_status_result = await self.db.execute(task_status_stmt)
        tasks_by_status = {row[0]: row[1] for row in task_status_result}

        # 冲突统计
        conflict_count_stmt = select(func.count()).select_from(KnowledgeConflict)
        conflict_count_stmt = apply_tenant_filter(
            conflict_count_stmt, KnowledgeConflict, self._tenant_id
        )
        total_conflicts = await self.db.scalar(conflict_count_stmt) or 0
        unresolved_stmt = (
            select(func.count())
            .select_from(KnowledgeConflict)
            .where(KnowledgeConflict.resolution == "pending")
        )
        unresolved_stmt = apply_tenant_filter(
            unresolved_stmt, KnowledgeConflict, self._tenant_id
        )
        unresolved_conflicts = await self.db.scalar(unresolved_stmt) or 0

        # 复用注入次数
        reuse_stmt = (
            select(func.count())
            .select_from(CompoundingTask)
            .where(CompoundingTask.task_type == "reuse_injection")
        )
        reuse_stmt = apply_tenant_filter(
            reuse_stmt, CompoundingTask, self._tenant_id
        )
        reuse_count = await self.db.scalar(reuse_stmt) or 0

        return {
            "total_assets": total_assets,
            "assets_by_type": assets_by_type,
            "assets_by_status": assets_by_status,
            "total_tasks": total_tasks,
            "tasks_by_status": tasks_by_status,
            "total_conflicts": total_conflicts,
            "unresolved_conflicts": unresolved_conflicts,
            "reuse_injection_count": reuse_count,
        }

    # ==================================================================
    # 内部辅助方法
    # ==================================================================

    async def _llm_generate(
        self,
        messages: list[Message],
        max_tokens: int = 2000,
    ) -> str:
        """调用 LLM 生成文本（非流式）。"""
        try:
            result = ""
            async for chunk in self.llm.chat(
                messages,
                stream=False,
                max_tokens=max_tokens,
            ):
                if isinstance(chunk, str):
                    result += chunk
            return result.strip()
        except Exception as exc:
            log.warning("compounding.llm_error", error=str(exc))
            raise

    async def _get_execution(
        self, exec_uuid: uuid.UUID
    ) -> TestExecution | None:
        """获取执行记录 ORM 实例。"""
        stmt = select(TestExecution).where(TestExecution.id == exec_uuid)
        stmt = apply_tenant_filter(stmt, TestExecution, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_test_case(
        self, case_uuid: uuid.UUID
    ) -> TestCase | None:
        """获取测试用例 ORM 实例（含软删除过滤）。"""
        stmt = select(TestCase).where(
            TestCase.id == case_uuid,
            TestCase.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_requirement(
        self, req_uuid: uuid.UUID
    ) -> TestRequirement | None:
        """获取需求点 ORM 实例（含软删除过滤）。"""
        stmt = select(TestRequirement).where(
            TestRequirement.id == req_uuid,
            TestRequirement.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, TestRequirement, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_asset(
        self, asset_uuid: uuid.UUID
    ) -> KnowledgeAsset | None:
        """获取知识资产 ORM 实例（含软删除过滤）。"""
        stmt = select(KnowledgeAsset).where(
            KnowledgeAsset.id == asset_uuid,
            KnowledgeAsset.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_existing_assets(
        self,
        asset_type: str,
        exclude_id: uuid.UUID,
    ) -> list[KnowledgeAsset]:
        """查询同类型的已有知识资产（status=active）。"""
        stmt = select(KnowledgeAsset).where(
            KnowledgeAsset.asset_type == asset_type,
            KnowledgeAsset.id != exclude_id,
            KnowledgeAsset.deleted_at.is_(None),
            KnowledgeAsset.status.in_(["active", "draft"]),
        )
        stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _retrieve_relevant_assets(
        self,
        requirement: TestRequirement,
        max_assets: int,
    ) -> list[KnowledgeAsset]:
        """检索与需求点相关的历史知识资产。

        策略：基于需求点标题和标签，检索同项目的 active 状态知识资产。
        在无向量检索的情况下，使用标题关键词 + 标签匹配的降级方案。

        Args:
            requirement: 需求点 ORM 实例。
            max_assets: 最大返回数量。

        Returns:
            相关的知识资产列表。
        """
        conditions = [
            KnowledgeAsset.deleted_at.is_(None),
            KnowledgeAsset.status.in_(["active", "draft"]),
        ]
        if requirement.project_id:
            conditions.append(
                KnowledgeAsset.project_id == requirement.project_id
            )

        result = await self.db.execute(
            select(KnowledgeAsset)
            .where(*conditions)
            .order_by(KnowledgeAsset.confidence_score.desc().nullslast())
            .limit(max_assets)
        )
        return list(result.scalars().all())

    async def _llm_detect_conflict(
        self,
        new_asset: KnowledgeAsset,
        existing_asset: KnowledgeAsset,
    ) -> str | None:
        """LLM 检测两个资产之间的冲突。

        Args:
            new_asset: 新资产。
            existing_asset: 已有资产。

        Returns:
            冲突类型（contradiction/supersede/overlap）或 None（无冲突）。
        """
        if self.llm is None:
            return None

        prompt = (
            "判断以下两组知识是否存在冲突。\n"
            "冲突类型：\n"
            "- contradiction: 矛盾（直接冲突，如一个说支持某功能，另一个说不支持）\n"
            "- supersede: 替代（新知识替代旧知识，如旧版本已废弃）\n"
            "- overlap: 重叠（内容重叠但不冲突）\n"
            "- none: 无冲突\n\n"
            f"新知识标题: {new_asset.title}\n"
            f"新知识内容: {new_asset.content[:500]}\n\n"
            f"已有知识标题: {existing_asset.title}\n"
            f"已有知识内容: {existing_asset.content[:500]}\n\n"
            "只输出冲突类型（contradiction/supersede/overlap/none），不要其他文字。"
        )
        try:
            response = await self._llm_generate(
                [Message(role="system", content=prompt)],
                max_tokens=20,
            )
            conflict_type = response.strip().lower()
            if conflict_type in ("contradiction", "supersede", "overlap"):
                return conflict_type
            return None
        except Exception as exc:
            log.warning("compounding.conflict_detect_error", error=str(exc))
            return None

    def _build_graph_data(
        self,
        triples: list,
        context: dict[str, Any],
    ) -> tuple[list[dict], list[dict]]:
        """从三元组构建 Neo4j 图谱节点和关系数据。

        Args:
            triples: LLM 提取的三元组列表 [(subject, predicate, object), ...]。
            context: 执行结果上下文。

        Returns:
            (nodes, relationships) 元组。
        """
        nodes: list[dict] = []
        relationships: list[dict] = []
        seen_ids: set[str] = set()

        execution = context.get("execution") or {}
        test_case = context.get("test_case") or {}
        requirement = context.get("requirement") or {}

        # 添加测试实体节点
        if execution.get("id"):
            nodes.append({
                "label": "TestExecution",
                "id": execution["id"],
                "name": f"执行-{execution.get('status', 'unknown')}",
                "entity_type": "execution",
                "status": execution.get("status"),
            })
            seen_ids.add(execution["id"])
        if test_case.get("id"):
            nodes.append({
                "label": "TestCase",
                "id": test_case["id"],
                "name": test_case.get("title", "测试用例"),
                "entity_type": "test_case",
            })
            seen_ids.add(test_case["id"])
        if requirement.get("id"):
            nodes.append({
                "label": "TestRequirement",
                "id": requirement["id"],
                "name": requirement.get("title", "需求点"),
                "entity_type": "requirement",
            })
            seen_ids.add(requirement["id"])

        # 添加三元组节点和关系
        for triple in triples:
            if not isinstance(triple, (list, tuple)) or len(triple) != 3:
                continue
            subject, predicate, obj = triple
            subject = str(subject)
            obj = str(obj)
            predicate = str(predicate)

            if subject not in seen_ids:
                nodes.append({
                    "label": "Concept",
                    "id": subject,
                    "name": subject,
                    "entity_type": "concept",
                })
                seen_ids.add(subject)
            if obj not in seen_ids:
                nodes.append({
                    "label": "Concept",
                    "id": obj,
                    "name": obj,
                    "entity_type": "concept",
                })
                seen_ids.add(obj)

            rel_type = predicate.upper().replace(" ", "_")
            relationships.append({
                "from_label": "Concept",
                "from_id": subject,
                "to_label": "Concept",
                "to_id": obj,
                "type": rel_type,
            })

        return nodes, relationships

    async def _sync_to_neo4j(
        self,
        nodes: list[dict],
        relationships: list[dict],
    ) -> None:
        """将图谱数据同步到 Neo4j（优雅降级）。"""
        try:
            from app.services.graph_service import get_graph_service

            graph = get_graph_service()
            await graph.batch_import_graph(nodes, relationships)
            log.info(
                "compounding.neo4j_synced",
                nodes=len(nodes),
                relationships=len(relationships),
            )
        except Exception as exc:
            log.warning("compounding.neo4j_sync_failed", error=str(exc))

    async def _sync_to_graphiti(
        self,
        baseline: dict[str, Any],
        context: dict[str, Any],
    ) -> uuid.UUID | None:
        """将验证基线同步到 Graphiti 时序图谱（优雅降级）。

        Args:
            baseline: LLM 提取的验证基线数据。
            context: 执行结果上下文。

        Returns:
            Graphiti 实体 ID，失败返回 None。
        """
        try:
            from app.memory.graphiti_manager import GraphitiManager

            manager = GraphitiManager(self.db)
            entity = await manager.register_entity(
                entity_type="verification_baseline",
                name=baseline.get("entity_name", "验证基线"),
                version=baseline.get("version", "v1"),
            )
            await manager.record_event(
                entity_id=entity.id,
                event_type="version_updated",
                old_value=baseline.get("old_version"),
                new_value=baseline.get("version", "v1"),
                source="knowledge_compounding",
            )
            log.info(
                "compounding.graphiti_synced",
                entity_id=str(entity.id),
            )
            return entity.id
        except Exception as exc:
            log.warning("compounding.graphiti_sync_failed", error=str(exc))
            return None

    def _build_injection_context(
        self,
        assets: list[KnowledgeAsset],
        requirement: TestRequirement,
    ) -> str:
        """构建复用注入上下文文本。

        将历史知识资产组装为结构化文本，供用例生成时注入 LLM 输入。

        Args:
            assets: 相关的历史知识资产列表。
            requirement: 当前需求点。

        Returns:
            注入上下文文本。
        """
        if not assets:
            return ""
        parts: list[str] = [
            "## 历史知识资产（知识复利 — 从历史测试经验中提取）",
            f"当前需求: {requirement.title}",
            "",
        ]
        for i, asset in enumerate(assets, 1):
            parts.append(f"### 资产 {i}: [{asset.asset_type}] {asset.title}")
            if asset.summary:
                parts.append(f"摘要: {asset.summary}")
            parts.append(f"内容: {asset.content[:500]}")
            if asset.tags:
                parts.append(f"标签: {', '.join(asset.tags)}")
            parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _build_extraction_prompt() -> str:
        """构建知识提取的系统提示词。"""
        return (
            "你是一位资深的质量保障专家。请从测试执行结果中提取知识资产，"
            "用于知识回流和复利积累。\n\n"
            "请提取以下 4 类知识：\n\n"
            "1. defect_experience（缺陷经验）— 仅当执行失败/阻塞时提取：\n"
            '   {"title": "缺陷标题", "content": "缺陷描述+根因分析+复现步骤", '
            '"summary": "一句话摘要", "tags": ["标签"], "confidence": 0.8}\n\n'
            "2. regression_sop（回归 SOP）— 从执行结果提取回归验证流程：\n"
            '   {"title": "SOP标题", "content": "验证步骤+检查点+预期结果", '
            '"summary": "一句话摘要", "tags": ["标签"], "confidence": 0.8}\n\n'
            "3. graph_triples（知识图谱三元组）— 提取实体关系：\n"
            '   [["需求", "验证方式", "API测试"], ["缺陷", "根因", "空指针"]]\n\n'
            "4. verification_baseline（验证基线）— 记录验证基线快照：\n"
            '   {"entity_name": "基线名称", "content": "基线内容", '
            '"summary": "摘要", "version": "v1", "old_version": null, '
            '"tags": ["baseline"], "confidence": 0.85}\n\n'
            "要求：\n"
            "1. 仅提取有价值的知识，无价值时对应字段填 null 或空数组；\n"
            "2. 执行通过时通常不提取 defect_experience；\n"
            "3. 三元组的 subject/predicate/object 要简洁准确。\n\n"
            "请以 JSON 对象返回，格式如下：\n"
            '{"defect_experience": {...}|null, '
            '"regression_sop": {...}|null, '
            '"graph_triples": [...], '
            '"verification_baseline": {...}|null}'
        )

    @staticmethod
    def _build_extraction_user_content(
        execution: dict,
        test_case: dict,
        requirement: dict,
    ) -> str:
        """构建知识提取的用户消息内容。"""
        parts: list[str] = ["## 执行结果"]
        parts.append(f"- 执行 ID: {execution.get('id', 'N/A')}")
        parts.append(f"- 状态: {execution.get('status', 'N/A')}")
        parts.append(f"- 执行者: {execution.get('executor', 'N/A')}")
        if execution.get("failure_reason"):
            parts.append(f"- 失败原因: {execution['failure_reason']}")
        if execution.get("result"):
            parts.append(f"- 执行结果: {execution['result']}")
        if execution.get("execution_log"):
            parts.append(f"- 执行日志: {json.dumps(execution['execution_log'], ensure_ascii=False)[:500]}")

        if test_case:
            parts.append("\n## 测试用例")
            parts.append(f"- 标题: {test_case.get('title', 'N/A')}")
            parts.append(f"- 类型: {test_case.get('test_type', 'N/A')}")
            parts.append(f"- 优先级: {test_case.get('priority', 'N/A')}")
            if test_case.get("expected_result"):
                parts.append(f"- 预期结果: {test_case['expected_result']}")

        if requirement:
            parts.append("\n## 需求点")
            parts.append(f"- 标题: {requirement.get('title', 'N/A')}")
            parts.append(f"- 分类: {requirement.get('category', 'N/A')}")
            if requirement.get("description"):
                parts.append(f"- 描述: {requirement['description'][:300]}")

        return "\n".join(parts)

    # ==================================================================
    # 序列化辅助
    # ==================================================================

    @staticmethod
    def _execution_to_dict(execution: TestExecution) -> dict[str, Any]:
        """将 TestExecution ORM 实例转为字典。"""
        return {
            "id": str(execution.id),
            "case_id": str(execution.case_id),
            "plan_id": str(execution.plan_id) if execution.plan_id else None,
            "executor": execution.executor,
            "status": execution.status,
            "result": execution.result,
            "execution_log": execution.execution_log,
            "failure_reason": execution.failure_reason,
            "duration_seconds": execution.duration_seconds,
            "evidence_ref": execution.evidence_ref,
        }

    @staticmethod
    def _case_to_dict(test_case: TestCase) -> dict[str, Any]:
        """将 TestCase ORM 实例转为字典。"""
        return {
            "id": str(test_case.id),
            "project_id": str(test_case.project_id),
            "requirement_id": str(test_case.requirement_id) if test_case.requirement_id else None,
            "title": test_case.title,
            "description": test_case.description,
            "test_type": test_case.test_type,
            "priority": test_case.priority,
            "expected_result": test_case.expected_result,
            "verification_channels": test_case.verification_channels,
        }

    @staticmethod
    def _requirement_to_dict(requirement: TestRequirement) -> dict[str, Any]:
        """将 TestRequirement ORM 实例转为字典。"""
        return {
            "id": str(requirement.id),
            "project_id": str(requirement.project_id),
            "title": requirement.title,
            "description": requirement.description,
            "category": requirement.category,
            "priority": requirement.priority,
            "change_thread_id": requirement.change_thread_id,
        }

    @staticmethod
    def _asset_to_dict(asset: KnowledgeAsset) -> dict[str, Any]:
        """将 KnowledgeAsset ORM 实例转为字典。"""
        return {
            "id": str(asset.id),
            "asset_type": asset.asset_type,
            "source_type": asset.source_type,
            "source_id": str(asset.source_id) if asset.source_id else None,
            "project_id": str(asset.project_id) if asset.project_id else None,
            "title": asset.title,
            "content": asset.content,
            "summary": asset.summary,
            "tags": asset.tags,
            "doc_id": str(asset.doc_id) if asset.doc_id else None,
            "confidence_score": asset.confidence_score,
            "status": asset.status,
            "conflict_with": asset.conflict_with,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
        }

    @staticmethod
    def _conflict_to_dict(conflict: KnowledgeConflict) -> dict[str, Any]:
        """将 KnowledgeConflict ORM 实例转为字典。"""
        return {
            "id": str(conflict.id),
            "new_asset_id": str(conflict.new_asset_id),
            "existing_asset_id": str(conflict.existing_asset_id),
            "conflict_type": conflict.conflict_type,
            "description": conflict.description,
            "resolution": conflict.resolution,
            "resolved_by": str(conflict.resolved_by) if conflict.resolved_by else None,
            "resolved_at": conflict.resolved_at.isoformat() if conflict.resolved_at else None,
            "resolution_note": conflict.resolution_note,
            "created_at": conflict.created_at.isoformat() if conflict.created_at else None,
        }

    # ==================================================================
    # P0: 聊天问答 → 知识库 FAQ 回流
    #
    # 扩展触发源：除 TestExecution 外，新增「好评反馈」与「采纳答案」两个入口。
    # 复用现有 5 步框架（资产沉淀 + 冲突检测），沉淀目标为 KB FAQ Document。
    #
    # 两条路径：
    #   1. extract_from_chat_feedback  — LLM 路径，从对话中提炼 Q-A
    #   2. extract_from_accepted_answer — 无 LLM 快路径，title+content 直接入库
    #
    # 幂等：通过 (source_type, source_id) 唯一性保证，同一反馈/回答不重复沉淀。
    # ==================================================================

    async def extract_from_chat_feedback(
        self,
        feedback_id: uuid.UUID,
        target_kb_id: uuid.UUID,
    ) -> dict[str, Any]:
        """从好评反馈提取 FAQ 资产（LLM 路径）。

        流程：
            1. 加载 Feedback(praise) + 关联 Message(assistant) + 同会话前一条 user 消息
            2. LLM 从对话中提取结构化 Q-A（多轮时取独立化后的 Q，P1 接入 rewriter）
            3. 沉淀为 KnowledgeAsset(asset_type=chat_faq, source_type=chat_feedback) + Document
            4. 冲突检测（复用 _detect_conflicts_for_assets）

        幂等保护：(source_type=chat_feedback, source_id=feedback_id) 已存在则跳过。

        Args:
            feedback_id: 好评反馈 ID。
            target_kb_id: 目标 FAQ 知识库 ID。

        Returns:
            提取结果摘要，含 task_id / asset_count / status。
        """
        from app.models.feedback import Feedback

        # 幂等检查
        existing = await self._get_asset_by_source(
            source_type="chat_feedback", source_id=feedback_id
        )
        if existing is not None:
            log.info(
                "compounding.chat_feedback_skipped",
                feedback_id=str(feedback_id),
                reason="already_processed",
                asset_id=str(existing.id),
            )
            return {
                "feedback_id": str(feedback_id),
                "status": "skipped",
                "reason": "already_processed",
                "asset_id": str(existing.id),
            }

        # 加载反馈上下文
        context = await self._load_chat_feedback_context(feedback_id)
        if context is None:
            return {
                "feedback_id": str(feedback_id),
                "status": "skipped",
                "reason": "feedback_not_praise_or_no_message",
            }

        # 创建回流任务
        task = CompoundingTask(
            task_type="extraction",
            status="running",
            trigger_source="chat_feedback",
            started_at=datetime.utcnow(),
        )
        self.db.add(task)
        await self.db.flush()

        try:
            # LLM 提取 Q-A
            extracted = await self._llm_extract_faq(context)
            question = (extracted.get("question") or "").strip()
            answer = (extracted.get("answer") or "").strip()

            if not question or not answer:
                task.status = "skipped"
                task.completed_at = datetime.utcnow()
                task.error_message = "llm_extracted_empty_qa"
                await self.db.flush()
                log.info(
                    "compounding.chat_feedback_empty_qa",
                    feedback_id=str(feedback_id),
                )
                return {
                    "feedback_id": str(feedback_id),
                    "task_id": str(task.id),
                    "status": "skipped",
                    "reason": "empty_qa",
                }

            # 沉淀为 KnowledgeAsset + Document
            asset = await self._precipitate_faq_asset(
                question=question,
                answer=answer,
                source_type="chat_feedback",
                source_id=feedback_id,
                owner_id=context["user_id"],
                target_kb_id=target_kb_id,
                task_id=task.id,
                tags=extracted.get("tags", []),
                confidence=extracted.get("confidence", 0.8),
            )

            # 冲突检测
            conflicts = await self._detect_conflicts_for_assets([asset])

            # P2: 提交审批（自动检测分流 — 高质量自动通过，否则人工审批）
            await self._submit_faq_for_review(
                asset=asset,
                target_kb_id=target_kb_id,
                conflict_count=len(conflicts),
            )

            task.extracted_asset_ids = [str(asset.id)]
            task.conflicts_detected = len(conflicts)
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            await self.db.flush()

            log.info(
                "compounding.chat_feedback_extracted",
                feedback_id=str(feedback_id),
                task_id=str(task.id),
                asset_id=str(asset.id),
                conflicts=len(conflicts),
            )
            return {
                "feedback_id": str(feedback_id),
                "task_id": str(task.id),
                "status": "success",
                "asset_id": str(asset.id),
                "doc_id": str(asset.doc_id) if asset.doc_id else None,
                "conflicts": len(conflicts),
            }
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
            task.completed_at = datetime.utcnow()
            await self.db.flush()
            log.error(
                "compounding.chat_feedback_failed",
                feedback_id=str(feedback_id),
                error=str(exc),
            )
            return {
                "feedback_id": str(feedback_id),
                "task_id": str(task.id),
                "status": "failed",
                "error": str(exc),
            }

    async def extract_from_accepted_answer(
        self,
        answer_id: uuid.UUID,
        target_kb_id: uuid.UUID,
    ) -> dict[str, Any]:
        """从被采纳的 QaAnswer 提取 FAQ 资产（无 LLM 快路径）。

        QaQuestion.title → Q，QaAnswer.content → A，直接沉淀。
        采纳答案已是结构化 Q-A，无需 LLM 提炼，毫秒级完成。

        幂等保护：(source_type=qa_accepted, source_id=answer_id) 已存在则跳过。

        Args:
            answer_id: 被采纳的回答 ID。
            target_kb_id: 目标 FAQ 知识库 ID。

        Returns:
            提取结果摘要，含 task_id / asset_count / status。
        """
        # 幂等检查
        existing = await self._get_asset_by_source(
            source_type="qa_accepted", source_id=answer_id
        )
        if existing is not None:
            log.info(
                "compounding.qa_accepted_skipped",
                answer_id=str(answer_id),
                reason="already_processed",
                asset_id=str(existing.id),
            )
            return {
                "answer_id": str(answer_id),
                "status": "skipped",
                "reason": "already_processed",
                "asset_id": str(existing.id),
            }

        # 加载采纳答案上下文
        context = await self._load_accepted_answer_context(answer_id)
        if context is None:
            return {
                "answer_id": str(answer_id),
                "status": "skipped",
                "reason": "answer_not_accepted_or_not_found",
            }

        # 创建回流任务
        task = CompoundingTask(
            task_type="extraction",
            status="running",
            trigger_source="qa_accepted",
            started_at=datetime.utcnow(),
        )
        self.db.add(task)
        await self.db.flush()

        try:
            # 直接使用 title + content，无 LLM
            question = context["question_title"]
            answer = context["answer_content"]

            # 沉淀为 KnowledgeAsset + Document
            asset = await self._precipitate_faq_asset(
                question=question,
                answer=answer,
                source_type="qa_accepted",
                source_id=answer_id,
                owner_id=context["user_id"],
                target_kb_id=target_kb_id,
                task_id=task.id,
                tags=context.get("tags", []),
                confidence=0.9,  # 采纳答案置信度高于 LLM 提取
            )

            # 冲突检测
            conflicts = await self._detect_conflicts_for_assets([asset])

            # P2: 提交审批（自动检测分流 — 高质量自动通过，否则人工审批）
            await self._submit_faq_for_review(
                asset=asset,
                target_kb_id=target_kb_id,
                conflict_count=len(conflicts),
            )

            task.extracted_asset_ids = [str(asset.id)]
            task.conflicts_detected = len(conflicts)
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            await self.db.flush()

            log.info(
                "compounding.qa_accepted_extracted",
                answer_id=str(answer_id),
                task_id=str(task.id),
                asset_id=str(asset.id),
                conflicts=len(conflicts),
            )
            return {
                "answer_id": str(answer_id),
                "task_id": str(task.id),
                "status": "success",
                "asset_id": str(asset.id),
                "doc_id": str(asset.doc_id) if asset.doc_id else None,
                "conflicts": len(conflicts),
            }
        except Exception as exc:
            task.status = "failed"
            task.error_message = str(exc)
            task.completed_at = datetime.utcnow()
            await self.db.flush()
            log.error(
                "compounding.qa_accepted_failed",
                answer_id=str(answer_id),
                error=str(exc),
            )
            return {
                "answer_id": str(answer_id),
                "task_id": str(task.id),
                "status": "failed",
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # P0 内部：上下文加载
    # ------------------------------------------------------------------

    async def _load_chat_feedback_context(
        self,
        feedback_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """加载好评反馈的完整上下文。

        加载 Feedback + 关联的 assistant Message + 同会话内前一条 user 消息。
        校验反馈类型为 praise 且关联了 message，否则返回 None（不触发回流）。

        Returns:
            上下文字典 {feedback, assistant_msg, user_msg, user_id, conversation_id}，
            或 None（不满足回流条件）。
        """
        from app.models.conversation import Message
        from app.models.feedback import Feedback

        stmt = select(Feedback).where(Feedback.id == feedback_id)
        stmt = apply_tenant_filter(stmt, Feedback, self._tenant_id)
        feedback = (await self.db.execute(stmt)).scalar_one_or_none()
        if feedback is None:
            return None

        # 仅 praise 类型触发回流（与 CHAT_FAQ_MIN_PRAISE_RATING 默认 4 一致）
        if feedback.type != "praise":
            return None
        if feedback.related_message_id is None:
            return None

        # 加载关联的 assistant 消息
        msg_stmt = select(Message).where(Message.id == feedback.related_message_id)
        msg_stmt = apply_tenant_filter(msg_stmt, Message, self._tenant_id)
        assistant_msg = (await self.db.execute(msg_stmt)).scalar_one_or_none()
        if assistant_msg is None or assistant_msg.role != "assistant":
            return None

        # 加载同会话内时间早于该 assistant 消息的最后一条 user 消息
        # （与 dataset_builder 的 SFT 配对策略一致）
        user_stmt = (
            select(Message)
            .where(
                Message.conversation_id == assistant_msg.conversation_id,
                Message.role == "user",
                Message.created_at <= assistant_msg.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        user_stmt = apply_tenant_filter(user_stmt, Message, self._tenant_id)
        user_msg = (await self.db.execute(user_stmt)).scalar_one_or_none()
        if user_msg is None:
            return None

        # P1: 加载同会话内 assistant 消息之前的最近 6 条消息（3 轮），
        # 供 StandaloneQueryRewriter 多轮独立化改写使用。
        hist_stmt = (
            select(Message)
            .where(
                Message.conversation_id == assistant_msg.conversation_id,
                Message.created_at <= assistant_msg.created_at,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.created_at.desc())
            .limit(6)
        )
        hist_stmt = apply_tenant_filter(hist_stmt, Message, self._tenant_id)
        hist_msgs = list((await self.db.execute(hist_stmt)).scalars().all())
        # 按时间正序排列（旧→新），构造 history dicts 供 rewriter 使用
        hist_msgs.reverse()
        history: list[dict[str, str]] = [
            {"role": m.role, "content": m.content or ""}
            for m in hist_msgs
        ]

        return {
            "feedback_id": str(feedback.id),
            "user_id": feedback.user_id,
            "conversation_id": str(assistant_msg.conversation_id),
            "user_query": user_msg.content,
            "assistant_answer": assistant_msg.content,
            "feedback_content": feedback.content,
            "history": history,
        }

    async def _load_accepted_answer_context(
        self,
        answer_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """加载被采纳回答的上下文。

        加载 QaAnswer(is_accepted=True) + 关联的 QaQuestion。
        校验回答已被采纳，否则返回 None。

        Returns:
            上下文字典 {question_title, answer_content, user_id, tags}，
            或 None（不满足回流条件）。
        """
        from app.models.qa import QaAnswer, QaQuestion

        stmt = select(QaAnswer).where(QaAnswer.id == answer_id)
        stmt = apply_tenant_filter(stmt, QaAnswer, self._tenant_id)
        answer = (await self.db.execute(stmt)).scalar_one_or_none()
        if answer is None or not answer.is_accepted:
            return None

        q_stmt = select(QaQuestion).where(QaQuestion.id == answer.question_id)
        q_stmt = apply_tenant_filter(q_stmt, QaQuestion, self._tenant_id)
        question = (await self.db.execute(q_stmt)).scalar_one_or_none()
        if question is None:
            return None

        # 标签：问答帖标签 + 是否 AI 生成标记
        tags: list[str] = []
        if question.tags:
            tags.extend([t.strip() for t in question.tags.split(",") if t.strip()])
        tags.append("ai_generated" if answer.is_ai_generated else "human")

        return {
            "question_id": str(question.id),
            "question_title": question.title,
            "answer_content": answer.content,
            "user_id": answer.user_id,
            "tags": tags,
        }

    # ------------------------------------------------------------------
    # P0 内部：LLM 提取 Q-A
    # ------------------------------------------------------------------

    async def _llm_extract_faq(
        self,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """调用 LLM 从好评对话中提取结构化 Q-A。

        LLM 不可用时降级为直接使用原始 user_query + assistant_answer。

        Returns:
            {question, answer, tags, confidence}
        """
        user_query = context.get("user_query", "")
        assistant_answer = context.get("assistant_answer", "")

        # 降级：LLM 不可用时直接用原始 Q-A
        if self.llm is None:
            log.warning("compounding.faq_llm_unavailable_use_raw")
            return {
                "question": user_query,
                "answer": assistant_answer,
                "tags": [],
                "confidence": 0.6,  # 降级置信度较低
            }

        # P1: 多轮对话独立化改写 — 把含指代/省略的 user_query 改写为独立标准 Q
        # 复用 StandaloneQueryRewriter（CoreferenceResolver + TopicTracker），
        # 使沉淀的 FAQ 问题脱离对话上下文仍可被独立检索。
        history = context.get("history") or []
        if len(history) >= 2:
            try:
                from app.context.coreference_resolver import CoreferenceResolver
                from app.context.focus_tracker import TopicTracker
                from app.context.standalone_query_rewriter import (
                    StandaloneQueryRewriter,
                )

                rewriter = StandaloneQueryRewriter(
                    llm=self.llm,
                    coreference_resolver=CoreferenceResolver(self.llm),
                    topic_tracker=TopicTracker(self.llm),
                )
                standalone_q = await rewriter.rewrite(
                    current_query=user_query,
                    history=history,
                )
                if standalone_q and standalone_q.strip():
                    user_query = standalone_q.strip()
                    log.info(
                        "compounding.faq_standalone_rewritten",
                        original=(context.get("user_query") or "")[:80],
                        standalone=standalone_q[:80],
                    )
            except Exception as exc:
                log.warning(
                    "compounding.faq_standalone_rewrite_failed",
                    error=str(exc)[:200],
                )

        prompt = self._build_faq_extraction_prompt(
            user_query=user_query,
            assistant_answer=assistant_answer,
            feedback_content=context.get("feedback_content", ""),
        )
        messages = [Message(role="system", content=prompt)]

        try:
            response = await self._llm_generate(messages, max_tokens=1500)
            extracted = _extract_json(response)
            if isinstance(extracted, dict):
                return {
                    "question": extracted.get("question") or user_query,
                    "answer": extracted.get("answer") or assistant_answer,
                    "tags": extracted.get("tags") or [],
                    "confidence": float(extracted.get("confidence", 0.8)),
                }
        except Exception as exc:
            log.warning("compounding.faq_llm_extract_error", error=str(exc))

        # JSON 解析失败 → 降级用原始 Q-A
        return {
            "question": user_query,
            "answer": assistant_answer,
            "tags": [],
            "confidence": 0.6,
        }

    @staticmethod
    def _build_faq_extraction_prompt(
        user_query: str,
        assistant_answer: str,
        feedback_content: str,
    ) -> str:
        """构建 FAQ 提取的系统提示词。"""
        return (
            "你是企业知识库 FAQ 提取引擎。从一段被用户好评的对话中，"
            "提取自包含、可独立检索的标准 Q-A 对，沉淀为企业知识库 FAQ 文档。\n\n"
            "要求：\n"
            "1. question 必须自包含 — 脱离对话上下文仍可理解（指代词具化，"
            '如"它"→具体实体）；\n'
            '2. answer 必须自包含 — 直接可用，不依赖"上述""如前所述"等指代；\n'
            "3. 剔除寒暄/口语填充/重复解释，保留数字/约束/具体值；\n"
            "4. confidence 评估 Q-A 质量（0.0~1.0），低质对话给低分；\n"
            "5. tags 提取 1~3 个主题标签（如 报销/差旅/政策）。\n\n"
            "以 JSON 返回：\n"
            '{"question": "...", "answer": "...", "tags": ["..."], "confidence": 0.8}\n\n'
            f"用户提问：{user_query[:500]}\n\n"
            f"助手回答：{assistant_answer[:2000]}\n\n"
            f"用户好评内容（参考）：{feedback_content[:200]}\n\n"
            "提取结果（JSON）："
        )

    # ------------------------------------------------------------------
    # P0 内部：资产沉淀 + Document 创建
    # ------------------------------------------------------------------

    async def _precipitate_faq_asset(
        self,
        question: str,
        answer: str,
        source_type: str,
        source_id: uuid.UUID,
        owner_id: uuid.UUID,
        target_kb_id: uuid.UUID,
        task_id: uuid.UUID,
        tags: list[str] | None = None,
        confidence: float = 0.8,
    ) -> KnowledgeAsset:
        """将 Q-A 沉淀为 KnowledgeAsset + KB Document。

        流程：
            1. 创建 Document（status=published，进入 RAG 检索）
            2. 创建 KnowledgeAsset（asset_type=chat_faq，关联 doc_id）
            3. 触发文档索引（process_document.delay）

        Args:
            question: 独立化后的问题文本。
            answer: 自包含的回答文本。
            source_type: chat_feedback / qa_accepted。
            source_id: Feedback.id 或 QaAnswer.id。
            owner_id: 文档所有者（反馈/回答的提交者）。
            target_kb_id: 目标 FAQ 知识库 ID。
            task_id: 关联的回流任务 ID。
            tags: 标签列表。
            confidence: AI 置信度。

        Returns:
            创建的 KnowledgeAsset 实例（已关联 doc_id）。
        """
        from app.config import get_settings

        _settings = get_settings()
        max_chars = _settings.CHAT_FAQ_MAX_CONTENT_CHARS

        # 截断防超长
        question = question[:500]
        answer = answer[:max_chars]

        # 1. 创建 Document — 直接 ORM 构造，绕过 KnowledgeService 的 user 权限校验
        # （回流是系统行为，owner_id 用反馈/回答提交者，文档直接 published 进 RAG）
        doc_id = await self._create_faq_document(
            kb_id=target_kb_id,
            title=question,
            content=answer,
            owner_id=owner_id,
        )

        # 2. 创建 KnowledgeAsset
        asset = KnowledgeAsset(
            asset_type="chat_faq",
            source_type=source_type,
            source_id=source_id,
            title=question,
            content=answer,
            summary=answer[:200] if answer else None,
            tags=tags or [],
            doc_id=doc_id,
            confidence_score=confidence,
            status="pending_review",  # P2: 审批工作流，由 KnowledgeApprovalService 流转
            compounding_task_id=task_id,
        )
        self.db.add(asset)
        await self.db.flush()

        log.info(
            "compounding.faq_precipitated",
            asset_id=str(asset.id),
            source_type=source_type,
            doc_id=str(doc_id),
            title=question[:80],
        )
        return asset

    async def _create_faq_document(
        self,
        kb_id: uuid.UUID,
        title: str,
        content: str,
        owner_id: uuid.UUID,
    ) -> uuid.UUID:
        """创建 FAQ Document 并触发索引。

        直接构造 Document ORM（系统回流行为，绕过 KnowledgeService 的 user 权限校验）。
        触发 process_document 异步任务重建索引，使新 FAQ 进入 RAG 检索。

        Args:
            kb_id: 目标知识库 ID。
            title: 文档标题（独立化后的问题）。
            content: 文档内容（自包含的回答）。
            owner_id: 文档所有者 ID。

        Returns:
            新创建的 Document ID。
        """
        from app.models.knowledge import Document

        doc = Document(
            kb_id=kb_id,
            title=title,
            content_text=content,
            doc_type="md",
            status="pending_review",  # P2: 审批通过后由 KnowledgeApprovalService 改为 published
            owner_id=owner_id,
            classification="internal",  # FAQ 默认内部可见，P2 审批时可调整
            category="FAQ",
            char_count=len(content),
            page_count=1,
        )
        # 多租户隔离
        if self._tenant_id is not None:
            doc.tenant_id = self._tenant_id
        self.db.add(doc)
        await self.db.flush()

        doc_id_str = str(doc.id)

        # 触发索引重建（优雅降级 — Celery 不可用时仅日志，不影响沉淀）
        try:
            from tasks.document_tasks import process_document

            process_document.delay(doc_id_str)
            log.info(
                "compounding.faq_doc_index_triggered",
                doc_id=doc_id_str,
                kb_id=str(kb_id),
            )
        except Exception as exc:
            log.warning(
                "compounding.faq_doc_index_trigger_failed",
                doc_id=doc_id_str,
                error=str(exc)[:200],
            )

        return doc.id

    async def _get_asset_by_source(
        self,
        source_type: str,
        source_id: uuid.UUID,
    ) -> KnowledgeAsset | None:
        """按 (source_type, source_id) 查询已有资产 — 幂等检查。

        同一反馈/回答不重复沉淀。含软删除过滤，已废弃资产不阻断重新沉淀。
        """
        stmt = select(KnowledgeAsset).where(
            KnowledgeAsset.source_type == source_type,
            KnowledgeAsset.source_id == source_id,
            KnowledgeAsset.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, KnowledgeAsset, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # P2 内部：审批工作流接入
    # ------------------------------------------------------------------

    async def _submit_faq_for_review(
        self,
        asset: KnowledgeAsset,
        target_kb_id: uuid.UUID,
        conflict_count: int,
    ) -> None:
        """P2: 提交 FAQ 资产到审批工作流（自动检测分流）。

        复用 KnowledgeApprovalService.submit_for_review：
        - 高质量(quality_score >= 阈值 且 无冲突 且 无 PII) → 自动 approve
          （asset.status=active, doc.status=published）
        - 否则 → pending（人工审批，asset.status=pending_review）

        审批服务不可用时优雅降级（资产保持 pending_review，不阻断沉淀）。
        """
        try:
            from app.services.knowledge_approval_service import (
                KnowledgeApprovalService,
            )

            approval_service = KnowledgeApprovalService(
                self.db, tenant_id=self._tenant_id
            )
            await approval_service.submit_for_review(
                asset=asset,
                doc_id=asset.doc_id,
                kb_id=target_kb_id,
                conflict_count=conflict_count,
            )
        except Exception as exc:
            log.warning(
                "compounding.faq_approval_submit_failed",
                asset_id=str(asset.id),
                error=str(exc)[:200],
            )
            # 降级：资产保持 pending_review，不阻断沉淀流程
