"""
Deep Research 服务 — P2-11：目标拆解 → 子课题证据卡片 → 矛盾检测 → 结构化报告。

面向"课题调研"类长程任务（蚂蚁 #20 / 组织洞察与风险识别方向）：

    1. 课题分解：LLM 将研究目标拆为 2-5 个子课题（失败回退单课题）；
    2. 证据卡片：每个子课题独立检索 + LLM 归纳结论，产出 EvidenceCard
       （结论 / 引用 / 置信度 / 状态）；
    3. 矛盾检测：跨课题结论两两过 ContradictionDetector（复用文档间矛盾检测）；
    4. 结构化报告：按 confirmed / uncertain / gap 三档分级输出 + 矛盾清单。

断点恢复（P2-13 复用）：传入 checkpoint_manager + task_id 时，
逐课题取证通过 run_stages_with_milestones 执行，失败重试跳过已完成课题。

遵循优雅降级：LLM 不可用时回退规则摘要；检索为空产出 gap 卡片而非失败。
遵循依赖倒置：llm / retriever / detector 均构造注入，可替换 Mock。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 置信度达到该阈值视为 confirmed，否则 uncertain
_CONFIRMED_CONFIDENCE_THRESHOLD: float = 0.7
#: 课题分解的子课题数量上下限
_MIN_TOPICS: int = 2
_MAX_TOPICS: int = 5
#: 每个子课题检索的文档数
_TOPIC_RETRIEVE_TOP_K: int = 5
#: 喂给 LLM 的单文档内容截断长度（控 token）
_DOC_SNIPPET_CHARS: int = 800


@dataclass
class EvidenceCard:
    """证据卡片 — 单个子课题的调研产出。

    Attributes:
        topic: 子课题
        conclusion: LLM 归纳结论（gap 时为空串）
        citations: 引用列表 [{doc_id, title, snippet, score}]
        confidence: 置信度 0.0-1.0
        status: confirmed / uncertain / gap
    """

    topic: str
    conclusion: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "gap"

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "conclusion": self.conclusion,
            "citations": self.citations,
            "confidence": self.confidence,
            "status": self.status,
        }


@dataclass
class ResearchReport:
    """结构化研究报告。

    Attributes:
        goal: 研究目标
        topics: 子课题列表
        cards: 证据卡片（与 topics 对应）
        contradictions: 跨课题矛盾清单 [{topic_a, topic_b, description, severity}]
        summary: 总体摘要
        confidence_distribution: 三档计数 {"confirmed": n, "uncertain": n, "gap": n}
    """

    goal: str
    topics: list[str]
    cards: list[EvidenceCard]
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    confidence_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "topics": self.topics,
            "cards": [c.to_dict() for c in self.cards],
            "contradictions": self.contradictions,
            "summary": self.summary,
            "confidence_distribution": self.confidence_distribution,
        }


class DeepResearchService:
    """Deep Research 服务 — 课题调研全流程编排。

    用法::

        service = DeepResearchService(llm, retriever)
        report = await service.research("调研公司报销制度的合规风险")
        # report.cards → 各子课题证据卡片
        # report.confidence_distribution → 确定/存疑/缺口分布
    """

    _DECOMPOSE_PROMPT = (
        "你是研究课题规划专家。将研究目标拆解为 {min_topics}-{max_topics} 个"
        "相对独立、可分别检索调研的子课题。\n"
        "要求：\n"
        "1. 每个子课题独占一行\n"
        "2. 只输出子课题名称，不要编号或解释\n"
        "3. 子课题合起来应完整覆盖研究目标\n\n"
        "研究目标：{goal}\n\n"
        "子课题："
    )

    _CONCLUDE_PROMPT = (
        "你是研究分析专家。基于以下知识库文档，针对子课题归纳结论。\n"
        "要求：\n"
        "1. 结论必须严格基于文档内容，不得编造\n"
        "2. 输出 JSON："
        '{{"conclusion": "100字内结论", "confidence": 0.0-1.0}}\n'
        "3. confidence 反映文档对子课题的支撑程度（信息少/间接则给低分）\n"
        "4. 只输出 JSON\n\n"
        "子课题：{topic}\n\n"
        "知识库文档：\n{context}\n\n"
        "分析结果："
    )

    _SUMMARY_PROMPT = (
        "你是研究报告撰写专家。基于各子课题的结论，为研究目标写一段"
        "150字内的总体摘要，点明主要发现、矛盾点与信息缺口。\n\n"
        "研究目标：{goal}\n\n"
        "子课题结论：\n{conclusions}\n\n"
        "总体摘要："
    )

    def __init__(
        self,
        llm: LLMProvider,
        retriever: Any,
        contradiction_detector: Any = None,
    ) -> None:
        """
        Args:
            llm: LLM Provider
            retriever: 混合检索器（需有 async search(query, kb_ids, top_k)）
            contradiction_detector: 矛盾检测器；None 时自动尝试创建
        """
        self._llm = llm
        self._retriever = retriever
        if contradiction_detector is not None:
            self._detector = contradiction_detector
        else:
            try:
                from app.context.contradiction_detector import ContradictionDetector

                self._detector = ContradictionDetector(llm)
            except Exception as exc:
                log.warning("deep_research.detector_init_failed", error=str(exc))
                self._detector = None

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 收集全部文本 chunk。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks).strip()

    # ------------------------------------------------------------------
    # 1. 课题分解
    # ------------------------------------------------------------------

    async def _decompose_goal(self, goal: str) -> list[str]:
        """LLM 拆解研究目标为子课题；失败回退 [goal]。"""
        try:
            text = await self._call_llm(
                self._DECOMPOSE_PROMPT.format(
                    goal=goal,
                    min_topics=_MIN_TOPICS,
                    max_topics=_MAX_TOPICS,
                )
            )
            topics = [
                line.strip().lstrip("0123456789.、- ")
                for line in text.split("\n")
                if line.strip()
            ]
            topics = [t for t in topics if t and t != goal][:_MAX_TOPICS]
            if len(topics) >= _MIN_TOPICS:
                log.info(
                    "deep_research.decomposed", goal=goal[:80], topics=len(topics)
                )
                return topics
        except Exception as exc:
            log.warning("deep_research.decompose_failed", error=str(exc))
        return [goal]

    # ------------------------------------------------------------------
    # 2. 逐课题取证（证据卡片）
    # ------------------------------------------------------------------

    async def _gather_evidence(
        self, topic: str, kb_ids: list[str] | None
    ) -> EvidenceCard:
        """对单个子课题检索 + 归纳，产出证据卡片。"""
        docs = await self._retriever.search(
            topic, kb_ids=kb_ids, top_k=_TOPIC_RETRIEVE_TOP_K
        )
        if not docs:
            log.info("deep_research.topic_gap", topic=topic[:80])
            return EvidenceCard(topic=topic, status="gap")

        citations = [
            {
                "doc_id": d.get("doc_id") or d.get("id", ""),
                "title": (d.get("metadata") or {}).get("title", ""),
                "snippet": (d.get("content") or "")[:200],
                "score": float(d.get("score", 0.0)),
            }
            for d in docs
        ]

        context = "\n---\n".join(
            (d.get("content") or "")[:_DOC_SNIPPET_CHARS] for d in docs
        )
        try:
            raw = await self._call_llm(
                self._CONCLUDE_PROMPT.format(topic=topic, context=context)
            )
            parsed = self._parse_json_object(raw)
            conclusion = str(parsed.get("conclusion", "")).strip()
            confidence = float(parsed.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except Exception as exc:
            log.warning(
                "deep_research.conclude_failed", topic=topic[:80], error=str(exc)
            )
            conclusion, confidence = "", 0.0

        if not conclusion:
            status = "gap"
        elif confidence >= _CONFIRMED_CONFIDENCE_THRESHOLD:
            status = "confirmed"
        else:
            status = "uncertain"

        return EvidenceCard(
            topic=topic,
            conclusion=conclusion,
            citations=citations,
            confidence=round(confidence, 3),
            status=status,
        )

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        """从 LLM 输出中提取 JSON 对象（容忍首尾杂讯）。"""
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"no JSON object in LLM output: {raw[:120]!r}")
        data = json.loads(raw[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("LLM output JSON is not an object")
        return data

    # ------------------------------------------------------------------
    # 3. 跨课题矛盾检测
    # ------------------------------------------------------------------

    async def _detect_contradictions(
        self, cards: list[EvidenceCard]
    ) -> list[dict[str, Any]]:
        """两两比对非空结论的证据卡片，检测跨课题矛盾。"""
        if self._detector is None:
            return []
        contradictions: list[dict[str, Any]] = []
        concluded = [c for c in cards if c.conclusion]
        for i in range(len(concluded)):
            for j in range(i + 1, len(concluded)):
                a, b = concluded[i], concluded[j]
                try:
                    result = await self._detector.check_doc_contradiction(
                        a.conclusion, b.conclusion
                    )
                except Exception as exc:
                    log.warning(
                        "deep_research.contradiction_check_failed",
                        error=str(exc),
                    )
                    continue
                if result.has_contradiction:
                    contradictions.append(
                        {
                            "topic_a": a.topic,
                            "topic_b": b.topic,
                            "description": result.description,
                            "severity": result.severity,
                        }
                    )
        return contradictions

    # ------------------------------------------------------------------
    # 4. 汇总报告
    # ------------------------------------------------------------------

    async def _summarize(self, goal: str, cards: list[EvidenceCard]) -> str:
        """LLM 生成总体摘要；失败回退规则拼接。"""
        concluded = [c for c in cards if c.conclusion]
        if not concluded:
            return "所有子课题均未在知识库中找到有效信息，存在全面信息缺口。"
        conclusions_text = "\n".join(
            f"- [{c.status}] {c.topic}：{c.conclusion}" for c in concluded
        )
        try:
            return await self._call_llm(
                self._SUMMARY_PROMPT.format(goal=goal, conclusions=conclusions_text)
            )
        except Exception as exc:
            log.warning("deep_research.summary_failed", error=str(exc))
            return conclusions_text

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def research(
        self,
        goal: str,
        kb_ids: list[str] | None = None,
        checkpoint_manager: Any = None,
        task_id: str | None = None,
    ) -> ResearchReport:
        """执行课题调研，返回结构化报告。

        Args:
            goal: 研究目标
            kb_ids: 限定知识库范围
            checkpoint_manager: 可选，注入后逐课题取证走里程碑 checkpoint
            task_id: 可选，里程碑 checkpoint key（Celery 任务 ID）
        """
        topics = await self._decompose_goal(goal)

        # 逐课题取证 — 有 checkpoint 时按里程碑执行（P2-13 断点恢复）；
        # 阶段返回可 JSON 序列化的 dict，被跳过阶段的结果由里程碑还原
        if checkpoint_manager is not None and task_id:
            from tasks.milestone_runner import (
                MilestoneStage,
                run_stages_with_milestones,
            )

            async def _gather_dict(topic: str) -> dict[str, Any]:
                return (await self._gather_evidence(topic, kb_ids)).to_dict()

            stage_results = await run_stages_with_milestones(
                [
                    MilestoneStage(
                        f"topic_{i}",
                        lambda t=topic: _gather_dict(t),
                    )
                    for i, topic in enumerate(topics)
                ],
                task_id=task_id,
                checkpoint_manager=checkpoint_manager,
            )
            cards = [
                EvidenceCard(**stage_results[f"topic_{i}"])
                for i in range(len(topics))
            ]
        else:
            cards = [await self._gather_evidence(t, kb_ids) for t in topics]

        contradictions = await self._detect_contradictions(cards)
        summary = await self._summarize(goal, cards)
        distribution = {
            "confirmed": sum(1 for c in cards if c.status == "confirmed"),
            "uncertain": sum(1 for c in cards if c.status == "uncertain"),
            "gap": sum(1 for c in cards if c.status == "gap"),
        }

        report = ResearchReport(
            goal=goal,
            topics=topics,
            cards=cards,
            contradictions=contradictions,
            summary=summary,
            confidence_distribution=distribution,
        )
        log.info(
            "deep_research.complete",
            goal=goal[:80],
            topics=len(topics),
            contradictions=len(contradictions),
            distribution=distribution,
        )
        return report
