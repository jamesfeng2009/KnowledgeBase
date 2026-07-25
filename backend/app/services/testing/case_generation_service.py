"""
测试用例生成服务 — 单一职责：基于需求点 + 上下文文档生成测试用例。

核心流程：
    需求点(TestRequirement) + 技术方案 + 接口文档
    → LLM 生成 → 测试用例(TestCase)

复用现有能力：
    - Document 表：技术方案/接口文档存储在知识库 Document 中
    - LLMProvider：用例生成通过 LLMProvider 抽象调用，不感知底层 Provider
    - SoftDeleteMixin：用例支持软删除，查询时统一过滤 deleted_at

关键设计：
    - 上下文融合：将需求点详情 + 技术方案文档 + 接口文档内容组合为 LLM 输入，
      使生成的用例覆盖接口参数、边界条件和异常场景；
    - 用例编号：项目内自增，格式 TC-{sequence:04d}（如 TC-0001），
      通过查询项目下已有最大编号 +1 生成，保证编号连续；
    - 生成完成后将需求状态置为 cases_ready，表示用例已就绪可进入评审。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.knowledge import Document
from app.models.testing import TestCase, TestRequirement
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)


def _extract_json(text: str) -> list | dict:
    """Extract JSON from LLM response, handling markdown code fences.

    LLM 经常将 JSON 包裹在 ```json ... ``` 代码块中，本函数先尝试
    提取代码块内容，再尝试直接解析，最后尝试正则匹配 JSON 数组/对象。

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的 JSON 数据（list 或 dict）。

    Raises:
        ValueError: 无法从文本中提取有效 JSON。
    """
    # Try to find JSON array or object in code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1)
    # Try direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Try to find JSON array/object pattern
        for pattern in [r"\[.*\]", r"\{.*\}"]:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    continue
        raise ValueError(f"Failed to parse JSON from LLM response: {text[:200]}")


class TestCaseGenerationService:
    """测试用例生成服务 — 基于需求点 + 上下文文档生成测试用例。

    通过 LLMProvider 抽象调用大模型，结合需求点详情与技术方案/接口文档，
    生成覆盖功能、接口、边界和异常场景的测试用例，并持久化到 TestCase 表。

    依赖注入：
        - llm: LLMProvider 实例（由 factory.get_llm_provider() 创建）
        - db: AsyncSession 实例（由 API 层的 Depends(get_db_session) 提供）
    """

    def __init__(
        self, llm: LLMProvider, db: AsyncSession, tenant_id: UUID | None = None
    ) -> None:
        self.llm = llm
        self.db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def generate_cases(
        self,
        requirement_id: str,
        context_doc_ids: list[str] | None = None,
        test_type: str | None = None,
        max_cases: int = 5,
    ) -> list[dict[str, Any]]:
        """基于需求点和上下文文档生成测试用例。

        读取需求点详情和上下文文档（技术方案/接口文档），调用 LLM 生成
        测试用例，每个用例包含标题、描述、前置条件、测试步骤、预期结果、
        测试类型、优先级和标签。生成完成后将需求状态置为 cases_ready。

        Args:
            requirement_id: 需求点 ID（UUID 字符串）。
            context_doc_ids: 可选，额外上下文文档 ID 列表
                （技术方案、接口文档等的 Document UUID）。
            test_type: 可选，指定测试类型
                （functional/api/ui/performance/security/compatibility），
                不指定则由 LLM 自动判断。
            max_cases: 最大生成用例数，默认 5。

        Returns:
            已创建的测试用例列表，每项为字典。

        Raises:
            ValueError: 需求点不存在。
        """
        # 1. 获取需求点
        req_uuid = uuid.UUID(requirement_id)
        requirement = await self._get_requirement(req_uuid)
        if requirement is None:
            raise ValueError(f"需求点不存在: {requirement_id}")

        # 2. 获取上下文文档
        context_docs = await self._get_context_docs(context_doc_ids or [])
        tech_content = self._build_context_text(context_docs)

        # 3. 构建 Prompt 并调用 LLM
        system_prompt = self._build_generate_prompt(test_type, max_cases)
        user_content = self._build_user_content(requirement, tech_content)
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_content),
        ]
        response = await self._llm_generate(messages, max_tokens=3000)

        # 4. 解析 JSON 响应
        cases_data = _extract_json(response)
        # 兼容 LLM 返回 {"test_cases": [...]} 或单条 dict 的情况
        if isinstance(cases_data, dict):
            cases_data = cases_data.get("test_cases", [cases_data])

        # 5. 限制生成数量
        cases_data = cases_data[:max_cases]

        # 6. 创建 TestCase 记录
        created: list[dict[str, Any]] = []
        for item in cases_data:
            case_no = await self._generate_case_no(requirement.project_id)
            case_type = item.get("test_type", test_type or "functional")
            test_case = TestCase(
                project_id=requirement.project_id,
                requirement_id=requirement.id,
                title=item.get("title", "未命名用例"),
                description=item.get("description"),
                preconditions=item.get("preconditions"),
                test_steps=item.get("test_steps", []),
                expected_result=item.get("expected_result"),
                test_type=case_type,
                priority=item.get("priority", "normal"),
                status="draft",
                tags=item.get("tags", []),
                created_by="ai_generate",
                tenant_id=self._tenant_id,  # RLS WITH CHECK 要求写入行携带当前租户 ID
                context_doc_ids=context_doc_ids,
                case_no=case_no,
            )
            self.db.add(test_case)
            await self.db.flush()
            created.append(self._to_dict(test_case))

        # 7. 更新需求状态为 cases_ready
        stmt = update(TestRequirement).where(
            TestRequirement.id == requirement.id
        )
        stmt = apply_tenant_filter(stmt, TestRequirement, self._tenant_id)
        await self.db.execute(stmt.values(status="cases_ready"))
        await self.db.flush()

        log.info(
            "test_case.generated",
            requirement_id=requirement_id,
            count=len(created),
        )
        return created

    # ------------------------------------------------------------------
    # 用例编号生成
    # ------------------------------------------------------------------

    async def _generate_case_no(self, project_id: uuid.UUID) -> str:
        """生成用例编号 — 格式: TC-{sequence:04d}。

        查询项目下所有已有用例编号，找到最大序号并 +1，
        格式化为 TC-0001、TC-0002 等。

        Args:
            project_id: 项目 ID。

        Returns:
            下一个可用的用例编号字符串。
        """
        stmt = select(TestCase.case_no).where(
            TestCase.project_id == project_id,
            TestCase.deleted_at.is_(None),
            TestCase.case_no.isnot(None),
        )
        stmt = apply_tenant_filter(stmt, TestCase, self._tenant_id)
        result = await self.db.execute(stmt)
        existing_nos = result.scalars().all()

        max_seq = 0
        for no in existing_nos:
            # 解析 TC-0001 → 1
            try:
                seq = int(no.split("-")[-1])
                if seq > max_seq:
                    max_seq = seq
            except (ValueError, IndexError):
                continue

        next_seq = max_seq + 1
        return f"TC-{next_seq:04d}"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _llm_generate(
        self,
        messages: list[Message],
        max_tokens: int = 2000,
    ) -> str:
        """调用 LLM 生成文本（非流式）。

        chat 为异步生成器，通过 ``async for chunk`` 消费所有文本片段。
        LLM 不可用时抛出异常，由调用方决定降级策略。

        Args:
            messages: 消息列表（system/user/assistant）。
            max_tokens: 最大生成 token 数。

        Returns:
            LLM 生成的完整文本。

        Raises:
            Exception: LLM 调用失败时透传异常。
        """
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
            log.warning("test_case.llm_error", error=str(exc))
            raise

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

    async def _get_context_docs(
        self, doc_ids: list[str]
    ) -> list[Document]:
        """获取上下文文档列表（含软删除过滤）。

        Args:
            doc_ids: 文档 ID 字符串列表。

        Returns:
            Document ORM 实例列表。
        """
        if not doc_ids:
            return []
        doc_uuids = [uuid.UUID(did) for did in doc_ids]
        stmt = select(Document).where(
            Document.id.in_(doc_uuids),
            Document.deleted_at.is_(None),
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _build_context_text(docs: list[Document]) -> str:
        """构建上下文文档文本。

        将多个文档内容拼接为带标题的文本块，每篇文档截取前 2000 字
        以控制 LLM 输入长度。

        Args:
            docs: Document ORM 实例列表。

        Returns:
            拼接后的上下文文本，无文档时返回空字符串。
        """
        if not docs:
            return ""
        parts: list[str] = []
        for doc in docs:
            content = doc.content_text or ""
            if not content and doc.content_html:
                content = re.sub(r"<[^>]+>", "", doc.content_html)
            parts.append(f"### {doc.title}\n{content[:2000]}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_generate_prompt(
        test_type: str | None, max_cases: int
    ) -> str:
        """构建用例生成的系统提示词。

        Args:
            test_type: 可选，指定测试类型。
            max_cases: 最大生成用例数。

        Returns:
            系统提示词文本。
        """
        type_hint = f"指定测试类型: {test_type}\n" if test_type else ""

        return (
            "你是一位资深的软件测试工程师。请根据提供的需求点和上下文文档，"
            f"生成最多 {max_cases} 个高质量的测试用例。\n\n"
            f"{type_hint}"
            "每个测试用例包含以下字段：\n"
            "- title: 用例标题\n"
            "- description: 用例描述\n"
            "- preconditions: 前置条件\n"
            "- test_steps: 测试步骤列表，每项含 step_no(序号) / "
            "action(操作) / expected(预期)\n"
            "- expected_result: 最终预期结果\n"
            "- test_type: 测试类型，取值: functional / api / ui / "
            "performance / security / compatibility\n"
            "- priority: 优先级，取值: low / normal / high / critical\n"
            "- tags: 标签列表\n\n"
            "要求：\n"
            "1. 覆盖正常流程、边界条件和异常场景；\n"
            "2. 测试步骤要具体可执行，预期结果要可验证；\n"
            "3. 如有接口文档，确保接口参数和返回值校验完整。\n\n"
            "请以 JSON 数组格式返回，不要包含其他解释文字。格式如下：\n"
            '[{"title": "...", "description": "...", "preconditions": "...", '
            '"test_steps": [{"step_no": 1, "action": "...", "expected": "..."}], '
            '"expected_result": "...", "test_type": "functional", '
            '"priority": "normal", "tags": ["..."]}]'
        )

    @staticmethod
    def _build_user_content(
        requirement: TestRequirement, tech_content: str
    ) -> str:
        """构建用户消息内容 — 需求点详情 + 上下文文档。

        Args:
            requirement: 需求点 ORM 实例。
            tech_content: 上下文文档文本。

        Returns:
            拼接后的用户消息文本。
        """
        parts: list[str] = [
            "## 需求点信息",
            f"- 标题: {requirement.title}",
            f"- 描述: {requirement.description or '无'}",
            f"- 分类: {requirement.category}",
            f"- 优先级: {requirement.priority}",
        ]
        if requirement.acceptance_criteria:
            parts.append("- 验收标准:")
            for i, criteria in enumerate(requirement.acceptance_criteria, 1):
                parts.append(f"  {i}. {criteria}")
        if tech_content:
            parts.append("\n## 上下文文档（技术方案/接口文档）")
            parts.append(tech_content)
        return "\n".join(parts)

    @staticmethod
    def _to_dict(test_case: TestCase) -> dict[str, Any]:
        """将 TestCase ORM 实例转为字典。"""
        return {
            "id": str(test_case.id),
            "project_id": str(test_case.project_id),
            "requirement_id": str(test_case.requirement_id)
            if test_case.requirement_id
            else None,
            "title": test_case.title,
            "description": test_case.description,
            "preconditions": test_case.preconditions,
            "test_steps": test_case.test_steps,
            "expected_result": test_case.expected_result,
            "test_type": test_case.test_type,
            "priority": test_case.priority,
            "status": test_case.status,
            "tags": test_case.tags,
            "created_by": test_case.created_by,
            "case_no": test_case.case_no,
            "context_doc_ids": test_case.context_doc_ids,
            "created_at": test_case.created_at.isoformat()
            if test_case.created_at
            else None,
        }
