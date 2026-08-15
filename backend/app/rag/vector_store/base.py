"""
向量存储抽象接口 — 单一职责：定义向量存储的统一契约。

遵循依赖倒置：检索器（HybridRetriever）和文档处理（document_tasks）
均通过 VectorStoreBase 接口操作向量数据，不依赖具体后端实现。
遵循开闭原则：新增向量存储后端只需继承 VectorStoreBase 并在 factory 注册。

搜索结果统一格式::

    {
        "doc_id": str,
        "chunk_id": str,
        "content": str,
        "score": float,           # 相似度分数（越高越相似）
        "source": "vector",       # 固定为 "vector"
        "kb_id": str | None,
        "title": str | None,      # title_path
        "parent_id": str | None,  # 父块 ID（父子索引回溯用）
    }
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.rag.chunker import Chunk

# 默认向量维度 — BGE-M3 输出 1024 维（私有部署模式）。
# SaaS 模式（OpenAI text-embedding-3-large）输出 3072 维，
# 实际维度由 dimension 属性动态从 Embedder 获取（P2-Step2 修复）。
_DEFAULT_DIMENSION: int = 1024


class VectorStoreBase(ABC):
    """向量存储抽象基类 — 定义 search / upsert / delete / health_check 四项契约。

    子类实现：
        - OpenSearchVectorStore — OpenSearch k-NN 引擎（默认，适合 < 500 万向量）
        - MilvusVectorStore    — Milvus 向量引擎（可选，适合 > 500 万向量）
    """

    @abstractmethod
    async def search(
        self,
        query_vec: list[float],
        kb_ids: list[str] | None = None,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量相似度检索 — 返回最相似的文档块列表。

        Args:
            query_vec: 查询向量（与索引时使用的 Embedder 一致）。
            kb_ids: 可选，限定检索的知识库 ID 列表。
            top_k: 返回结果数量上限。
            filters: P0 wiki 层级过滤 — 标准 key:
                series_id / path_prefix / parent_id / depth / version_of。
                由 app.rag.filter_builder 转为后端 filter 子句。
                None 或空时不过滤（向后兼容）。

        Returns:
            候选文档列表，按相似度降序排列，每项格式见模块文档。
        """
        ...

    @abstractmethod
    async def upsert(
        self,
        doc_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        kb_id: str | None = None,
        doc_updated_at: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        doc_meta: dict[str, Any] | None = None,
    ) -> int:
        """批量写入（插入或更新）向量数据。

        Args:
            doc_id: 文档 ID。
            chunks: Chunk 对象列表（含元数据）。
            embeddings: 与 chunks 对应的向量嵌入列表。
            kb_id: 文档所属知识库 ID — 写入 ``kb_id`` 字段，
                与检索端按知识库过滤（``terms: {kb_id: ...}``）对齐；
                缺省时回退使用 chunk 携带的 kb_id。
            doc_updated_at: 文档更新时间（ISO 格式）— 检索端 recency
                平局裁决依据；缺省则不写入（该文档不参与新鲜度排序）。
            effective_from: 文档生效时间（ISO 格式，规范类文档可选）。
            effective_to: 文档失效时间（ISO 格式，规范类文档可选）。
            doc_meta: P0 wiki 层级元数据 — series_id/path/doc_parent_id/
                depth/version_of。写入索引后支持检索时按 filters 过滤。
                None 时跳过层级字段（向后兼容旧文档）。

        Returns:
            成功写入的向量数量。
        """
        ...

    @abstractmethod
    async def delete(self, doc_id: str) -> None:
        """删除指定文档的所有向量数据。

        Args:
            doc_id: 文档 ID。
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查 — 返回向量存储服务是否可用。

        Returns:
            True 表示服务可用，False 表示不可用。
        """
        ...

    @abstractmethod
    async def fetch_by_ids(
        self, chunk_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """按 chunk_id 批量获取文档元数据（不含向量）。

        父子索引回溯核心：检索命中子块后，按 ``parent_id`` 批量获取
        父块原文，实现「小块检索 → 大块返回」的上下文扩充。

        Args:
            chunk_ids: 需要获取的 chunk_id 列表。

        Returns:
            ``{chunk_id: {content, title_path, ...}}`` 映射表。
            不存在的 chunk_id 不出现在返回值中。
        """
        ...

    # ------------------------------------------------------------------
    # 公共工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_kb_id(chunk: Chunk, doc_id: str, kb_id: str | None = None) -> str:
        """解析写入向量库的知识库 ID — 与检索端 kb_id 过滤对齐（安全）。

        优先级：显式入参 ``kb_id`` > chunk 携带的 ``kb_id`` 属性 >
        ``chunk.doc_id`` / ``doc_id``（兼容旧调用方的兜底值）。
        """
        return kb_id or getattr(chunk, "kb_id", None) or getattr(chunk, "doc_id", None) or doc_id

    @staticmethod
    def _format_result(
        doc_id: str,
        chunk_id: str,
        content: str,
        score: float,
        kb_id: str | None = None,
        title: str | None = None,
        parent_id: str | None = None,
        updated_at: Any = None,
        effective_from: Any = None,
        effective_to: Any = None,
        doc_role: str | None = None,
    ) -> dict[str, Any]:
        """格式化搜索结果为统一字典格式。

        ``updated_at`` / ``effective_from`` / ``effective_to`` 为检索层
        recency 加权与生效窗口过滤字段，仅在写入侧提供时出现（向后兼容）。
        ``doc_role`` 为 P2 文档角色粗标（normal/constraint_source），
        供运营在普通召回路径识别约束文档。
        """
        result: dict[str, Any] = {
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "content": content,
            "score": score,
            "source": "vector",
            "kb_id": kb_id,
            "title": title,
            "parent_id": parent_id,
        }
        if updated_at is not None:
            result["updated_at"] = updated_at
        if effective_from is not None:
            result["effective_from"] = effective_from
        if effective_to is not None:
            result["effective_to"] = effective_to
        if doc_role is not None:
            result["doc_role"] = doc_role
        return result

    @property
    def dimension(self) -> int:
        """向量维度 — P2-Step2: 动态从当前 Embedder 获取，解决 SaaS 模式维度不匹配。

        SaaS 模式 OpenAI text-embedding-3-large 输出 3072 维，
        私有部署 BGE-M3 via TEI 输出 1024 维。
        旧代码硬编码 1024 导致 SaaS 模式 upsert 时维度不匹配静默失败。

        C1/C2 fix: 支持跨模态索引的维度覆盖 — 跨模态索引固定使用
        jina-clip-v2 的 1024 维，与文本 Embedder 的维度无关。
        """
        # C1/C2 fix: 跨模态索引维度覆盖
        if getattr(self, "_dimension_override", None) is not None:
            return self._dimension_override
        try:
            from app.llm.embedder import get_embedder

            embedder = get_embedder()
            if embedder.dim > 0:
                return embedder.dim
        except Exception:
            pass
        return _DEFAULT_DIMENSION
