"""
文档智能处理服务 — 单一职责：文档入库后 LLM 自动摘要/标签/分类/行动项/FAQ。

遵循开闭原则：新增智能处理能力只需添加方法，不修改既有逻辑。
遵循优雅降级：LLM 不可用时跳过处理，不阻塞文档入库流程。

五项自动化能力：
    auto_summarize       — 200 字摘要
    auto_tag             — 3-5 个关键词标签
    auto_classify        — 文档分类
    extract_action_items — 会议纪要行动项提取
    auto_generate_faq    — 从文档生成问答对
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.action import DocumentAction
from app.models.knowledge import Document
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 文档分类选项
DOC_CATEGORIES = [
    "政策", "SOP", "技术文档", "会议纪要", "培训资料", "产品文档", "合同模板",
]


class DocIntelligenceService:
    """文档智能处理服务 — LLM 自动摘要/标签/分类/行动项/FAQ。"""

    def __init__(self, llm: LLMProvider, db: AsyncSession) -> None:
        self.llm = llm
        self.db = db

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def process_all(self, doc_id: str) -> dict[str, Any]:
        """执行全部智能处理 — 文档入库后链式调用。

        并行执行摘要/标签/分类（互不依赖），
        行动项提取仅对会议纪要类文档执行。

        Args:
            doc_id: 文档 ID（UUID 字符串）。

        Returns:
            处理结果摘要。
        """
        import asyncio

        doc_uuid = self._parse_uuid(doc_id)
        doc = await self._get_doc(doc_uuid)
        if not doc:
            return {"doc_id": doc_id, "status": "failed", "error": "文档不存在"}

        # 并行执行摘要/标签/分类
        results: dict[str, Any] = {"doc_id": doc_id, "status": "success"}
        tasks = [
            self._safe_run("summary", self.auto_summarize, doc),
            self._safe_run("tags", self.auto_tag, doc),
            self._safe_run("category", self.auto_classify, doc),
        ]
        task_results = await asyncio.gather(*tasks)
        for name, result in task_results:
            results[name] = result

        # 行动项提取仅对会议纪要执行
        if results.get("category") == "会议纪要":
            actions = await self._safe_run("actions", self.extract_action_items, doc)
            results["actions"] = actions[1]

        await self.db.commit()
        logger.info(
            "doc_intelligence.processed",
            doc_id=doc_id,
            summary=bool(results.get("summary")),
            tags=results.get("tags"),
            category=results.get("category"),
        )
        return results

    # ------------------------------------------------------------------
    # 单项处理
    # ------------------------------------------------------------------

    async def auto_summarize(self, doc: Document) -> str:
        """自动摘要 — 200 字以内总结核心内容。

        Args:
            doc: Document ORM 实例。

        Returns:
            摘要文本。
        """
        content = self._get_content(doc)
        prompt = (
            "请用 200 字以内总结以下文档的核心内容，"
            "只输出摘要文本，不要额外解释：\n\n"
            f"{content[:3000]}"
        )
        summary = await self._llm_generate(prompt, max_tokens=300)
        doc.summary = summary
        await self.db.flush()
        logger.info("doc_intelligence.summary", doc_id=str(doc.id), length=len(summary))
        return summary

    async def auto_tag(self, doc: Document) -> list[str]:
        """自动标签 — LLM 提取 3-5 个关键词标签。

        Args:
            doc: Document ORM 实例。

        Returns:
            标签列表。
        """
        content = self._get_content(doc)
        prompt = (
            "从以下文档中提取 3-5 个关键词标签，"
            "用逗号分隔，只输出标签：\n\n"
            f"{content[:2000]}"
        )
        response = await self._llm_generate(prompt, max_tokens=100)
        tags = [t.strip() for t in response.split(",") if t.strip()]
        # 存入 knowledge_bases.tags（通过 Document 所在 KB 的 tags 扩展）
        # 这里存入 doc 的 metadata 中（通过 content_json 扩展不合适，用 KB tags）
        # 实际存入方式：更新 KB 的 tags 字段
        if doc.knowledge_base and tags:
            existing = doc.knowledge_base.tags or []
            merged = list(set(existing + tags))
            doc.knowledge_base.tags = merged
            await self.db.flush()
        logger.info("doc_intelligence.tags", doc_id=str(doc.id), tags=tags)
        return tags

    async def auto_classify(self, doc: Document) -> str:
        """自动分类 — 判断文档所属类别。

        Args:
            doc: Document ORM 实例。

        Returns:
            分类名称。
        """
        content = self._get_content(doc)
        prompt = (
            f"判断以下文档属于哪个类别，只输出类别名称，不要解释：\n"
            f"可选类别：{', '.join(DOC_CATEGORIES)}\n\n"
            f"{content[:2000]}"
        )
        category = await self._llm_generate(prompt, max_tokens=20)
        category = category.strip()
        if category not in DOC_CATEGORIES:
            category = "技术文档"  # 默认分类
        doc.category = category
        await self.db.flush()
        logger.info("doc_intelligence.category", doc_id=str(doc.id), category=category)
        return category

    async def extract_action_items(self, doc: Document) -> list[dict[str, Any]]:
        """行动项提取 — 从会议纪要/SOP 提取 TODO 项。

        Args:
            doc: Document ORM 实例。

        Returns:
            行动项列表，每项含 assignee/deadline/content/priority。
        """
        content = self._get_content(doc)
        prompt = (
            "从以下文档中提取所有行动项（TODO），格式为 JSON 数组：\n"
            '[{"assignee": "负责人", "deadline": "YYYY-MM-DD", '
            '"content": "行动内容", "priority": "high/medium/low"}]\n\n'
            f"{content[:3000]}\n\n"
            "只输出 JSON，不要额外解释。"
        )
        response = await self._llm_generate(prompt, max_tokens=500)
        actions_data = self._parse_json(response, default=[])

        saved: list[dict[str, Any]] = []
        for item in actions_data:
            action = DocumentAction(
                doc_id=doc.id,
                assignee=item.get("assignee"),
                deadline=self._parse_date(item.get("deadline")),
                content=item.get("content", ""),
                priority=item.get("priority", "medium"),
                status="pending",
            )
            self.db.add(action)
            saved.append({
                "assignee": action.assignee,
                "deadline": str(action.deadline) if action.deadline else None,
                "content": action.content,
                "priority": action.priority,
            })
        await self.db.flush()
        logger.info(
            "doc_intelligence.actions",
            doc_id=str(doc.id),
            count=len(saved),
        )
        return saved

    async def auto_generate_faq(self, doc: Document) -> list[dict[str, str]]:
        """FAQ 自动生成 — 从文档内容生成问答对。

        Args:
            doc: Document ORM 实例。

        Returns:
            问答对列表，每项含 question/answer。
        """
        content = self._get_content(doc)
        prompt = (
            "从以下文档中生成 3 个常见问答对，格式为 JSON 数组：\n"
            '[{"question": "问题", "answer": "答案"}]\n\n'
            f"{content[:3000]}\n\n"
            "只输出 JSON。"
        )
        response = await self._llm_generate(prompt, max_tokens=600)
        faqs = self._parse_json(response, default=[])
        logger.info("doc_intelligence.faq", doc_id=str(doc.id), count=len(faqs))
        return faqs

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _llm_generate(self, prompt: str, max_tokens: int = 500) -> str:
        """调用 LLM 生成文本（非流式）。

        LLM 不可用时返回空字符串，不抛异常。
        """
        try:
            result = ""
            async for chunk in self.llm.chat(
                [Message(role="system", content=prompt)],
                stream=False,
                max_tokens=max_tokens,
            ):
                if isinstance(chunk, str):
                    result += chunk
            return result.strip()
        except Exception as exc:
            logger.warning("doc_intelligence.llm_error", error=str(exc))
            return ""

    async def _safe_run(self, name: str, func, doc: Document) -> tuple[str, Any]:
        """安全执行单项处理 — 异常不传播，返回默认值。"""
        try:
            result = await func(doc)
            return name, result
        except Exception as exc:
            logger.warning(
                "doc_intelligence.safe_run_error",
                name=name,
                error=str(exc),
            )
            return name, None

    async def _get_doc(self, doc_uuid) -> Document | None:
        """获取文档 ORM 实例（含 knowledge_base 关联）。"""
        from sqlalchemy.orm import selectinload

        result = await self.db.execute(
            select(Document)
            .where(Document.id == doc_uuid)
            .options(selectinload(Document.knowledge_base))
        )
        return result.scalars().first()

    @staticmethod
    def _get_content(doc: Document) -> str:
        """获取文档纯文本内容。"""
        return doc.content_text or doc.content_html or ""

    @staticmethod
    def _parse_uuid(doc_id: str):
        import uuid as uuid_mod

        return uuid_mod.UUID(doc_id)

    @staticmethod
    def _parse_json(text: str, default: Any) -> Any:
        """安全解析 JSON — 失败时返回默认值。"""
        try:
            # 去除可能的 markdown 代码块标记
            clean = text.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:-1])
            return json.loads(clean)
        except (json.JSONDecodeError, IndexError):
            logger.warning("doc_intelligence.json_parse_error", text=text[:100])
            return default

    @staticmethod
    def _parse_date(date_str: str | None):
        """安全解析日期字符串。"""
        if not date_str:
            return None
        try:
            from datetime import datetime

            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
