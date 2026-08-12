"""
LLM 意图解析器 — 单一职责：规则未命中时用 LLM 解析意图。

仅在 RuleMatcher 未命中时调用，使用 function calling 格式返回结构化意图。
max_tokens=100，确保轻量调用（约 100-200 Token 消耗）。

遵循优雅降级：LLM 不可用或解析失败时返回 None，由 IntentRouter 兜底。
"""

from __future__ import annotations

import json
from typing import Any

from app.intent.router import (
    IntentConstraints,
    IntentResult,
    IntentType,
    SlotName,
    _SHORTCUT_INTENTS,
    _TERMINAL_INTENTS,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# 硬约束支持的键 — 用于白名单校验 LLM 返回的 hard 字段，
# 防止 LLM 注入未知键导致下游误过滤。
_HARD_KEYS: frozenset[str] = frozenset({
    "classification_max",
    "exclude_classifications",
    "kb_ids",
    "mandatory_keywords",
})
# 软约束支持的键
_SOFT_KEYS: frozenset[str] = frozenset({
    "time_range",
    "doc_type",
    "source",
    "preferred_keywords",
})
# 合法密级值（对应 permission_service 密级权重表）
_CLASSIFICATION_VALUES: frozenset[str] = frozenset({
    "public", "internal", "confidential", "secret",
})

_SYSTEM_PROMPT = (
    "你是企业知识库的意图识别器。分析用户输入，返回 JSON：\n"
    '{"intent": "rag_search|list_documents|get_document|create_document|complex_query|unsupported|unclear",\n'
    ' "confidence": 0.0-1.0,\n'
    ' "parameters": {},\n'
    ' "missing_slots": [],\n'
    ' "constraints": {"hard": {}, "soft": {}}}\n'
    "意图说明：\n"
    "- rag_search: 搜索/查找/问答文档内容\n"
    "- list_documents: 列出/浏览文档或知识库\n"
    "- get_document: 查看特定文档详情\n"
    "- create_document: 创建/上传文档\n"
    "- complex_query: 需要多步推理或工具调用的复杂查询\n"
    "- unsupported: 超出知识库服务范围的问题（订票/转账/天气/闲聊等），拒绝回答\n"
    "- unclear: 关键参数缺失或歧义，无法确定检索目标，需要澄清\n"
    "missing_slots 说明（仅 intent=unclear 时填写）：\n"
    "- search_query: 缺少检索主题（如\"查一下文档\"未指明查什么）\n"
    "- time_range: 缺少时间范围\n"
    "- classification: 缺少密级\n"
    "- doc_type: 缺少文档类型\n"
    "- kb: 缺少知识库\n"
    "constraints 说明：把用户显式表达的检索限制提取为结构化约束。\n"
    "hard（必须满足）：classification_max（密级上限，取值 public/internal/confidential/secret）、"
    "exclude_classifications（排除密级，数组）、kb_ids（限定知识库，数组）、"
    "mandatory_keywords（必须包含关键词，数组）\n"
    "soft（优先满足）：time_range（时间范围）、doc_type（文档类型）、"
    "source（来源）、preferred_keywords（优先关键词，数组）\n"
    "只返回 JSON，不附加解释。"
)


class LLMIntentParser:
    """LLM 意图解析器 — 仅规则未命中时调用。"""

    def __init__(self, llm_provider: Any) -> None:
        """初始化 LLM 意图解析器。

        Args:
            llm_provider: LLM Provider 实例。
        """
        self._llm = llm_provider

    async def parse(
        self,
        query: str,
        context: str,
    ) -> IntentResult | None:
        """用 LLM 解析用户意图。

        Args:
            query: 用户输入的自然语言查询。
            context: 对话上下文（截取前 500 字防止过长）。

        Returns:
            IntentResult | None: 解析结果，失败返回 None。
        """
        if not self._llm:
            return None

        try:
            # LLMProvider.chat 是异步生成器（见 app/llm/base.py 调用约定），
            # 必须用 async for 消费，await 会抛 TypeError。
            chunks: list[str] = []
            async for chunk in self._llm.chat(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"上下文: {context[:500]}\n用户输入: {query}",
                    },
                ],
                max_tokens=100,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            # 解析 JSON 响应
            content = "".join(chunks).strip()
            data = json.loads(content)

            intent_str = data.get("intent", "complex_query")
            try:
                intent = IntentType(intent_str)
            except ValueError:
                intent = IntentType.COMPLEX_QUERY

            confidence = float(data.get("confidence", 0.0))
            parameters = data.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}

            # 解析缺失槽位（仅 UNCLEAR 有意义）
            raw_missing = data.get("missing_slots", [])
            missing_slots: list[str] = []
            if isinstance(raw_missing, list):
                for slot in raw_missing:
                    if isinstance(slot, str) and slot.strip():
                        missing_slots.append(slot.strip())

            # 解析结构化约束（方案二）— 白名单过滤，防止注入未知键
            constraints = self._parse_constraints(data.get("constraints"))

            # 终态出口（拒识/澄清）与快捷意图均走快捷处理器；
            # 终态出口置信度不足时回退 COMPLEX_QUERY，避免误拒识。
            use_shortcut = (
                intent in _SHORTCUT_INTENTS or intent in _TERMINAL_INTENTS
            ) and confidence >= 0.7

            return IntentResult(
                intent=intent,
                confidence=confidence,
                parameters=parameters,
                missing_slots=missing_slots,
                constraints=constraints,
                use_shortcut=use_shortcut,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("llm_parser.parse_failed", error=str(exc), query=query[:50])
            return None
        except Exception as exc:
            log.warning("llm_parser.llm_error", error=str(exc), query=query[:50])
            return None

    @staticmethod
    def _parse_constraints(raw: Any) -> IntentConstraints | None:
        """解析并白名单校验 LLM 返回的约束结构。

        仅保留 hard/soft 白名单内的键，非法密级值一律丢弃，
        防止 LLM 输出未知键导致下游误过滤或密级越权。
        """
        if not isinstance(raw, dict):
            return None

        hard_raw = raw.get("hard")
        soft_raw = raw.get("soft")
        hard: dict[str, Any] = {}
        soft: dict[str, Any] = {}

        if isinstance(hard_raw, dict):
            for key in _HARD_KEYS:
                if key not in hard_raw:
                    continue
                if key == "classification_max":
                    val = hard_raw.get(key)
                    if isinstance(val, str) and val in _CLASSIFICATION_VALUES:
                        hard[key] = val
                elif key in ("exclude_classifications", "kb_ids", "mandatory_keywords"):
                    vals = hard_raw.get(key)
                    if isinstance(vals, list):
                        clean = [v for v in vals if isinstance(v, str) and v.strip()]
                        if clean:
                            hard[key] = clean

        if isinstance(soft_raw, dict):
            for key in _SOFT_KEYS:
                if key not in soft_raw:
                    continue
                val = soft_raw.get(key)
                if key == "preferred_keywords":
                    if isinstance(val, list):
                        clean = [v for v in val if isinstance(v, str) and v.strip()]
                        if clean:
                            soft[key] = clean
                elif isinstance(val, str) and val.strip():
                    soft[key] = val.strip()

        if not hard and not soft:
            return None
        return IntentConstraints(hard=hard, soft=soft)
