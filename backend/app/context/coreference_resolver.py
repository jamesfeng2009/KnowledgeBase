"""
指代消解器 — 将省略句补全为完整查询。

使用场景：用户说"那上海呢？"，焦点追踪器提供 {topic: "限号政策", entity: "北京"}，
本组件将省略句补全为"上海今天车辆限号多少？"。

设计要点：
    - 仅当查询为省略句时触发（检测省略特征词），避免无谓 LLM 调用
    - 1 次轻量 LLM 调用（max_tokens=100）
    - 失败时返回原始查询（优雅降级）

遵循单一职责：本模块只负责指代消解，不做焦点提取。
遵循优雅降级：LLM 不可用时返回原始查询，不阻断对话流程。
"""

from __future__ import annotations

from app.context.focus_tracker import ConversationFocus
from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


class CoreferenceResolver:
    """指代消解器 — 补全省略句。

    使用方式::

        resolver = CoreferenceResolver(llm)
        if resolver.needs_resolution("那上海呢？"):
            resolved = await resolver.resolve("那上海呢？", focus)
            # resolved = "上海今天车辆限号多少？"
    """

    # 省略句特征词 — 出现这些词时可能需要指代消解
    _ELLIPSIS_INDICATORS: list[str] = [
        "呢", "怎么样", "如何", "也是", "他", "她", "它",
        "这个", "那个", "上面", "刚才", "也是这样", "同样",
    ]

    # 明确动词 — 出现这些词时查询已完整，不需要消解
    _EXPLICIT_VERBS: list[str] = [
        "搜索", "查找", "查看", "创建", "上传", "提交", "列出",
        "search", "find", "view", "create", "upload", "list",
        "什么是", "为什么", "解释",
    ]

    _RESOLVE_PROMPT: str = (
        "你是对话指代消解专家。根据对话历史和焦点，将用户的省略句补全为完整查询。\n\n"
        "规则：\n"
        "1. 如果用户查询已经是完整句子，原样返回\n"
        "2. 如果是省略句，根据对话历史和焦点补全主语和谓语\n"
        "3. 只输出补全后的查询，不要包含解释\n\n"
        "对话历史（最近几轮）：\n{history}\n\n"
        "对话焦点：{focus}\n"
        "用户查询：{query}\n\n"
        "补全后的查询："
    )

    def __init__(self, llm: LLMProvider | None = None) -> None:
        """初始化指代消解器。

        Args:
            llm: LLM Provider，为 None 时只做检测不消解。
        """
        self._llm = llm

    def needs_resolution(self, query: str) -> bool:
        """检测查询是否可能需要指代消解。

        启发式规则：
        - 查询长度 < 30 字符（短句更可能省略）
        - 包含省略特征词
        - 不包含明确的动词

        Args:
            query: 用户查询文本。

        Returns:
            True 如果可能需要指代消解。
        """
        query_stripped = query.strip()
        if len(query_stripped) > 30:
            return False
        if len(query_stripped) < 2:
            return False

        has_indicator = any(ind in query_stripped for ind in self._ELLIPSIS_INDICATORS)
        has_explicit_verb = any(
            kw in query_stripped for kw in self._EXPLICIT_VERBS
        )
        return has_indicator and not has_explicit_verb

    async def resolve(
        self,
        query: str,
        focus: ConversationFocus | None,
        history: list[dict[str, str]] | None = None,
        focus_stack: list[ConversationFocus] | None = None,
    ) -> str:
        """指代消解 — 补全省略句。

        P4-C 增强：注入对话历史和焦点栈，支持多轮跨指代。

        Args:
            query: 用户原始查询。
            focus: 当前对话焦点（来自 TopicTracker）。
            history: 对话历史（P4-C 新增，可选）。注入 LLM prompt 提供上下文。
            focus_stack: 焦点历史栈（P4-C 新增，可选）。让 LLM 回溯多轮焦点。

        Returns:
            补全后的查询；无焦点或不需要消解时返回原始查询。
        """
        # 无焦点或不需消解 → 原样返回
        if focus is None or not self.needs_resolution(query):
            return query

        if self._llm is None:
            # 无 LLM 时做简单规则补全
            return self._rule_resolve(query, focus)

        # P4-C: 构建增强 prompt — 注入历史 + 焦点栈
        history_text = "（无）"
        if history:
            recent = history[-6:]
            history_text = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')[:100]}"
                for m in recent
            )

        focus_text = focus.to_context_str()
        if focus_stack:
            for i, f in enumerate(focus_stack[-3:]):
                focus_text += f"\n  轮{i}: 主题={f.topic}, 实体={f.entity}"

        prompt = self._RESOLVE_PROMPT.format(
            history=history_text,
            focus=focus_text,
            query=query,
        )
        try:
            resolved = await self._call_llm(prompt)
            # 清理 LLM 输出
            resolved = resolved.strip().strip('"').strip("'").strip("「」").strip("「").strip("」")
            if resolved and len(resolved) < 200:
                if resolved != query:
                    log.info(
                        "coreference.resolved",
                        original=query[:100],
                        resolved=resolved[:100],
                        focus=focus.to_context_str(),
                    )
                return resolved
            # LLM 返回空或过长 → 规则补全
            return self._rule_resolve(query, focus)
        except Exception as exc:
            log.warning("coreference.resolve_failed", error=str(exc))
            # LLM 异常 → 规则补全降级
            return self._rule_resolve(query, focus)

    def _rule_resolve(self, query: str, focus: ConversationFocus) -> str:
        """规则补全 — 无 LLM 时的简单策略。

        策略：将焦点主题 + 查询中的实体合并。
        例如：focus(限号政策, 北京) + "那上海呢？" → "上海限号政策"
        """
        query_stripped = query.strip()

        # 提取查询中可能的实体名（移除省略特征词）
        entity_in_query = query_stripped
        for indicator in self._ELLIPSIS_INDICATORS:
            entity_in_query = entity_in_query.replace(indicator, "")
        entity_in_query = entity_in_query.strip("？?！!，,。.")

        if entity_in_query and entity_in_query != focus.entity:
            return f"{entity_in_query}{focus.topic}"
        elif entity_in_query and entity_in_query == focus.entity:
            return query  # 实体相同，无需补全
        else:
            return query

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=100):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)
