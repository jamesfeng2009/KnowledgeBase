"""
Skill 注册表 — 渐进式技能加载（Find Skills）的核心数据结构。

设计灵感来自 Claude Agent Skills 的按需加载机制：
    - 未激活时：只加载 skill 的 name + category + tags + description（几十 token）；
    - 激活时：加载完整的 Tool schema（parameters JSON Schema，200-500 token）。

本模块维护轻量技能索引，提供按名称子集加载完整 schema 的能力，
供 SkillFinder 进行意图匹配后按需加载。

核心类：
    - SkillMetadata: 单个技能的轻量元数据（name/category/tags/description）。
    - SkillRegistry: 技能注册表，从 MCP Server 构建索引 + 按需加载 schema。

使用方式::

    from app.rag.skill_registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_from_server(mcp_server)  # 构建轻量索引

    # 1. 获取轻量索引（token 开销极小）
    index = registry.get_index()

    # 2. 匹配后按需加载完整 schema
    tools = registry.load_tools(["knowledge_search", "document_get"])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm.base import Tool
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class SkillMetadata:
    """技能轻量元数据 — 用于意图匹配，token 开销极小。

    对比完整 Tool schema（含 parameters JSON Schema），
    SkillMetadata 仅含 name/category/tags/description，
    每个技能约 20-30 token（完整 schema 约 200-500 token）。

    Attributes:
        name: 技能名称（对应工具名）。
        category: 分类（如 search/document/workflow/analytics/general）。
        tags: 标签列表，用于关键词匹配。
        description: 技能描述（比 Tool.description 可更详细）。
    """

    name: str
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供 SkillFinder 匹配）。"""
        return {
            "name": self.name,
            "category": self.category,
            "tags": list(self.tags),
            "description": self.description,
        }

    def match_score(self, query: str, query_lower: str, query_terms: list[str]) -> int:
        """计算查询与技能的匹配分数（越高越匹配）。

        匹配规则：
            - 工具名包含查询词 → +10 分
            - 分类名匹配查询词 → +5 分
            - 标签匹配查询词 → +8 分/词
            - 描述包含查询词 → +3 分/词

        Args:
            query: 原始查询字符串。
            query_lower: 查询的小写版本。
            query_terms: 查询分词后的词列表。

        Returns:
            匹配分数（0 表示无匹配）。
        """
        score = 0
        name_lower = self.name.lower()
        desc_lower = self.description.lower()
        category_lower = self.category.lower()

        for term in query_terms:
            term_lower = term.lower()
            if term_lower in name_lower:
                score += 10
            if term_lower in category_lower:
                score += 5
            for tag in self.tags:
                if term_lower in tag.lower():
                    score += 8
            if term_lower in desc_lower:
                score += 3
        return score


class SkillRegistry:
    """技能注册表 — 轻量索引 + 按需加载。

    从 MCP Server 构建轻量技能索引，支持：
        1. ``get_index()`` — 返回所有技能的元数据（用于匹配）；
        2. ``load_tools(names)`` — 按名称子集加载完整 Tool schema。

    设计原则：
        - 索引构建 O(n) 一次，后续匹配 O(1) 查表；
        - 按需加载只返回匹配工具的 schema，未匹配的工具不加载；
        - 向后兼容：未标注 category/tags 的工具视为 "general" 分类。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._metadata: dict[str, SkillMetadata] = {}
        self._server: Any = None  # KnowledgeBaseMCPServer 引用
        # P0-1: 技能描述向量（进程内缓存）— 技能量级为数十个，
        # 进程内余弦计算与 pgvector 等效，避免运行时易变索引的表生命周期管理
        self._embeddings: dict[str, list[float]] = {}

    def load_from_server(self, server: Any) -> None:
        """从 MCP Server 构建轻量技能索引。

        调用 ``server.get_skill_index()`` 获取元数据列表，
        构建 ``SkillMetadata`` 字典。不加载完整 schema。

        Args:
            server: KnowledgeBaseMCPServer 实例。
        """
        self._server = server
        self._metadata.clear()
        self._embeddings.clear()  # 索引重建后向量失效，需重新预计算

        try:
            index_data = server.get_skill_index()
        except Exception as exc:
            log.warning("skill_registry.load_failed", error=str(exc))
            return

        for item in index_data:
            name = item.get("name", "")
            if not name:
                continue
            self._metadata[name] = SkillMetadata(
                name=name,
                category=item.get("category", "general"),
                tags=item.get("tags", []),
                description=item.get("description", ""),
            )
        log.info(
            "skill_registry.loaded",
            count=len(self._metadata),
            categories=list({m.category for m in self._metadata.values()}),
        )

    def get_index(self) -> list[dict[str, Any]]:
        """返回所有技能的轻量元数据列表。

        用于 SkillFinder 意图匹配，token 开销极小。
        """
        return [meta.to_dict() for meta in self._metadata.values()]

    def get_metadata(self, name: str) -> SkillMetadata | None:
        """按名称获取技能元数据。

        Args:
            name: 技能名称。

        Returns:
            SkillMetadata 或 None（未找到时）。
        """
        return self._metadata.get(name)

    def get_all_names(self) -> list[str]:
        """返回所有已注册技能的名称列表。"""
        return list(self._metadata.keys())

    # ------------------------------------------------------------------
    # P0-1: 描述向量预计算（语义召回通道）
    # ------------------------------------------------------------------

    async def build_embeddings(self, embedder: Any) -> int:
        """预计算技能描述向量 — 语义召回通道的索引构建。

        每个技能的嵌入文本为 ``name + category + tags + description`` 拼接，
        与关键词匹配共用同一语义空间，保证向量通道与关键词通道互补。

        Args:
            embedder: EmbeddingProvider 实例（复用现有嵌入设施）。

        Returns:
            成功预计算的技能向量数；embedder 不可用或无技能时返回 0。
        """
        if not self._metadata:
            return 0
        try:
            names: list[str] = []
            texts: list[str] = []
            for name, meta in self._metadata.items():
                text = " ".join(
                    part
                    for part in [meta.name, meta.category, " ".join(meta.tags), meta.description]
                    if part
                )
                names.append(name)
                texts.append(text)
            vectors = await embedder.embed(texts)
            self._embeddings = {
                name: vec
                for name, vec in zip(names, vectors, strict=False)
                if vec
            }
            log.info(
                "skill_registry.embeddings_built",
                count=len(self._embeddings),
                total=len(self._metadata),
            )
            return len(self._embeddings)
        except Exception as exc:
            # 优雅降级：向量构建失败不影响关键词通道
            log.warning("skill_registry.embeddings_build_failed", error=str(exc))
            self._embeddings = {}
            return 0

    def get_embedding(self, name: str) -> list[float] | None:
        """获取技能的描述向量（未预计算时返回 None）。"""
        return self._embeddings.get(name)

    def get_all_embeddings(self) -> dict[str, list[float]]:
        """返回全部技能向量（SkillFinder 语义召回用）。"""
        return self._embeddings

    def get_categories(self) -> list[str]:
        """返回所有技能分类列表（去重）。"""
        return list({meta.category for meta in self._metadata.values()})

    async def load_tools(self, names: list[str]) -> list[Tool]:
        """按名称子集加载完整 Tool schema — 按需加载入口。

        只有被 SkillFinder 匹配到的工具才会加载完整 schema，
        避免全量加载浪费 token。

        Args:
            names: 需要加载的技能名称列表。

        Returns:
            匹配到的 Tool 列表（可能为空）。
        """
        if self._server is None:
            log.warning("skill_registry.no_server")
            return []

        try:
            return await self._server.list_tools_by_names(names)
        except Exception as exc:
            log.error("skill_registry.load_tools_failed", error=str(exc), names=names)
            return []

    def get_token_estimate(self, names: list[str] | None = None) -> int:
        """估算指定技能集的 token 开销。

        Args:
            names: 技能名称列表（None 表示全部）。

        Returns:
            估算 token 数（粗略：字符数 / 3）。
        """
        if names is None:
            targets = list(self._metadata.values())
        else:
            targets = [self._metadata[n] for n in names if n in self._metadata]

        total_chars = 0
        for meta in targets:
            # 轻量元数据：name + category + tags + description
            total_chars += len(meta.name) + len(meta.category)
            total_chars += sum(len(t) for t in meta.tags)
            total_chars += len(meta.description)
        return total_chars // 3  # 粗略估算
