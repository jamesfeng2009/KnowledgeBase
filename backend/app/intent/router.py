"""
意图路由器 — 单一职责：将用户自然语言解析为结构化意图，决定走快捷路径还是 Agent Loop。

稳态/敏态分离的核心入口：
    1. 规则匹配（零 Token）— 正则匹配常见意图模式
    2. LLM 兜底（1 次轻量调用）— 规则未命中时用 LLM 解析意图
    3. 兜底策略 — 仍未命中则走 Agent Loop（COMPLEX_QUERY）

遵循开闭原则：新增意图类型只需在 RuleMatcher 追加规则 + IntentType 追加枚举值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


class IntentType(str, Enum):
    """意图类型 — 决定走快捷路径还是 Agent Loop。"""

    RAG_SEARCH = "rag_search"            # 文档检索问答 → 快捷路径
    LIST_DOCUMENTS = "list_documents"    # 列出文档/知识库 → 快捷路径
    GET_DOCUMENT = "get_document"        # 查看文档详情 → 快捷路径
    CREATE_DOCUMENT = "create_document"  # 创建/上传文档 → Agent Loop（需 HITL）
    COMPLEX_QUERY = "complex_query"      # 复杂查询 → Agent Loop


# 可走快捷路径的意图集合（确定性检索 + 1 次 LLM 生成）
_SHORTCUT_INTENTS: frozenset[IntentType] = frozenset({
    IntentType.RAG_SEARCH,
    IntentType.LIST_DOCUMENTS,
    IntentType.GET_DOCUMENT,
})


@dataclass
class IntentResult:
    """意图路由结果。

    Attributes:
        intent: 识别到的意图类型。
        confidence: 置信度 [0.0, 1.0]。
        parameters: 意图参数（如搜索关键词、文档 ID 等）。
        use_shortcut: 是否走快捷路径（True=确定性检索+1次LLM，False=Agent Loop）。
    """

    intent: IntentType
    confidence: float
    parameters: dict[str, Any] = field(default_factory=dict)
    use_shortcut: bool = False


class IntentRouter:
    """意图路由器 — 规则优先，LLM 兜底。

    使用方式::

        router = IntentRouter(llm_provider=get_llm_provider())
        result = await router.route("帮我查一下报销流程", memory_context, "qa")
        if result.use_shortcut:
            # 走快捷路径
        else:
            # 走 Agent Loop
    """

    def __init__(self, llm_provider: Any | None = None) -> None:
        """初始化意图路由器。

        Args:
            llm_provider: LLM Provider 实例（LLM 兜底用），None 则不做 LLM 解析。
        """
        from app.intent.rule_matcher import RuleMatcher

        self._rule_matcher = RuleMatcher()
        self._llm_parser = None
        if llm_provider:
            try:
                from app.intent.llm_parser import LLMIntentParser

                self._llm_parser = LLMIntentParser(llm_provider)
            except Exception as exc:
                log.warning("intent_router.llm_parser_init_failed", error=str(exc))

    async def route(
        self,
        query: str,
        memory_context: str,
        agent_type: str,
    ) -> IntentResult:
        """路由用户查询到对应意图。

        决策流程：
            1. 规则匹配（零 Token）— 命中且置信度 ≥ 0.8 直接返回
            2. LLM 意图解析（仅规则未命中时）— 1 次轻量 LLM 调用
            3. 兜底 → COMPLEX_QUERY（走 Agent Loop）

        Args:
            query: 用户输入的自然语言查询。
            memory_context: 对话上下文（系统提示词 + 记忆 + 历史）。
            agent_type: Agent 类型（qa / workflow / action）。

        Returns:
            IntentResult: 路由结果，含意图类型和是否走快捷路径。
        """
        # 1. 规则匹配（零 Token）
        result = self._rule_matcher.match(query)
        if result and result.confidence >= 0.8:
            log.debug(
                "intent_router.rule_match",
                intent=result.intent.value,
                confidence=result.confidence,
            )
            return result

        # 2. LLM 意图解析（仅规则未命中时）
        if self._llm_parser:
            try:
                from app.config import get_settings

                settings = get_settings()
                if settings.INTENT_ROUTER_LLM_FALLBACK:
                    result = await self._llm_parser.parse(query, memory_context)
                    if result and result.confidence >= settings.INTENT_ROUTER_CONFIDENCE_THRESHOLD:
                        log.debug(
                            "intent_router.llm_match",
                            intent=result.intent.value,
                            confidence=result.confidence,
                        )
                        return result
            except Exception as exc:
                log.warning("intent_router.llm_parse_failed", error=str(exc))

        # 3. 兜底 → 走 Agent Loop
        log.debug("intent_router.fallback_to_complex")
        return IntentResult(
            intent=IntentType.COMPLEX_QUERY,
            confidence=0.0,
            use_shortcut=False,
        )
