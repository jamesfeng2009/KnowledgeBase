"""
需求分析服务 — 单一职责：从 PRD/UI 稿自动提取原子需求点。

核心流程：
    PRD/UI稿 → LLM 分析 → 原子需求点(TestRequirement)

复用现有能力：
    - Document 表：PRD/UI 稿存储在知识库 Document 中
    - LLMProvider：需求提取通过 LLMProvider 抽象调用，不感知底层 Provider
    - SoftDeleteMixin：需求点支持软删除，查询时统一过滤 deleted_at

关键设计：
    - LLM 返回 JSON 数组，每个需求点含 title/description/category/priority/
      acceptance_criteria/source_text 六个字段；
    - 解析 LLM 响应时兼容 markdown 代码块包裹的 JSON；
    - 提取完成后将需求状态置为 analyzed，表示已分析待生成用例。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.knowledge import Document
from app.models.testing import TestRequirement
from app.utils.logger import get_logger

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


class RequirementAnalysisService:
    """需求分析服务 — 从 PRD/UI 稿自动提取原子需求点。

    通过 LLMProvider 抽象调用大模型，分析 PRD/UI 稿文档内容，
    拆分为原子需求点并持久化到 TestRequirement 表。

    依赖注入：
        - llm: LLMProvider 实例（由 factory.get_llm_provider() 创建）
        - db: AsyncSession 实例（由 API 层的 Depends(get_db_session) 提供）
    """

    def __init__(self, llm: LLMProvider, db: AsyncSession) -> None:
        self.llm = llm
        self.db = db

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def extract_requirements(
        self,
        project_id: str,
        doc_id: str,
        target_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """从 PRD/UI 稿自动提取原子需求点。

        读取知识库 Document 内容，调用 LLM 分析并拆分为原子需求点，
        每个需求点包含标题、描述、分类、优先级、验收标准和原始文本片段。
        提取完成后将需求状态置为 analyzed。

        Args:
            project_id: 测试项目 ID（UUID 字符串）。
            doc_id: 来源文档 ID（知识库 Document UUID 字符串）。
            target_categories: 可选，指定提取的需求分类
                （functional/non_functional/ui/api/performance），
                不指定则由 LLM 自动分类。

        Returns:
            已创建的需求点列表，每项为字典。

        Raises:
            ValueError: 文档不存在或内容为空。
        """
        # 1. 解析 UUID
        project_uuid = uuid.UUID(project_id)
        doc_uuid = uuid.UUID(doc_id)

        # 2. 获取文档
        doc = await self._get_doc(doc_uuid)
        if doc is None:
            raise ValueError(f"文档不存在: {doc_id}")

        # 3. 提取纯文本内容
        content = self._get_content(doc)
        if not content.strip():
            raise ValueError(f"文档内容为空: {doc_id}")

        # 4. 构建 Prompt 并调用 LLM
        system_prompt = self._build_extract_prompt(target_categories)
        user_content = (
            f"文档标题: {doc.title}\n\n"
            f"文档内容:\n{content[:4000]}"
        )
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_content),
        ]
        response = await self._llm_generate(messages, max_tokens=2000)

        # 5. 解析 JSON 响应
        requirements_data = _extract_json(response)
        # 兼容 LLM 返回 {"requirements": [...]} 或单条 dict 的情况
        if isinstance(requirements_data, dict):
            requirements_data = requirements_data.get("requirements", [requirements_data])

        # 6. 创建 TestRequirement 记录
        created: list[dict[str, Any]] = []
        for item in requirements_data:
            requirement = TestRequirement(
                project_id=project_uuid,
                source_doc_id=doc_uuid,
                title=item.get("title", "未命名需求"),
                description=item.get("description"),
                category=item.get("category", "functional"),
                priority=item.get("priority", "normal"),
                acceptance_criteria=item.get("acceptance_criteria", []),
                source_text=item.get("source_text"),
                source="ai_extract",
                status="analyzed",
            )
            self.db.add(requirement)
            await self.db.flush()
            created.append(self._to_dict(requirement))

        log.info(
            "requirement.extracted",
            project_id=project_id,
            doc_id=doc_id,
            count=len(created),
        )
        return created

    async def list_requirements(
        self,
        project_id: uuid.UUID,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页查询项目的需求点列表。

        Args:
            project_id: 项目 ID。
            page: 页码，从 1 开始。
            size: 每页数量。

        Returns:
            (需求点列表, 总数) 元组。
        """
        offset = (page - 1) * size

        # 查询总数（含软删除过滤）
        count_stmt = (
            select(func.count())
            .select_from(TestRequirement)
            .where(
                TestRequirement.project_id == project_id,
                TestRequirement.deleted_at.is_(None),
            )
        )
        total = await self.db.scalar(count_stmt) or 0

        # 分页查询
        stmt = (
            select(TestRequirement)
            .where(
                TestRequirement.project_id == project_id,
                TestRequirement.deleted_at.is_(None),
            )
            .order_by(TestRequirement.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        items = [self._to_dict(r) for r in result.scalars().all()]

        return items, total

    async def get_requirement(
        self,
        requirement_id: uuid.UUID,
    ) -> TestRequirement | None:
        """按 ID 查询需求点（含软删除过滤）。

        Args:
            requirement_id: 需求点 ID。

        Returns:
            TestRequirement ORM 实例，不存在时返回 None。
        """
        result = await self.db.execute(
            select(TestRequirement).where(
                TestRequirement.id == requirement_id,
                TestRequirement.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def update_requirement(
        self,
        requirement_id: uuid.UUID,
        **kwargs: Any,
    ) -> TestRequirement:
        """更新需求点字段。

        仅更新 kwargs 中提供的非 None 字段，支持 title/description/
        category/priority/acceptance_criteria/status 等字段。

        Args:
            requirement_id: 需求点 ID。
            **kwargs: 待更新的字段键值对。

        Returns:
            更新后的 TestRequirement ORM 实例。

        Raises:
            ValueError: 需求点不存在。
        """
        requirement = await self.get_requirement(requirement_id)
        if requirement is None:
            raise ValueError(f"需求点不存在: {requirement_id}")

        for key, value in kwargs.items():
            if hasattr(requirement, key) and value is not None:
                setattr(requirement, key, value)

        await self.db.flush()
        log.info(
            "requirement.updated",
            requirement_id=str(requirement_id),
            fields=list(kwargs.keys()),
        )
        return requirement

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _llm_generate(
        self,
        messages: list[Message],
        max_tokens: int = 1000,
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
            log.warning("requirement.llm_error", error=str(exc))
            raise

    async def _get_doc(self, doc_uuid: uuid.UUID) -> Document | None:
        """获取文档 ORM 实例（含软删除过滤）。"""
        result = await self.db.execute(
            select(Document).where(
                Document.id == doc_uuid,
                Document.deleted_at.is_(None),
            )
        )
        return result.scalars().first()

    @staticmethod
    def _get_content(doc: Document) -> str:
        """获取文档纯文本内容。

        优先使用 content_text；若不存在则从 content_html 中
        去除 HTML 标签后提取纯文本。
        """
        content = doc.content_text or ""
        if not content and doc.content_html:
            content = re.sub(r"<[^>]+>", "", doc.content_html)
        return content

    @staticmethod
    def _build_extract_prompt(target_categories: list[str] | None) -> str:
        """构建需求提取的系统提示词。

        Args:
            target_categories: 可选，指定重点关注的需求分类。

        Returns:
            系统提示词文本。
        """
        categories_hint = ""
        if target_categories:
            categories_hint = f"重点关注以下分类: {', '.join(target_categories)}\n"

        return (
            "你是一位资深的软件测试需求分析师。请分析以下 PRD/UI 稿文档，"
            "提取所有原子需求点。\n\n"
            f"{categories_hint}"
            "每个需求点包含以下字段：\n"
            "- title: 需求标题（简洁明确）\n"
            "- description: 需求详细描述\n"
            "- category: 需求分类，取值: functional(功能) / "
            "non_functional(非功能) / ui(界面) / api(接口) / performance(性能)\n"
            "- priority: 优先级，取值: low / normal / high / critical\n"
            "- acceptance_criteria: 验收标准列表（字符串数组）\n"
            "- source_text: 提取该需求的原始文本片段\n\n"
            "请以 JSON 数组格式返回，不要包含其他解释文字。格式如下：\n"
            '[{"title": "...", "description": "...", "category": "functional", '
            '"priority": "normal", "acceptance_criteria": ["..."], '
            '"source_text": "..."}]'
        )

    @staticmethod
    def _to_dict(requirement: TestRequirement) -> dict[str, Any]:
        """将 TestRequirement ORM 实例转为字典。"""
        return {
            "id": str(requirement.id),
            "project_id": str(requirement.project_id),
            "source_doc_id": str(requirement.source_doc_id)
            if requirement.source_doc_id
            else None,
            "title": requirement.title,
            "description": requirement.description,
            "category": requirement.category,
            "priority": requirement.priority,
            "acceptance_criteria": requirement.acceptance_criteria,
            "source_text": requirement.source_text,
            "source": requirement.source,
            "status": requirement.status,
            "created_at": requirement.created_at.isoformat()
            if requirement.created_at
            else None,
        }
