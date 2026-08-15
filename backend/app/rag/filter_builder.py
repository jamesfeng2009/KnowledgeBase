"""
层级过滤器构建器 — 单一职责：将通用 filters dict 转为各后端的过滤子句。

P0 wiki 层级改造：向量检索（OpenSearch k-NN / Milvus）和全文检索（BM25）
都需要按层级元数据过滤（series_id / path_prefix / parent_id / depth / version_of）。
本模块统一构建过滤子句，避免向量存储和检索器各自重复实现。

标准 filters key（调用方传入的 dict key）::

    {
        "series_id":   "prod-a",          # 所属系列精确匹配
        "path_prefix": "产品/合规/",       # 层级路径前缀匹配
        "parent_id":   "<uuid>",          # 父文档精确匹配
        "depth":       2,                 # 层级深度精确匹配
        "version_of":  "<uuid>",          # 版本族主文档匹配
    }

字段命名说明：
    - 向量索引已有 chunk 级 ``parent_id``（父子回溯用），文档级父文档用
      ``doc_parent_id`` 区分，避免字段语义冲突。
    - 所有字段在索引中为 keyword/integer 类型，支持 term/prefix 精确过滤。

遵循单一职责：只做 filter 子句构建，不执行检索。
遵循开闭原则：新增过滤维度只需扩展 _FILTER_BUILDERS 字典。
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

#: 向量索引中文档级父文档的字段名（区别于 chunk 级 parent_id）
DOC_PARENT_ID_FIELD: str = "doc_parent_id"

#: 支持的标准 filters key 集合（用于校验和文档）
SUPPORTED_FILTER_KEYS: frozenset[str] = frozenset({
    "series_id",
    "path_prefix",
    "parent_id",
    "depth",
    "version_of",
    "doc_status",  # P0-1: 文档状态过滤（published/draft/pending_review/archived）
    "doc_role",    # P2: 文档角色粗标过滤（normal/constraint_source，运营用）
})


# ======================================================================
# OpenSearch filter 子句构建
# ======================================================================


def _os_term(field: str, value: Any) -> dict[str, Any]:
    """构建 OpenSearch term 子句（精确匹配）。"""
    return {"term": {field: value}}


def _os_prefix(field: str, value: Any) -> dict[str, Any]:
    """构建 OpenSearch prefix 子句（前缀匹配，用于 path 层级）。"""
    return {"prefix": {field: str(value)}}


#: OpenSearch filter 构建器映射：key → (索引字段名, 构建函数)
#: 每个构建器接收 value，返回一个 OpenSearch filter 子句 dict。
_OS_FILTER_BUILDERS: dict[str, tuple[str, Any]] = {
    "series_id": ("series_id", lambda v: _os_term("series_id", v)),
    "path_prefix": ("path", lambda v: _os_prefix("path", v)),
    "parent_id": (DOC_PARENT_ID_FIELD, lambda v: _os_term(DOC_PARENT_ID_FIELD, str(v))),
    "depth": ("depth", lambda v: _os_term("depth", int(v))),
    "version_of": ("version_of", lambda v: _os_term("version_of", str(v))),
    "doc_status": ("doc_status", lambda v: _os_term("doc_status", str(v))),
    "doc_role": ("doc_role", lambda v: _os_term("doc_role", str(v))),
}


def build_opensearch_filter_clauses(
    filters: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """将通用 filters dict 转为 OpenSearch filter 子句列表。

    用于向量检索（k-NN bool query 的 filter 数组）和 BM25 全文检索
    （query_clause["bool"]["filter"]）。未知 key 记 warning 并跳过，
    不抛异常（向后兼容，避免前端传入新 key 时检索全挂）。

    Args:
        filters: 标准 filters dict（series_id/path_prefix/parent_id/depth/version_of）。

    Returns:
        OpenSearch filter 子句列表，如 ``[{"term": {"series_id": "x"}}]``。
        filters 为 None 或空时返回空列表。
    """
    if not filters:
        return []

    clauses: list[dict[str, Any]] = []
    for key, value in filters.items():
        if value is None:
            continue
        builder = _OS_FILTER_BUILDERS.get(key)
        if builder is None:
            log.warning("filter_builder.os.unknown_key", key=key)
            continue
        _, build_fn = builder
        try:
            clauses.append(build_fn(value))
        except (ValueError, TypeError) as exc:
            log.warning("filter_builder.os.build_error", key=key, value=value, error=str(exc))
    return clauses


def build_opensearch_combined_filter(
    kb_ids: list[str] | None,
    filters: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """合并 kb_ids 和 filters 为 OpenSearch filter 子句列表。

    kb_ids → terms 子句（多值 OR），filters → 各自子句。
    两者都为空时返回空列表（调用方据此决定是否加 filter 数组）。

    Args:
        kb_ids: 知识库 ID 列表。
        filters: 层级过滤 dict。

    Returns:
        合并后的 filter 子句列表。
    """
    clauses: list[dict[str, Any]] = []
    if kb_ids:
        clauses.append({"terms": {"kb_id": kb_ids}})
    clauses.extend(build_opensearch_filter_clauses(filters))
    return clauses


# ======================================================================
# Milvus expr 字符串构建
# ======================================================================


def _milvus_str_literal(value: Any) -> str:
    """将值转为 Milvus expr 的字符串字面量（单引号转义）。"""
    s = str(value).replace("'", "\\'")
    return f"'{s}'"


#: Milvus filter 构建器映射：key → (字段名, 构建函数)
#: Milvus expr 语法：``field == 'value'`` 或 ``field like 'prefix%'``
_MILVUS_FILTER_BUILDERS: dict[str, tuple[str, Any]] = {
    "series_id": ("series_id", lambda v: f"series_id == {_milvus_str_literal(v)}"),
    "path_prefix": ("path", lambda v: f"path like {_milvus_str_literal(str(v) + '%')}"),
    "parent_id": (DOC_PARENT_ID_FIELD, lambda v: f"{DOC_PARENT_ID_FIELD} == {_milvus_str_literal(v)}"),
    "depth": ("depth", lambda v: f"depth == {int(v)}"),
    "version_of": ("version_of", lambda v: f"version_of == {_milvus_str_literal(v)}"),
    "doc_status": ("doc_status", lambda v: f"doc_status == {_milvus_str_literal(v)}"),
    "doc_role": ("doc_role", lambda v: f"doc_role == {_milvus_str_literal(v)}"),
}


def build_milvus_expr(
    kb_ids: list[str] | None,
    filters: dict[str, Any] | None,
) -> str:
    """将 kb_ids + filters 转为 Milvus expr 字符串。

    Milvus 的 expr 是单个字符串（AND 连接），与 OpenSearch 的 filter 数组不同。
    kb_ids 转为 ``kb_id in ['a','b']``，filters 各 key 转为对应表达式。
    两者都为空时返回空字符串（Milvus 不传 expr = 不过滤）。

    Args:
        kb_ids: 知识库 ID 列表。
        filters: 层级过滤 dict。

    Returns:
        Milvus expr 字符串，如 ``"kb_id in ['a'] and series_id == 'x'"``。
        无过滤条件时返回空字符串。
    """
    parts: list[str] = []

    if kb_ids:
        ids_str = ", ".join(_milvus_str_literal(k) for k in kb_ids)
        parts.append(f"kb_id in [{ids_str}]")

    if filters:
        for key, value in filters.items():
            if value is None:
                continue
            builder = _MILVUS_FILTER_BUILDERS.get(key)
            if builder is None:
                log.warning("filter_builder.milvus.unknown_key", key=key)
                continue
            _, build_fn = builder
            try:
                parts.append(build_fn(value))
            except (ValueError, TypeError) as exc:
                log.warning("filter_builder.milvus.build_error", key=key, value=value, error=str(exc))

    return " and ".join(parts)


# ======================================================================
# filters 校验工具
# ======================================================================


def validate_filters(filters: dict[str, Any] | None) -> list[str]:
    """校验 filters dict，返回未知 key 列表（不抛异常）。

    用于 API 层入参校验时记录日志，不阻断请求。

    Args:
        filters: 待校验的 filters dict。

    Returns:
        未知 key 列表（空列表表示全部合法或 filters 为空）。
    """
    if not filters:
        return []
    return [k for k in filters if k not in SUPPORTED_FILTER_KEYS]
