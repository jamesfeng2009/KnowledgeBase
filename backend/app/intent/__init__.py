"""
意图路由模块 — 稳态/敏态分离核心。

将用户自然语言意图解析为结构化意图，简单查询走确定性快捷路径（零/少 Token），
复杂查询走原有 Agent Loop。

模块结构：
    - router.py: IntentRouter 意图路由器（规则优先 + LLM 兜底）
    - rule_matcher.py: RuleMatcher 规则匹配器（零 Token）
    - llm_parser.py: LLMIntentParser LLM 意图解析（规则未命中时）
    - shortcut_handler.py: ShortcutHandler 快捷路径处理器
"""
