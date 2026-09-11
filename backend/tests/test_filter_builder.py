"""层级过滤器构建器测试 — P0 wiki 层级改造。

验证 filter_builder 将通用 filters dict 正确转为：
    - OpenSearch filter 子句列表（term/prefix/terms）
    - Milvus expr 字符串（== / like / in）

覆盖：空输入、单 key、多 key 组合、kb_ids 合并、未知 key 跳过、
None 值跳过、depth=0 边界、path_prefix 前缀语义、validate_filters。
"""

from __future__ import annotations

from unittest.mock import patch

from app.config import get_settings
from app.rag.filter_builder import (
    DOC_PARENT_ID_FIELD,
    SUPPORTED_FILTER_KEYS,
    build_milvus_expr,
    build_opensearch_combined_filter,
    build_opensearch_filter_clauses,
    validate_filters,
)


# ======================================================================
# build_opensearch_filter_clauses
# ======================================================================


class TestOpenSearchFilterClauses:
    """OpenSearch filter 子句构建。"""

    def test_none_filters_returns_empty(self) -> None:
        assert build_opensearch_filter_clauses(None) == []

    def test_empty_filters_returns_empty(self) -> None:
        assert build_opensearch_filter_clauses({}) == []

    def test_series_id_term(self) -> None:
        clauses = build_opensearch_filter_clauses({"series_id": "prod-a"})
        assert clauses == [{"term": {"series_id": "prod-a"}}]

    def test_path_prefix_uses_prefix_query(self) -> None:
        """path_prefix 必须用 prefix 查询（匹配整个子树），而非 term 精确匹配。"""
        clauses = build_opensearch_filter_clauses({"path_prefix": "产品/合规/"})
        assert clauses == [{"prefix": {"path": "产品/合规/"}}]

    def test_parent_id_maps_to_doc_parent_id(self) -> None:
        """parent_id 必须映射到 doc_parent_id 字段（区别于 chunk 级 parent_id）。"""
        clauses = build_opensearch_filter_clauses({"parent_id": "uuid-123"})
        assert clauses == [{"term": {DOC_PARENT_ID_FIELD: "uuid-123"}}]

    def test_depth_zero_is_valid(self) -> None:
        """depth=0 是有效值（根文档），必须正确构建子句而非跳过。"""
        clauses = build_opensearch_filter_clauses({"depth": 0})
        assert clauses == [{"term": {"depth": 0}}]

    def test_depth_positive(self) -> None:
        clauses = build_opensearch_filter_clauses({"depth": 2})
        assert clauses == [{"term": {"depth": 2}}]

    def test_version_of_term(self) -> None:
        clauses = build_opensearch_filter_clauses({"version_of": "master-uuid"})
        assert clauses == [{"term": {"version_of": "master-uuid"}}]

    def test_multiple_keys_combined(self) -> None:
        clauses = build_opensearch_filter_clauses(
            {"series_id": "s1", "depth": 1, "path_prefix": "产品/"}
        )
        # 三个子句都应存在（顺序按 dict 迭代）
        assert len(clauses) == 3
        assert {"term": {"series_id": "s1"}} in clauses
        assert {"term": {"depth": 1}} in clauses
        assert {"prefix": {"path": "产品/"}} in clauses

    def test_none_value_skipped(self) -> None:
        """值为 None 的 key 应跳过（不构建子句）。"""
        clauses = build_opensearch_filter_clauses(
            {"series_id": "s1", "parent_id": None, "depth": None}
        )
        assert clauses == [{"term": {"series_id": "s1"}}]

    def test_unknown_key_skipped(self) -> None:
        """未知 key 应跳过（不抛异常，向后兼容）。"""
        clauses = build_opensearch_filter_clauses(
            {"series_id": "s1", "unknown_field": "x"}
        )
        assert clauses == [{"term": {"series_id": "s1"}}]

    def test_parent_id_stringified(self) -> None:
        """parent_id 值应转为字符串（UUID 对象也支持）。"""
        clauses = build_opensearch_filter_clauses({"parent_id": 12345})
        assert clauses == [{"term": {DOC_PARENT_ID_FIELD: "12345"}}]

    def test_doc_role_term(self) -> None:
        """P2: doc_role 精确过滤（运营检索 constraint_source 文档）。"""
        clauses = build_opensearch_filter_clauses({"doc_role": "constraint_source"})
        assert clauses == [{"term": {"doc_role": "constraint_source"}}]

    def test_doc_role_stringified(self) -> None:
        """P2: doc_role 值应转为字符串。"""
        clauses = build_opensearch_filter_clauses({"doc_role": "normal"})
        assert clauses == [{"term": {"doc_role": "normal"}}]


# ======================================================================
# build_opensearch_combined_filter
# ======================================================================


class TestOpenSearchCombinedFilter:
    """kb_ids + filters 合并构建。"""

    def test_both_none_returns_empty(self) -> None:
        assert build_opensearch_combined_filter(None, None) == []

    def test_kb_ids_only(self) -> None:
        clauses = build_opensearch_combined_filter(["kb1", "kb2"], None)
        assert clauses == [{"terms": {"kb_id": ["kb1", "kb2"]}}]

    def test_filters_only(self) -> None:
        clauses = build_opensearch_combined_filter(None, {"series_id": "s1"})
        assert clauses == [{"term": {"series_id": "s1"}}]

    def test_kb_ids_and_filters_combined(self) -> None:
        """kb_ids（权限过滤）和 filters（层级过滤）应在同一 filter 数组。"""
        clauses = build_opensearch_combined_filter(
            ["kb1"], {"series_id": "s1", "depth": 0}
        )
        # kb_ids terms 在前，filters 在后
        assert {"terms": {"kb_id": ["kb1"]}} in clauses
        assert {"term": {"series_id": "s1"}} in clauses
        assert {"term": {"depth": 0}} in clauses
        assert len(clauses) == 3

    def test_empty_kb_ids_list_returns_empty(self) -> None:
        """空 kb_ids 列表不应生成 terms 子句（避免空 terms 匹配全部）。"""
        assert build_opensearch_combined_filter([], None) == []


# ======================================================================
# build_milvus_expr
# ======================================================================


class TestMilvusExpr:
    """Milvus expr 字符串构建。"""

    def test_both_none_returns_empty(self) -> None:
        assert build_milvus_expr(None, None) == ""

    def test_kb_ids_only(self) -> None:
        expr = build_milvus_expr(["kb1", "kb2"], None)
        assert "kb_id in [" in expr
        assert "'kb1'" in expr
        assert "'kb2'" in expr

    def test_series_id_expr(self) -> None:
        expr = build_milvus_expr(None, {"series_id": "prod-a"})
        assert expr == "series_id == 'prod-a'"

    def test_path_prefix_uses_like(self) -> None:
        """path_prefix 必须用 like 'prefix%' 匹配子树。"""
        expr = build_milvus_expr(None, {"path_prefix": "产品/合规/"})
        assert expr == "path like '产品/合规/%'"

    def test_parent_id_maps_to_doc_parent_id(self) -> None:
        expr = build_milvus_expr(None, {"parent_id": "uuid-x"})
        assert expr == f"{DOC_PARENT_ID_FIELD} == 'uuid-x'"

    def test_depth_zero(self) -> None:
        expr = build_milvus_expr(None, {"depth": 0})
        assert expr == "depth == 0"

    def test_version_of_expr(self) -> None:
        expr = build_milvus_expr(None, {"version_of": "master"})
        assert expr == "version_of == 'master'"

    def test_doc_role_expr(self) -> None:
        """P2: doc_role 精确过滤。"""
        expr = build_milvus_expr(None, {"doc_role": "constraint_source"})
        assert expr == "doc_role == 'constraint_source'"

    def test_kb_ids_and_filters_joined_by_and(self) -> None:
        expr = build_milvus_expr(["kb1"], {"series_id": "s1"})
        assert " and " in expr
        assert "kb_id in [" in expr
        assert "series_id == 's1'" in expr

    def test_multiple_filters_joined_by_and(self) -> None:
        expr = build_milvus_expr(None, {"series_id": "s1", "depth": 1})
        assert " and " in expr
        assert "series_id == 's1'" in expr
        assert "depth == 1" in expr

    def test_single_quote_escaped(self) -> None:
        """字符串值中的单引号应转义（防 expr 注入）。"""
        expr = build_milvus_expr(None, {"series_id": "a'b"})
        # 转义后应为 a\'b
        assert "\\'" in expr

    def test_none_value_skipped(self) -> None:
        expr = build_milvus_expr(None, {"series_id": "s1", "depth": None})
        assert expr == "series_id == 's1'"

    def test_unknown_key_skipped(self) -> None:
        expr = build_milvus_expr(None, {"series_id": "s1", "unknown": "x"})
        assert expr == "series_id == 's1'"


# ======================================================================
# P0 密级下推 — classification 多值白名单子句
# ======================================================================


class TestOpenSearchClassificationClause:
    """P0 密级下推：OpenSearch classification 子句（容忍 / strict 双模式）。"""

    def test_tolerant_mode_allows_missing_field(self) -> None:
        """容忍模式（默认，回填完成前）：should [terms, must_not exists] —
        存量索引中无 classification 字段的旧文档放行到 Final Gate。"""
        assert get_settings().CLASSIFICATION_PUSHDOWN_STRICT is False
        clauses = build_opensearch_filter_clauses(
            {"classification": ["public", "internal"]}
        )
        assert len(clauses) == 1
        clause = clauses[0]
        assert clause["bool"]["minimum_should_match"] == 1
        should = clause["bool"]["should"]
        assert {"terms": {"classification": ["public", "internal"]}} in should
        assert {
            "bool": {"must_not": {"exists": {"field": "classification"}}}
        } in should

    def test_strict_mode_pure_terms(self) -> None:
        """strict 模式（回填完成后开启）：纯 terms 白名单 —
        字段缺失的文档不再放行。"""
        with patch.object(
            get_settings(), "CLASSIFICATION_PUSHDOWN_STRICT", True
        ):
            clauses = build_opensearch_filter_clauses(
                {"classification": ["public", "internal"]}
            )
        assert clauses == [{"terms": {"classification": ["public", "internal"]}}]

    def test_single_string_value_normalized(self) -> None:
        """单字符串值归一化为单元素列表（防御性兼容）。"""
        clauses = build_opensearch_filter_clauses({"classification": "public"})
        assert (
            {"terms": {"classification": ["public"]}}
            in clauses[0]["bool"]["should"]
        )

    def test_combined_with_kb_ids(self) -> None:
        """kb_ids 与 classification 白名单在同一 filter 数组（AND 语义）。"""
        clauses = build_opensearch_combined_filter(
            ["kb1"], {"classification": ["public"]}
        )
        assert {"terms": {"kb_id": ["kb1"]}} in clauses
        assert len(clauses) == 2


class TestMilvusClassificationExpr:
    """P0 密级下推：Milvus expr（仅 strict 模式下推，容忍模式跳过）。"""

    def test_tolerant_mode_skips_pushdown(self) -> None:
        """容忍模式：Milvus 无法表达字段缺失语义，跳过下推（Final Gate 兜底）。"""
        expr = build_milvus_expr(["kb1"], {"classification": ["public"]})
        assert "classification" not in expr
        assert expr == "kb_id in ['kb1']"

    def test_strict_mode_in_clause(self) -> None:
        """strict 模式（回填完成后开启）：``classification in [...]`` 白名单。"""
        with patch.object(
            get_settings(), "CLASSIFICATION_PUSHDOWN_STRICT", True
        ):
            expr = build_milvus_expr(["kb1"], {"classification": ["public", "internal"]})
        assert expr == "kb_id in ['kb1'] and classification in ['public', 'internal']"

    def test_strict_mode_combined_with_other_filters(self) -> None:
        with patch.object(
            get_settings(), "CLASSIFICATION_PUSHDOWN_STRICT", True
        ):
            expr = build_milvus_expr(
                None, {"classification": ["public"], "doc_status": "published"}
            )
        assert " and " in expr
        assert "classification in ['public']" in expr
        assert "doc_status == 'published'" in expr


# ======================================================================
# validate_filters
# ======================================================================


class TestValidateFilters:
    """filters 校验。"""

    def test_none_returns_empty(self) -> None:
        assert validate_filters(None) == []

    def test_empty_returns_empty(self) -> None:
        assert validate_filters({}) == []

    def test_all_valid_keys(self) -> None:
        assert validate_filters(
            {"series_id": "x", "path_prefix": "y", "parent_id": "z", "depth": 0,
             "version_of": "w"}
        ) == []

    def test_returns_unknown_keys(self) -> None:
        unknown = validate_filters({"series_id": "x", "foo": "y", "bar": "z"})
        assert set(unknown) == {"foo", "bar"}

    def test_supported_keys_constant(self) -> None:
        """SUPPORTED_FILTER_KEYS 应包含全部标准 key（P0 层级 5 个 + P0-1 doc_status + P2 doc_role + P0 密级下推 classification）。"""
        assert SUPPORTED_FILTER_KEYS == frozenset(
            {
                "series_id",
                "path_prefix",
                "parent_id",
                "depth",
                "version_of",
                "doc_status",
                "doc_role",
                "classification",
            }
        )
