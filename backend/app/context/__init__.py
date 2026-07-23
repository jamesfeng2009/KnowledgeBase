"""
上下文工程模块 — 对话焦点追踪、指代消解、语义选择、滚动摘要、漂移检测、矛盾检测。

模块结构：
    - focus_tracker.py          P3-A: 对话焦点追踪 (TopicTracker + ConversationFocus)
    - coreference_resolver.py   P3-A: 指代消解 (CoreferenceResolver)
    - context_selector.py       P3-B: 语义上下文选择 (ContextSelector)
    - conversation_summarizer.py P3-C: 对话历史滚动摘要 (ConversationSummarizer)
    - drift_detector.py             P4-A: 漂移检测 (DriftDetector + DriftResult)
    - contradiction_detector.py     P4-B: 矛盾检测 (ContradictionDetector + ContradictionResult)
    - preference_drift_detector.py  P4-F: 偏好偏移检测 (PreferenceDriftDetector + PreferenceDriftResult)
    - retrieval_matcher.py          P4-D: 检索匹配检测 (RetrievalMatcher + RetrievalMatchResult)
    - repetition_detector.py        P4-G: 重复提问检测 (RepetitionDetector + RepetitionResult)

设计原则：
    - 规则优先，LLM 兜底（省 Token）
    - 所有组件优雅降级（LLM/Embedder 不可用时回退到现有逻辑）
    - 零侵入集成（通过 ChatService.prepare_chat 统一接入）
"""
