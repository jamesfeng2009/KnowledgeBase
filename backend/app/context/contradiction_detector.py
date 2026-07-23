"""
矛盾检测器 — 检测对话中的矛盾信息。

三种检测场景：
    1. 用户陈述矛盾：用户当前陈述与历史陈述矛盾（prepare_chat 阶段）
    2. 回答-知识库矛盾：AI 回答与检索文档矛盾（_reflect 阶段）
    3. 文档间矛盾：检索结果中文档互相矛盾（_retrieve 后）

设计要点：
    - 共同实体预筛：先检查是否有共同实体，无则跳过 LLM 调用（省 Token）
    - 1 次轻量 LLM 调用（max_tokens=150）
    - LLM 不可用时跳过检测，不阻断对话

遵循单一职责：本模块只负责矛盾检测，不做焦点追踪或漂移检测。
遵循优雅降级：LLM 不可用时跳过，返回无矛盾结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ContradictionResult:
    """矛盾检测结果。

    Attributes:
        has_contradiction: 是否检测到矛盾。
        contradiction_type: 矛盾类型 "user_statement" / "answer_vs_kb" / "doc_vs_doc"。
        description: 矛盾描述。
        conflicting_sources: 冲突来源列表。
        severity: 严重程度 "high" / "medium" / "low"。
        action: 建议动作 "warn" / "block" / "flag"。
    """

    has_contradiction: bool
    contradiction_type: str = ""
    description: str = ""
    conflicting_sources: list[str] = field(default_factory=list)
    severity: str = "low"
    action: str = "flag"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "has_contradiction": self.has_contradiction,
            "contradiction_type": self.contradiction_type,
            "description": self.description,
            "conflicting_sources": self.conflicting_sources,
            "severity": self.severity,
            "action": self.action,
        }


class ContradictionDetector:
    """矛盾检测器 — 检测对话中的矛盾信息。

    使用方式::

        detector = ContradictionDetector(llm)
        result = await detector.check_user_contradiction(query, history)
        if result.has_contradiction:
            # SSE 推送矛盾警告
            ...
    """

    #: 用户矛盾检测的 LLM prompt
    _USER_CONTRADICTION_PROMPT: str = (
        "你是对话矛盾检测专家。判断用户当前陈述是否与之前的陈述矛盾。\n\n"
        "规则：\n"
        "1. 如果当前陈述与历史陈述矛盾，输出 JSON: "
        '{{"contradiction": true, "description": "矛盾描述", "severity": "high/medium/low"}}\n'
        "2. 如果一致或无关，输出 JSON: "
        '{{"contradiction": false, "description": "", "severity": "low"}}\n'
        "3. 只输出 JSON，不要额外解释\n\n"
        "对话历史（最近3轮）：\n{history}\n\n"
        "当前陈述：{query}\n\n"
        "判断结果："
    )

    #: 回答-知识库矛盾检测的 LLM prompt
    _ANSWER_CONSISTENCY_PROMPT: str = (
        "你是回答一致性检测专家。判断 AI 回答是否与检索到的知识库文档矛盾。\n\n"
        "规则：\n"
        "1. 如果回答与文档内容矛盾，输出 JSON: "
        '{{"contradiction": true, "description": "矛盾描述", "severity": "high"}}\n'
        "2. 如果一致或文档无相关内容，输出 JSON: "
        '{{"contradiction": false, "description": "", "severity": "low"}}\n'
        "3. 只输出 JSON\n\n"
        "知识库文档：\n{context}\n\n"
        "AI 回答：{answer}\n\n"
        "判断结果："
    )

    #: 文档间矛盾检测的 LLM prompt
    _DOC_CONTRADICTION_PROMPT: str = (
        "你是文档矛盾检测专家。判断以下两段文档内容是否互相矛盾。\n\n"
        "规则：\n"
        "1. 如果互相矛盾，输出 JSON: "
        '{{"contradiction": true, "description": "矛盾描述", "severity": "medium"}}\n'
        "2. 如果一致或无关，输出 JSON: "
        '{{"contradiction": false, "description": "", "severity": "low"}}\n'
        "3. 只输出 JSON\n\n"
        "文档A：{doc_a}\n\n"
        "文档B：{doc_b}\n\n"
        "判断结果："
    )

    #: 历史预览字符数
    _HISTORY_PREVIEW_CHARS: int = 200

    def __init__(self, llm: LLMProvider | None = None) -> None:
        """初始化矛盾检测器。

        Args:
            llm: LLM Provider，为 None 时跳过所有 LLM 检测。
        """
        self._llm = llm

    async def check_user_contradiction(
        self,
        query: str,
        history: list[dict[str, str]],
    ) -> ContradictionResult:
        """检测用户当前陈述是否与历史陈述矛盾。

        Args:
            query: 当前用户查询。
            history: 对话历史 [{"role": "user/assistant", "content": "..."}]。

        Returns:
            ContradictionResult: 矛盾检测结果。
        """
        # 历史不足 → 无法检测
        user_msgs = [m for m in history if m.get("role") == "user"]
        # 如果当前 query 已在历史末尾，排除它（比较的是之前陈述）
        if user_msgs and user_msgs[-1].get("content", "") == query:
            user_msgs = user_msgs[:-1]
        if len(user_msgs) < 1:
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="user_statement",
            )

        # 共同实体预筛 — 无共同实体则跳过 LLM
        if not self._has_common_entities(query, user_msgs[-1].get("content", "")):
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="user_statement",
            )

        if self._llm is None:
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="user_statement",
            )

        # LLM 检测
        history_text = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')[:self._HISTORY_PREVIEW_CHARS]}"
            for m in history[-6:]
        )
        prompt = self._USER_CONTRADICTION_PROMPT.format(
            history=history_text, query=query,
        )
        try:
            result = await self._call_llm_json(prompt)
            return ContradictionResult(
                has_contradiction=result.get("contradiction", False),
                contradiction_type="user_statement",
                description=result.get("description", ""),
                severity=result.get("severity", "medium"),
                action="warn" if result.get("contradiction") else "flag",
            )
        except Exception as exc:
            log.warning("contradiction.user_check_failed", error=str(exc))
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="user_statement",
            )

    async def check_answer_consistency(
        self,
        answer: str,
        retrieved_docs: list[dict[str, Any]],
    ) -> ContradictionResult:
        """检测 AI 回答是否与检索文档矛盾。

        Args:
            answer: AI 生成的回答。
            retrieved_docs: 检索到的文档列表。

        Returns:
            ContradictionResult: 矛盾检测结果。
        """
        if not retrieved_docs or not answer:
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="answer_vs_kb",
            )

        if self._llm is None:
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="answer_vs_kb",
            )

        # 拼接文档上下文（最多 3 篇，每篇截断）
        context_parts = []
        for doc in retrieved_docs[:3]:
            content = doc.get("content", doc.get("text", ""))
            context_parts.append(content[:500] if content else "")
        context = "\n---\n".join(context_parts)

        if not context.strip():
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="answer_vs_kb",
            )

        prompt = self._ANSWER_CONSISTENCY_PROMPT.format(
            context=context, answer=answer[:1000],
        )
        try:
            result = await self._call_llm_json(prompt)
            sources = [
                doc.get("doc_id", doc.get("id", "unknown"))
                for doc in retrieved_docs[:3]
            ]
            return ContradictionResult(
                has_contradiction=result.get("contradiction", False),
                contradiction_type="answer_vs_kb",
                description=result.get("description", ""),
                conflicting_sources=sources if result.get("contradiction") else [],
                severity=result.get("severity", "high"),
                action="block" if result.get("contradiction") else "flag",
            )
        except Exception as exc:
            log.warning("contradiction.answer_check_failed", error=str(exc))
            return ContradictionResult(
                has_contradiction=False,
                contradiction_type="answer_vs_kb",
            )

    async def check_doc_contradiction(
        self,
        retrieved_docs: list[dict[str, Any]],
    ) -> list[ContradictionResult]:
        """检测检索结果中文档间是否互相矛盾。

        Args:
            retrieved_docs: 检索到的文档列表。

        Returns:
            list[ContradictionResult]: 矛盾列表（仅包含有矛盾的结果）。
        """
        if len(retrieved_docs) < 2:
            return []

        if self._llm is None:
            return []

        results: list[ContradictionResult] = []
        # 两两比对（最多前 5 篇，避免 O(n²) 爆炸）
        docs = retrieved_docs[:5]
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                doc_a = docs[i].get("content", docs[i].get("text", ""))[:500]
                doc_b = docs[j].get("content", docs[j].get("text", ""))[:500]
                if not doc_a or not doc_b:
                    continue

                prompt = self._DOC_CONTRADICTION_PROMPT.format(
                    doc_a=doc_a, doc_b=doc_b,
                )
                try:
                    result = await self._call_llm_json(prompt)
                    if result.get("contradiction"):
                        results.append(ContradictionResult(
                            has_contradiction=True,
                            contradiction_type="doc_vs_doc",
                            description=result.get("description", ""),
                            conflicting_sources=[
                                str(docs[i].get("doc_id", docs[i].get("id", "unknown"))),
                                str(docs[j].get("doc_id", docs[j].get("id", "unknown"))),
                            ],
                            severity=result.get("severity", "medium"),
                            action="flag",
                        ))
                except Exception as exc:
                    log.debug("contradiction.doc_check_pair_failed", error=str(exc))

        return results

    def _has_common_entities(self, text_a: str, text_b: str) -> bool:
        """共同实体预筛 — 检查两段文本是否有共同实体。

        使用 P2 EntityRegistry 进行实体识别。如果两段文本没有共同实体，
        则不太可能矛盾，跳过 LLM 调用。
        EntityRegistry 未注册相关实体时降级为关键词重叠检查。
        """
        try:
            from app.ontology.entity_registry import EntityRegistry

            _, entities_a = EntityRegistry.expand_query(text_a)
            _, entities_b = EntityRegistry.expand_query(text_b)
            if entities_a and entities_b:
                return bool(set(entities_a) & set(entities_b))
            # EntityRegistry 未识别到实体 → 降级为关键词重叠
        except Exception:
            pass

        # 关键词重叠降级 — 中文无空格，用字符 bigram
        def _char_bigrams(text: str) -> set[str]:
            """提取中文文本的字符 bigram（2-gram）。"""
            return {text[i : i + 2] for i in range(len(text) - 1)}

        bigrams_a = _char_bigrams(text_a.lower())
        bigrams_b = _char_bigrams(text_b.lower())
        return bool(bigrams_a & bigrams_b)

    async def _call_llm_json(self, prompt: str) -> dict[str, Any]:
        """调用 LLM 并解析 JSON 响应。"""
        import json

        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=150):
            if isinstance(chunk, str):
                chunks.append(chunk)
        text = "".join(chunks).strip()

        # 清理 markdown 代码块包裹
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # 尝试解析 JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start : end + 1])
            raise
