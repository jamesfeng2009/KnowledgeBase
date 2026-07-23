# P1 IntentRouter + P2 EntityRegistry 设计方案

> 基于 2026-07-23 项目全面 review 制定

---

## 一、项目现状总结

### 已有基础设施

| 能力 | 实现位置 | 状态 |
|------|---------|------|
| Agent Loop（think→retrieve→generate） | `app/rag/engine.py` AgenticRAGEngine | 完整，每次请求至少 2 次 LLM 调用 |
| 危险操作拦截 | `app/rag/tool_guard.py` DangerousToolGuard | 完整，写操作需 HITL 确认 |
| 权限前置过滤 | `engine.py` _retrieve() 中 permission_filter | 完整，ABAC 在重排前过滤 |
| 查询重写/扩展 | `app/rag/query_rewriter.py` | 完整，4 种策略可配置 |
| 技能发现 | `app/rag/skill_finder.py` + `skill_registry.py` | 完整，渐进式加载 |
| Neo4j 图谱 | `app/services/graph_service.py` | 部分，6 类节点声明但只用 Concept |
| 三元组提取 | `graph_service.py` extract_triples | 部分，规则+LLM 但无类型归一化 |
| 混合检索 | `app/rag/retriever.py` HybridRetriever | 完整，向量+BM25+跨模态三路 |
| ABAC 权限 | `app/services/permission_service.py` | 完整，角色+密级+成员关系 |
| 多租户隔离 | DB RLS + Repository 过滤 | 完整，但 Neo4j 查询缺 tenant_id 过滤 |

### 核心问题

**P1 问题：** 所有查询都走 Agent Loop，即使"列出我的知识库"这种确定性操作也要 LLM think 1 次 + LLM generate 1 次 = 2 次 LLM 调用。

**P2 问题：** 图谱实体全部硬编码为 Concept，关系类型是中文原词（"属于"而非 BELONGS_TO），无同义词归并，图谱检索未接入检索管线。

---

## 二、P1 设计 — IntentRouter 稳态/敏态分离

### 2.1 设计目标

在 `ChatService.prepare_chat()` 之后、`stream_chat()` 之前插入意图路由层：

- **80% 日常查询**（文档搜索、知识库浏览、文档详情）→ 跳过 Agent Loop，直接走确定性检索 + 1 次 LLM 生成
- **20% 复杂查询**（多跳推理、工具编排、工作流）→ 原有 Agent Loop 不变
- 意图识别**优先规则匹配**（零 Token），匹配失败时 LLM fallback

### 2.2 架构设计

```
ChatAPI /chat/stream
  │
  ├─ ChatService.prepare_chat()          [已有] 会话+记忆+模型解析
  │
  ├─ IntentRouter.route(query, context)  [新增] 意图路由
  │     │
  │     ├─ RuleMatcher.match()           规则匹配（零 Token）
  │     │     ├─ "搜索/查找/查一下" → intent=rag_search
  │     │     ├─ "列出/有哪些/列表" → intent=list_documents
  │     │     ├─ "创建/上传/添加" → intent=create_document
  │     │     └─ 无匹配 → LLM fallback
  │     │
  │     └─ LLMIntentParser.parse()       LLM 意图解析（仅规则未命中时）
  │           └─ function calling → intent + parameters
  │
  ├─ 路由分流
  │     ├─ 快捷路径（rag_search / list_documents）
  │     │     ├─ HybridRetriever.search()    确定性检索（零 LLM）
  │     │     ├─ PermissionFilter             确定性权限过滤
  │     │     ├─ Generator.generate()         1 次 LLM 生成回答
  │     │     └─ 返回 SSE 流
  │     │
  │     └─ 原有路径（复杂查询）
  │           └─ AgenticRAGEngine.answer()   原有 Agent Loop
  │
  └─ stream_chat()                       [已有] SSE 输出
```

### 2.3 核心类设计

```python
# app/intent/__init__.py
# app/intent/router.py

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class IntentType(str, Enum):
    """意图类型"""
    RAG_SEARCH = "rag_search"           # 文档检索问答
    LIST_DOCUMENTS = "list_documents"   # 列出文档/知识库
    GET_DOCUMENT = "get_document"       # 查看文档详情
    CREATE_DOCUMENT = "create_document" # 创建/上传文档
    COMPLEX_QUERY = "complex_query"     # 复杂查询 → 走 Agent Loop

@dataclass
class IntentResult:
    """意图路由结果"""
    intent: IntentType
    confidence: float
    parameters: dict[str, Any] = field(default_factory=dict)
    # 是否跳过 Agent Loop，走确定性快捷路径
    use_shortcut: bool = False
    # 调整后的 memory_context（可选）
    adjusted_context: str | None = None


class IntentRouter:
    """意图路由器 — 规则优先，LLM 兜底"""

    def __init__(self, llm_provider=None):
        self._rule_matcher = RuleMatcher()
        self._llm_parser = LLMIntentParser(llm_provider) if llm_provider else None

    async def route(
        self,
        query: str,
        memory_context: str,
        agent_type: str,
    ) -> IntentResult:
        # 1. 规则匹配（零 Token）
        result = self._rule_matcher.match(query)
        if result and result.confidence >= 0.8:
            return result

        # 2. LLM 意图解析（仅规则未命中时）
        if self._llm_parser:
            try:
                result = await self._llm_parser.parse(query, memory_context)
                if result:
                    return result
            except Exception:
                pass  # 降级到复杂查询

        # 3. 兜底 → 走 Agent Loop
        return IntentResult(
            intent=IntentType.COMPLEX_QUERY,
            confidence=0.0,
            use_shortcut=False,
        )
```

```python
# app/intent/rule_matcher.py

import re

class RuleMatcher:
    """规则匹配器 — 零 Token 意图识别"""

    # 规则定义：意图 → 正则模式列表
    _RULES: list[tuple[IntentType, list[re.Pattern], float]] = [
        (IntentType.LIST_DOCUMENTS, [
            re.compile(r"(列出|列表|有哪些|显示).*(文档|知识库|文件)", re.I),
            re.compile(r"(list|show|all)\s+(documents?|knowledge)", re.I),
        ], 0.9),

        (IntentType.RAG_SEARCH, [
            re.compile(r"(搜索|查找|查一下|搜一下|查询|找一下).+", re.I),
            re.compile(r"(search|find|query|look\s+for)\s+", re.I),
            re.compile(r".+(是什么|怎么|如何|为什么|什么是)", re.I),
        ], 0.85),

        (IntentType.GET_DOCUMENT, [
            re.compile(r"(查看|打开|看看|详情).+", re.I),
            re.compile(r"(view|open|detail|get)\s+", re.I),
        ], 0.85),

        (IntentType.CREATE_DOCUMENT, [
            re.compile(r"(创建|上传|添加|新建).*(文档|文件|知识)", re.I),
            re.compile(r"(create|upload|add|new)\s+(document|file)", re.I),
        ], 0.9),
    ]

    def match(self, query: str) -> IntentResult | None:
        query_stripped = query.strip()
        if not query_stripped:
            return None

        for intent, patterns, confidence in self._RULES:
            for pattern in patterns:
                if pattern.search(query_stripped):
                    return IntentResult(
                        intent=intent,
                        confidence=confidence,
                        use_shortcut=intent != IntentType.COMPLEX_QUERY,
                    )
        return None
```

```python
# app/intent/llm_parser.py

class LLMIntentParser:
    """LLM 意图解析 — 仅规则未命中时调用"""

    _SYSTEM_PROMPT = """你是企业知识库的意图识别器。分析用户输入，返回 JSON：
    {"intent": "rag_search|list_documents|get_document|create_document|complex_query",
     "confidence": 0.0-1.0,
     "parameters": {}}
    只返回 JSON，不附加解释。"""

    async def parse(self, query: str, context: str) -> IntentResult | None:
        if not self._llm:
            return None
        response = await self._llm.chat(
            messages=[
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": f"上下文: {context[:500]}\n用户输入: {query}"},
            ],
            max_tokens=100,
        )
        # 解析 JSON → IntentResult
        import json
        data = json.loads(response.content)
        intent = IntentType(data["intent"])
        return IntentResult(
            intent=intent,
            confidence=data["confidence"],
            parameters=data.get("parameters", {}),
            use_shortcut=intent != IntentType.COMPLEX_QUERY and data["confidence"] >= 0.7,
        )
```

```python
# app/intent/shortcut_handler.py

from app.rag.retriever import HybridRetriever
from app.rag.generator import Generator
from app.services.permission_service import PermissionService

class ShortcutHandler:
    """快捷路径处理器 — 确定性执行 + 1 次 LLM 生成"""

    async def handle(
        self,
        intent: IntentResult,
        query: str,
        user: User,
        db: AsyncSession,
        kb_ids: list[str] | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[SSEEvent | str]:
        """处理快捷路径意图，返回 SSE 流"""

        if intent.intent == IntentType.RAG_SEARCH:
            async for event in self._handle_search(query, user, db, kb_ids, memory_context):
                yield event
        elif intent.intent == IntentType.LIST_DOCUMENTS:
            async for event in self._handle_list(user, db, kb_ids):
                yield event
        # ... 其他快捷意图

    async def _handle_search(self, query, user, db, kb_ids, memory_context):
        # 1. 确定性检索（零 LLM）
        retriever = HybridRetriever()
        candidates = await retriever.search(query, kb_ids=kb_ids, top_k=20)

        # 2. 确定性权限过滤（零 LLM）
        perm = PermissionService(db, user)
        filtered = await perm.filter_documents(candidates)

        # 3. 确定性重排（零 LLM）
        reranker = get_reranker()
        reranked = await reranker.rerank(query, filtered, top_k=5)

        # 4. yield sources 事件
        yield SSEEvent(data={"sources": reranked}, event=SSEEventType.SOURCES)

        # 5. LLM 生成回答（1 次 LLM 调用）
        generator = Generator(get_llm_provider())
        async for token in generator.generate(query, reranked, [], memory_context):
            yield token

        # 6. yield done 事件
        yield SSEEvent(data={"token_count": ..., "shortcut": True}, event=SSEEventType.DONE)
```

### 2.4 ChatService 集成点

```python
# app/services/chat_service.py — 修改 stream_chat()

class ChatService:
    def __init__(self, db, user, tenant_id=None):
        # ... 现有初始化
        self._intent_router = IntentRouter(llm_provider=get_llm_provider())
        self._shortcut_handler = ShortcutHandler()

    async def stream_chat(self, prepared: PreparedChat) -> AsyncIterator[SSEEvent | str]:
        # [新增] 意图路由
        intent = await self._intent_router.route(
            prepared.query, prepared.memory_context, prepared.agent_type
        )

        # yield intent 事件（前端可展示意图识别结果）
        yield SSEEvent(data={"intent": intent.intent.value, "confidence": intent.confidence},
                       event=SSEEventType.INTENT)

        if intent.use_shortcut:
            # 快捷路径 — 确定性检索 + 1 次 LLM 生成
            async for event in self._shortcut_handler.handle(
                intent, prepared.query, self.user, self._db,
                kb_ids=None, memory_context=prepared.memory_context
            ):
                if isinstance(event, str):
                    self._full_response_parts.append(event)
                yield event
        else:
            # 原有路径 — Agent Loop
            engine = get_rag_engine()
            async for chunk in engine.answer(
                prepared.query, str(self.user.id), str(prepared.conversation_id),
                memory_context=prepared.memory_context,
                tenant_id=str(prepared.tenant_id) if prepared.tenant_id else None,
            ):
                if isinstance(chunk, str):
                    self._full_response_parts.append(chunk)
                yield chunk
```

### 2.5 SSE 事件类型扩展

```python
# app/utils/sse.py — 新增 INTENT 事件类型
class SSEEventType(str, Enum):
    META = "meta"
    INTENT = "intent"              # [新增] 意图识别结果
    QUERY_REWRITE = "query_rewrite"
    THINKING = "thinking"
    RETRIEVE_START = "retrieve_start"
    RETRIEVE_END = "retrieve_end"
    TOOL_CALL_START = "tool_call_start"
    APPROVAL_REQUIRED = "approval_required"
    TOOL_CALL_END = "tool_call_end"
    SOURCES = "sources"
    QUALITY = "quality"
    DONE = "done"
```

### 2.6 配置项

```python
# app/config.py — 新增
INTENT_ROUTER_ENABLED: bool = True          # IntentRouter 总开关
INTENT_ROUTER_LLM_FALLBACK: bool = True     # 规则未命中时是否调 LLM
INTENT_ROUTER_CONFIDENCE_THRESHOLD: float = 0.7  # LLM 意图置信度阈值
INTENT_SHORTCUT_ENABLED: bool = True        # 是否启用快捷路径
```

---

## 三、P2 设计 — EntityRegistry 企业本体

### 3.1 设计目标

- **写入侧：** 文档入库时三元组提取后，经过 EntityRegistry 归一化（实体类型分类 + 同义词归并 + 谓词映射），再写入 Neo4j
- **查询侧：** 检索前用 EntityRegistry 做实体识别 + 同义词扩展 + 图谱关系扩展，作为 HybridRetriever 的第四路召回
- **不涉及：** 跨系统字段映射、业务规则引擎、SOP 操作手册（知识库不承担 ERP 职责）

### 3.2 架构设计

```
写入侧（文档入库）：
  GraphService.extract_triples()
    → [新增] EntityRegistry.normalize_triples()  归一化
      ├─ 实体类型分类：Concept → Concept/Policy/Product/Person/Department
      ├─ 同义词归并："合约"="合同"="协议" → canonical "contract"
      └─ 谓词映射："属于"→BELONGS_TO, "引用"→REFERENCES
    → GraphService.batch_import_graph()  写入 Neo4j（标准 label + 关系）

查询侧（用户检索）：
  IntentRouter → RAG_SEARCH 快捷路径
    → [新增] EntityRegistry.resolve_query()  查询实体识别
      ├─ 同义词解析："回款" → "payment_received"
      └─ 关系扩展：contract → customer → product
    → HybridRetriever.search()
      ├─ _vector_search()     向量召回（已有）
      ├─ _fulltext_search()   BM25 召回（已有）
      ├─ _cross_modal_search() 跨模态召回（已有）
      └─ [新增] _graph_search()  图谱召回（新增第四路）
    → _merge_and_dedupe()
```

### 3.3 核心类设计

```python
# app/ontology/__init__.py
# app/ontology/entity_registry.py

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class EntityType(str, Enum):
    """标准实体类型 — 对齐 GraphService 已有声明的 6 类节点"""
    DOCUMENT = "Document"
    CONCEPT = "Concept"
    POLICY = "Policy"
    PRODUCT = "Product"
    DEPARTMENT = "Department"
    PERSON = "Person"

class RelationType(str, Enum):
    """标准关系类型 — 对齐 GraphService 已有声明的 8 种关系"""
    REFERENCES = "REFERENCES"
    MENTIONS = "MENTIONS"
    RELATES_TO = "RELATES_TO"
    REPLACES = "REPLACES"
    BELONGS_TO = "BELONGS_TO"
    HYPERNYM = "HYPERNYM"
    AUTHORED_BY = "AUTHORED_BY"
    APPROVED_BY = "APPROVED_BY"


@dataclass
class EntityDefinition:
    """实体定义"""
    canonical_name: str           # 规范名称 "contract"
    display_name: str             # 显示名称 "合同"
    entity_type: EntityType       # 实体类型
    synonyms: list[str]           # 同义词 ["合约", "协议", "contract"]
    description: str = ""


@dataclass
class TripleNormalization:
    """三元组归一化结果"""
    subject_canonical: str
    subject_type: EntityType
    predicate_standard: RelationType
    object_canonical: str
    object_type: EntityType


class EntityRegistry:
    """实体注册表 — 同义词索引 + 类型分类 + 谓词映射"""

    _entities: dict[str, EntityDefinition] = {}        # canonical_name → definition
    _synonym_index: dict[str, str] = {}                # synonym/alias → canonical_name
    _predicate_map: dict[str, RelationType] = {}       # 中文谓词 → 标准关系类型

    @classmethod
    def register(cls, entity: EntityDefinition) -> None:
        """注册实体定义"""
        cls._entities[entity.canonical_name] = entity
        cls._synonym_index[entity.canonical_name] = entity.canonical_name
        cls._synonym_index[entity.display_name] = entity.canonical_name
        for syn in entity.synonyms:
            cls._synonym_index[syn.lower()] = entity.canonical_name

    @classmethod
    def register_predicate(cls, chinese_pred: str, standard: RelationType) -> None:
        """注册谓词映射"""
        cls._predicate_map[chinese_pred] = standard

    @classmethod
    def resolve_entity(cls, term: str) -> EntityDefinition | None:
        """同义词解析 — "合约" → EntityDefinition(canonical="contract")"""
        canonical = cls._synonym_index.get(term.lower())
        if not canonical:
            return None
        return cls._entities.get(canonical)

    @classmethod
    def normalize_triple(
        cls, subject: str, predicate: str, object_: str
    ) -> TripleNormalization:
        """归一化三元组 — 写入 Neo4j 前调用"""
        # 实体归一化
        s_def = cls.resolve_entity(subject)
        o_def = cls.resolve_entity(object_)

        s_canonical = s_def.canonical_name if s_def else subject
        s_type = s_def.entity_type if s_def else EntityType.CONCEPT
        o_canonical = o_def.canonical_name if o_def else object_
        o_type = o_def.entity_type if o_def else EntityType.CONCEPT

        # 谓词映射
        pred_standard = cls._predicate_map.get(predicate, RelationType.RELATES_TO)

        return TripleNormalization(
            subject_canonical=s_canonical,
            subject_type=s_type,
            predicate_standard=pred_standard,
            object_canonical=o_canonical,
            object_type=o_type,
        )

    @classmethod
    def expand_query(cls, query: str) -> tuple[list[str], list[str]]:
        """查询扩展 — 返回 (同义词扩展词列表, 关联实体列表)

        用于检索前的实体识别 + 关系扩展。
        """
        expanded_terms: list[str] = []
        related_entities: list[str] = []

        # 简单分词匹配（后续可升级为 jieba 分词）
        for term in cls._split_terms(query):
            entity = cls.resolve_entity(term)
            if entity:
                # 同义词扩展
                expanded_terms.extend(entity.synonyms)
                expanded_terms.append(entity.display_name)
                # 关联实体（通过图谱关系查询，延迟到 GraphService 调用时）

        return expanded_terms, related_entities

    @classmethod
    def _split_terms(cls, text: str) -> list[str]:
        """简易分词 — 2-4 字滑窗（与 SkillFinder 策略对齐）"""
        terms = []
        # 中文 2-4 字滑窗
        for n in (4, 3, 2):
            for i in range(len(text) - n + 1):
                terms.append(text[i:i+n])
        # 英文单词
        import re
        terms.extend(re.findall(r'[a-zA-Z_]+', text))
        return terms
```

```python
# app/ontology/predicates.py — 预置谓词映射表

"""中文谓词 → 标准关系类型映射表"""

# 对齐 GraphService._RULE_PATTERNS 中的 11 个规则模板
PREDICATE_MAPPINGS = {
    "属于": RelationType.BELONGS_TO,
    "包含": RelationType.RELATES_TO,
    "引用": RelationType.REFERENCES,
    "替代": RelationType.REPLACES,
    "依赖": RelationType.RELATES_TO,
    "基于": RelationType.RELATES_TO,
    "是": RelationType.HYPERNYM,
    "使用": RelationType.RELATES_TO,
    "定义": RelationType.RELATES_TO,
    "管理": RelationType.RELATES_TO,
    "实现": RelationType.RELATES_TO,
    "提及": RelationType.MENTIONS,
    "编写": RelationType.AUTHORED_BY,
    "审批": RelationType.APPROVED_BY,
}
```

```python
# app/ontology/entities.py — 预置实体定义

"""企业常见实体定义 — 可通过 API 动态扩展"""

ENTITY_DEFINITIONS = [
    EntityDefinition(
        canonical_name="contract",
        display_name="合同",
        entity_type=EntityType.CONCEPT,
        synonyms=["合约", "协议", "contract", "agreement"],
    ),
    EntityDefinition(
        canonical_name="customer",
        display_name="客户",
        entity_type=EntityType.PERSON,
        synonyms=["客户", "甲方", "customer", "client"],
    ),
    EntityDefinition(
        canonical_name="product",
        display_name="产品",
        entity_type=EntityType.PRODUCT,
        synonyms=["产品", "商品", "product"],
    ),
    EntityDefinition(
        canonical_name="policy",
        display_name="政策",
        entity_type=EntityType.POLICY,
        synonyms=["政策", "制度", "规定", "policy", "regulation"],
    ),
    EntityDefinition(
        canonical_name="department",
        display_name="部门",
        entity_type=EntityType.DEPARTMENT,
        synonyms=["部门", "科室", "department", "division"],
    ),
    EntityDefinition(
        canonical_name="invoice",
        display_name="发票",
        entity_type=EntityType.CONCEPT,
        synonyms=["发票", "票据", "invoice", "receipt"],
    ),
    EntityDefinition(
        canonical_name="payment",
        display_name="回款",
        entity_type=EntityType.CONCEPT,
        synonyms=["回款", "收款", "付款", "payment", "payment_received"],
    ),
]
```

### 3.4 GraphService 集成 — 三元组归一化

```python
# app/services/graph_service.py — 修改 extract_triples_from_chunks()

async def extract_triples_from_chunks(self, chunks, doc_id, llm_provider=None, ...):
    # ... 现有规则提取 + LLM 提取逻辑不变 ...

    # [新增] 归一化三元组
    from app.ontology.entity_registry import EntityRegistry

    normalized_triples = []
    for triple in raw_triples:
        normalized = EntityRegistry.normalize_triple(
            subject=triple["subject"],
            predicate=triple["predicate"],
            object_=triple["object"],
        )
        normalized_triples.append({
            "subject": normalized.subject_canonical,
            "subject_type": normalized.subject_type.value,  # "Concept" / "Policy" 等
            "predicate": normalized.predicate_standard.value,  # "BELONGS_TO" 等
            "object": normalized.object_canonical,
            "object_type": normalized.object_type.value,
        })

    # [修改] batch_import_graph 接收归一化后的三元组
    await self.batch_import_graph(normalized_triples, doc_id)
```

```python
# app/services/graph_service.py — 修改 batch_import_graph()

async def batch_import_graph(self, triples: list[dict], doc_id: str):
    """批量写入图谱 — 使用标准实体类型和关系类型

    修改点：
    1. 节点 label 使用 triple["subject_type"] / triple["object_type"]，不再硬编码 Concept
    2. 关系类型使用 triple["predicate"]（已是标准英文），不再 upper() 中文
    3. 新增 tenant_id 到节点属性（补齐图谱层租户隔离）
    """
    # Cypher: UNWIND + MERGE
    # 节点: MERGE (n:{label} {name: $name, tenant_id: $tenant_id})
    # 关系: MERGE (s)-[:{rel_type}]->(o)
```

### 3.5 HybridRetriever 集成 — 图谱召回第四路

```python
# app/rag/retriever.py — 新增 _graph_search()

class HybridRetriever:
    async def search(self, query, kb_ids=None, top_k=20):
        # 现有三路并发
        tasks = [
            self._vector_search(query, kb_ids, top_k),
            self._fulltext_search(query, kb_ids, top_k),
        ]
        if self._cross_modal_available():
            tasks.append(self._cross_modal_search(query, kb_ids, top_k))

        # [新增] 第四路：图谱召回
        if self._graph_available():
            tasks.append(self._graph_search(query, kb_ids, top_k))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        # ... 现有 _merge_and_dedupe 逻辑不变 ...

    async def _graph_search(self, query, kb_ids, top_k) -> list[dict]:
        """图谱召回 — 通过实体关系找到关联文档

        流程：
        1. EntityRegistry.expand_query() 识别查询中的实体 + 同义词
        2. GraphService.find_related_nodes() 多跳遍历找到关联 Document 节点
        3. 返回关联文档的 chunk（标记 source="graph"）
        """
        from app.ontology.entity_registry import EntityRegistry
        from app.services.graph_service import GraphService

        # 1. 实体识别 + 同义词扩展
        expanded_terms, _ = EntityRegistry.expand_query(query)
        if not expanded_terms:
            return []  # 查询中无已知实体，跳过图谱召回

        # 2. 图谱遍历
        graph = GraphService()
        related_docs = []
        for term in expanded_terms:
            try:
                nodes = await graph.find_related_nodes(
                    entity_name=term,
                    max_depth=2,  # 2 跳遍历
                )
                # 提取关联的 Document 节点
                for node in nodes:
                    if node.get("label") == "Document":
                        related_docs.append({
                            "doc_id": node.get("doc_id"),
                            "title": node.get("title", ""),
                            "score": 0.5,  # 图谱召回固定分，后续可加权
                            "source": "graph",
                        })
            except Exception:
                continue  # Neo4j 不可用时优雅降级

        # 3. 去重 + 截断
        seen = set()
        result = []
        for doc in related_docs:
            if doc["doc_id"] not in seen:
                seen.add(doc["doc_id"])
                result.append(doc)
        return result[:top_k]
```

### 3.6 Neo4j 租户隔离补齐

```python
# app/services/graph_service.py — 所有 Cypher 查询增加 tenant_id 过滤

async def find_related_nodes(self, entity_name, max_depth=2, tenant_id=None):
    """图遍历 — 增加 tenant_id 过滤"""
    tenant_filter = ""
    params = {"name": entity_name, "max_depth": max_depth}
    if tenant_id:
        tenant_filter = "AND n.tenant_id = $tenant_id"
        params["tenant_id"] = str(tenant_id)

    cypher = f"""
        MATCH (n {{name: $name}})
        WHERE n.tenant_id IS NULL {tenant_filter}
        MATCH (n)-[r*1..$max_depth]->(m)
        WHERE m.tenant_id IS NULL {tenant_filter}
        RETURN DISTINCT m
    """
    # ... 执行查询 ...
```

### 3.7 配置项

```python
# app/config.py — 新增
ENTITY_REGISTRY_ENABLED: bool = True       # EntityRegistry 总开关
GRAPH_SEARCH_ENABLED: bool = True          # 图谱召回开关
GRAPH_SEARCH_MAX_DEPTH: int = 2            # 图谱遍历最大跳数
GRAPH_SEARCH_MAX_RESULTS: int = 10         # 图谱召回最大结果数
GRAPH_SEARCH_SCORE: float = 0.5            # 图谱召回固定分（合并时的权重）
```

---

## 四、P1 × P2 协同设计

```
用户输入 "帮我查一下华为的合同"
  │
  ├─ P1 IntentRouter.route()
  │    └─ RuleMatcher → intent=RAG_SEARCH, confidence=0.85, use_shortcut=True
  │
  ├─ P1 ShortcutHandler._handle_search()
  │    │
  │    ├─ P2 EntityRegistry.expand_query("华为的合同")
  │    │    ├─ "合同" → resolve → canonical="contract"
  │    │    │    synonyms: ["合约", "协议", "contract", "agreement"]
  │    │    └─ "华为" → 未注册（不在预置实体中）→ 原词保留
  │    │    → expanded_terms = ["合约", "协议", "contract", "agreement"]
  │    │
  │    ├─ HybridRetriever.search("华为的合同 合约 协议 contract agreement")
  │    │    ├─ _vector_search()    向量召回
  │    │    ├─ _fulltext_search()  BM25 召回（含扩展词）
  │    │    ├─ _cross_modal_search() 跨模态召回
  │    │    └─ _graph_search()     图谱召回（contract → customer → 关联文档）
  │    │    → _merge_and_dedupe()
  │    │
  │    ├─ PermissionService.filter_documents()  权限过滤
  │    ├─ Reranker.rerank()                     重排
  │    └─ Generator.generate()                  1 次 LLM 生成回答
  │
  └─ SSE 流输出
       event: intent    → {"intent": "rag_search", "confidence": 0.85}
       event: sources   → [{doc_id, title, score}...]
       data: tokens      → "根据检索结果，华为相关的合同如下..."
       event: done       → {"token_count": 150, "shortcut": true}
```

**Token 消耗对比：**

| 场景 | 现有架构 | P1+P2 后 |
|------|---------|----------|
| "查一下合同文档" | think(1) + generate(1) = 2 次 LLM | generate(1) = 1 次 LLM |
| "列出所有知识库" | think(1) + generate(1) = 2 次 LLM | 0 次 LLM（确定性返回） |
| "华为的合同有哪些" | think(1) + retrieve + generate(1) = 2 次 LLM | generate(1) = 1 次 LLM + 图谱扩展 |
| "分析微服务架构的优缺点" | think(2) + retrieve + generate(1) = 3 次 LLM | 不变（走 Agent Loop） |

---

## 五、任务拆分

### P1 — IntentRouter（7 个任务）

#### P1-T1: 创建 intent 模块骨架
- **优先级：** P0
- **文件：**
  - `app/intent/__init__.py`
  - `app/intent/router.py` — IntentRouter + IntentType + IntentResult
  - `app/intent/rule_matcher.py` — RuleMatcher
  - `app/intent/llm_parser.py` — LLMIntentParser
  - `app/intent/shortcut_handler.py` — ShortcutHandler
- **依赖：** 无
- **验收：** 模块可 import，IntentRouter.route() 返回 IntentResult

#### P1-T2: 规则匹配器实现
- **优先级：** P0
- **文件：** `app/intent/rule_matcher.py`
- **内容：** 4 种意图的正则规则（RAG_SEARCH / LIST_DOCUMENTS / GET_DOCUMENT / CREATE_DOCUMENT），中英文双语
- **依赖：** P1-T1
- **验收：** 20 个测试用例覆盖中英文意图识别

#### P1-T3: 快捷路径处理器实现
- **优先级：** P0
- **文件：** `app/intent/shortcut_handler.py`
- **内容：** ShortcutHandler.handle() + _handle_search() + _handle_list()，复用现有 HybridRetriever + PermissionService + Reranker + Generator
- **依赖：** P1-T1, P1-T2
- **验收：** 快捷路径返回正确 SSE 流（sources + tokens + done）

#### P1-T4: ChatService 集成
- **优先级：** P0
- **文件：** `app/services/chat_service.py`（修改 stream_chat 方法）
- **内容：** 在 stream_chat 开头插入 IntentRouter.route()，根据 use_shortcut 分流到 ShortcutHandler 或 AgenticRAGEngine
- **依赖：** P1-T3
- **验收：** 原有 chat 功能不受影响，简单查询走快捷路径

#### P1-T5: SSE 事件类型扩展
- **优先级：** P1
- **文件：** `app/utils/sse.py`（新增 INTENT 事件类型）
- **内容：** SSEEventType.INTENT + SSEEventType.DONE 增加 shortcut 字段
- **依赖：** P1-T4
- **验收：** 前端可接收 intent 事件

#### P1-T6: 配置项 + LLM fallback
- **优先级：** P1
- **文件：** `app/config.py`（新增配置项）、`app/intent/llm_parser.py`
- **内容：** INTENT_ROUTER_ENABLED / INTENT_ROUTER_LLM_FALLBACK / INTENT_SHORTCUT_ENABLED 配置项，LLMIntentParser 实现
- **依赖：** P1-T2
- **验收：** 规则未命中时 LLM fallback 正常工作

#### P1-T7: 单元测试 + 集成测试
- **优先级：** P0
- **文件：**
  - `tests/unit/test_intent_router.py` — 规则匹配、意图路由、fallback
  - `tests/unit/test_shortcut_handler.py` — 快捷路径执行
  - `tests/integration/test_chat_with_intent.py` — 端到端 chat 流
- **依赖：** P1-T4
- **验收：** 测试覆盖率 ≥ 85%，原有测试全部通过

---

### P2 — EntityRegistry + 本体增强检索（8 个任务）

#### P2-T1: 创建 ontology 模块骨架
- **优先级：** P0
- **文件：**
  - `app/ontology/__init__.py`
  - `app/ontology/entity_registry.py` — EntityRegistry + EntityType + RelationType
  - `app/ontology/entities.py` — 预置实体定义（7 个核心实体）
  - `app/ontology/predicates.py` — 预置谓词映射（14 个映射）
- **依赖：** 无
- **验收：** 模块可 import，EntityRegistry.resolve_entity("合约") 返回 contract 定义

#### P2-T2: 实体注册表核心实现
- **优先级：** P0
- **文件：** `app/ontology/entity_registry.py`
- **内容：**
  - EntityRegistry.register() / resolve_entity() / normalize_triple() / expand_query()
  - 同义词索引构建（synonym → canonical_name）
  - 谓词映射表（中文 → RelationType）
  - 简易分词（2-4 字滑窗 + 英文单词）
- **依赖：** P2-T1
- **验收：** normalize_triple("合约", "属于", "华为") → (contract, BELONGS_TO, Concept)

#### P2-T3: GraphService 三元组归一化集成
- **优先级：** P0
- **文件：** `app/services/graph_service.py`（修改 extract_triples_from_chunks + batch_import_graph）
- **内容：**
  - extract_triples 提取后调用 EntityRegistry.normalize_triple() 归一化
  - batch_import_graph 使用标准 EntityType label + RelationType 关系
  - 节点属性增加 tenant_id
- **依赖：** P2-T2
- **验收：** 新入库文档的图谱节点使用标准 label（非全部 Concept），关系使用英文标准类型

#### P2-T4: Neo4j 查询租户隔离补齐
- **优先级：** P0
- **文件：** `app/services/graph_service.py`（修改所有 Cypher 查询）
- **内容：** find_related_nodes / get_related_recommendations / get_graph_data 等 5+ 个查询方法增加 `WHERE n.tenant_id = $tenant_id` 过滤
- **依赖：** P2-T3
- **验收：** 不同租户的图谱数据互相隔离

#### P2-T5: HybridRetriever 图谱召回第四路
- **优先级：** P1
- **文件：** `app/rag/retriever.py`（新增 _graph_search 方法）
- **内容：**
  - _graph_search() — EntityRegistry.expand_query → GraphService.find_related_nodes → 关联 Document 召回
  - search() 中并发执行第四路
  - _merge_and_dedupe 处理 graph source 结果
  - Neo4j 不可用时优雅降级（返回空列表）
- **依赖：** P2-T2, P2-T4
- **验收：** 图谱召回返回关联文档，合并去重正确

#### P2-T6: 查询实体识别 + 扩展
- **优先级：** P1
- **文件：** `app/ontology/entity_registry.py`（完善 expand_query 方法）
- **内容：**
  - expand_query() 返回 (同义词列表, 关联实体列表)
  - 集成到 HybridRetriever.search() — 扩展词追加到 BM25 查询
  - 集成到 ShortcutHandler._handle_search() — 查询前实体识别
- **依赖：** P2-T2, P2-T5
- **验收：** "查一下合约" 检索时自动扩展为 "合同 合约 协议 contract"

#### P2-T7: 配置项 + 优雅降级
- **优先级：** P1
- **文件：** `app/config.py`（新增配置项）
- **内容：** ENTITY_REGISTRY_ENABLED / GRAPH_SEARCH_ENABLED / GRAPH_SEARCH_MAX_DEPTH 等配置项，EntityRegistry 不可用时降级
- **依赖：** P2-T5
- **验收：** 关闭配置后系统正常工作（无图谱召回，不影响现有检索）

#### P2-T8: 单元测试 + 集成测试
- **优先级：** P0
- **文件：**
  - `tests/unit/test_entity_registry.py` — 实体注册、同义词解析、三元组归一化
  - `tests/unit/test_graph_search.py` — 图谱召回、降级
  - `tests/integration/test_ontology_retrieval.py` — 端到端检索
- **依赖：** P2-T5, P2-T6
- **验收：** 测试覆盖率 ≥ 85%，原有测试全部通过

---

### 依赖关系图

```
P1-T1 (骨架) ─→ P1-T2 (规则) ─→ P1-T3 (快捷路径) ─→ P1-T4 (ChatService集成)
                   │                                      │
                   └─→ P1-T6 (配置+LLM)                  ├─→ P1-T5 (SSE)
                                                         └─→ P1-T7 (测试)

P2-T1 (骨架) ─→ P2-T2 (核心) ─→ P2-T3 (GraphService) ─→ P2-T4 (租户隔离)
                   │                                       │
                   └─→ P2-T5 (图谱召回) ←──────────────────┘
                         │
                         └─→ P2-T6 (查询扩展) ─→ P2-T7 (配置)
                                                    │
                                                    └─→ P2-T8 (测试)
```

### 执行顺序

```
Phase 1（P1 核心，约 1 周）:
  P1-T1 → P1-T2 → P1-T3 → P1-T4 → P1-T7

Phase 2（P1 完善 + P2 启动，约 1 周）:
  P1-T5, P1-T6（并行）
  P2-T1 → P2-T2（并行）

Phase 3（P2 核心，约 1.5 周）:
  P2-T3 → P2-T4 → P2-T5 → P2-T6

Phase 4（P2 完善，约 0.5 周）:
  P2-T7, P2-T8
```

---

## 六、风险分析

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 规则匹配误判 → 简单查询走 Agent Loop | 中 | 低（降级到原有路径） | 规则保守匹配 + 置信度阈值 + LLM fallback |
| 快捷路径跳过质量守卫 | 中 | 中 | ShortcutHandler 中保留质量评分逻辑 |
| EntityRegistry 分词不准 | 高 | 低（仅影响扩展词，不阻断检索） | 2-4 字滑窗 + 后续可升级 jieba |
| Neo4j 写入归一化后旧数据不兼容 | 中 | 中 | 增量迁移，旧 Concept 节点保留，新数据用标准 label |
| 图谱召回增加检索延迟 | 低 | 低 | 图谱查询 < 30ms，并发执行不阻塞其他路 |
| 前端未适配 INTENT 事件 | 低 | 低 | 未知事件类型前端自动忽略 |
