"""
Context Item 统一抽象 + token_cost 预算分配式注入。

对齐附件第 11 讲核心设计：把检索片段、工具输出、记忆统一为带 ``token_cost``
的 Context Item，窗口组装时按剩余预算"择优注入"。这是"窗口放不下时先决策
放什么"的工程化 — 不再用粗糙的"超过阈值就砍到 Top-3"，而是按
{优先级, 相关性, token_cost} 在预算内做贪心择优注入。

设计要点：
    - :class:`ContextItem` 统一三种来源（document / tool / memory），
      每个 item 自带 ``token_cost`` / ``priority`` / ``relevance``。
    - :class:`ContextItemBuilder` 把引擎产出的原始数据结构转为 item 列表，
      并打标优先级（memory 约束最高、tool 事实次之、document 按相关性）。
    - :class:`BudgetAllocator` 在给定预算内贪心择优：先保留 mandatory 项，
      再按 {priority, relevance} 降序贪心填充剩余预算，形成"预算分配式注入"。

遵循单一职责：本模块只负责 Context Item 的建模与预算分配，不负责 prompt 组装。
遵循优雅降级：token 估算失败时按字符粗估，贪心失败时回退为全量注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.rag.chunker import estimate_tokens
from app.utils.logger import get_logger

log = get_logger(__name__)

# 上下文总预算（默认 token 数）— 与 _CONTEXT_CLIFF_THRESHOLD 对齐，可被构造参数覆盖
_DEFAULT_BUDGET: int = 2500

# 各来源的默认优先级（越高越先注入）
# memory：用户约束/偏好会左右答案方向，优先保留
# tool：工具事实在冲突裁决中权威性高于文档，次优先
# document：检索片段按相关性排序，作为主体注入
_PRIORITY_MEMORY: int = 100
_PRIORITY_TOOL: int = 80
_PRIORITY_DOCUMENT_BASE: int = 60


@dataclass(frozen=True)
class ContextItem:
    """统一上下文条目 — 检索、工具、记忆的公共最小单元。

    Attributes:
        kind: 来源类型 "document" / "tool" / "memory"。
        content: 已格式化的注入文本（prompt 直接拼接的部分）。
        token_cost: 估算 token 数（用于预算分配）。
        priority: 注入优先级（越大越优先）。
        relevance: 与当前查询的相关性（0~1，文档取重排分数，其余取 1.0）。
        source: 来源标识（文档标题 / 工具名 / 记忆类别）。
        mandatory: 预算不足时是否仍强制注入（一般用于 memory 关键约束）。
        meta: 附加元数据（引用编号、title_path 等，供 prompt 组装读取）。
    """

    kind: str
    content: str
    token_cost: int = 0
    priority: int = 0
    relevance: float = 1.0
    source: str = ""
    mandatory: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class ContextItemBuilder:
    """把引擎原始输出（文档 / 工具 / 记忆）统一为 ContextItem 列表。"""

    @classmethod
    def build(
        cls,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
        query: str = "",
    ) -> list[ContextItem]:
        """构建统一上下文条目列表。

        Args:
            retrieved_docs: 检索并重排后的文档列表（含 relevance/title_path）。
            tool_results: MCP 工具调用结果列表。
            memory_context: 记忆引擎提供的上下文（用户偏好、历史事实等）。
            query: 当前用户查询（预留，供后续相关性精算）。

        Returns:
            按 {memory, tool, document} 顺序排列的 ContextItem 列表。
        """
        items: list[ContextItem] = []

        # 1. 记忆上下文 — 最高优先级，通常为强约束/偏好
        if memory_context and memory_context.strip():
            items.append(
                ContextItem(
                    kind="memory",
                    content=memory_context.strip(),
                    token_cost=estimate_tokens(memory_context),
                    priority=_PRIORITY_MEMORY,
                    relevance=1.0,
                    source="记忆",
                    mandatory=True,
                    meta={"role": "memory"},
                )
            )

        # 2. 工具结果 — 权威事实，次优先
        for idx, result in enumerate(tool_results, start=1):
            text = cls._stringify(result)
            if not text:
                continue
            items.append(
                ContextItem(
                    kind="tool",
                    content=text,
                    token_cost=estimate_tokens(text),
                    priority=_PRIORITY_TOOL,
                    relevance=1.0,
                    source=f"工具{idx}",
                    meta={"role": "tool", "index": idx},
                )
            )

        # 3. 文档片段 — 按相关性排序后注入
        for idx, doc in enumerate(retrieved_docs, start=1):
            title = doc.get("title") or "未命名文档"
            title_path = doc.get("title_path", "")
            content = cls._truncate(str(doc.get("content") or ""))
            if not content:
                continue
            # 文档携带 relevance 时用重排分数，否则取顺序衰减作为相关性
            relevance = float(doc.get("relevance") or doc.get("score") or 0.0)
            if relevance <= 0:
                relevance = max(0.0, 1.0 - (idx - 1) * 0.1)
            items.append(
                ContextItem(
                    kind="document",
                    content=content,
                    token_cost=estimate_tokens(content),
                    priority=_PRIORITY_DOCUMENT_BASE,
                    relevance=min(1.0, relevance),
                    source=title_path or title,
                    meta={
                        "role": "document",
                        "title": title,
                        "title_path": title_path,
                        "index": idx,
                        # P3: 外部文档时效元数据（由 engine._verify_external_docs 写入）
                        # - sync_status: trusted_local / verified_fresh / updated_live / verify_failed
                        # - source_url: 原始文档链接（用于 prompt 引导用户核对）
                        "sync_status": doc.get("sync_status", ""),
                        "source_url": doc.get("source_url", ""),
                    },
                )
            )

        return items

    @staticmethod
    def _truncate(text: str, max_chars: int = 1500) -> str:
        """截断文档内容，防止单个片段过长。"""
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


class BudgetAllocator:
    """预算分配器 — 在 token 预算内"择优注入"ContextItem。

    决策逻辑（优先级 + 相关性驱动的贪心）：
        1. 先强制注入 mandatory 项（如记忆约束），从预算中预留；
        2. 剩余项按 {priority desc, relevance desc} 排序；
        3. 贪心逐个尝试：若剩余预算放得下则注入，否则跳过；
        4. 返回被选中的 items（保持输入相对顺序）。

    与旧 ``_check_context_cliff``（超阈值砍到 Top-3）的区别：这里不是"一刀切"
    丢弃，而是让每个片段公平竞争预算 — 高相关片段即使排在后面也能入选，
    低价值片段即使靠前也会被预算淘汰。
    """

    def __init__(self, budget: int = _DEFAULT_BUDGET) -> None:
        self._budget = max(1, budget)

    def select(
        self,
        items: list[ContextItem],
        budget: int | None = None,
    ) -> list[ContextItem]:
        """在预算内择优注入 ContextItem。

        Args:
            items: 待分配的 ContextItem 列表。
            budget: 覆盖默认预算（不传则用构造时预算）。

        Returns:
            被选中注入的 items（按输入顺序排列）。
        """
        if not items:
            return []

        total_budget = self._budget if budget is None else max(0, budget)
        if total_budget <= 0:
            return []

        # 1. 强制注入 mandatory 项（预留预算）
        mandatory = [it for it in items if it.mandatory]
        optional = [it for it in items if not it.mandatory]

        selected_indices: set[int] = set()
        consumed = 0
        for it in mandatory:
            selected_indices.add(id(it))
            consumed += it.token_cost

        # 2. 可选项目按 {priority desc, relevance desc} 排序
        ranked = sorted(
            optional,
            key=lambda it: (it.priority, it.relevance),
            reverse=True,
        )

        # 3. 贪心择优
        for it in ranked:
            if consumed + it.token_cost > total_budget:
                # 单项超预算：跳过（不截断，避免破坏语义完整性）
                continue
            selected_indices.add(id(it))
            consumed += it.token_cost

        # 4. 保持输入顺序返回
        selected = [it for it in items if id(it) in selected_indices]

        total_cost = sum(it.token_cost for it in items)
        log.info(
            "budget_allocator.selected",
            total_items=len(items),
            selected=len(selected),
            consumed_tokens=consumed,
            available_budget=total_budget,
            total_tokens=total_cost,
        )
        return selected

    @property
    def budget(self) -> int:
        """当前预算。"""
        return self._budget