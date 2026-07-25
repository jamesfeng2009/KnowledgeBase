"""
LLM 意图解析器 — 单一职责：规则未命中时用 LLM 解析意图。

仅在 RuleMatcher 未命中时调用，使用 function calling 格式返回结构化意图。
max_tokens=100，确保轻量调用（约 100-200 Token 消耗）。

遵循优雅降级：LLM 不可用或解析失败时返回 None，由 IntentRouter 兜底。
"""

from __future__ import annotations

import json
from typing import Any

from app.intent.router import IntentResult, IntentType, _SHORTCUT_INTENTS
from app.utils.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    "你是企业知识库的意图识别器。分析用户输入，返回 JSON：\n"
    '{"intent": "rag_search|list_documents|get_document|create_document|complex_query",\n'
    ' "confidence": 0.0-1.0,\n'
    ' "parameters": {}}\n'
    "意图说明：\n"
    "- rag_search: 搜索/查找/问答文档内容\n"
    "- list_documents: 列出/浏览文档或知识库\n"
    "- get_document: 查看特定文档详情\n"
    "- create_document: 创建/上传文档\n"
    "- complex_query: 需要多步推理或工具调用的复杂查询\n"
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

            return IntentResult(
                intent=intent,
                confidence=confidence,
                parameters=parameters if isinstance(parameters, dict) else {},
                use_shortcut=intent in _SHORTCUT_INTENTS and confidence >= 0.7,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("llm_parser.parse_failed", error=str(exc), query=query[:50])
            return None
        except Exception as exc:
            log.warning("llm_parser.llm_error", error=str(exc), query=query[:50])
            return None
