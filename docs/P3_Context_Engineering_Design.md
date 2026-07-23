# P3 上下文工程设计方案

> 对应架构升级：上下文工程的"选/压/写/隔"四维度增强
> 前置依赖：P1 IntentRouter + P2 EntityRegistry 已完成

## 一、问题定义

### 1.1 核心痛点

多轮对话中，全量历史原文注入导致 LLM 被无关上下文干扰：

```
对话历史：
  用户: 北京今天天气怎么样？        ← Topic: 天气
  助手: 北京今天晴，25°C...
  用户: 北京今天车辆限号多少？      ← Topic: 限号 (焦点切换)
  助手: 今天限行尾号3和7...
  用户: 那上海呢？                 ← 省略主语，指代"上海限号"还是"上海天气"？
```

### 1.2 根因分析

| 失败模式 | 现状 | 影响 |
|----------|------|------|
| **上下文分心** | 固定16条窗口全量注入，不区分焦点 | 模型被早期天气对话干扰 |
| **上下文混乱** | 无指代消解，"那上海呢"直接送检索 | 检索结果混入天气文档 |
| **上下文膨胀** | 对话历史原文注入，无摘要压缩 | 超8轮对话后token浪费严重 |
| **上下文中毒** | Agent Loop 中间推理无持久化笔记 | 多轮迭代丢失关键决策信息 |

### 1.3 设计目标

1. **焦点追踪**：自动识别对话主题切换，为指代消解提供上下文
2. **语义选择**：根据当前查询相关性筛选历史消息，非全量注入
3. **滚动摘要**：旧对话压缩为摘要，近期保留原文（ConversationSummaryBuffer 策略）
4. **草稿本**：Agent Loop 中间推理持久化，压缩时保留高密度信息
5. **零回归**：所有新功能优雅降级，失败时回退到现有逻辑

## 二、架构设计

### 2.1 模块结构

```
app/
├── context/                          # P3 新增模块
│   ├── __init__.py
│   ├── focus_tracker.py              # P3-A: 对话焦点追踪
│   ├── coreference_resolver.py       # P3-A: 指代消解
│   ├── context_selector.py           # P3-B: 语义上下文选择
│   └── conversation_summarizer.py    # P3-C: 对话历史滚动摘要
├── rag/
│   ├── engine.py                     # 修改: P3-E Scratchpad
│   └── context_budget.py             # 修改: P3-C 摘要感知压缩
├── services/
│   └── chat_service.py               # 修改: P3-A/B/C 集成入口
├── memory/
│   └── memory_manager.py             # 修改: P3-F LLM事实提取
├── config.py                         # 修改: P3 配置项
└── utils/
    └── sse.py                        # 修改: P3 SSE 事件
```

### 2.2 数据流（P3 增强后）

```
用户输入 "那上海呢？"
    │
    ▼
ChatService.prepare_chat()
    ├── 1. 持久化用户消息
    ├── 2. 加载记忆上下文（四级记忆，不变）
    ├── 3. P3-A: TopicTracker.extract_focus(history)
    │      → ConversationFocus(topic="限号政策", entity="北京")
    ├── 4. P3-A: CoreferenceResolver.resolve("那上海呢？", focus)
    │      → "上海今天车辆限号多少？"
    ├── 5. P3-B: ContextSelector.select(resolved_query, history, top_k=5)
    │      → 筛选限号相关历史，过滤天气对话
    ├── 6. P3-C: ConversationSummarizer.summarize_if_needed(history)
    │      → 旧历史压缩为摘要 + 保留近期原文
    └── 7. _build_engine_memory_context()
           = system_prompt + 摘要 + 选中的历史 + 记忆偏好
    │
    ▼
ChatService.stream_chat()
    ├── IntentRouter 路由（不变）
    └── AgenticRAGEngine.answer()
         ├── P3-E: Scratchpad 在 think 循环中累积推理笔记
         ├── ContextBudget 压缩（感知 Scratchpad）
         └── Generator.generate()
```

### 2.3 与 P1/P2 的关系

| 组件 | P1 IntentRouter | P2 EntityRegistry | P3 Context Engineering |
|------|-----------------|-------------------|------------------------|
| 定位 | 意图路由（走快捷路径 or Agent Loop） | 实体语义层（同义词/图谱） | 上下文治理（选/压/写/隔） |
| 协作 | P3 指代消解后的查询送 IntentRouter | P3 焦点中的实体送 EntityRegistry 扩展 | P3 依赖 P1/P2 不变 |
| 顺序 | P3 → P1 → P2 | P3 → P2 → 检索 | 最先执行 |

## 三、详细设计

### 3.1 P3-A：对话焦点追踪 + 指代消解

#### 3.1.1 ConversationFocus 数据结构

```python
# app/context/focus_tracker.py

@dataclass
class ConversationFocus:
    """对话焦点 — 描述当前对话的主题和实体。"""
    topic: str           # "限号政策" — 当前话题
    entity: str          # "北京" — 当前讨论的主体实体
    intent: str          # "查询" — 用户意图（查询/操作/对比等）
    turn_index: int      # 焦点确立的轮次
    confidence: float    # 置信度 [0.0, 1.0]

    def to_context_str(self) -> str:
        """渲染为 LLM prompt 片段。"""
        return f"当前对话焦点：主题={self.topic}，实体={self.entity}，意图={self.intent}"
```

#### 3.1.2 TopicTracker 实现

```python
class TopicTracker:
    """对话焦点追踪器 — 从历史消息中提取当前焦点。

    策略：
    1. 规则优先：检测关键词（"搜索/查看/对比" → intent；实体名 → entity）
    2. LLM 兜底：规则未命中时，1 次轻量 LLM 调用（max_tokens=80）
    3. 焦点继承：如果最新查询无明显主题切换，继承上一轮焦点
    """

    _FOCUS_PROMPT = (
        "分析以下对话历史，提取当前对话焦点。\n"
        "输出格式：topic|entity|intent（用|分隔，不要换行）\n"
        "示例：限号政策|北京|查询\n\n"
        "对话历史（最近3轮）：\n{history}\n\n"
        "当前焦点："
    )

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self._llm = llm
        self._last_focus: ConversationFocus | None = None

    async def extract_focus(
        self,
        history: list[dict[str, str]],
    ) -> ConversationFocus | None:
        """从对话历史中提取当前焦点。

        Args:
            history: 对话历史列表 [{"role": "user/assistant", "content": "..."}]

        Returns:
            ConversationFocus | None: 当前焦点，无法确定时返回 None
        """
        if not history or len(history) < 2:
            return self._last_focus  # 单轮对话无法确定焦点

        # 1. 规则优先：从最近 user 消息提取
        recent_user_msgs = [m for m in history[-6:] if m.get("role") == "user"]
        if recent_user_msgs:
            focus = self._rule_extract(recent_user_msgs[-1]["content"])
            if focus:
                self._last_focus = focus
                return focus

        # 2. LLM 兜底
        if self._llm:
            focus = await self._llm_extract(history[-6:])
            if focus:
                self._last_focus = focus
                return focus

        # 3. 继承上一轮焦点
        return self._last_focus

    def _rule_extract(self, query: str) -> ConversationFocus | None:
        """规则提取焦点 — 零 Token。"""
        # 实体识别：从 P2 EntityRegistry 借力
        try:
            from app.ontology.entity_registry import EntityRegistry
            terms, entity_names = EntityRegistry.expand_query(query)
            if entity_names:
                entity_def = EntityRegistry.resolve_entity(entity_names[0])
                if entity_def:
                    # 推断 intent
                    intent = "查询"
                    if any(kw in query for kw in ["对比", "比较", "区别"]):
                        intent = "对比"
                    elif any(kw in query for kw in ["创建", "上传", "提交"]):
                        intent = "操作"
                    return ConversationFocus(
                        topic=entity_def.display_name,
                        entity=entity_def.display_name,
                        intent=intent,
                        turn_index=0,
                        confidence=0.8,
                    )
        except Exception:
            pass
        return None

    async def _llm_extract(self, recent: list[dict]) -> ConversationFocus | None:
        """LLM 提取焦点 — 1 次轻量调用。"""
        history_text = "\n".join(
            f"{m['role']}: {m['content'][:150]}" for m in recent
        )
        prompt = self._FOCUS_PROMPT.format(history=history_text)
        try:
            text = await self._call_llm(prompt)
            parts = text.strip().split("|")
            if len(parts) >= 3:
                return ConversationFocus(
                    topic=parts[0].strip(),
                    entity=parts[1].strip(),
                    intent=parts[2].strip(),
                    turn_index=0,
                    confidence=0.7,
                )
        except Exception as exc:
            log.warning("topic_tracker.llm_extract_failed", error=str(exc))
        return None

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=80):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    def reset(self) -> None:
        """重置焦点状态 — 供测试使用。"""
        self._last_focus = None
```

#### 3.1.3 CoreferenceResolver 实现

```python
# app/context/coreference_resolver.py

class CoreferenceResolver:
    """指代消解器 — 将省略句补全为完整查询。

    使用场景：用户说"那上海呢？"，焦点追踪器提供 {topic: "限号政策", entity: "北京"}，
    本组件将省略句补全为"上海今天车辆限号多少？"。

    设计要点：
    - 仅当查询为省略句时触发（检测省略特征词）
    - 1 次轻量 LLM 调用（max_tokens=100）
    - 失败时返回原始查询（优雅降级）
    """

    # 省略句特征词 — 出现这些词时可能需要指代消解
    _ELLIPSIS_INDICATORS: list[str] = [
        "呢", "怎么样", "如何", "也是", "呢？", "他", "她", "它",
        "这个", "那个", "上面", "刚才", "也是这样", "同样",
    ]

    _RESOLVE_PROMPT = (
        "你是对话指代消解专家。根据对话焦点，将用户的省略句补全为完整查询。\n\n"
        "规则：\n"
        "1. 如果用户查询已经是完整句子，原样返回\n"
        "2. 如果是省略句，根据焦点补全主语和谓语\n"
        "3. 只输出补全后的查询，不要包含解释\n\n"
        "对话焦点：{focus}\n"
        "用户查询：{query}\n\n"
        "补全后的查询："
    )

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self._llm = llm

    def needs_resolution(self, query: str) -> bool:
        """检测查询是否可能需要指代消解。

        启发式规则：
        - 查询长度 < 15 字符（短句更可能省略）
        - 包含省略特征词
        - 不包含明确的动词（搜索/查看/创建等）
        """
        query_stripped = query.strip()
        if len(query_stripped) > 30:
            return False
        has_indicator = any(ind in query_stripped for ind in self._ELLIPSIS_INDICATORS)
        has_explicit_verb = any(
            kw in query_stripped for kw in ["搜索", "查找", "查看", "创建", "上传", "列出", "search", "find", "view"]
        )
        return has_indicator and not has_explicit_verb

    async def resolve(
        self,
        query: str,
        focus: ConversationFocus | None,
    ) -> str:
        """指代消解 — 补全省略句。

        Args:
            query: 用户原始查询。
            focus: 当前对话焦点（来自 TopicTracker）。

        Returns:
            补全后的查询；无焦点或不需要消解时返回原始查询。
        """
        # 无焦点或不需消解 → 原样返回
        if focus is None or not self.needs_resolution(query):
            return query

        if self._llm is None:
            return query

        prompt = self._RESOLVE_PROMPT.format(
            focus=focus.to_context_str(),
            query=query,
        )
        try:
            resolved = await self._call_llm(prompt)
            resolved = resolved.strip().strip('"').strip("'").strip("「」")
            if resolved and resolved != query:
                log.info(
                    "coreference.resolved",
                    original=query[:100],
                    resolved=resolved[:100],
                    focus=focus.to_context_str(),
                )
                return resolved
        except Exception as exc:
            log.warning("coreference.resolve_failed", error=str(exc))

        return query

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=100):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)
```

#### 3.1.4 集成到 ChatService

在 `prepare_chat()` 的第 3 步（加载记忆上下文）和第 4 步（构建 memory_context）之间插入 P3-A：

```python
# chat_service.py prepare_chat() 中的 P3-A 集成

# 3. 加载记忆上下文（四级记忆）
memory_ctx = await self.memory.build_context(...)

# P3-A: 焦点追踪 + 指代消解
resolved_query = query  # 默认不修改
conversation_focus = None
try:
    settings = get_settings()
    if settings.CONTEXT_FOCUS_TRACKING_ENABLED:
        # 从 DB 加载最近 N 轮历史（用于焦点追踪）
        history = await self.msg_repo.get_by_conversation(
            conversation_id, limit=settings.CONTEXT_FOCUS_HISTORY_WINDOW
        )
        history_dicts = [
            {"role": msg.role, "content": msg.content}
            for msg in history
        ]

        topic_tracker = self._get_topic_tracker()
        conversation_focus = await topic_tracker.extract_focus(history_dicts)

        if conversation_focus and settings.COREFERENCE_RESOLUTION_ENABLED:
            resolver = self._get_coreference_resolver()
            resolved_query = await resolver.resolve(query, conversation_focus)

        if resolved_query != query:
            log.info(
                "chat.query_resolved",
                original=query[:100],
                resolved=resolved_query[:100],
            )
except Exception as exc:
    logger.warning("chat.focus_tracking_failed", error=str(exc))
    resolved_query = query  # 优雅降级

# 4. 构建引擎 memory_context（使用 resolved_query）
memory_context = await self._build_engine_memory_context(
    conversation_id, agent_type, memory_ctx, conversation_focus
)

return PreparedChat(
    query=resolved_query,  # 使用消解后的查询
    original_query=query,  # 保留原始查询（供前端展示）
    ...
)
```

### 3.2 P3-B：语义上下文选择器

```python
# app/context/context_selector.py

class ContextSelector:
    """语义上下文选择器 — 根据当前查询相关性筛选历史消息。

    替换当前的"固定16条窗口全量注入"策略：
    1. 向量化当前查询和每条历史消息
    2. 余弦相似度排序
    3. 取 top_k 最相关消息
    4. 保证最近 2 轮始终入选（近因优先）
    5. 总 token 不超过预算

    优雅降级：Embedder 不可用时回退到固定窗口策略。
    """

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        max_tokens: int = 800,       # 选中历史的总 token 预算
        always_keep_recent: int = 4, # 始终保留最近 N 条消息
        similarity_threshold: float = 0.3,  # 相似度低于此值的历史不选
    ) -> None:
        self._embedder = embedder
        self._max_tokens = max_tokens
        self._always_keep_recent = always_keep_recent
        self._similarity_threshold = similarity_threshold

    async def select(
        self,
        query: str,
        history: list[dict[str, str]],
        top_k: int = 5,
    ) -> list[dict[str, str]]:
        """从历史消息中选择与当前查询语义相关的消息。

        Args:
            query: 当前用户查询（消解后）。
            history: 完整对话历史。
            top_k: 最多选择的消息条数。

        Returns:
            选中的消息列表（按时间正序排列）。
        """
        if not history or len(history) <= self._always_keep_recent:
            return history  # 历史不足时全量返回

        try:
            embedder = await self._get_embedder()
            if embedder is None:
                return self._fallback_select(history)  # 降级为固定窗口

            # 向量化查询和历史消息
            query_vec = (await embedder.embed([query]))[0]
            history_texts = [
                f"{m['role']}: {m['content'][:200]}" for m in history
            ]
            history_vecs = await embedder.embed(history_texts)

            # 计算余弦相似度
            similarities = self._cosine_similarity_batch(query_vec, history_vecs)

            # 排序：相似度高的优先，但最近 always_keep_recent 条始终入选
            scored = list(enumerate(similarities))
            # 按相似度降序排序
            scored.sort(key=lambda x: x[1], reverse=True)

            selected_indices: set[int] = set()
            # 1. 先选相似度高的（超过阈值的）
            total_tokens = 0
            for idx, sim in scored:
                if len(selected_indices) >= top_k:
                    break
                if sim < self._similarity_threshold:
                    continue
                msg_tokens = len(history_texts[idx]) // 3  # 粗估 token
                if total_tokens + msg_tokens > self._max_tokens:
                    continue
                selected_indices.add(idx)
                total_tokens += msg_tokens

            # 2. 保证最近 N 条入选
            recent_start = len(history) - self._always_keep_recent
            for i in range(recent_start, len(history)):
                selected_indices.add(i)

            # 3. 按时间正序排列
            result = [history[i] for i in sorted(selected_indices)]
            return result

        except Exception as exc:
            log.warning("context_selector.select_failed", error=str(exc))
            return self._fallback_select(history)

    def _fallback_select(self, history: list[dict]) -> list[dict]:
        """降级策略：固定窗口（取最近 N 条）。"""
        return history[-self._always_keep_recent * 2:]  # 最近 8 条

    @staticmethod
    def _cosine_similarity_batch(
        query_vec: list[float],
        history_vecs: list[list[float]],
    ) -> list[float]:
        """批量计算余弦相似度。"""
        import math
        query_norm = math.sqrt(sum(x * x for x in query_vec))
        if query_norm == 0:
            return [0.0] * len(history_vecs)
        results = []
        for vec in history_vecs:
            vec_norm = math.sqrt(sum(x * x for x in vec))
            if vec_norm == 0:
                results.append(0.0)
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec))
            results.append(dot / (query_norm * vec_norm))
        return results
```

### 3.3 P3-C：对话历史滚动摘要

```python
# app/context/conversation_summarizer.py

class ConversationSummarizer:
    """对话历史滚动摘要 — ConversationSummaryBuffer 策略。

    当对话历史超过 token 阈值时，将旧消息压缩为滚动摘要，
    保留近期消息原文。结构：

    [摘要: "用户询问了北京天气和限号政策..."] + [最近4条原文]

    设计要点：
    - T_max 触发压缩，T_retained 压缩后目标
    - 摘要持久化到 Conversation.summary 字段（增量更新）
    - LLM 压缩失败时回退为截断（保留最近 N 条）
    """

    _SUMMARIZE_PROMPT = (
        "请将以下对话历史压缩为简洁的摘要（不超过200字）。\n"
        "保留关键信息：讨论的主题、已确认的事实、用户偏好。\n"
        "省略寒暄和重复内容。\n\n"
        "对话历史：\n{history}\n\n"
        "摘要："
    )

    def __init__(
        self,
        llm: LLMProvider | None = None,
        max_tokens: int = 600,          # 超过此值触发摘要
        retained_tokens: int = 200,     # 压缩后保留的近期消息 token
        summary_max_chars: int = 300,   # 摘要最大字符数
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._retained_tokens = retained_tokens
        self._summary_max_chars = summary_max_chars
        self._cached_summary: str = ""  # 持久化的旧摘要

    async def summarize_if_needed(
        self,
        history: list[dict[str, str]],
        existing_summary: str = "",
    ) -> tuple[str, list[dict[str, str]]]:
        """如果历史超过阈值，压缩旧消息为摘要。

        Args:
            history: 完整对话历史。
            existing_summary: 已有的旧摘要（增量合并）。

        Returns:
            tuple[摘要文本, 保留的近期消息列表]:
            - 摘要文本：旧历史压缩后的摘要（可能为空）
            - 近期消息：保留原文的最近 N 条消息
        """
        if not history:
            return existing_summary, []

        # 估算总 token
        total_chars = sum(len(m.get("content", "")) for m in history)
        total_tokens = total_chars // 3  # 粗估

        if total_tokens <= self._max_tokens:
            # 不需要压缩
            return existing_summary, history

        # 分割：旧消息（待压缩）+ 近期消息（保留原文）
        # 找到分割点，使近期消息约 retained_tokens
        retained_chars = self._retained_tokens * 3
        split_idx = len(history)
        chars_so_far = 0
        for i in range(len(history) - 1, -1, -1):
            chars_so_far += len(history[i].get("content", ""))
            if chars_so_far >= retained_chars:
                split_idx = i
                break

        old_messages = history[:split_idx]
        recent_messages = history[split_idx:]

        if not old_messages:
            return existing_summary, recent_messages

        # 压缩旧消息
        summary = existing_summary
        try:
            if self._llm:
                # 合并旧摘要 + 新旧消息
                history_text = "\n".join(
                    f"{m['role']}: {m['content'][:200]}" for m in old_messages
                )
                prompt_input = ""
                if existing_summary:
                    prompt_input = f"已有摘要：{existing_summary}\n\n新对话：\n{history_text}"
                else:
                    prompt_input = history_text

                prompt = self._SUMMARIZE_PROMPT.format(history=prompt_input)
                new_summary = await self._call_llm(prompt)
                if new_summary:
                    summary = new_summary[:self._summary_max_chars]
                    log.info(
                        "conversation_summarizer.compressed",
                        original_tokens=total_tokens,
                        summary_chars=len(summary),
                        retained_messages=len(recent_messages),
                    )
        except Exception as exc:
            log.warning("conversation_summarizer.llm_failed", error=str(exc))
            # 降级：用旧摘要 + 截断旧消息的第一条
            if old_messages:
                first_msg = old_messages[0]
                summary = f"{existing_summary}\n{first_msg['role']}: {first_msg['content'][:100]}"

        return summary, recent_messages

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=300):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks).strip()
```

### 3.4 P3-E：Scratchpad 草稿本

在 `engine.py` 的 AgentState 和 `_run_decision_loop_streaming` 中增加 Scratchpad：

```python
# engine.py 修改点

class AgentState(TypedDict, total=False):
    # ... 现有字段 ...
    scratchpad: str  # P3-E: 草稿本 — 累积每轮推理笔记

# _run_decision_loop_streaming() 中
_THINK_SYSTEM_STABLE = (
    "你是企业知识库助手的决策大脑。分析用户问题和已有信息，决定下一步：\n"
    '- 回复 "retrieve"：需要检索知识库补充信息；\n'
    '- 回复 "tool_call"：需要调用企业系统工具（如查 OA/ERP/IT 工单）；\n'
    '- 回复 "generate"：已有足够信息，可以生成最终答案。\n\n'
    "只回复上述三个关键词之一，不要附加解释。"
)
# P3-E: Scratchpad 作为 live zone 的一部分注入 think 上下文
# 不修改 _THINK_SYSTEM_STABLE（保持 KV Cache 前缀稳定）

# 在 while 循环中，think 之前：
# P3-E: Scratchpad 追加推理笔记
scratchpad = state.get("scratchpad", "")
if scratchpad:
    # Scratchpad 作为 live zone 消息注入，不影响稳定前缀
    # 但在 ContextBudget 压缩时，Scratchpad 优先保留
    pass  # 在 _think 中处理

# 在 _think() 方法中：
async def _think(self, state: AgentState) -> str:
    base_messages = state.get("messages", [])
    dynamic_parts = [
        f"当前状态：迭代 {state['iteration']}/{state['max_iterations']}"
    ]
    if state["retrieved_docs"]:
        dynamic_parts.append(f"已有文档 {len(state['retrieved_docs'])} 篇")
    if state["tool_results"]:
        dynamic_parts.append(f"工具结果 {len(state['tool_results'])} 条")

    # P3-E: 注入 Scratchpad
    scratchpad = state.get("scratchpad", "")
    if scratchpad:
        dynamic_parts.append(f"\n推理笔记：\n{scratchpad}")

    dynamic_parts.append("请决定下一步。")
    # ... 后续不变 ...
```

在 `_retrieve` 和 `_tool_call` 后追加 Scratchpad 笔记：

```python
# retrieve 后
state["scratchpad"] = state.get("scratchpad", "") + (
    f"\n[轮{state['iteration']}] retrieve: 检索到 {len(state['retrieved_docs'])} 篇文档"
)

# tool_call 后
state["scratchpad"] = state.get("scratchpad", "") + (
    f"\n[轮{state['iteration']}] tool_call: 调用 {tool_name}，结果摘要: {deduped[:80]}"
)
```

在 `context_budget.py` 中，压缩时保留 Scratchpad：

```python
# ContextBudgetManager.compress() 修改
def compress(self, messages: list[dict[str, Any]], scratchpad: str = "") -> list[dict[str, Any]]:
    # 三段式压缩（不变）
    head = messages[:2]
    tail = messages[-self._keep_recent:]
    middle = messages[2:-self._keep_recent]

    # 压缩中间消息
    summary_parts = [self._compress_single_message(msg.get("content", "")) for msg in middle]
    # P3-E: Scratchpad 作为高密度信息追加到摘要
    if scratchpad:
        summary_parts.append(f"推理轨迹:{scratchpad[-200:]}")  # 保留最后200字

    compressed_msg = {
        "role": "user",
        "content": "[系统] 早期上下文摘要：" + "；".join(s for s in summary_parts if s),
    }
    return head + [compressed_msg] + tail
```

### 3.5 P3-F：长期记忆增强

#### 3.5.1 LLM 驱动事实提取

替换 `memory_manager.py` 中 `extract_and_save_facts()` 的关键词启发式：

```python
# memory_manager.py 修改

_FACT_EXTRACTION_PROMPT = (
    "分析以下对话，提取值得长期记住的用户偏好和事实。\n"
    "只提取明确的偏好和事实，不要推测。\n"
    "输出格式：每行一个事实，格式为 category|content\n"
    "category 可选：preference（用户偏好）/ fact（事实信息）\n\n"
    "对话内容：\n{conversation}\n\n"
    "提取结果："
)

async def extract_and_save_facts(
    self,
    user_id: uuid.UUID,
    messages: list[dict],
) -> list[str]:
    """从对话中提取值得记住的事实。

    P3-F: 使用 LLM 提取替代关键词启发式。
    """
    # 构建对话文本
    conversation = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')[:200]}"
        for m in messages[-10:]  # 最近 10 条
    )
    if len(conversation) < 50:
        return []  # 对话太短不提取

    try:
        llm = get_llm_provider()
        prompt = self._FACT_EXTRACTION_PROMPT.format(conversation=conversation)
        result_msgs: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in llm.chat(result_msgs, stream=True, max_tokens=200):
            if isinstance(chunk, str):
                chunks.append(chunk)
        result_text = "".join(chunks).strip()

        extracted = []
        for line in result_text.split("\n"):
            line = line.strip()
            if "|" not in line:
                continue
            category, content = line.split("|", 1)
            category = category.strip().lower()
            content = content.strip()
            if category in ("preference", "fact") and content:
                await self.mem0.add_fact(
                    user_id=user_id,
                    fact_text=content,
                    category=category,
                )
                extracted.append(content)

        if extracted:
            logger.info("facts_extracted_llm", user_id=str(user_id), count=len(extracted))

        return extracted

    except Exception as exc:
        logger.warning("fact_extraction_llm_failed", error=str(exc))
        # 降级为关键词启发式
        return await self._keyword_extract_facts(user_id, messages)
```

## 四、配置项设计

```python
# config.py 新增

# === P3 上下文工程 ===
CONTEXT_FOCUS_TRACKING_ENABLED: bool = True       # P3-A 焦点追踪总开关
CONTEXT_FOCUS_HISTORY_WINDOW: int = 12            # 焦点追踪加载的历史消息数
COREFERENCE_RESOLUTION_ENABLED: bool = True       # P3-A 指代消解开关
CONTEXT_SELECTOR_ENABLED: bool = True             # P3-B 语义选择器开关
CONTEXT_SELECTOR_TOP_K: int = 5                   # 语义选择器最多选中消息数
CONTEXT_SELECTOR_MAX_TOKENS: int = 800            # 语义选择器 token 预算
CONVERSATION_SUMMARIZER_ENABLED: bool = True      # P3-C 滚动摘要开关
CONVERSATION_SUMMARIZER_MAX_TOKENS: int = 600     # 摘要触发阈值
CONVERSATION_SUMMARIZER_RETAINED_TOKENS: int = 200  # 摘要后保留的近期 token
SCRATCHPAD_ENABLED: bool = True                   # P3-E Scratchpad 开关
LLM_FACT_EXTRACTION_ENABLED: bool = True          # P3-F LLM 事实提取开关
```

## 五、SSE 事件设计

```python
# sse.py 新增

class SSEEventType:
    # ... 现有事件 ...
    # P3: 上下文工程
    CONTEXT_RESOLVED = "context_resolved"  # 指代消解结果
    CONTEXT_SUMMARY = "context_summary"    # 摘要事件（可选，调试用）
```

`context_resolved` 事件结构：
```json
{
    "original_query": "那上海呢？",
    "resolved_query": "上海今天车辆限号多少？",
    "focus_topic": "限号政策",
    "focus_entity": "北京",
    "selected_history_count": 3,
    "has_summary": true
}
```

## 六、Task 拆分

### P3-A：焦点追踪 + 指代消解（P0 优先级）

| Task ID | 描述 | 文件 | 依赖 |
|---------|------|------|------|
| P3-A-T1 | 创建 `app/context/` 模块 + `__init__.py` | `app/context/__init__.py` | 无 |
| P3-A-T2 | 实现 `ConversationFocus` 数据结构 + `TopicTracker` | `app/context/focus_tracker.py` | P3-A-T1 |
| P3-A-T3 | 实现 `CoreferenceResolver` | `app/context/coreference_resolver.py` | P3-A-T2 |
| P3-A-T4 | 新增 config 配置项 + SSE 事件 | `app/config.py`, `app/utils/sse.py` | P3-A-T1 |
| P3-A-T5 | 集成到 `ChatService.prepare_chat()` | `app/services/chat_service.py` | P3-A-T2, T3, T4 |
| P3-A-T6 | 编写单元测试 | `tests/test_focus_tracker.py`, `tests/test_coreference_resolver.py` | P3-A-T2, T3 |

### P3-B：语义上下文选择器（P1 优先级）

| Task ID | 描述 | 文件 | 依赖 |
|---------|------|------|------|
| P3-B-T1 | 实现 `ContextSelector` | `app/context/context_selector.py` | P3-A-T1 |
| P3-B-T2 | 集成到 `_build_engine_memory_context()` | `app/services/chat_service.py` | P3-B-T1, P3-A-T5 |
| P3-B-T3 | 编写单元测试 | `tests/test_context_selector.py` | P3-B-T1 |

### P3-C：对话历史滚动摘要（P1 优先级）

| Task ID | 描述 | 文件 | 依赖 |
|---------|------|------|------|
| P3-C-T1 | 实现 `ConversationSummarizer` | `app/context/conversation_summarizer.py` | P3-A-T1 |
| P3-C-T2 | 集成到 `_build_engine_memory_context()` | `app/services/chat_service.py` | P3-C-T1, P3-B-T2 |
| P3-C-T3 | 修改 `ContextBudgetManager.compress()` 感知 Scratchpad | `app/rag/context_budget.py` | P3-C-T1 |
| P3-C-T4 | 编写单元测试 | `tests/test_conversation_summarizer.py` | P3-C-T1 |

### P3-E：Scratchpad 草稿本（P2 优先级）

| Task ID | 描述 | 文件 | 依赖 |
|---------|------|------|------|
| P3-E-T1 | AgentState 增加 `scratchpad` 字段 | `app/rag/engine.py` | 无 |
| P3-E-T2 | `_think()` 注入 Scratchpad + `_retrieve`/`_tool_call` 追加笔记 | `app/rag/engine.py` | P3-E-T1 |
| P3-E-T3 | `ContextBudgetManager.compress()` 保留 Scratchpad | `app/rag/context_budget.py` | P3-E-T1 |
| P3-E-T4 | 编写单元测试 | `tests/test_scratchpad.py` | P3-E-T2 |

### P3-F：长期记忆增强（P2 优先级）

| Task ID | 描述 | 文件 | 依赖 |
|---------|------|------|------|
| P3-F-T1 | 替换 `extract_and_save_facts()` 为 LLM 驱动 | `app/memory/memory_manager.py` | 无 |
| P3-F-T2 | 编写单元测试 | `tests/test_fact_extraction.py` | P3-F-T1 |

### 最终验证

| Task ID | 描述 |
|---------|------|
| P3-TEST-1 | 运行 P3 新增测试 |
| P3-TEST-2 | 运行受影响模块测试（chat_service, engine, context_budget, memory_manager） |
| P3-TEST-3 | 运行全量测试验证无回归 |

## 七、执行顺序

```
P3-A-T1 → P3-A-T2 → P3-A-T3 → P3-A-T4 → P3-A-T5 → P3-A-T6
                                                         │
              P3-B-T1 → P3-B-T2 → P3-B-T3              │
                                                         │
              P3-C-T1 → P3-C-T2 → P3-C-T3 → P3-C-T4    │
                                                         │
              P3-E-T1 → P3-E-T2 → P3-E-T3 → P3-E-T4    │
                                                         │
              P3-F-T1 → P3-F-T2                         │
                                                         ▼
                                              P3-TEST-1/2/3
```

P3-A 是所有任务的前置依赖（提供焦点上下文），必须先完成。P3-B/C/E/F 可以并行开发。

## 八、风险分析

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| LLM 调用增加延迟 | P3-A 每次+1次LLM调用(~200ms) | 规则优先，LLM兜底；省略句检测避免无谓调用 |
| 指代消解误判 | 补全查询偏离原意 | `needs_resolution()` 保守检测；前端展示原始查询 |
| 语义选择器依赖 Embedder | Embedder 不可用时降级 | `_fallback_select()` 固定窗口兜底 |
| 摘要丢失关键信息 | 早期对话被压缩 | 摘要保留主题+事实+偏好；T_max/T_retained 可调 |
| Scratchpad 增加上下文 | think token 增加 | ContextBudget 压缩时 Scratchpad 截断到 200 字 |
| LLM 事实提取成本 | 每次对话+1次LLM调用 | 仅对话结束时调用；降级为关键词启发式 |

## 九、Token 消耗对比

| 场景 | 现状 | P3 后 | 节省 |
|------|------|-------|------|
| 10轮对话历史注入 | 16条×~150字=2400字(~685tok) | 摘要300字+选中5条×150字=1050字(~300tok) | ~56% |
| 指代消解 | 无（查询原文送检索） | +1次LLM(~100tok) | 检索准确率↑ |
| 焦点追踪 | 无 | +1次LLM(~80tok) | 上下文相关性↑ |
| Agent Loop 5轮 | messages累积~2000tok | Scratchpad 200字 + 压缩~1200tok | ~40% |

**净效果**：每次对话增加 ~180 tok（焦点+消解），但历史注入减少 ~385 tok，净节省 ~205 tok/对话，且检索准确率显著提升。
