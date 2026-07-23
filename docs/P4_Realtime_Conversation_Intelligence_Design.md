# P4 实时对话智能方案 — 矛盾检测 / 漂移检测 / 指代消解增强 / 偏好偏移 / 重复提问

> **版本**: v1.0
> **日期**: 2026-07-23
> **状态**: 待 Review
> **前置依赖**: P3 上下文工程（已完成，58 测试通过）

---

## 1. 现状分析

### 1.1 能力矩阵

| 能力 | 现状 | 实时性 | 覆盖率 | 关键缺口 |
|------|------|--------|--------|----------|
| **矛盾检测** | 仅知识回流层离线检测 | 离线 | 20% | 对话中无实时矛盾检测 |
| **漂移检测** | 完全不存在 | — | 0% | 焦点继承假设连续性，恰是漂移检测的反面 |
| **指代消解** | P3-A 已实现，实时集成 | 实时 | 60% | 无多轮跨指代、无历史注入、与 Query Rewriter 重叠 |
| **偏好偏移** | 完全不存在 | — | 0% | 用户说"简单点"/"详细点"时系统不感知，回答风格不调整 |
| **重复提问** | 完全不存在 | — | 0% | 用户连续重复提问时系统不识别不满信号，不切换检索策略 |

### 1.2 矛盾检测现状

**已有**: `compounding_service._llm_detect_conflict()` — 知识资产间离线 LLM 比对，输出 contradiction/supersede/overlap。

**缺失**:
- 文档入库时无矛盾检测（两篇矛盾文档可同时入库）
- 对话中 AI 回答 vs. 知识库矛盾 — 无检测
- 用户陈述前后矛盾 — 无检测
- 检索结果中文档间矛盾 — 无感知
- `_reflect` 节点只做幻觉检测（faithfulness），不检测矛盾

### 1.3 漂移检测现状

**完全空白**。全仓库搜索 `drift`/`漂移` 在业务代码中零命中。

**关键问题**:
- `TopicTracker._last_focus` 是单值继承，**假设话题连续** — 提取失败时无条件继承旧焦点
- `conversation_focus` 存入 `PreparedChat` 但**不传入 `engine.answer()`**，引擎对焦点完全盲然
- `AgentState` 无 focus/drift 字段
- 无新旧焦点相似度计算、无漂移分数、无漂移处理策略
- 指代消解在漂移发生时会用旧焦点**错误补全**，放大问题

### 1.4 指代消解现状

**已有**: `CoreferenceResolver` 三级策略（规则检测 → LLM 消解 → 规则降级），实时集成在 `prepare_chat()` 中。

**缺失**:
- `_last_focus` 是单值，非焦点历史栈 → 无法处理多轮跨指代
- LLM 消解 prompt 只注入 `focus.to_context_str()`，**不注入对话历史原文** → LLM 缺乏上下文
- 与 `Query Rewriter` 职责重叠但无协调 → 双重改写可能冲突
- 消解结果不持久化 → 下一轮焦点追踪看到原始 query

---

## 2. 总体架构

### 2.1 设计原则

1. **准实时 + 混合执行** — 用户接受 embedding 延迟；关键路径同步执行保证正确性，非关键路径通过 `asyncio.create_task` 后台执行不阻塞首 token，不引入 Celery 异步队列
2. **优雅降级** — 所有新组件 LLM/Embedder 不可用时降级为规则或跳过，不阻断主流程
3. **最小侵入** — 新增模块优先于修改现有模块；必须修改时保持向后兼容
4. **Token 预算** — 每个检测器单次 LLM 调用 ≤ 150 tokens，总增量 ≤ 500 tokens/轮

### 2.2 执行模型 — 同步关键路径 + 后台异步检测

> **设计决策**: 不引入 Celery 异步队列，用 `asyncio.create_task` 区分关键路径与非关键路径。

**不用 Celery**: Celery 为文档解析等批量后台任务设计，经 broker 往返 100-500ms 比直接 `await` 更慢，结果需轮询回传，不适合每轮对话的实时检测。

**不会"卡死"**: `prepare_chat` 是 `async def`，`await` 是协程调度不阻塞事件循环。关键路径 = DriftDetector（~50-100ms embedding）+ CoreferenceResolver（~400-600ms LLM，仅在 `needs_resolution` 时）≈ 500-700ms，比 P3 仅多 ~100ms。

| 执行方式 | 组件 | 原因 |
|---------|------|------|
| 同步（关键路径） | DriftDetector → CoreferenceResolver | 漂移检测必须先于指代消解；resolved_query 须在引擎启动前就绪 |
| 后台异步（fire-and-forget） | ContradictionDetector（用户矛盾） | 不影响 query/answer，仅产出警告；`asyncio.create_task` 启动，完成后推 SSE |
| Agent Loop 内同步 | ContradictionDetector（回答-知识库）、RetrievalMatcher | 在 `_reflect`/`_retrieve` 内执行，不阻塞 `prepare_chat` |

### 2.3 集成架构图

```
用户输入 query
    │
    ▼
┌─ prepare_chat() ──────────────────────────────────────────────────┐
│                                                                    │
│  1. 加载历史 (12 轮)                                                │
│  2. TopicTracker.extract_focus(history)  → focus                   │
│  3. ★ DriftDetector.check(query, focus, history)  → drift_result   │
│     │   drift_score > 阈值 → 重置焦点 + SSE 事件                    │
│     │   drift_score ≤ 阈值 → 继续使用 focus                         │
│  4. CoreferenceResolver.resolve(query, focus, history) → resolved  │
│     │   ★ 注入历史原文 + 焦点栈（P4-C 增强）                        │
│  5. _build_engine_memory_context(resolved_query, focus)             │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ engine.answer() Agent Loop ───────────────────────────────────────┐
│                                                                    │
│  think → retrieve → think → ... → generate → reflect               │
│                          │                          │              │
│                  ★ RetrievalMatcher          ★ ContradictionDetector│
│                  检索结果-焦点匹配检测              答案-知识库矛盾检测│
│                  mismatch → 扩大检索              contradiction →   │
│                                                  SSE 警告 + 标记    │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 新增文件清单

| 文件 | 模块 | 优先级 |
|------|------|--------|
| `app/context/drift_detector.py` | 漂移检测器 | P0 |
| `app/context/contradiction_detector.py` | 矛盾检测器 | P0 |
| `app/context/retrieval_matcher.py` | 检索匹配检测器 | P2 |
| `tests/test_drift_detector.py` | 漂移检测测试 | P0 |
| `tests/test_contradiction_detector.py` | 矛盾检测测试 | P0 |
| `tests/test_retrieval_matcher.py` | 检索匹配测试 | P2 |

### 2.5 修改文件清单

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `app/context/focus_tracker.py` | 焦点历史栈 + 置信度利用 | P1 |
| `app/context/coreference_resolver.py` | 历史注入 + 焦点栈支持 | P1 |
| `app/services/chat_service.py` | 集成 DriftDetector + 传 focus 给引擎 | P0 |
| `app/rag/engine.py` | AgentState 加 focus 字段 + 集成检测器 | P0/P2 |
| `app/utils/sse.py` | 新增 SSE 事件类型 | P0 |
| `app/config.py` | P4 配置项 | P0 |

---

## 3. 详细设计

### 3.1 P4-A (P0): 漂移检测器 DriftDetector

#### 3.1.1 核心思路

用户话题从"北京限号"跳到"公司报销流程"时，当前系统会：
1. TopicTracker 规则提取命中"报销"关键词 → 新焦点覆盖旧焦点（此时是正确的）
2. 但如果用户从"北京限号"跳到"那上海呢"（换了实体但同话题）→ 这不是漂移

**关键区分**：
- **实体切换**（北京→上海，同话题"限号"）→ 不是漂移，指代消解应处理
- **话题跳跃**（限号→报销，不同话题域）→ 是漂移，应重置焦点

#### 3.1.2 检测策略（三级）

```
Level 1: 规则检测（零 Token）
  - TopicTracker 规则提取成功（命中关键词）→ 新焦点与旧焦点 topic 不同 → drift
  - 话题关键词字典完全无交集 → drift_score = 1.0

Level 2: Embedding 相似度（准实时，用户接受延迟）
  - 对 current_query 和 last_focus.topic 做 embedding
  - cosine_similarity < 0.4 → drift_score = 1 - similarity
  - 0.4 ≤ similarity < 0.6 → drift_score = 0.5（可能漂移）

Level 3: 置信度衰减
  - 连续 N 轮焦点置信度 < 0.3 → drift_score += 0.2
```

#### 3.1.3 数据结构

```python
@dataclass
class DriftResult:
    """漂移检测结果。"""
    is_drift: bool               # 是否发生漂移
    drift_score: float           # 0.0-1.0，越高越可能漂移
    previous_focus: ConversationFocus | None  # 漂移前的焦点
    detection_method: str        # "rule" / "embedding" / "confidence"
    action: str                  # "reset_focus" / "keep_focus" / "expand_retrieval"


class DriftDetector:
    """话题漂移检测器 — 在 prepare_chat 中实时执行。

    检测用户查询是否偏离了当前对话焦点。
    漂移发生时重置焦点，防止指代消解用旧焦点错误补全。
    """

    _DRIFT_SIMILARITY_THRESHOLD: float = 0.4   # cosine < 0.4 = 漂移
    _POSSIBLE_DRIFT_THRESHOLD: float = 0.6     # 0.4-0.6 = 可能漂移
    _CONFIDENCE_DECAY_ROUNDS: int = 3          # 连续 3 轮低置信度

    def __init__(self, embedder=None):
        self._embedder = embedder
        self._low_confidence_streak: int = 0

    async def check(
        self,
        query: str,
        current_focus: ConversationFocus | None,
        history: list[dict[str, str]],
    ) -> DriftResult:
        """检测话题漂移。

        Args:
            query: 当前用户查询
            current_focus: 当前对话焦点（上一轮的）
            history: 对话历史

        Returns:
            DriftResult: 漂移检测结果 + 建议动作
        """
        # 无焦点 → 无漂移可言
        if not current_focus:
            return DriftResult(False, 0.0, None, "none", "keep_focus")

        # Level 1: 规则检测
        rule_result = self._rule_check(query, current_focus)
        if rule_result is not None:
            return rule_result

        # Level 2: Embedding 相似度
        if self._embedder:
            emb_result = await self._embedding_check(query, current_focus)
            if emb_result is not None:
                return emb_result

        # Level 3: 置信度衰减
        if current_focus.confidence < 0.3:
            self._low_confidence_streak += 1
            if self._low_confidence_streak >= self._CONFIDENCE_DECAY_ROUNDS:
                return DriftResult(True, 0.6, current_focus, "confidence", "reset_focus")
        else:
            self._low_confidence_streak = 0

        return DriftResult(False, 0.0, current_focus, "none", "keep_focus")

    def _rule_check(
        self, query: str, focus: ConversationFocus
    ) -> DriftResult | None:
        """规则检测 — 用话题关键词字典判断话题域是否完全切换。"""
        # 获取查询中的话题关键词
        query_topics = self._extract_topics(query)
        if not query_topics:
            return None  # 规则无法判断，交给 embedding

        focus_topic = focus.topic
        # 查询话题与当前焦点话题完全不同
        if focus_topic not in query_topics:
            # 检查是否有交集（如"限号"和"交通"算同域）
            if not self._is_same_domain(focus_topic, query_topics[0]):
                return DriftResult(
                    True, 1.0, focus, "rule", "reset_focus"
                )

        return None

    async def _embedding_check(
        self, query: str, focus: ConversationFocus
    ) -> DriftResult | None:
        """Embedding 相似度检测。"""
        try:
            query_vec = await self._embedder.embed([query])
            focus_vec = await self._embedder.embed([focus.topic])
            similarity = self._cosine_similarity(query_vec[0], focus_vec[0])

            if similarity < self._DRIFT_SIMILARITY_THRESHOLD:
                return DriftResult(
                    True, 1.0 - similarity, focus, "embedding", "reset_focus"
                )
            elif similarity < self._POSSIBLE_DRIFT_THRESHOLD:
                return DriftResult(
                    True, 1.0 - similarity, focus, "embedding", "expand_retrieval"
                )
        except Exception:
            pass  # embedder 不可用，降级

        return None
```

#### 3.1.4 集成到 prepare_chat

```python
# chat_service.py prepare_chat() 中，在 extract_focus 之后：

# P4-A: 漂移检测
drift_result = None
if focus and _settings.DRIFT_DETECTION_ENABLED:
    drift_detector = self._get_drift_detector()
    if drift_detector:
        drift_result = await drift_detector.check(query, focus, history_dicts)
        if drift_result.is_drift and drift_result.action == "reset_focus":
            # 漂移！重置焦点，不继承旧焦点
            topic_tracker.reset_focus()
            focus = await topic_tracker.extract_focus(history_dicts)
            logger.info("chat.drift_detected",
                       drift_score=drift_result.drift_score,
                       method=drift_result.detection_method)
```

#### 3.1.5 SSE 事件

```python
# sse.py 新增
DRIFT_DETECTED = "drift_detected"  # 话题漂移检测事件
```

---

### 3.2 P4-B (P0): 矛盾检测器 ContradictionDetector

#### 3.2.1 检测场景

| 场景 | 检测时机 | 方法 | 优先级 |
|------|---------|------|--------|
| **用户陈述前后矛盾** | prepare_chat，新 query vs. 历史 user 消息 | LLM 比对 | P0 |
| **AI 回答 vs. 知识库矛盾** | Agent Loop reflect 阶段，answer vs. retrieved_docs | LLM 比对 | P0 |
| **检索结果文档间矛盾** | Agent Loop retrieve 后，docs 互相比对 | LLM 比对 | P1 |

#### 3.2.2 数据结构

```python
@dataclass
class ContradictionResult:
    """矛盾检测结果。"""
    has_contradiction: bool
    contradiction_type: str    # "user_statement" / "answer_vs_kb" / "doc_vs_doc"
    description: str           # 矛盾描述
    conflicting_sources: list[str]  # 冲突来源（消息ID/文档标题）
    severity: str              # "high" / "medium" / "low"
    action: str                # "warn" / "block" / "flag"


class ContradictionDetector:
    """矛盾检测器 — 实时检测对话中的矛盾。

    三种检测场景：
    1. 用户陈述前后矛盾（prepare_chat 阶段）
    2. AI 回答 vs. 知识库矛盾（reflect 阶段）
    3. 检索结果文档间矛盾（retrieve 后）
    """

    _CONTRADICTION_PROMPT = (
        "判断以下两组陈述是否存在矛盾。\n"
        "矛盾定义：两组陈述对同一事实给出互相排斥的结论。\n"
        "输出格式：type|description\n"
        "type 可选：contradiction（矛盾）/ consistent（一致）/ unrelated（无关）\n"
        "如果是 contradiction，description 说明矛盾点。\n"
        "如果是 consistent 或 unrelated，description 输出 none。\n\n"
    )

    _MAX_HISTORY_FOR_CHECK: int = 6   # 检查最近 6 条 user 消息
    _MAX_DOCS_FOR_CHECK: int = 5      # 最多两两比对 5 篇文档

    async def check_user_contradiction(
        self,
        query: str,
        history: list[dict[str, str]],
    ) -> ContradictionResult:
        """检测用户当前陈述与历史陈述的矛盾。

        在 prepare_chat 阶段执行。
        """
        # 提取历史 user 消息
        user_msgs = [m["content"] for m in history if m["role"] == "user"]
        if len(user_msgs) < 2:
            return ContradictionResult(False, "", "", [], "", "none")

        # 只检查最近几条，避免过多 LLM 调用
        recent = user_msgs[-self._MAX_HISTORY_FOR_CHECK:-1]  # 不含当前 query

        # 规则预筛：当前 query 与历史消息是否有共同实体
        # 无共同实体 → 大概率无关，跳过 LLM 检测
        if not self._has_shared_entity(query, recent[-1] if recent else ""):
            return ContradictionResult(False, "", "", [], "", "none")

        # LLM 检测
        return await self._llm_check_contradiction(
            query, recent[-1], "user_statement"
        )

    async def check_answer_consistency(
        self,
        answer: str,
        retrieved_docs: list[dict],
    ) -> ContradictionResult:
        """检测 AI 回答与检索到的知识库文档的矛盾。

        在 Agent Loop reflect 阶段执行。
        """
        if not retrieved_docs or not answer:
            return ContradictionResult(False, "", "", [], "", "none")

        # 取 top-3 文档内容片段
        doc_contents = [d.get("content", "")[:500] for d in retrieved_docs[:3]]
        combined_docs = "\n---\n".join(doc_contents)

        return await self._llm_check_contradiction(
            answer, combined_docs, "answer_vs_kb"
        )

    async def check_doc_contradiction(
        self,
        retrieved_docs: list[dict],
    ) -> list[ContradictionResult]:
        """检测检索结果中文档间的矛盾。

        在 retrieve 后执行。两两比对 top-N 文档。
        """
        results = []
        docs_to_check = retrieved_docs[:self._MAX_DOCS_FOR_CHECK]

        for i, doc_a in enumerate(docs_to_check):
            for doc_b in docs_to_check[i+1:]:
                result = await self._llm_check_contradiction(
                    doc_a.get("content", "")[:300],
                    doc_b.get("content", "")[:300],
                    "doc_vs_doc",
                )
                if result.has_contradiction:
                    result.conflicting_sources = [
                        doc_a.get("title", f"doc_{i}"),
                        doc_b.get("title", f"doc_{j}"),
                    ]
                    results.append(result)

        return results
```

#### 3.2.3 集成点

**prepare_chat 阶段**（用户陈述矛盾）:
```python
# prepare_chat 中 — 后台启动矛盾检测（不阻塞首 token）
import asyncio

contra_task = None
if _settings.CONTRADICTION_DETECTION_ENABLED:
    contra_detector = self._get_contradiction_detector()
    if contra_detector:
        contra_task = asyncio.create_task(
            contra_detector.check_user_contradiction(resolved_query, history_dicts)
        )
# task 引用存入 PreparedChat，在 stream_chat 中检查完成状态

# stream_chat 中 — 在 token 流式循环中检查后台任务
contra_pushed = False
async for event in engine.answer(...):
    if prepared.contradiction_task and not contra_pushed:
        if prepared.contradiction_task.done():
            try:
                result = prepared.contradiction_task.result()
                if result.has_contradiction:
                    yield SSEEvent(
                        data={"type": "user_contradiction",
                              "description": result.description},
                        event=SSEEventType.CONTRADICTION_DETECTED,
                    )
            except Exception:
                pass  # 优雅降级
            contra_pushed = True
    yield event  # 转发 token / 事件
```

**Agent Loop reflect 阶段**（回答-知识库矛盾）:
```python
# engine.py _reflect() 方法末尾
if settings.CONTRADICTION_DETECTION_ENABLED:
    contra_detector = ContradictionDetector(self._llm)
    consistency = await contra_detector.check_answer_consistency(
        state["answer"], state["retrieved_docs"]
    )
    if consistency.has_contradiction:
        yield SSEEvent(
            data={"type": "answer_vs_kb", "description": consistency.description},
            event=SSEEventType.CONTRADICTION_DETECTED,
        )
        # 标记低置信度
        state["low_confidence"] = True
```

#### 3.2.4 SSE 事件

```python
# sse.py 新增
CONTRADICTION_DETECTED = "contradiction_detected"  # 矛盾检测事件
```

---

### 3.3 P4-C (P1): 指代消解增强

#### 3.3.1 焦点历史栈

```python
# focus_tracker.py 修改 TopicTracker

class TopicTracker:
    _FOCUS_STACK_SIZE: int = 5  # 保留最近 5 个焦点

    def __init__(self, llm: LLMProvider | None = None):
        self._llm = llm
        self._focus_stack: list[ConversationFocus] = []  # 替换 _last_focus

    @property
    def _last_focus(self) -> ConversationFocus | None:
        """兼容属性 — 返回栈顶焦点。"""
        return self._focus_stack[-1] if self._focus_stack else None

    def _push_focus(self, focus: ConversationFocus) -> None:
        """压入新焦点，保持栈大小。"""
        self._focus_stack.append(focus)
        if len(self._focus_stack) > self._FOCUS_STACK_SIZE:
            self._focus_stack.pop(0)

    def reset_focus(self) -> None:
        """清空焦点栈 — 漂移检测触发时调用。"""
        self._focus_stack.clear()

    def get_focus_history(self, n: int = 3) -> list[ConversationFocus]:
        """获取最近 N 个焦点 — 供指代消解回溯。"""
        return self._focus_stack[-n:] if self._focus_stack else []
```

#### 3.3.2 增强指代消解 prompt

```python
# coreference_resolver.py 修改 resolve 方法

_RESOLVE_PROMPT = (
    "你是对话指代消解助手。根据对话历史和焦点，将用户查询中的省略/代词补全为完整查询。\n"
    "规则：\n"
    "1. 只补全省略部分，不改变用户原意\n"
    "2. 如果查询已经完整，原样返回\n"
    "3. 输出格式：只输出补全后的查询，不要解释\n\n"
    "对话历史（最近几轮）：\n{history}\n\n"
    "对话焦点（最近几个）：\n{focus_history}\n\n"
    "用户当前查询：{query}\n\n"
    "补全后的查询："
)

async def resolve(
    self,
    query: str,
    focus: ConversationFocus | None,
    history: list[dict[str, str]] | None = None,  # ★ 新增
    focus_stack: list[ConversationFocus] | None = None,  # ★ 新增
) -> str:
    """指代消解 — 增强版：注入历史 + 焦点栈。"""
    if not focus or not self.needs_resolution(query):
        return query

    # 构建 prompt
    history_text = ""
    if history:
        recent = history[-6:]  # 最近 3 轮
        history_text = "\n".join(
            f"{m['role']}: {m['content'][:100]}" for m in recent
        )

    focus_text = focus.to_context_str()
    if focus_stack:
        # 注入焦点历史，让 LLM 能回溯多轮
        for i, f in enumerate(focus_stack[-3:]):
            focus_text += f"\n  轮{i}: 主题={f.topic}, 实体={f.entity}"

    prompt = self._RESOLVE_PROMPT.format(
        history=history_text or "（无）",
        focus_history=focus_text,
        query=query,
    )

    # LLM 消解（max_tokens=100）
    try:
        llm = get_llm_provider()
        result_text = ""
        async for chunk in llm.chat(
            [{"role": "user", "content": prompt}],
            stream=True, max_tokens=100
        ):
            if isinstance(chunk, str):
                result_text += chunk

        result_text = result_text.strip()
        if result_text and len(result_text) < 200 and result_text != query:
            return result_text
    except Exception:
        pass

    # 规则降级
    return self._rule_resolve(query, focus)
```

#### 3.3.3 与 Query Rewriter 协调

```python
# chat_service.py — 指代消解后跳过 Query Rewriter 的 rewrite 策略

# 在 engine.answer() 调用时，通过参数控制
# 如果 query 已被 CoreferenceResolver 改写，Query Rewriter 只做 expansion（同义词扩展）
# 不再做 rewrite（修正/消歧），避免双重改写冲突

# 方案：在 PreparedChat 中标记 query_was_resolved
@dataclass
class PreparedChat:
    # ... 现有字段 ...
    query_was_resolved: bool = False  # P4-C: 标记查询是否经过指代消解
    contradiction_task: asyncio.Task | None = None  # P4-B: 后台矛盾检测任务

# engine.answer() 中：
if query_was_resolved:
    # 跳过 rewrite 策略，只做 expansion
    rewritten = await self._query_rewriter.rewrite(query, strategy="expansion")
else:
    rewritten = await self._query_rewriter.rewrite(query)  # 默认全策略
```

---

### 3.4 P4-D (P2): 检索匹配检测器 RetrievalMatcher

#### 3.4.1 核心思路

检索回来的文档可能与当前话题不匹配（特别是漂移后）。检测方法：

```
1. 对 resolved_query 做 embedding
2. 对每篇 retrieved_doc 的 title+snippet 做 embedding
3. 计算 cosine similarity
4. 如果 top-1 文档相似度 < 0.3 → 检索不匹配
5. 不匹配 → 扩大检索范围（增加 top_k）或标记 low_confidence
```

#### 3.4.2 集成到 Agent Loop

```python
# engine.py _retrieve() 方法末尾

async def _retrieve(self, state: AgentState) -> dict:
    # ... 现有检索逻辑 ...

    # P4-D: 检索匹配检测
    if state.get("conversation_focus") and settings.RETRIEVAL_MATCH_CHECK_ENABLED:
        matcher = RetrievalMatcher(self._embedder)
        match_result = await matcher.check(
            state["query"], state["retrieved_docs"]
        )
        if not match_result.is_match:
            # 检索不匹配 — 扩大 top_k 重新检索
            log.warning("retrieval.mismatch", score=match_result.match_score)
            state["retrieval_mismatch"] = True
            # SSE 推送不匹配警告
            yield SSEEvent(
                data={"match_score": match_result.match_score},
                event=SSEEventType.RETRIEVAL_MISMATCH,
            )

    return {"retrieved_docs": reranked}
```

---

### 3.5 P4-E (P1): 焦点传入引擎

#### 3.5.1 AgentState 扩展

```python
# engine.py AgentState 新增字段
class AgentState(TypedDict, total=False):
    # ... 现有字段 ...
    # P4-E: 对话焦点传入引擎
    conversation_focus: dict[str, Any] | None  # 焦点信息
    drift_info: dict[str, Any] | None          # 漂移检测结果
```

#### 3.5.2 engine.answer() 接收 focus

```python
async def answer(
    self,
    query: str,
    user_id: str,
    session_id: str,
    kb_ids: list[str] | None = None,
    memory_context: str = "",
    tenant_id: str | None = None,
    db: Any = None,
    user_uuid: uuid.UUID | None = None,
    conversation_focus: dict[str, Any] | None = None,  # ★ 新增
    drift_info: dict[str, Any] | None = None,          # ★ 新增
) -> AsyncIterator[SSEEvent | str]:
```

#### 3.5.3 _think 节点注入焦点

```python
async def _think(self, state: AgentState) -> dict:
    # ... 现有逻辑 ...

    # P4-E: 将焦点注入 think 的动态上下文
    focus = state.get("conversation_focus")
    if focus:
        dynamic_parts.append(
            f"当前对话焦点：主题={focus.get('topic', '')}, "
            f"实体={focus.get('entity', '')}, 意图={focus.get('intent', '')}"
        )

    drift = state.get("drift_info")
    if drift and drift.get("is_drift"):
        dynamic_parts.append(
            "注意：用户可能切换了话题，请关注当前问题的独立完整性。"
        )
```

---

### 3.6 P4-F (P1): 偏好偏移检测 (PreferenceDriftDetector)

> **执行方式**: 同步（prepare_chat 关键路径），规则检测零 Token，不阻塞首 token。

#### 3.6.1 设计

用户在对话中可能显式改变回答风格偏好：
- "简单点" / "太长了" / "简洁" → 偏好简洁
- "详细点" / "展开说" / "具体" → 偏好详细
- "用英文回答" / "in English" → 偏好英文
- "用中文回答" → 偏好中文
- "不要代码" / "给代码" → 代码偏好

```python
@dataclass
class PreferenceDriftResult:
    has_preference_change: bool
    preference_type: str    # "concise" / "detailed" / "language" / "code"
    new_value: str          # "concise" / "detailed" / "en" / "zh" / "no_code" / "with_code"
    detected_from: str      # "rule"

class PreferenceDriftDetector:
    """偏好偏移检测器 — 纯规则，零 LLM Token。

    在 prepare_chat 中同步执行，检测结果注入 system prompt。
    """

    _PREFERENCE_RULES: dict[str, list[str]] = {
        "concise": ["简单点", "太长了", "简洁", "简短", "精简", "少说"],
        "detailed": ["详细点", "展开", "具体", "详尽", "多说", "详细说明"],
        "en": ["用英文", "in english", "answer in english", "用英语"],
        "zh": ["用中文", "用汉语", "answer in chinese"],
        "no_code": ["不要代码", "别给代码", "不用代码"],
        "with_code": ["给代码", "要代码", "带代码", "show code"],
    }

    def detect(self, query: str, current_preferences: dict | None = None) -> PreferenceDriftResult:
        """纯规则检测 — 扫描查询中的偏好关键词。"""
        query_lower = query.lower()
        for pref_type, keywords in self._PREFERENCE_RULES.items():
            for kw in keywords:
                if kw in query_lower:
                    return PreferenceDriftResult(
                        has_preference_change=True,
                        preference_type=pref_type.split("_")[0] if "_" in pref_type else pref_type,
                        new_value=pref_type,
                        detected_from="rule",
                    )
        return PreferenceDriftResult(
            has_preference_change=False,
            preference_type="",
            new_value="",
            detected_from="rule",
        )
```

集成点：`prepare_chat` 中在焦点提取之后、指代消解之前执行。检测结果存入 `PreparedChat.preference_overrides`，在 `stream_chat` 中注入 system prompt。

---

### 3.7 P4-G (P2): 重复提问检测 (RepetitionDetector)

> **执行方式**: 同步（prepare_chat），复用 DriftDetector 的 embedding，零额外 Token。

#### 3.7.1 设计

用户连续提问高度相似的问题（cosine > 0.85），说明上一轮回答未满足需求。

```python
@dataclass
class RepetitionResult:
    is_repetition: bool
    similarity_score: float
    previous_query: str | None
    repetition_count: int   # 连续重复次数
    action: str             # "expand_retrieval" / "none"

class RepetitionDetector:
    """重复提问检测器 — 复用 embedding，零额外成本。"""

    _REPETITION_THRESHOLD: float = 0.85
    _MAX_REPETITION_COUNT: int = 3

    async def check(
        self,
        current_query: str,
        history: list[dict[str, str]],
        current_embedding: list[float] | None = None,
    ) -> RepetitionResult:
        """检测当前查询是否与最近的用户查询高度相似。"""
        # 提取历史中的 user 消息
        recent_user_msgs = [m for m in history[-6:] if m.get("role") == "user"]
        if not recent_user_msgs:
            return RepetitionResult(False, 0.0, None, 0, "none")

        last_query = recent_user_msgs[-1].get("content", "")
        if not last_query:
            return RepetitionResult(False, 0.0, None, 0, "none")

        # 复用 embedding 或重新计算
        similarity = await self._compute_similarity(
            current_query, last_query, current_embedding
        )

        if similarity < self._REPETITION_THRESHOLD:
            return RepetitionResult(False, similarity, last_query, 0, "none")

        # 计算连续重复次数
        count = 1
        for msg in reversed(recent_user_msgs[:-1]):
            sim = await self._compute_similarity(current_query, msg.get("content", ""))
            if sim >= self._REPETITION_THRESHOLD:
                count += 1
            else:
                break

        action = "expand_retrieval" if count >= 2 else "none"
        return RepetitionResult(True, similarity, last_query, count, action)
```

集成点：`prepare_chat` 中在 DriftDetector 之后执行（复用 embedding）。当 `action == "expand_retrieval"` 时，在 `engine.answer()` 中扩大 top_k 或切换检索策略。

---

## 4. 配置项

```python
# config.py 新增 P4 配置

# P4-A: 漂移检测
DRIFT_DETECTION_ENABLED: bool = True
DRIFT_SIMILARITY_THRESHOLD: float = 0.4      # cosine < 0.4 = 漂移
DRIFT_POSSIBLE_THRESHOLD: float = 0.6        # 0.4-0.6 = 可能漂移

# P4-B: 矛盾检测
CONTRADICTION_DETECTION_ENABLED: bool = True
CONTRADICTION_CHECK_USER_STATEMENTS: bool = True   # 用户陈述矛盾
CONTRADICTION_CHECK_ANSWER_CONSISTENCY: bool = True # 回答-知识库矛盾
CONTRADICTION_CHECK_DOC_CONTRADICTION: bool = False # 文档间矛盾（默认关，开销大）

# P4-C: 指代消解增强
COREFERENCE_INJECT_HISTORY: bool = True       # 注入历史到 LLM prompt
COREFERENCE_FOCUS_STACK_SIZE: int = 5         # 焦点栈大小

# P4-D: 检索匹配检测
RETRIEVAL_MATCH_CHECK_ENABLED: bool = True
RETRIEVAL_MATCH_THRESHOLD: float = 0.3        # top-1 相似度 < 0.3 = 不匹配

# P4-F: 偏好偏移检测
PREFERENCE_DRIFT_ENABLED: bool = True

# P4-G: 重复提问检测
REPETITION_DETECTION_ENABLED: bool = True
REPETITION_SIMILARITY_THRESHOLD: float = 0.85  # cosine > 0.85 = 重复
```

---

## 5. Task 拆分

### P0 (必须完成)

| Task ID | 内容 | 文件 | 预计测试数 |
|---------|------|------|-----------|
| P4-A-1 | DriftDetector 核心实现 | `app/context/drift_detector.py` | 12 |
| P4-A-2 | 集成到 prepare_chat + SSE 事件 | `chat_service.py`, `sse.py` | — |
| P4-A-3 | 配置项 + config 验证 | `config.py` | — |
| P4-B-1 | ContradictionDetector 核心实现 | `app/context/contradiction_detector.py` | 15 |
| P4-B-2 | 集成到 prepare_chat (用户矛盾) | `chat_service.py` | — |
| P4-B-3 | 集成到 _reflect (回答-知识库矛盾) | `engine.py` | — |
| P4-B-4 | SSE 事件 + 配置项 | `sse.py`, `config.py` | — |

### P1 (应该完成)

| Task ID | 内容 | 文件 | 预计测试数 |
|---------|------|------|-----------|
| P4-C-1 | 焦点历史栈 (TopicTracker 改造) | `focus_tracker.py` | 8 |
| P4-C-2 | 增强指代消解 (历史注入 + 焦点栈) | `coreference_resolver.py` | 10 |
| P4-C-3 | 与 Query Rewriter 协调 | `chat_service.py`, `engine.py` | — |
| P4-E-1 | AgentState 加 focus 字段 | `engine.py` | — |
| P4-E-2 | focus 传入 engine.answer() + _think 注入 | `engine.py`, `chat_service.py` | 5 |
| P4-F-1 | PreferenceDriftDetector 核心实现 | `app/context/preference_drift_detector.py` | 8 |
| P4-F-2 | 集成到 prepare_chat + system prompt 注入 | `chat_service.py` | — |

### P2 (可以做)

| Task ID | 内容 | 文件 | 预计测试数 |
|---------|------|------|-----------|
| P4-D-1 | RetrievalMatcher 核心实现 | `app/context/retrieval_matcher.py` | 8 |
| P4-D-2 | 集成到 _retrieve | `engine.py` | — |
| P4-B-5 | 检索结果文档间矛盾检测 | `engine.py` | 5 |
| P4-G-1 | RepetitionDetector 核心实现 | `app/context/repetition_detector.py` | 6 |
| P4-G-2 | 集成到 prepare_chat + 检索策略调整 | `chat_service.py`, `engine.py` | — |

---

## 6. 延迟预算分析

用户确认准实时可接受。每轮对话新增延迟预算：

| 组件 | 触发条件 | 方法 | 延迟 | Token 成本 |
|------|---------|------|------|-----------|
| DriftDetector Level 1 | 每轮 | 规则 | <1ms | 0 |
| DriftDetector Level 2 | 规则无法判断 | Embedding ×2 | ~50-100ms | 0 |
| ContradictionDetector (用户) | 共同实体预筛通过 | LLM (max 150) | ~500-800ms | ~150 |
| ContradictionDetector (回答) | reflect 阶段 | LLM (max 150) | ~500-800ms | ~150 |
| CoreferenceResolver (增强) | needs_resolution | LLM (max 100) | ~400-600ms | ~100 |
| RetrievalMatcher | 每轮检索后 | Embedding ×N | ~100-200ms | 0 |

**首 token 延迟**（关键路径，同步）: DriftDetector（~50-100ms）+ CoreferenceResolver（~400-600ms，仅需要时）≈ 500-700ms
**后台延迟**（不阻塞首 token）: ContradictionDetector(用户) ≈ 500-800ms，完成后推 SSE 事件
**Agent Loop 内延迟**（不阻塞 prepare_chat）: ContradictionDetector(回答) + RetrievalMatcher

混合执行模型下，首 token 延迟仅比 P3 多 ~50-100ms（DriftDetector embedding），ContradictionDetector 在后台运行不增加首 token 等待时间。

**用户可接受**：用户已确认"消息 embedding 延迟可接受"、"FAQ 问答也不可能一直秒回"、"准实时就可以"。

---

## 7. 降级策略

| 组件 | 降级条件 | 降级行为 |
|------|---------|---------|
| DriftDetector | embedder 不可用 | 只用规则检测（Level 1） |
| DriftDetector | 所有检测失败 | 不检测，沿用 P3 焦点继承 |
| ContradictionDetector | LLM 不可用 | 跳过检测，不阻断对话 |
| ContradictionDetector | 共同实体预筛不通过 | 跳过 LLM 检测（零成本） |
| CoreferenceResolver (增强) | LLM 不可用 | 规则降级（P3 已有） |
| RetrievalMatcher | embedder 不可用 | 跳过匹配检测 |
| 焦点传入引擎 | focus 为 None | 引擎不感知焦点（P3 行为） |

**核心原则**: 所有 P4 组件失败时，系统行为退化为 P3，不引入新 bug。

---

## 8. 测试计划

### 8.1 P0 测试

**test_drift_detector.py** (12 tests):
- 规则检测：话题完全切换 → drift
- 规则检测：实体切换同话题 → no drift
- 规则检测：无焦点 → no drift
- Embedding 检测：相似度 < 0.4 → drift
- Embedding 检测：相似度 0.4-0.6 → possible drift
- Embedding 检测：相似度 > 0.6 → no drift
- 置信度衰减：连续 3 轮低置信度 → drift
- Embedder 异常 → 降级为规则
- reset_focus 清空栈
- DriftResult 序列化

**test_contradiction_detector.py** (15 tests):
- 用户陈述矛盾检测（LLM 成功）
- 用户陈述一致（LLM 返回 consistent）
- 用户陈述无关（LLM 返回 unrelated）
- 无共同实体预筛跳过
- 历史不足 2 条跳过
- LLM 异常降级
- 回答-知识库矛盾检测
- 回答-知识库一致
- 无检索文档跳过
- 文档间矛盾检测
- 文档间一致
- 文档数不足跳过
- ContradictionResult 序列化
- 配置开关关闭

### 8.2 P1 测试

**test_focus_tracker_enhanced.py** (8 tests):
- 焦点栈 push/pop
- 栈大小限制
- get_focus_history
- reset_focus 清空栈
- _last_focus 兼容属性
- 漂移后重置 + 新焦点提取

**test_coreference_enhanced.py** (10 tests):
- 历史注入 LLM prompt
- 焦点栈注入 LLM prompt
- 多轮跨指代（回溯 2 轮）
- 历史为空时降级
- LLM 异常规则降级
- 与 Query Rewriter 协调（resolved 跳过 rewrite）
- query_was_resolved 标记

**test_preference_drift_detector.py** (8 tests):
- 检测"简单点" → concise
- 检测"详细点" → detailed
- 检测"用英文" → en
- 检测"用中文" → zh
- 检测"不要代码" → no_code
- 检测"给代码" → with_code
- 无偏好关键词 → no change
- PreferenceDriftResult 序列化

### 8.3 P2 测试

**test_retrieval_matcher.py** (8 tests):
- 匹配（相似度 > 0.3）
- 不匹配（相似度 < 0.3）
- 无文档跳过
- embedder 异常降级
- 空查询跳过

**test_repetition_detector.py** (6 tests):
- 高相似度 (>0.85) → 重复
- 低相似度 (<0.85) → 非重复
- 连续重复 3 次 → expand_retrieval
- 无历史 → 非重复
- embedder 异常降级
- RepetitionResult 序列化

### 8.4 回归测试

- 全部 P3 测试（58 tests）必须通过
- chat_service、engine、config、sse 受影响模块测试通过
- 全量测试套件无新增失败

---

## 9. 实现顺序

```
P0:
  1. config.py — P4 配置项
  2. sse.py — 新增 SSE 事件类型
  3. drift_detector.py — 核心实现 + 测试
  4. contradiction_detector.py — 核心实现 + 测试
  5. chat_service.py — 集成 DriftDetector + ContradictionDetector
  6. engine.py — 集成 ContradictionDetector 到 _reflect

P1:
  7. focus_tracker.py — 焦点历史栈
  8. coreference_resolver.py — 增强消解
  9. engine.py — AgentState + focus 传入 + _think 注入
  10. chat_service.py — Query Rewriter 协调
  11. preference_drift_detector.py — 核心实现 + 测试
  12. chat_service.py — 集成 PreferenceDriftDetector

P2:
  13. retrieval_matcher.py — 核心实现 + 测试
  14. engine.py — 集成到 _retrieve
  15. repetition_detector.py — 核心实现 + 测试
  16. chat_service.py — 集成 RepetitionDetector
```

每步完成后运行对应测试，全部通过后再进入下一步。
P0 全部完成后运行 P3 回归测试。
P1 全部完成后运行全量回归测试。
P2 全部完成后运行全量回归测试。
