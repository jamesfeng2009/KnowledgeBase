"""多轮对话独立化改写器 — 产出可独立检索的标准 Q。

与 CoreferenceResolver 的差异：
    - CoreferenceResolver 服务「当下检索」，产出查询词（补全省略句，仍是问句）；
    - StandaloneQueryRewriter 服务「知识沉淀」，产出自包含标准 Q（脱离上下文仍可理解、
      可被未来独立检索），用于 FAQ 入库。

三步改写：
    1. 指代具化：复用 CoreferenceResolver，把「它/这个/那」→具体实体；
    2. 上下文合并：复用 TopicTracker 提取焦点，把话题主体注入（如「差旅报销」→
       「公司差旅管理办法」），使 Q 自包含；
    3. 自包含校验：LLM 校验改写后的 Q 脱离上下文是否仍可理解，不可则重写。

降级策略：
    - LLM 不可用 → 返回 CoreferenceResolver 消解后的查询（第 1 步结果）；
    - 改写后 Q 长度 < 5 字 → 视为改写失败，返回消解后查询；
    - 改写后 Q 与原查询相似度 > 0.95 → 无需独立化，直接用原查询。

使用方式::

    rewriter = StandaloneQueryRewriter(llm, CoreferenceResolver(llm), TopicTracker(llm))
    standalone_q = await rewriter.rewrite(
        current_query="那国际航班呢？",
        history=[{"role": "user", "content": "公司差旅报销标准？"},
                 {"role": "assistant", "content": "经济舱按实报销..."}],
    )
    # standalone_q = "公司差旅管理办法中，国际航班的报销标准是什么？"
"""
from __future__ import annotations

from typing import Any

from app.context.coreference_resolver import CoreferenceResolver
from app.context.focus_tracker import ConversationFocus, TopicTracker
from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


class StandaloneQueryRewriter:
    """多轮对话独立化改写器 — 产出可独立检索的标准 Q。

    使用方式::

        rewriter = StandaloneQueryRewriter(llm, CoreferenceResolver(llm), TopicTracker(llm))
        standalone_q = await rewriter.rewrite(current_query, history)
    """

    _REWRITE_PROMPT: str = (
        "你是企业知识库 FAQ 标准化引擎。将一个多轮对话中的用户提问，"
        "改写为自包含、可独立检索的标准问题，使其脱离对话上下文仍可理解。\n\n"
        "规则：\n"
        "1. 把指代词具化为具体实体（「它/这个/那个」→具体名称）；\n"
        "2. 把省略的话题主体补全（焦点主题 + 当前提问 → 完整问题）；\n"
        "3. 保留原意，不增加未提及的信息；\n"
        "4. 产出的问题必须是完整的疑问句，可被独立检索；\n"
        "5. 如果输入已经是自包含的完整问题，原样返回。\n\n"
        "对话历史（最近几轮）：\n{history}\n\n"
        "对话焦点：{focus}\n"
        "已消解的查询：{resolved}\n\n"
        "改写后的标准问题（只输出问题本身，不要解释）："
    )

    _MIN_Q_LENGTH: int = 5  # 改写后 Q 最小长度，低于此视为失败
    _MAX_Q_LENGTH: int = 200  # 改写后 Q 最大长度，超过则截断或退化
    _HISTORY_TURNS: int = 6  # 注入 LLM 的历史轮数

    def __init__(
        self,
        llm: LLMProvider | None = None,
        coreference_resolver: CoreferenceResolver | None = None,
        topic_tracker: TopicTracker | None = None,
    ) -> None:
        """初始化独立化改写器。

        Args:
            llm: LLM Provider，为 None 时只做指代消解，不做独立化改写。
            coreference_resolver: 指代消解器，为 None 时内部创建。
            topic_tracker: 焦点追踪器，为 None 时内部创建。
        """
        self._llm = llm
        self._resolver = coreference_resolver or CoreferenceResolver(llm)
        self._tracker = topic_tracker or TopicTracker(llm)

    async def rewrite(
        self,
        current_query: str,
        history: list[dict[str, str]] | None = None,
        focus_stack: list[ConversationFocus] | None = None,
    ) -> str:
        """把多轮对话中的当前提问改写为独立标准 Q。

        Args:
            current_query: 当前轮用户提问（可能含指代/省略）。
            history: 对话历史 [{"role": "user/assistant", "content": "..."}]。
            focus_stack: 焦点历史栈（可选，支持跨轮回溯）。

        Returns:
            独立化后的标准 Q；LLM 不可用或改写失败时返回消解后的查询（降级）。
        """
        current_query = (current_query or "").strip()
        if not current_query:
            return current_query

        history = history or []

        # Step 1: 焦点提取（复用 TopicTracker）
        focus = await self._tracker.extract_focus(history)

        # Step 2: 指代消解（复用 CoreferenceResolver）
        resolved = await self._resolver.resolve(
            query=current_query,
            focus=focus,
            history=history,
            focus_stack=focus_stack,
        )
        resolved = (resolved or "").strip()

        # 单轮对话或无需独立化 → 直接返回消解结果
        if len(history) < 2:
            return resolved or current_query

        # Step 3: 独立化改写（LLM）
        if self._llm is None:
            # LLM 不可用 → 返回消解结果（降级）
            log.info("standalone_rewriter.llm_unavailable_use_resolved")
            return resolved or current_query

        try:
            standalone = await self._llm_rewrite(
                resolved=resolved,
                focus=focus,
                history=history,
            )
            standalone = self._sanitize(standalone)

            # 校验：改写后 Q 太短 → 视为失败，退化
            if len(standalone) < self._MIN_Q_LENGTH:
                log.info(
                    "standalone_rewriter.too_short_degraded",
                    resolved=resolved[:80],
                    standalone=standalone[:80],
                )
                return resolved or current_query

            # 校验：改写后 Q 与消解结果几乎相同 → 无需独立化
            if self._similarity(standalone, resolved) > 0.95:
                log.info("standalone_rewriter.no_change_needed")
                return resolved or current_query

            log.info(
                "standalone_rewriter.rewritten",
                original=current_query[:80],
                resolved=resolved[:80],
                standalone=standalone[:80],
            )
            return standalone
        except Exception as exc:
            log.warning(
                "standalone_rewriter.rewrite_failed",
                error=str(exc)[:200],
            )
            return resolved or current_query

    async def _llm_rewrite(
        self,
        resolved: str,
        focus: ConversationFocus | None,
        history: list[dict[str, str]],
    ) -> str:
        """调用 LLM 把消解后的查询改写为独立标准 Q。"""
        # 构建历史文本（最近 N 轮）
        history_text = "（无）"
        if history:
            recent = history[-self._HISTORY_TURNS:]
            history_text = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')[:150]}"
                for m in recent
            )

        focus_text = focus.to_context_str() if focus else "（无）"

        prompt = self._REWRITE_PROMPT.format(
            history=history_text,
            focus=focus_text,
            resolved=resolved[:500],
        )

        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=200):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    @staticmethod
    def _sanitize(text: str) -> str:
        """清理 LLM 输出 — 去除引号、换行、前后空白。"""
        if not text:
            return ""
        cleaned = text.strip()
        # 去除包裹引号
        for q in ('"', '"', '"', "'", "「", "」", "“", "”"):
            cleaned = cleaned.strip(q)
        # 去除可能的换行
        cleaned = " ".join(cleaned.split())
        return cleaned

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """计算两字符串的简单相似度（字符级 Jaccard）。

        用于判断改写后 Q 是否与原查询几乎相同（无需独立化）。
        零依赖，避免引入额外的相似度库。
        """
        if not a or not b:
            return 0.0
        set_a = set(a)
        set_b = set(b)
        if not set_a and not set_b:
            return 1.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0
