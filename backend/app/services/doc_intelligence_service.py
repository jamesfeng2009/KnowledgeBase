"""
文档智能处理服务 — 单一职责：文档入库后 LLM 自动摘要/标签/分类/行动项/FAQ/约束。

遵循开闭原则：新增智能处理能力只需添加方法，不修改既有逻辑。
遵循优雅降级：LLM 不可用时跳过处理，不阻塞文档入库流程。

六项自动化能力：
    auto_summarize       — 200 字摘要
    auto_tag             — 3-5 个关键词标签
    auto_classify        — 文档分类
    extract_action_items — 会议纪要行动项提取
    auto_generate_faq    — 从文档生成问答对
    extract_constraints  — 约束条款两级打标（P2 · GAP-3，设计 §5）
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider, Message
from app.models.action import DocumentAction
from app.models.constraint import ConstraintRule
from app.models.knowledge import Document
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)

#: 文档分类选项
DOC_CATEGORIES = [
    "政策", "SOP", "技术文档", "会议纪要", "培训资料", "产品文档", "合同模板",
]

# --- P2 约束打标 · Stage A 正则预筛（设计 §5.1）---
# 约束性语言高置信模式：只做候选召回（高查准低查全），语义判断留给 Stage B。
# 允许漏检 — 漏检只损失"自动打标"，运营可手动 INSERT 补标。
_CONSTRAINT_PATTERNS: re.Pattern[str] = re.compile(
    r"(禁止|不得|严禁|不许|必须|务必|不得超过|不得低于|一律"
    r"|双签|会签|红线|高压线|问责|违规|审计要求|合规要求"
    r"|立即生效|废止|以本制度为准|最终解释权)"
)

#: severity 合法取值（与 constraint_rules DDL 一致）
_CONSTRAINT_SEVERITIES = frozenset({"block", "confirm", "warn"})

#: Stage B 单次调用打包的候选段数（控制单次输出长度）
_CONSTRAINT_BATCH_SIZE = 8

#: Stage A 段落粒度：过短的碎片跳过，过长的段落截断
_PARA_MIN_CHARS = 20
_PARA_MAX_CHARS = 1000


def _clean_str_list(value: Any, *, max_len: int) -> list[str]:
    """清洗 LLM 输出的字符串数组 — 去空/去重/限长。"""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:max_len]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _clean_normalized(value: Any, rule_text: str) -> dict[str, Any]:
    """清洗 normalized JSONB — 兜底 statement，白名单字段。"""
    normalized: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in (
            "statement",
            "condition",
            "required_mentions",
            "forbidden_patterns",
            "amount_limits",
        ):
            if key in value:
                normalized[key] = value[key]
    if not normalized.get("statement"):
        normalized["statement"] = rule_text[:200]
    return normalized


class DocIntelligenceService:
    """文档智能处理服务 — LLM 自动摘要/标签/分类/行动项/FAQ。"""

    def __init__(self, llm: LLMProvider, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.llm = llm
        self.db = db
        self._tenant_id = tenant_id

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

        # 并行执行摘要/标签/分类/约束打标（互不依赖）
        results: dict[str, Any] = {"doc_id": doc_id, "status": "success"}
        tasks = [
            self._safe_run("summary", self.auto_summarize, doc),
            self._safe_run("tags", self.auto_tag, doc),
            self._safe_run("category", self.auto_classify, doc),
            # P2: 约束条款两级打标（GAP-3）— 与摘要/分类同队列同降级策略
            self._safe_run("constraints", self.extract_constraints, doc),
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
    # P2 约束条款两级打标（GAP-3 · 设计 §5）
    # ------------------------------------------------------------------

    async def extract_constraints(self, doc: Document) -> list[dict[str, Any]]:
        """约束条款两级打标 — Stage A 正则预筛 + Stage B 轻量 LLM 结构化抽取。

        流程（设计 §5 图 3）：
            1. 版本链 retire：该文档的旧规则（active/pending_review）全部
               置 retired，新规则落库后回填 superseded_by（reindex 天然触发）。
            2. Stage A：段落粒度正则预筛，未命中段落零 LLM（长文档约 95% 免调用）。
            3. Stage B：命中的候选段打包送轻量模型（CONSTRAINT_MODEL →
               MEMORY_SIDECAR_MODEL → 主模型，sidecar 解析链）结构化抽取。
            4. 置信度分流：≥ CONSTRAINT_AUTO_CONFIDENCE 直接 active；
               [REVIEW, AUTO) 进 pending_review 人审队列（照常注入，安全优先）；
               < REVIEW 丢弃。
            5. 文档级粗标：抽到条款则 doc.doc_role=constraint_source。

        Args:
            doc: Document ORM 实例。

        Returns:
            落库的规则摘要列表（rule_id/severity/status/confidence）。
        """
        from app.config import get_settings

        settings = get_settings()
        if not settings.CONSTRAINT_CLASSIFIER_ENABLED:
            return []

        # 1. 版本链 retire — 旧条款软退休（禁 DELETE，审计可回放）
        old_rules = await self._retire_doc_rules(doc.id)

        # 2. Stage A 正则预筛
        content = self._get_content(doc)
        paragraphs = self._prefilter_paragraphs(
            content, limit=settings.CONSTRAINT_MAX_CANDIDATE_CHUNKS
        )
        if not paragraphs:
            await self._sync_doc_role(doc, old_rules)
            return []

        # 3. Stage B 轻量 LLM 结构化抽取（批量打包）
        llm = self._resolve_constraint_llm()
        extracted: list[dict[str, Any]] = []
        for start in range(0, len(paragraphs), _CONSTRAINT_BATCH_SIZE):
            batch = paragraphs[start : start + _CONSTRAINT_BATCH_SIZE]
            items = await self._extract_batch(llm, batch)
            extracted.extend(items)

        # 4. 置信度分流落库
        saved = await self._save_rules(doc, extracted, old_rules)
        await self._sync_doc_role(doc, old_rules, saved)

        logger.info(
            "doc_intelligence.constraints",
            doc_id=str(doc.id),
            candidates=len(paragraphs),
            extracted=len(extracted),
            saved=len(saved),
        )
        return [
            {
                "rule_id": str(r["rule"].id),
                "severity": r["rule"].severity,
                "status": r["rule"].status,
                "confidence": r["rule"].classifier_confidence,
            }
            for r in saved
        ]

    async def _retire_doc_rules(self, doc_id: UUID) -> list[ConstraintRule]:
        """文档旧规则软退休 — active/pending_review → retired。

        返回退休的旧规则（供新规则回填 superseded_by 版本链）。
        """
        stmt = select(ConstraintRule).where(
            ConstraintRule.document_id == doc_id,
            ConstraintRule.status.in_(["active", "pending_review"]),
        )
        rules = list((await self.db.execute(stmt)).scalars())
        for rule in rules:
            rule.status = "retired"
        if rules:
            await self.db.flush()
        return rules

    @staticmethod
    def _prefilter_paragraphs(content: str, *, limit: int) -> list[str]:
        """Stage A — 段落粒度正则预筛（零 LLM）。

        只做候选召回（高查准低查全），语义判断全部留给 Stage B。
        """
        paragraphs: list[str] = []
        for raw in re.split(r"\n\s*\n|\n", content):
            text = raw.strip()
            if len(text) < _PARA_MIN_CHARS:
                continue
            if not _CONSTRAINT_PATTERNS.search(text):
                continue
            paragraphs.append(text[:_PARA_MAX_CHARS])
            if len(paragraphs) >= limit:
                break
        return paragraphs

    @staticmethod
    def _resolve_constraint_llm() -> LLMProvider:
        """轻量抽取模型解析链 — CONSTRAINT_MODEL → MEMORY_SIDECAR_MODEL → 主模型。"""
        from app.config import get_settings
        from app.llm.factory import get_llm_provider_by_model

        settings = get_settings()
        for model_id in (settings.CONSTRAINT_MODEL, settings.MEMORY_SIDECAR_MODEL):
            if model_id:
                try:
                    return get_llm_provider_by_model(model_id)
                except Exception as exc:
                    logger.warning(
                        "doc_intelligence.constraint_llm_fallback",
                        model_id=model_id,
                        error=str(exc),
                    )
        return get_llm_provider()

    async def _extract_batch(
        self, llm: LLMProvider, batch: list[str]
    ) -> list[dict[str, Any]]:
        """Stage B — 一批候选段的轻量 LLM 结构化抽取（temperature=0 语义）。"""
        numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(batch))
        prompt = (
            "你是企业制度合规专家。以下是文档中的候选段落（带编号）。"
            "请判断每个段落是否包含约束性条款（必须遵守的业务规则、红线、"
            "审批要求、禁令），对包含约束条款的段落各输出一条结构化 JSON。\n\n"
            f"候选段落：\n{numbered}\n\n"
            "输出 JSON 数组（没有约束条款则输出 []），每项格式：\n"
            '{"index": 段落编号, "is_constraint": true, '
            '"rule_text": "约束条款原文（保留关键限定词）", '
            '"severity": "block|confirm|warn", '
            '"trigger_entities": ["条款涉及的业务实体词，如 报销/采购/审批"], '
            '"trigger_domains": ["finance|legal|security|hr 或其他域，可为空数组"], '
            '"confidence": 0.0到1.0的抽取置信度, '
            '"normalized": {"statement": "条款一句话概括", '
            '"required_mentions": ["答案必须提及的词"], '
            '"forbidden_patterns": ["答案禁止出现的词"], '
            '"amount_limits": [{"op": "gt", "value": 5000, "on_violation": "block"}]}}\n\n'
            "severity 判定标准：block=违反即红线（法律/资金/安全风险）；"
            "confirm=须人工确认的操作要求；warn=提醒性规范。\n"
            "普通描述、背景说明、非强制建议（\"建议\"\"可以\"）不是约束条款。\n"
            "只输出 JSON，不要解释。"
        )
        response = await self._llm_generate(prompt, max_tokens=1200, llm=llm)
        items = self._parse_json(response, default=[])
        if not isinstance(items, list):
            return []

        valid: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("is_constraint"):
                continue
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(batch):
                continue  # 段落映射失效 — 丢弃，防错位落库
            text = str(item.get("rule_text") or "").strip()
            severity = str(item.get("severity") or "").strip()
            if not text or severity not in _CONSTRAINT_SEVERITIES:
                continue
            try:
                confidence = float(item.get("confidence") or 0.0)
            except (ValueError, TypeError):
                confidence = 0.0
            valid.append(
                {
                    "index": index,
                    "rule_text": text,
                    "severity": severity,
                    "trigger_entities": _clean_str_list(
                        item.get("trigger_entities"), max_len=64
                    ),
                    "trigger_domains": _clean_str_list(
                        item.get("trigger_domains"), max_len=32
                    ),
                    "confidence": min(max(confidence, 0.0), 1.0),
                    "normalized": _clean_normalized(item.get("normalized"), text),
                }
            )
        return valid

    async def _save_rules(
        self,
        doc: Document,
        extracted: list[dict[str, Any]],
        old_rules: list[ConstraintRule],
    ) -> list[dict[str, Any]]:
        """置信度分流落库 — ≥AUTO active / [REVIEW,AUTO) pending_review / <REVIEW 丢弃。

        Returns:
            [{"rule": ORM 实例, "para_index": 段落编号}]。
        """
        from app.config import get_settings

        settings = get_settings()
        # 同段落重复抽取去重（LLM 偶发对一段输出多条）
        seen_para: set[int] = set()
        saved: list[dict[str, Any]] = []
        for item in extracted:
            if item["index"] in seen_para:
                continue
            confidence = item["confidence"]
            if confidence < settings.CONSTRAINT_REVIEW_CONFIDENCE:
                continue  # 低置信丢弃
            status = (
                "active"
                if confidence >= settings.CONSTRAINT_AUTO_CONFIDENCE
                else "pending_review"
            )
            rule = ConstraintRule(
                kb_id=doc.kb_id,
                document_id=doc.id,
                chunk_id=f"{doc.id}:para:{item['index']}",
                rule_text=item["rule_text"],
                normalized=item["normalized"],
                severity=item["severity"],
                trigger_domains=item["trigger_domains"],
                trigger_entities=item["trigger_entities"],
                classifier_confidence=confidence,
                status=status,
            )
            self.db.add(rule)
            seen_para.add(item["index"])
            saved.append({"rule": rule, "para_index": item["index"]})
        await self.db.flush()

        # 版本链回填 — 旧规则指向本次重打标的新版本（审计回放）
        if old_rules and saved:
            successor = saved[0]["rule"]
            for old in old_rules:
                old.superseded_by = successor.id
            await self.db.flush()
        return saved

    async def _sync_doc_role(
        self,
        doc: Document,
        old_rules: list[ConstraintRule],
        saved: list[dict[str, Any]] | None = None,
    ) -> None:
        """文档级粗标同步 — 有在效规则则 constraint_source，否则 normal。"""
        has_rules = bool(saved) or any(
            r.status in ("active", "pending_review") for r in old_rules
        )
        role = "constraint_source" if has_rules else "normal"
        if doc.doc_role != role:
            doc.doc_role = role
            await self.db.flush()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _llm_generate(
        self, prompt: str, max_tokens: int = 500, llm: LLMProvider | None = None
    ) -> str:
        """调用 LLM 生成文本（非流式）。

        LLM 不可用时返回空字符串，不抛异常。
        llm 参数允许指定轻量 Provider（P2 约束打标走 sidecar 模型）。
        """
        try:
            result = ""
            async for chunk in (llm or self.llm).chat(
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

        stmt = (
            select(Document)
            .where(Document.id == doc_uuid)
            .options(selectinload(Document.knowledge_base))
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
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
