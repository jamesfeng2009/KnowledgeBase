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
    """意图类型 — 决定走快捷路径还是 Agent Loop。

    稳态/敏态分离的三类出口：
    - 快捷路径（_SHORTCUT_INTENTS）：确定性检索 + 1 次 LLM 生成；
    - 终态出口（_TERMINAL_INTENTS）：拒识（UNSUPPORTED）或澄清（UNCLEAR），
      直接返回结果，不进入检索与 Agent Loop；
    - Agent Loop：COMPLEX_QUERY / CREATE_DOCUMENT。
    """

    RAG_SEARCH = "rag_search"            # 文档检索问答 → 快捷路径
    LIST_DOCUMENTS = "list_documents"    # 列出文档/知识库 → 快捷路径
    GET_DOCUMENT = "get_document"        # 查看文档详情 → 快捷路径
    CREATE_DOCUMENT = "create_document"  # 创建/上传文档 → Agent Loop（需 HITL）
    COMPLEX_QUERY = "complex_query"      # 复杂查询 → Agent Loop
    UNSUPPORTED = "unsupported"          # 超出知识库服务范围 → 终态拒识
    UNCLEAR = "unclear"                  # 参数缺失/歧义 → 终态澄清


# 可走快捷路径的意图集合（确定性检索 + 1 次 LLM 生成）
_SHORTCUT_INTENTS: frozenset[IntentType] = frozenset({
    IntentType.RAG_SEARCH,
    IntentType.LIST_DOCUMENTS,
    IntentType.GET_DOCUMENT,
})

# 终态出口意图集合（拒识 + 澄清）— 不进入检索与 Agent Loop，
# 由快捷处理器直接返回 SSE 事件，防止越权/瞎猜回答。
_TERMINAL_INTENTS: frozenset[IntentType] = frozenset({
    IntentType.UNSUPPORTED,
    IntentType.UNCLEAR,
})


class SlotName:
    """Slot 槽位名常量 — 意图参数与澄清缺槽的统一命名。

    命名对齐 RAG 检索参数，便于前端按槽位渲染补全表单。
    """

    SEARCH_QUERY = "search_query"      # 检索主题（必填核心槽）
    TIME_RANGE = "time_range"          # 时间范围（软约束/可选槽）
    CLASSIFICATION = "classification"  # 密级上限（硬约束/可选槽）
    DOC_TYPE = "doc_type"              # 文档类型（软约束/可选槽）
    SOURCE = "source"                  # 来源/系统（软约束/可选槽）
    KB = "kb"                          # 限定知识库（硬约束/可选槽）


@dataclass
class IntentConstraints:
    """意图约束 — 硬/软约束分离。

    方案二核心：把"不要检索涉密文档"这类约束显式建模为结构化参数，
    贯穿整个检索链路强制执行，而不是靠 LLM 每次重新判断。

    Attributes:
        hard: 硬约束（必须满足，不满足即过滤）。支持的键：
            - classification_max: str — 密级上限（public/internal/confidential/secret）
            - exclude_classifications: list[str] — 排除的密级集合
            - kb_ids: list[str] — 限定知识库 ID 集合
            - mandatory_keywords: list[str] — 必须包含的关键词
        soft: 软约束（优先满足，作为生成提示偏好）。支持的键：
            - time_range: str — 时间范围偏好
            - doc_type: str — 文档类型偏好
            - source: str — 来源偏好
            - preferred_keywords: list[str] — 优先关键词
    """

    hard: dict[str, Any] = field(default_factory=dict)
    soft: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntentResult:
    """意图路由结果。

    Attributes:
        intent: 识别到的意图类型。
        confidence: 置信度 [0.0, 1.0]。
        parameters: 意图参数（如搜索关键词、文档 ID 等）。
        missing_slots: 缺失的槽位名列表（intent=UNCLEAR 时必须非空，
            用于澄清出口提示用户补充）。
        constraints: 结构化硬/软约束（方案二，检索链路强制执行）。
        use_shortcut: 是否走快捷路径（True=确定性检索+1次LLM，False=Agent Loop）。
    """

    intent: IntentType
    confidence: float
    parameters: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    constraints: IntentConstraints | None = None
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
