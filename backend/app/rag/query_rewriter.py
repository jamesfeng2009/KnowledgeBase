"""查询重写模块 — 在检索前优化用户查询，提升召回质量。

P2-B Tasks 1-3: 查询重写 / 扩展 / 分解 / HyDE。

四种策略可独立启用/禁用：
1. **QueryRewrite** — 修正拼写、消歧、补充上下文，使查询更精确
2. **QueryExpansion** — 添加同义词/相关词，扩大召回面
3. **QueryDecomposition** — 将复杂问题分解为子查询，分别检索
4. **HyDE** — 生成假设性答案文档，用其嵌入向量检索（适合语义模糊的查询）

设计要点：
- 所有策略使用 LLM 生成，失败时回退到原始查询（不阻断流程）
- 结果缓存（LRU），同一查询不重复调用 LLM（幂等性）
- 结构化日志记录每次重写的输入/输出/策略/耗时
- 结果通过 ``QueryRewriteResult`` 统一返回，供 engine 和 SSE 使用

幂等保障：
- ``rewrite()`` 对同一输入（query + context）返回一致结果
- LRU 缓存 key = hash(query + context)，避免重复 LLM 调用
- 缓存失效后重新生成，不影响幂等性
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


# ======================================================================
# P1-10: Query 类型分类（规则式，零 LLM 调用）
# ======================================================================


class QueryType(str, Enum):
    """查询类型 — 决定路由到哪些改写策略。

    - SINGLE: 单意图明确查询 → rewrite + expansion（精确化 + 扩召回面）
    - MULTI: 多意图/复合查询 → rewrite + decomposition（拆子查询分别检索）
    - VAGUE: 模糊/语义不清查询 → rewrite + hyde（假设文档承载语义）
    """

    SINGLE = "single"
    MULTI = "multi"
    VAGUE = "vague"


# 强多意图标记 — 命中 1 个即判定复合查询
_MULTI_STRONG_MARKERS: tuple[str, ...] = (
    "以及", "同时", "还要", "另外", "然后", "并且", "；", ";",
)
# 弱多意图标记 — 需配合长度阈值（短查询中"和/与/并"多为词组内连接）
_MULTI_WEAK_MARKERS: tuple[str, ...] = ("和", "与", "并", "再")
# 模糊查询标记 — 指示代词/泛化动词，语义需假设文档承载
_VAGUE_MARKERS: tuple[str, ...] = (
    "这个", "那个", "怎么办", "怎么处理", "怎么弄", "如何办", "咋办", "啥",
)
# 弱标记触发多意图所需的最小查询长度（字符数）
_MULTI_WEAK_MIN_LEN: int = 12

# 策略路由表 — QueryType → 策略名列表（与配置开关取交集后执行）
_STRATEGY_ROUTE_TABLE: dict[QueryType, list[str]] = {
    QueryType.SINGLE: ["rewrite", "expansion"],
    QueryType.MULTI: ["rewrite", "decomposition"],
    QueryType.VAGUE: ["rewrite", "hyde"],
}


def classify_query_type(query: str) -> QueryType:
    """规则式查询分类 — 零 LLM 调用，毫秒级。

    判定顺序：vague → multi → single（模糊优先，语义承载比拆分更关键）。

    Args:
        query: 用户原始查询

    Returns:
        QueryType 分类结果
    """
    # 1. 模糊查询 — 含指示代词/泛化动词
    if any(marker in query for marker in _VAGUE_MARKERS):
        return QueryType.VAGUE

    # 2. 多意图 — 强标记命中 / 多个问号 / 弱标记 + 长度阈值
    if any(marker in query for marker in _MULTI_STRONG_MARKERS):
        return QueryType.MULTI
    if query.count("?") + query.count("？") >= 2:
        return QueryType.MULTI
    weak_hits = sum(1 for marker in _MULTI_WEAK_MARKERS if marker in query)
    if weak_hits >= 2:
        return QueryType.MULTI
    if weak_hits >= 1 and len(query) >= _MULTI_WEAK_MIN_LEN:
        return QueryType.MULTI

    # 3. 兜底 — 单意图
    return QueryType.SINGLE


# 召回质量评估器类型 — 接收查询文本，返回召回质量分（越高越好）。
# 由 engine 侧注入（封装 retriever.search 小 top_k 双跑）。
RecallEvaluator = Callable[[str], Awaitable[float]]


# ======================================================================
# 数据结构
# ======================================================================


@dataclass
class QueryRewriteResult:
    """查询重写结果 — 统一封装所有策略的输出。

    Attributes:
        original: 原始用户查询
        rewritten: 重写后的查询（用于向量检索）
        expanded_terms: 扩展的同义词/相关词列表（用于全文检索补充）
        sub_queries: 分解的子查询列表（用于多路检索）
        hyde_document: HyDE 假设文档（用于向量检索替代原始查询嵌入）
        strategy: 实际使用的策略名称列表
        latency_ms: 总耗时（毫秒）
        cache_hit: 是否命中缓存
        query_type: P1-10 规则分类结果（single/multi/vague），未启用自动路由时为 None
        recall_original: P1-10 在线回退 — 原查询召回质量分（未评估为 None）
        recall_rewritten: P1-10 在线回退 — 改写后召回质量分（未评估为 None）
        fallback_to_original: P1-10 在线回退 — 改写后召回更差时为 True，
            此时 get_search_query() 返回原始查询
    """

    original: str
    rewritten: str = ""
    expanded_terms: list[str] = field(default_factory=list)
    sub_queries: list[str] = field(default_factory=list)
    hyde_document: str | None = None
    strategy: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    cache_hit: bool = False
    query_type: str | None = None
    recall_original: float | None = None
    recall_rewritten: float | None = None
    fallback_to_original: bool = False

    def get_search_query(self) -> str:
        """获取用于向量检索的查询文本。

        P1-10 在线回退优先：双跑对比判定改写后召回更差时，直接返回原始查询。
        否则优先使用 HyDE 文档（语义更丰富），其次使用重写后的查询，
        最后回退到原始查询。

        Returns:
            检索用查询文本
        """
        if self.fallback_to_original:
            return self.original
        if self.hyde_document:
            return self.hyde_document
        if self.rewritten:
            return self.rewritten
        return self.original

    def get_all_queries(self) -> list[str]:
        """获取所有用于检索的查询列表（含子查询）。

        用于多路检索：主查询 + 子查询，每条分别检索后合并结果。

        Returns:
            查询列表（至少包含一个元素）
        """
        queries: list[str] = []
        main_query = self.get_search_query()
        if main_query:
            queries.append(main_query)
        queries.extend(self.sub_queries)
        if not queries:
            queries.append(self.original)
        return queries

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供 SSE 事件 / 日志使用）。"""
        return {
            "original": self.original,
            "rewritten": self.rewritten,
            "expanded_terms": self.expanded_terms,
            "sub_queries": self.sub_queries,
            "hyde_document": self.hyde_document,
            "strategy": self.strategy,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
            "query_type": self.query_type,
            "recall_original": self.recall_original,
            "recall_rewritten": self.recall_rewritten,
            "fallback_to_original": self.fallback_to_original,
            "search_query": self.get_search_query(),
        }


# ======================================================================
# 查询重写器
# ======================================================================


class QueryRewriter:
    """查询重写器 — 在检索前优化用户查询。

    支持四种策略，可独立启用/禁用：
    - rewrite: 修正拼写、消歧、补充上下文
    - expansion: 添加同义词/相关词
    - decomposition: 分解复杂查询为子查询
    - hyde: 生成假设文档用于检索

    用法::

        rewriter = QueryRewriter(llm_provider)
        result = await rewriter.rewrite("公司的休假政策是什么？")
        # result.get_search_query() → 用于向量检索
        # result.get_all_queries() → 用于多路检索
    """

    # LLM 提示词 — 查询重写
    _REWRITE_PROMPT = (
        "你是查询重写专家。将用户的查询重写为更适合知识库检索的形式。\n"
        "要求：\n"
        "1. 修正拼写错误和语法问题\n"
        "2. 消除歧义，补充必要的上下文\n"
        "3. 保留原意，不要改变查询的意图\n"
        "4. 输出简洁的重写查询，不要包含解释\n\n"
        "原始查询: {query}\n"
        "上下文: {context}\n\n"
        "重写查询:"
    )

    # LLM 提示词 — 查询扩展
    _EXPANSION_PROMPT = (
        "你是搜索关键词扩展专家。为以下查询生成 3-5 个同义词或相关词，"
        "用于扩大全文检索的召回范围。\n"
        "要求：\n"
        "1. 每个词/短语独占一行\n"
        "2. 只输出扩展词，不要包含解释或编号\n"
        "3. 词/短语应与原始查询语义相关但表达不同\n\n"
        "查询: {query}\n\n"
        "扩展词:"
    )

    # LLM 提示词 — 查询分解
    _DECOMPOSITION_PROMPT = (
        "你是查询分解专家。如果用户的查询是复杂问题（包含多个子问题），"
        "将其分解为 2-4 个独立的子查询。\n"
        "要求：\n"
        "1. 每个子查询独占一行\n"
        "2. 只输出子查询，不要包含解释或编号\n"
        "3. 如果查询已经很简单，输出空行\n"
        "4. 子查询应覆盖原始查询的所有方面\n\n"
        "原始查询: {query}\n\n"
        "子查询:"
    )

    # LLM 提示词 — HyDE 假设文档
    _HYDE_PROMPT = (
        "你是知识库文档生成专家。根据用户的查询，生成一段假设性的文档内容，"
        "这段内容如果存在于知识库中，将能完美回答该查询。\n"
        "要求：\n"
        "1. 内容长度 100-200 字\n"
        "2. 使用正式的文档语体\n"
        "3. 包含查询相关的关键信息和术语\n"
        "4. 不要包含「假设文档」等元说明\n\n"
        "查询: {query}\n"
        "上下文: {context}\n\n"
        "假设文档:"
    )

    def __init__(
        self,
        llm: LLMProvider,
        enable_rewrite: bool = True,
        enable_expansion: bool = True,
        enable_decomposition: bool = False,
        enable_hyde: bool = False,
        cache_size: int = 128,
        auto_route: bool = False,
        enable_online_fallback: bool = False,
        fallback_margin: float = 0.0,
    ) -> None:
        """初始化查询重写器。

        Args:
            llm: LLM Provider 实例
            enable_rewrite: 启用查询重写
            enable_expansion: 启用查询扩展
            enable_decomposition: 启用查询分解
            enable_hyde: 启用 HyDE 假设文档
            cache_size: LRU 缓存大小
            auto_route: P1-10 按 query 类型自动路由策略（规则分类，零 LLM）。
                False 时保持原行为（执行全部配置启用的策略）。
            enable_online_fallback: P1-10 在线回退开关 — 仅作为 engine 侧
                是否注入召回评估器的标记，rewrite() 本身以 recall_evaluator
                是否传入为准。
            fallback_margin: P1-10 回退判定余量 — 改写后召回分低于
                原查询召回分减去该余量时才回退（避免噪声抖动）。
        """
        self._llm = llm
        self._enable_rewrite = enable_rewrite
        self._enable_expansion = enable_expansion
        self._enable_decomposition = enable_decomposition
        self._enable_hyde = enable_hyde
        self.auto_route = auto_route
        self.enable_online_fallback = enable_online_fallback
        self._fallback_margin = fallback_margin

        # 内存缓存 — key: hash(query+context), value: QueryRewriteResult
        self._cache: dict[str, QueryRewriteResult] = {}
        self._cache_size = cache_size

    def _cache_key(self, query: str, context: str) -> str:
        """生成缓存 key — hash(query + context)。"""
        raw = f"{query}||{context}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _get_cached(self, key: str) -> QueryRewriteResult | None:
        """从缓存获取结果。"""
        result = self._cache.get(key)
        if result is not None:
            result.cache_hit = True
        return result

    def _set_cached(self, key: str, result: QueryRewriteResult) -> None:
        """存入缓存（LRU 淘汰）。"""
        if len(self._cache) >= self._cache_size:
            # 淘汰最早的 key
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[key] = result

    def clear_cache(self) -> None:
        """清除缓存 — 供测试使用。"""
        self._cache.clear()

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 并返回文本结果。

        收集所有 yield 的 str chunk 拼接为完整文本。
        dict chunk（tool_use）被忽略。

        Args:
            prompt: 完整提示词

        Returns:
            LLM 生成的文本
        """
        messages: list[Message] = [
            {"role": "user", "content": prompt},
        ]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks).strip()

    def _resolve_strategies(self, query: str) -> tuple[set[str], QueryType | None]:
        """解析本次查询实际执行的策略集合。

        auto_route 开启时按规则分类路由（与配置开关取交集，配置仍是总闸）；
        关闭时返回全部配置启用的策略（保持原行为）。
        P2-12 降级模式：DEGRADE_MODE_ENABLED 开启时移除高成本策略
        （hyde / decomposition），保留 rewrite / expansion 保核心链路。

        Args:
            query: 用户原始查询

        Returns:
            (策略名集合, 分类结果或 None)
        """
        enabled: dict[str, bool] = {
            "rewrite": self._enable_rewrite,
            "expansion": self._enable_expansion,
            "decomposition": self._enable_decomposition,
            "hyde": self._enable_hyde,
        }

        # P2-12: 降级模式 — 动态读取配置（支持运行时切换），
        # 高负载时关闭 HyDE / QueryDecomposition 两个高成本 LLM 策略
        try:
            from app.config import get_settings

            if get_settings().DEGRADE_MODE_ENABLED:
                enabled["hyde"] = False
                enabled["decomposition"] = False
        except Exception:
            pass  # 配置不可用时保持原样，不阻断主链路

        if not self.auto_route:
            return {name for name, on in enabled.items() if on}, None

        query_type = classify_query_type(query)
        routed = set(_STRATEGY_ROUTE_TABLE[query_type])
        strategies = {name for name in routed if enabled.get(name, False)}
        # 路由结果为空（目标策略全被配置禁用）— 回退到配置启用集合，保证有策略执行
        if not strategies:
            strategies = {name for name, on in enabled.items() if on}
        return strategies, query_type

    async def rewrite(
        self,
        query: str,
        context: str = "",
        recall_evaluator: RecallEvaluator | None = None,
    ) -> QueryRewriteResult:
        """重写查询 — 主入口，编排所有启用的策略。

        幂等性：同一 (query, context) 输入返回缓存结果，不重复调用 LLM。
        P1-10：auto_route 开启时按 query 类型路由策略；recall_evaluator
        传入时对改写结果做双跑对比，召回更差则回退原查询（判定结果随
        缓存复用，同一查询只双跑一次，控成本）。

        Args:
            query: 用户原始查询
            context: 可选上下文（如对话历史摘要）
            recall_evaluator: 可选召回质量评估器（engine 注入，封装小
                top_k 检索双跑）；None 时跳过在线回退评估

        Returns:
            QueryRewriteResult 重写结果
        """
        # 1. 缓存检查
        cache_key = self._cache_key(query, context)
        cached = self._get_cached(cache_key)
        if cached is not None:
            log.info(
                "query_rewriter.cache_hit",
                query=query[:100],
                cache_key=cache_key,
            )
            return cached

        # 2. 执行重写
        t0 = time.monotonic()
        result = QueryRewriteResult(original=query)
        strategy: list[str] = []

        # P1-10: 解析本次实际执行的策略（自动路由或全量配置）
        active, query_type = self._resolve_strategies(query)
        if query_type is not None:
            result.query_type = query_type.value

        # 2a. 查询重写
        if "rewrite" in active:
            try:
                rewritten = await self._do_rewrite(query, context)
                if rewritten and rewritten != query:
                    result.rewritten = rewritten
                    strategy.append("rewrite")
                    log.info(
                        "query_rewriter.rewrite_done",
                        original=query[:100],
                        rewritten=rewritten[:100],
                    )
            except Exception as exc:
                log.warning(
                    "query_rewriter.rewrite_failed",
                    error=str(exc),
                    query=query[:100],
                )

        # 2b. 查询扩展
        if "expansion" in active:
            try:
                terms = await self._do_expansion(query)
                if terms:
                    result.expanded_terms = terms
                    strategy.append("expansion")
                    log.info(
                        "query_rewriter.expansion_done",
                        terms_count=len(terms),
                        terms=terms,
                    )
            except Exception as exc:
                log.warning(
                    "query_rewriter.expansion_failed",
                    error=str(exc),
                    query=query[:100],
                )

        # 2c. 查询分解
        if "decomposition" in active:
            try:
                sub_queries = await self._do_decomposition(query)
                if sub_queries:
                    result.sub_queries = sub_queries
                    strategy.append("decomposition")
                    log.info(
                        "query_rewriter.decomposition_done",
                        sub_queries_count=len(sub_queries),
                    )
            except Exception as exc:
                log.warning(
                    "query_rewriter.decomposition_failed",
                    error=str(exc),
                    query=query[:100],
                )

        # 2d. HyDE 假设文档
        if "hyde" in active:
            try:
                hyde_doc = await self._do_hyde(query, context)
                if hyde_doc:
                    result.hyde_document = hyde_doc
                    strategy.append("hyde")
                    log.info(
                        "query_rewriter.hyde_done",
                        doc_length=len(hyde_doc),
                    )
            except Exception as exc:
                log.warning(
                    "query_rewriter.hyde_failed",
                    error=str(exc),
                    query=query[:100],
                )

        # 2e. P1-10: 在线回退 — 双跑对比改写前后召回质量
        if recall_evaluator is not None and result.get_search_query() != query:
            try:
                original_score = await recall_evaluator(query)
                rewritten_score = await recall_evaluator(result.get_search_query())
                result.recall_original = original_score
                result.recall_rewritten = rewritten_score
                if rewritten_score < original_score - self._fallback_margin:
                    result.fallback_to_original = True
                    log.info(
                        "query_rewriter.online_fallback",
                        query=query[:100],
                        recall_original=original_score,
                        recall_rewritten=rewritten_score,
                    )
            except Exception as exc:
                # 评估失败不影响主链路 — 保持改写结果
                log.warning(
                    "query_rewriter.fallback_eval_failed",
                    error=str(exc),
                    query=query[:100],
                )

        result.strategy = strategy
        result.latency_ms = round((time.monotonic() - t0) * 1000, 2)

        # 3. 缓存结果（含在线回退判定 — 同一查询只双跑一次）
        result.cache_hit = False
        self._set_cached(cache_key, result)

        log.info(
            "query_rewriter.complete",
            query=query[:100],
            strategy=strategy,
            latency_ms=result.latency_ms,
            query_type=result.query_type,
            has_rewritten=bool(result.rewritten),
            has_expanded=bool(result.expanded_terms),
            has_sub_queries=bool(result.sub_queries),
            has_hyde=bool(result.hyde_document),
            fallback_to_original=result.fallback_to_original,
        )

        return result

    async def _do_rewrite(self, query: str, context: str) -> str:
        """执行查询重写 — 修正拼写、消歧、补充上下文。"""
        prompt = self._REWRITE_PROMPT.format(
            query=query,
            context=context or "(无)",
        )
        result = await self._call_llm(prompt)
        # 清理：去掉可能的引号和前缀
        result = result.strip().strip('"').strip("'").strip("「」")
        return result

    async def _do_expansion(self, query: str) -> list[str]:
        """执行查询扩展 — 生成同义词/相关词。"""
        prompt = self._EXPANSION_PROMPT.format(query=query)
        result = await self._call_llm(prompt)
        terms = [
            line.strip()
            for line in result.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        # 去重，最多保留 5 个
        seen: set[str] = set()
        unique: list[str] = []
        for term in terms[:5]:
            lower = term.lower()
            if lower not in seen:
                seen.add(lower)
                unique.append(term)
        return unique

    async def _do_decomposition(self, query: str) -> list[str]:
        """执行查询分解 — 将复杂查询拆分为子查询。"""
        prompt = self._DECOMPOSITION_PROMPT.format(query=query)
        result = await self._call_llm(prompt)
        sub_queries = [
            line.strip()
            for line in result.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        # 过滤掉与原查询完全相同的
        sub_queries = [sq for sq in sub_queries if sq.lower() != query.lower()]
        return sub_queries[:4]  # 最多 4 个子查询

    async def _do_hyde(self, query: str, context: str) -> str:
        """执行 HyDE — 生成假设性文档。"""
        prompt = self._HYDE_PROMPT.format(
            query=query,
            context=context or "(无)",
        )
        result = await self._call_llm(prompt)
        return result.strip()


# ======================================================================
# 工厂函数
# ======================================================================


_query_rewriter: QueryRewriter | None = None


def get_query_rewriter() -> QueryRewriter | None:
    """获取全局 QueryRewriter 实例（单例）。

    根据 settings 配置决定是否启用各策略。
    LLM 不可用时返回 None（engine 侧需判空）。

    Returns:
        QueryRewriter 实例，或 None（未配置/不可用）
    """
    global _query_rewriter
    if _query_rewriter is not None:
        return _query_rewriter

    from app.config import get_settings

    settings = get_settings()

    # 未启用任何策略时不创建
    if not any([
        settings.QUERY_REWRITE_ENABLED,
        settings.QUERY_EXPANSION_ENABLED,
        settings.QUERY_DECOMPOSITION_ENABLED,
        settings.HYDE_ENABLED,
    ]):
        return None

    try:
        from app.llm.factory import get_llm_provider

        llm = get_llm_provider()
        _query_rewriter = QueryRewriter(
            llm=llm,
            enable_rewrite=settings.QUERY_REWRITE_ENABLED,
            enable_expansion=settings.QUERY_EXPANSION_ENABLED,
            enable_decomposition=settings.QUERY_DECOMPOSITION_ENABLED,
            enable_hyde=settings.HYDE_ENABLED,
            auto_route=getattr(settings, "QUERY_REWRITE_AUTO_ROUTE", False),
            enable_online_fallback=getattr(
                settings, "QUERY_REWRITE_ONLINE_FALLBACK", False
            ),
            fallback_margin=getattr(settings, "QUERY_REWRITE_FALLBACK_MARGIN", 0.0),
        )
        log.info(
            "query_rewriter.initialized",
            rewrite=settings.QUERY_REWRITE_ENABLED,
            expansion=settings.QUERY_EXPANSION_ENABLED,
            decomposition=settings.QUERY_DECOMPOSITION_ENABLED,
            hyde=settings.HYDE_ENABLED,
            auto_route=_query_rewriter.auto_route,
            online_fallback=_query_rewriter.enable_online_fallback,
        )
        return _query_rewriter
    except Exception as exc:
        log.warning("query_rewriter.init_failed", error=str(exc))
        return None


def reset_query_rewriter() -> None:
    """重置全局 QueryRewriter 实例 — 供测试使用。"""
    global _query_rewriter
    _query_rewriter = None
