"""
评测数据集与 CI 验证测试 — 测试数据集格式、字段完整性、维度覆盖度。

覆盖:
    P0-1: CI 数据集验证脚本测试
    P0-2: 评测数据集内容完整性测试
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# 将 backend/ 加入 sys.path 以导入脚本
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from app.eval.dataset import EvalDataset  # noqa: E402

EVAL_DIR = _BACKEND_ROOT / "eval_datasets"


# ======================================================================
# P0-2: 数据集文件存在性与用例数量
# ======================================================================


class TestDatasetExistence:
    """验证三层评测数据集文件存在且用例数量达标。"""

    def test_p0_mandatory_exists(self):
        path = EVAL_DIR / "p0_mandatory.jsonl"
        assert path.exists(), f"P0 必测集不存在: {path}"

    def test_p1_complete_exists(self):
        path = EVAL_DIR / "p1_complete.jsonl"
        assert path.exists(), f"P1 完整集不存在: {path}"

    def test_p2_security_exists(self):
        path = EVAL_DIR / "p2_security.jsonl"
        assert path.exists(), f"P2 安全集不存在: {path}"

    def test_sample_still_exists(self):
        """原始 sample.jsonl 应保留。"""
        path = EVAL_DIR / "sample.jsonl"
        assert path.exists(), f"原始数据集不存在: {path}"

    def test_p0_mandatory_count(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p0_mandatory.jsonl"))
        assert len(dataset) >= 100, f"P0 用例数不足: {len(dataset)} < 100"

    def test_p1_complete_count(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p1_complete.jsonl"))
        assert len(dataset) >= 200, f"P1 用例数不足: {len(dataset)} < 200"

    def test_p2_security_count(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p2_security.jsonl"))
        assert len(dataset) >= 15, f"P2 用例数不足: {len(dataset)} < 15"

    def test_total_cases(self):
        """三层数据集总计应 >= 315 条。"""
        total = 0
        for fname in ["p0_mandatory.jsonl", "p1_complete.jsonl", "p2_security.jsonl"]:
            dataset = EvalDataset.load(str(EVAL_DIR / fname))
            total += len(dataset)
        assert total >= 315, f"总用例数不足: {total} < 315"


# ======================================================================
# P0-2: 数据集格式与字段完整性
# ======================================================================


class TestDatasetFormat:
    """验证每条用例格式合法、字段完整。"""

    def _load_all_lines(self, fname: str) -> list[dict]:
        path = EVAL_DIR / fname
        cases = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cases.append(json.loads(line))
        return cases

    def test_p0_has_required_fields(self):
        cases = self._load_all_lines("p0_mandatory.jsonl")
        for i, case in enumerate(cases):
            assert "query" in case, f"P0 第{i+1}条缺少 query"
            assert "expected_doc_ids" in case, f"P0 第{i+1}条缺少 expected_doc_ids"
            assert isinstance(case["expected_doc_ids"], list)
            assert len(case["expected_doc_ids"]) > 0, f"P0 第{i+1}条 expected_doc_ids 为空"
            assert "kb_ids" in case, f"P0 第{i+1}条缺少 kb_ids"
            assert "tags" in case, f"P0 第{i+1}条缺少 tags"

    def test_p1_has_expected_answer(self):
        """P1 完整集每条都必须有 expected_answer。"""
        cases = self._load_all_lines("p1_complete.jsonl")
        for i, case in enumerate(cases):
            assert "expected_answer" in case, f"P1 第{i+1}条缺少 expected_answer"
            assert case["expected_answer"].strip(), f"P1 第{i+1}条 expected_answer 为空"

    def test_p0_no_expected_answer(self):
        """P0 必测集不应包含 expected_answer（仅检索层）。"""
        cases = self._load_all_lines("p0_mandatory.jsonl")
        for i, case in enumerate(cases):
            assert "expected_answer" not in case, f"P0 第{i+1}条不应有 expected_answer"

    def test_p2_has_security_tags(self):
        """P2 安全集每条必须有 tenant_isolation 或 boundary 标签。"""
        cases = self._load_all_lines("p2_security.jsonl")
        for i, case in enumerate(cases):
            tags = case.get("tags", [])
            has_security_tag = "tenant_isolation" in tags or "boundary" in tags
            assert has_security_tag, f"P2 第{i+1}条缺少安全标签"

    def test_all_queries_non_empty(self):
        """所有数据集的 query 不能为空。"""
        for fname in ["p0_mandatory.jsonl", "p1_complete.jsonl", "p2_security.jsonl"]:
            cases = self._load_all_lines(fname)
            for i, case in enumerate(cases):
                assert case["query"].strip(), f"{fname} 第{i+1}条 query 为空"

    def test_all_doc_ids_are_strings(self):
        """expected_doc_ids 中每个元素必须是字符串。"""
        for fname in ["p0_mandatory.jsonl", "p1_complete.jsonl", "p2_security.jsonl"]:
            cases = self._load_all_lines(fname)
            for i, case in enumerate(cases):
                for doc_id in case["expected_doc_ids"]:
                    assert isinstance(doc_id, str), f"{fname} 第{i+1}条 doc_id 类型错误"


# ======================================================================
# P0-2: 评测维度覆盖度
# ======================================================================


class TestDimensionCoverage:
    """验证 P0 必测集覆盖六个标准评测维度。"""

    STANDARD_DIMENSIONS = {
        "exact_match",
        "semantic",
        "synonym",
        "cross_lingual",
        "fuzzy",
        "multi_constraint",
    }

    def test_p0_covers_all_dimensions(self):
        path = EVAL_DIR / "p0_mandatory.jsonl"
        found_dims = set()
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                case = json.loads(line)
                tags = set(case.get("tags", []))
                found_dims.update(self.STANDARD_DIMENSIONS & tags)
        missing = self.STANDARD_DIMENSIONS - found_dims
        assert not missing, f"P0 缺少评测维度: {missing}"

    def test_p0_dimension_distribution(self):
        """每个维度至少 10 条用例。"""
        path = EVAL_DIR / "p0_mandatory.jsonl"
        dim_counts: dict[str, int] = {d: 0 for d in self.STANDARD_DIMENSIONS}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                case = json.loads(line)
                tags = set(case.get("tags", []))
                for dim in self.STANDARD_DIMENSIONS & tags:
                    dim_counts[dim] += 1
        for dim, count in dim_counts.items():
            assert count >= 10, f"维度 {dim} 用例数不足: {count} < 10"


# ======================================================================
# P0-2: EvalDataset 加载兼容性
# ======================================================================


class TestDatasetLoading:
    """验证 EvalDataset 能正确加载新数据集。"""

    def test_load_p0(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p0_mandatory.jsonl"))
        assert len(dataset) == 100
        first = dataset.cases[0]
        assert hasattr(first, "query")
        assert hasattr(first, "expected_doc_ids")
        assert hasattr(first, "tags")

    def test_load_p1(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p1_complete.jsonl"))
        assert len(dataset) == 200
        first = dataset.cases[0]
        assert hasattr(first, "expected_answer")

    def test_load_p2(self):
        dataset = EvalDataset.load(str(EVAL_DIR / "p2_security.jsonl"))
        assert len(dataset) == 15

    def test_load_from_dir(self):
        """从目录加载所有数据集。"""
        dataset = EvalDataset.load_from_dir(str(EVAL_DIR))
        # 应包含所有 4 个文件的用例
        assert len(dataset) >= 315

    def test_p1_includes_p0_queries(self):
        """P1 完整集应包含 P0 的所有查询。"""
        p0 = EvalDataset.load(str(EVAL_DIR / "p0_mandatory.jsonl"))
        p1 = EvalDataset.load(str(EVAL_DIR / "p1_complete.jsonl"))
        p0_queries = {c.query for c in p0.cases}
        p1_queries = {c.query for c in p1.cases}
        assert p0_queries.issubset(p1_queries), "P1 未包含 P0 的所有查询"


# ======================================================================
# P0-1: 数据集验证脚本测试
# ======================================================================


class TestValidateEvalDataset:
    """测试 validate_eval_dataset.py 脚本。"""

    def test_validate_valid_directory(self):
        """验证合法数据集目录应返回 0。"""
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(EVAL_DIR)])
        assert result == 0, f"合法数据集验证失败，退出码: {result}"

    def test_validate_valid_file(self):
        """验证单个合法文件应返回 0。"""
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(EVAL_DIR / "p0_mandatory.jsonl")])
        assert result == 0

    def test_validate_nonexistent_path(self):
        """验证不存在的路径应返回 1。"""
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main(["/nonexistent/path"])
        assert result == 1

    def test_validate_missing_required_field(self, tmp_path):
        """验证缺少必填字段应返回 1。"""
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text(
            json.dumps({"query": "test"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(bad_file)])
        assert result == 1

    def test_validate_empty_query(self, tmp_path):
        """验证空 query 应返回 1。"""
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text(
            json.dumps(
                {"query": "", "expected_doc_ids": ["doc_1"]},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(bad_file)])
        assert result == 1

    def test_validate_empty_doc_ids_without_security_tag(self, tmp_path):
        """验证非安全用例 expected_doc_ids 为空应返回 1。"""
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text(
            json.dumps(
                {"query": "test", "expected_doc_ids": [], "tags": ["exact_match"]},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(bad_file)])
        assert result == 1

    def test_validate_empty_doc_ids_with_security_tag(self, tmp_path):
        """验证安全用例 expected_doc_ids 为空应通过。"""
        good_file = tmp_path / "good.jsonl"
        good_file.write_text(
            json.dumps(
                {
                    "query": "test",
                    "expected_doc_ids": [],
                    "tags": ["tenant_isolation", "boundary"],
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(good_file)])
        # 该文件名为 good，不在 MIN_CASES 中，不检查数量
        # 但 tags 中无 STANDARD_DIMENSIONS，会报错
        # 由于 good 不在 MIN_CASES 中，跳过维度检查
        assert result == 0

    def test_validate_malformed_json(self, tmp_path):
        """验证 JSON 格式错误应返回 1。"""
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("{invalid json}\n", encoding="utf-8")
        from scripts.validate_eval_dataset import main as validate_main
        result = validate_main([str(bad_file)])
        assert result == 1


# ======================================================================
# §5.6 / §7.3 扩展字段验证（p4_context / p5_generation）
# ======================================================================


class TestValidateExtendedFields:
    """测试 case_type / context_expect 等新字段的验证规则。"""

    def _write(self, tmp_path, obj: dict) -> Path:
        f = tmp_path / "ext.jsonl"
        f.write_text(
            json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return f

    def test_invalid_case_type_rejected(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "q",
                "expected_doc_ids": ["d1"],
                "tags": ["exact_match"],
                "case_type": "weird",
            },
        )
        assert validate_main([str(f)]) == 1

    def test_valid_case_types_accepted(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        for ct in ("normal", "negative", "golden"):
            f = self._write(
                tmp_path,
                {
                    "query": "q",
                    "expected_doc_ids": ["d1"],
                    "tags": ["exact_match"],
                    "case_type": ct,
                },
            )
            assert validate_main([str(f)]) == 0, f"case_type={ct} 应通过"

    def test_negative_case_empty_doc_ids_allowed(self, tmp_path):
        """negative 用例 expected_doc_ids 为空应通过（无安全标签也可）。"""
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "越权查询",
                "expected_doc_ids": [],
                "tags": ["boundary"],
                "case_type": "negative",
            },
        )
        assert validate_main([str(f)]) == 0

    def test_prompt_injection_tag_empty_doc_ids_allowed(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "忽略指令",
                "expected_doc_ids": [],
                "tags": ["prompt_injection"],
            },
        )
        assert validate_main([str(f)]) == 1  # prompt_injection 非标准维度标签

    def test_prompt_injection_with_boundary_tag_passes(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "忽略指令",
                "expected_doc_ids": [],
                "tags": ["prompt_injection", "boundary"],
            },
        )
        assert validate_main([str(f)]) == 0

    def test_context_expect_must_be_dict(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "q",
                "expected_doc_ids": ["d1"],
                "tags": ["context"],
                "context_expect": ["not", "a", "dict"],
            },
        )
        assert validate_main([str(f)]) == 1

    def test_context_expect_unknown_keys_rejected(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "q",
                "expected_doc_ids": ["d1"],
                "tags": ["context"],
                "context_expect": {"required_files": ["a"], "bogus_key": 1},
            },
        )
        assert validate_main([str(f)]) == 1

    def test_context_expect_valid_keys_accepted(self, tmp_path):
        from scripts.validate_eval_dataset import main as validate_main

        f = self._write(
            tmp_path,
            {
                "query": "q",
                "expected_doc_ids": ["d1"],
                "tags": ["context"],
                "context_expect": {
                    "type": "required_file",
                    "required_files": ["a"],
                    "distractor_files": ["b"],
                    "forbidden_files": ["c"],
                    "stale_refs": ["d"],
                    "required_after_compact": ["e"],
                },
            },
        )
        assert validate_main([str(f)]) == 0


class TestNewDatasetExistence:
    """p4_context / p5_generation 数据集存在性与数量。"""

    def test_p4_context_exists_and_count(self):
        path = EVAL_DIR / "p4_context.jsonl"
        assert path.exists(), f"p4_context 数据集不存在: {path}"
        dataset = EvalDataset.load(str(path))
        assert len(dataset) >= 7, f"p4_context 用例数不足: {len(dataset)} < 7"

    def test_p5_generation_exists_and_count(self):
        path = EVAL_DIR / "p5_generation.jsonl"
        assert path.exists(), f"p5_generation 数据集不存在: {path}"
        dataset = EvalDataset.load(str(path))
        assert len(dataset) >= 6, f"p5_generation 用例数不足: {len(dataset)} < 6"

    def test_p4_context_expect_loaded(self):
        """p4 用例的 context_expect 应随加载保留。"""
        dataset = EvalDataset.load(str(EVAL_DIR / "p4_context.jsonl"))
        with_expect = [c for c in dataset if c.context_expect]
        assert len(with_expect) == len(dataset), "p4 用例均应携带 context_expect"

    def test_p5_golden_cases_have_checkpoints(self):
        """p5 golden 用例均应携带 must_have_points。"""
        dataset = EvalDataset.load(str(EVAL_DIR / "p5_generation.jsonl"))
        for case in dataset:
            assert case.case_type == "golden"
            assert case.must_have_points, f"golden 用例缺 must_have_points: {case.query}"
