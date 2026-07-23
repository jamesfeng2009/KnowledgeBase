"""
规则匹配器 — 单一职责：零 Token 意图识别。

通过正则表达式匹配用户输入中的常见意图模式，覆盖中英文双语。
规则保守匹配（高置信度阈值），未命中时交由 LLM 兜底。

遵循开闭原则：新增意图规则只需在 _RULES 追加条目。
"""

from __future__ import annotations

import re

from app.intent.router import IntentResult, IntentType, _SHORTCUT_INTENTS


class RuleMatcher:
    """规则匹配器 — 零 Token 意图识别。

    使用预编译正则表达式匹配用户输入，覆盖 4 种常见意图：
        - LIST_DOCUMENTS: "列出文档" / "list documents"
        - RAG_SEARCH: "搜索XX" / "XX是什么" / "search XX"
        - GET_DOCUMENT: "查看XX" / "view XX"
        - CREATE_DOCUMENT: "创建文档" / "upload document"
    """

    # 规则定义：(意图类型, [正则模式列表], 置信度)
    _RULES: list[tuple[IntentType, list[re.Pattern[str]], float]] = [
        # --- LIST_DOCUMENTS: 列出/列表/有哪些 ---
        (
            IntentType.LIST_DOCUMENTS,
            [
                re.compile(
                    r"(列出|列表|有哪些|显示一下|看一下列表).*(文档|知识库|文件|资料)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(list|show|all)\s+(documents?|knowledge|files?)",
                    re.IGNORECASE,
                ),
                re.compile(r"(我的|所有).*(文档|知识库)", re.IGNORECASE),
            ],
            0.9,
        ),
        # --- RAG_SEARCH: 搜索/查找/是什么/怎么 ---
        (
            IntentType.RAG_SEARCH,
            [
                re.compile(
                    r"(搜索|查找|查一下|搜一下|查询|找一下|帮我查|检索)(.+)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(search|find|query|look\s+for)\s+(.+)",
                    re.IGNORECASE,
                ),
                re.compile(r".*(是什么|怎么|如何|为什么|什么是|区别|对比|优缺点)"),
                re.compile(r".*(流程|步骤|方法|规范|要求).*(是|有)"),
            ],
            0.85,
        ),
        # --- GET_DOCUMENT: 查看/打开/详情 ---
        (
            IntentType.GET_DOCUMENT,
            [
                re.compile(
                    r"(查看|打开|看看|详情|阅读)(.+)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(view|open|detail|read)\s+(.+)",
                    re.IGNORECASE,
                ),
            ],
            0.85,
        ),
        # --- CREATE_DOCUMENT: 创建/上传/添加 ---
        (
            IntentType.CREATE_DOCUMENT,
            [
                re.compile(
                    r"(创建|上传|添加|新建|导入).*(文档|文件|知识|资料)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(create|upload|add|new|import)\s+(document|file|knowledge)",
                    re.IGNORECASE,
                ),
            ],
            0.9,
        ),
    ]

    def match(self, query: str) -> IntentResult | None:
        """匹配用户查询到意图。

        Args:
            query: 用户输入的自然语言查询。

        Returns:
            IntentResult | None: 匹配结果，未匹配返回 None。
        """
        query_stripped = query.strip()
        if not query_stripped:
            return None

        for intent, patterns, confidence in self._RULES:
            for pattern in patterns:
                match = pattern.search(query_stripped)
                if match:
                    # 提取参数（如搜索关键词）
                    parameters: dict[str, str] = {}
                    if intent == IntentType.RAG_SEARCH and match.lastindex and match.lastindex >= 2:
                        parameters["search_query"] = match.group(match.lastindex).strip()
                    elif intent == IntentType.GET_DOCUMENT and match.lastindex and match.lastindex >= 2:
                        parameters["document_ref"] = match.group(match.lastindex).strip()

                    return IntentResult(
                        intent=intent,
                        confidence=confidence,
                        parameters=parameters,
                        use_shortcut=intent in _SHORTCUT_INTENTS,
                    )
        return None
