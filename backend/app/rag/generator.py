"""
生成器 — 单一职责：调用 LLM Provider 流式生成答案并注入检索上下文。

职责：
    - 将检索到的文档与工具结果组装为上下文 prompt；
    - Context Cliff 监控（P2）：当注入上下文总 token 超过阈值时自动降级，
      避免长上下文导致 LLM 对中间位置信息提取能力下降；
    - 调用 LLM Provider 流式生成答案（async generator）；
    - 引导 LLM 在答案中使用 [n] 引用标注（供 CitationExtractor 解析）。

遵循单一职责：本模块只负责 prompt 组装与生成流转，不涉及检索与重排。
遵循依赖倒置：通过 LLMProvider 抽象调用，不感知底层是 Anthropic 还是 vLLM。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

from app.config import get_settings
from app.llm.base import LLMProvider, Message
from app.rag.citation import CitationExtractor
from app.rag.chunker import estimate_tokens
from app.rag.context_item import BudgetAllocator, ContextItemBuilder
from app.utils.logger import get_logger

log = get_logger(__name__)

# 答案最大 token 数
_MAX_TOKENS: int = 4096
# P2: Context Cliff 阈值 — 超过此值后 LLM 对中间位置信息提取能力显著下降
_CONTEXT_CLIFF_THRESHOLD: int = 2500
# P2: Context Cliff 降级后保留的 chunk 数
_CONTEXT_CLIFF_FALLBACK_TOP_K: int = 3
# P2: 单个文档内容的最大截断字符数（防止单个 chunk 过长）
_DOC_MAX_CHARS: int = 1500
# P1: 约束 severity → prompt 标签（红线段渲染）
_CONSTRAINT_LABELS: dict[str, str] = {
    "block": "【红线·必须遵守】",
    "confirm": "【需人工确认】",
    "warn": "【提醒】",
}


class Generator:
    """答案生成器 — 组装上下文并流式生成。

    使用方式::

        generator = Generator(llm_provider)
        async for token in generator.generate(query, retrieved_docs, tool_results):
            yield token  # SSE 流式输出
    """

    def __init__(
        self,
        llm: LLMProvider,
        citation_extractor: CitationExtractor | None = None,
        context_budget: int | None = None,
    ) -> None:
        self.llm = llm
        self.citation_extractor = citation_extractor or CitationExtractor()
        # P0-1: 预算分配式注入 — 窗口组装时按 token_cost 择优注入 ContextItem
        self._allocator = BudgetAllocator(
            budget=context_budget or _CONTEXT_CLIFF_THRESHOLD
        )
        # P0-Stage2: 最近一次 generate 的真实 token 用量（由 LLM Provider yield）
        # 并发隔离修复：Generator 为引擎级共享实例，若用普通实例属性，
        # 并发请求会互相覆写/读取对方的 usage（A 请求重置 None 时 B 正在累加，
        # 导致用量错记到别的请求上）。改用 ContextVar 按 asyncio 任务隔离，
        # 每个请求任务读写自己的副本；异步生成器在消费方任务上下文中执行，
        # 故 generate() 内的 set 对同一任务内后续的 get 可见。
        self._usage_var: ContextVar[dict[str, Any] | None] = ContextVar(
            f"generator_last_usage_{id(self)}", default=None
        )

    @property
    def last_usage(self) -> dict[str, Any] | None:
        """当前请求任务的最近一次 token 用量（按 asyncio 任务隔离）。"""
        return self._usage_var.get()

    @last_usage.setter
    def last_usage(self, value: dict[str, Any] | None) -> None:
        self._usage_var.set(value)

    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
        constraint_context: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """流式生成答案，逐 token yield 供 SSE 消费。

        Args:
            query: 用户问题。
            retrieved_docs: 检索并重排后的文档列表。
            tool_results: MCP 工具调用结果列表。
            memory_context: 记忆引擎提供的上下文（用户偏好、历史事实等）。
            constraint_context: 约束注入通道输出（ConstraintChannel.fetch，
                source=constraint）— 确定性红线条款，block 级全量注入。

        Yields:
            str: 答案文本片段。

        Raises:
            Exception: LLM 调用失败时原样抛出（错误不作为答案产出，
                上层因此不会将错误文本写入缓存或持久化）。
        """
        system_prompt = self._build_system_prompt(
            retrieved_docs,
            tool_results,
            memory_context,
            constraint_context,
        )
        messages: list[Message] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        log.info(
            "generator.start",
            query_len=len(query),
            doc_count=len(retrieved_docs),
            tool_count=len(tool_results),
            constraint_count=len(constraint_context or []),
        )

        # P0-Stage2: 重置用量记录
        self.last_usage = None

        try:
            async for chunk in self.llm.chat(messages, stream=True, max_tokens=_MAX_TOKENS):
                # P0-Stage2: 捕获 usage dict（由 Provider 在流末尾 yield）
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    self.last_usage = chunk
                    continue
                if isinstance(chunk, str) and chunk:
                    yield chunk
        except Exception as exc:
            # LLM 错误不得作为答案 yield — 错误文本会被 engine 拼进 answer
            # 并回写缓存 / 持久化为 assistant 消息，造成错误答案被缓存复用。
            # 记录日志后原样抛出，由上层决定降级策略（不产出即不写缓存）。
            log.error("generator.error", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Prompt 组装
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str,
        constraint_context: list[dict[str, Any]] | None = None,
    ) -> str:
        """组装系统 prompt — 注入检索上下文、工具结果与引用指引。

        P0-1 预算分配式注入：将文档 / 工具 / 记忆统一为带 token_cost 的
        ContextItem，交给 BudgetAllocator 在窗口预算内"择优注入"。相比旧的
        Context Cliff 一刀切（超阈值砍到 Top-3），这里让每个片段按
        {优先级, 相关性, token_cost} 公平竞争预算：高相关片段公平入选，
        低价值片段被预算淘汰，而非简单地按位置裁剪。

        P1 约束先行配额（设计 §7）：block 级约束全量注入、不受预算约束，
        先占预算后其余来源竞争剩余额度 — 安全优先于效果，约束超量时
        宁可挤占语义预算也不丢红线。
        """
        # P1: 约束专段先行 — block 全量保留（mandatory），confirm/warn
        # 在 CONSTRAINT_BUDGET_MAX_TOKENS 内排序截断（build_constraint_items）
        constraint_items = ContextItemBuilder.build_constraint_items(
            constraint_context,
            budget_max_tokens=get_settings().CONSTRAINT_BUDGET_MAX_TOKENS,
        )
        constraint_tokens = sum(it.token_cost for it in constraint_items)

        # P0-1: 构建统一 ContextItem 并按剩余预算择优注入
        items = ContextItemBuilder.build(
            retrieved_docs=retrieved_docs,
            tool_results=tool_results,
            memory_context=memory_context,
        )
        # 约束已占额度从预算中扣除（约束不足时挤掉最低优先级 document 项）
        remaining_budget = (
            None
            if constraint_tokens == 0
            else max(1, self._allocator._budget - constraint_tokens)
        )
        selected = self._allocator.select(items, budget=remaining_budget)

        # 从选中项中还原三类来源（供 prompt 分段组装）
        memory_parts = [it for it in selected if it.kind == "memory"]
        doc_items = [it for it in selected if it.kind == "document"]
        tool_items = [it for it in selected if it.kind == "tool"]

        parts: list[str] = [
            "你是企业知识库助手。请基于以下检索到的上下文和企业工具结果回答用户问题。",
            "如果上下文不足以回答，请明确说明并建议补充信息。",
            "禁止编造未在上下文中出现的事实。",
        ]

        # 引用指引
        if doc_items:
            parts.append(
                "在引用知识库内容时，请使用 [n] 标注引用来源（n 从 1 开始，"
                "对应下方「知识库来源」的编号）。"
            )

        # 记忆上下文
        if memory_parts:
            parts.append(f"\n=== 用户偏好 / 历史上下文 ===\n{memory_parts[0].content}")

        # P1: 强制约束红线段 — 位于知识库来源之前（注意力前部高地），
        # 与 think 末尾的宪法提醒（engine._CONSTRAINT_REMINDER）构成
        # "首尾三明治"双高地；确定性注入，不依赖相似度召回。
        if constraint_items:
            parts.append("\n=== 强制约束（红线，必须遵守）===")
            for item in constraint_items:
                severity = item.meta.get("severity", "warn")
                label = _CONSTRAINT_LABELS.get(severity, "提醒")
                parts.append(f"{label} {item.content}")

        # 知识库来源（带编号）— P3: 包含 title_path 上下文锚点 + 时效元数据
        if doc_items:
            parts.append("\n=== 知识库来源 ===")
            has_stale_doc = False  # 是否存在可能过时的文档（用于追加时效规则）
            for idx, item in enumerate(doc_items, start=1):
                title_path = item.meta.get("title_path", "")
                title = item.meta.get("title", "")
                sync_status = item.meta.get("sync_status", "")
                source_url = item.meta.get("source_url", "")

                # P3: 时效标注 — 根据 sync_status 生成
                freshness_note = ""
                if sync_status == "updated_live":
                    freshness_note = " （实时回源：已是最新版本）"
                elif sync_status == "verified_fresh":
                    freshness_note = " （实时回源：内容一致）"
                elif sync_status == "verify_failed":
                    freshness_note = " （校验失败，内容可能已过时）"
                    has_stale_doc = True
                elif sync_status == "trusted_local":
                    # 信任本地缓存 — 未做实时校验，可能已过时
                    has_stale_doc = True

                # P3: 原始链接（供用户核对）
                source_line = f"\n原始链接：{source_url}" if source_url else ""

                header = title_path or title
                parts.append(
                    f"[{idx}] {header}{freshness_note}{source_line}\n{item.content}"
                )

            # P3: 时效规则 — 当存在可能过时的文档时，引导 LLM 主动声明
            if has_stale_doc:
                parts.append(
                    "\n【时效性规则】\n"
                    "当引用文档涉及政策/流程/费率/数字等时效性内容时，"
                    "若文档标注为「信任本地缓存」或「校验失败」，"
                    "请在回答末尾追加提示：\n"
                    "「⚠️ 以上信息可能已更新，建议访问原始链接确认最新版本。」"
                )

        # 工具结果
        if tool_items:
            parts.append("\n=== 企业工具结果 ===")
            for idx, item in enumerate(tool_items, start=1):
                parts.append(f"工具 {idx}：{item.content}")

        return "\n".join(parts)

    def _check_context_cliff(
        self,
        retrieved_docs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """P2: Context Cliff 监控 — 检测并降级过长的注入上下文。

        当检索文档总 token 超过 _CONTEXT_CLIFF_THRESHOLD 时，自动截断为
        Top-_CONTEXT_CLIFF_FALLBACK_TOP_K 个文档，并记录告警日志。

        Args:
            retrieved_docs: 重排后的文档列表。

        Returns:
            可能截断后的文档列表。
        """
        if not retrieved_docs:
            return retrieved_docs

        # 计算总 token 数 — 口径与实际注入一致：基于 _truncate 截断后的
        # 内容估算。注入 prompt 的是截断后内容（见 _build_system_prompt），
        # 若按原始全文估算会系统性高估 token 数，导致未超阈值也误触发降级。
        total_tokens = 0
        for doc in retrieved_docs:
            content = self._truncate(str(doc.get("content") or ""))
            total_tokens += estimate_tokens(content)

        if total_tokens <= _CONTEXT_CLIFF_THRESHOLD:
            return retrieved_docs

        # 触发 Context Cliff 降级
        original_count = len(retrieved_docs)
        truncated = retrieved_docs[:_CONTEXT_CLIFF_FALLBACK_TOP_K]

        # 计算降级后的 token 数（同样基于截断后内容，与注入口径一致）
        truncated_tokens = sum(
            estimate_tokens(self._truncate(str(doc.get("content") or "")))
            for doc in truncated
        )

        log.warning(
            "generator.context_cliff_degraded",
            original_tokens=total_tokens,
            original_count=original_count,
            truncated_tokens=truncated_tokens,
            truncated_count=len(truncated),
            threshold=_CONTEXT_CLIFF_THRESHOLD,
        )

        return truncated

    @staticmethod
    def _truncate(text: str, max_chars: int = 1500) -> str:
        """截断文档内容，避免 prompt 过长。"""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    @staticmethod
    def _stringify(result: dict[str, Any] | str) -> str:
        """将工具结果序列化为可读字符串。"""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            content = result.get("content") or result.get("result") or result
            return str(content)
        return str(result)

    def extract_citations(
        self,
        text: str,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """从生成文本中提取引用卡片（供生成后调用）。"""
        return self.citation_extractor.extract(text, sources)
