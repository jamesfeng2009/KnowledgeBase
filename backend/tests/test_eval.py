"""
离线评测系统测试 — app/eval/ 模块。

覆盖范围：
    - dataset.py：EvalCase 创建、JSONL 加载（正常/空文件/格式错误/目录加载）
    - runner.py：recall_at_k / mrr / ndcg 计算正确性（边界情况）、EvalRunner.run() 集成
    - repository.py：compare_with_baseline 回归检测、数据库不可用降级、持久化往返
    - CLI：run_eval.main() 退出码（正常 0 / 回归 1）
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery，参考 test_document_parser.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# dataset.py 测试
# ======================================================================


class TestEvalCase:
    """EvalCase 数据类测试。"""

    def test_creation_defaults(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase(query="测试问题")
        assert case.query == "测试问题"
        assert case.expected_doc_ids == []
        assert case.expected_answer is None
        assert case.kb_ids is None
        assert case.tags == []

    def test_creation_full(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase(
            query="报销流程",
            expected_doc_ids=["doc_1", "doc_2"],
            expected_answer="填写报销单",
            kb_ids=["kb_1"],
            tags=["财务", "报销"],
        )
        assert case.expected_doc_ids == ["doc_1", "doc_2"]
        assert case.expected_answer == "填写报销单"
        assert case.kb_ids == ["kb_1"]
        assert case.tags == ["财务", "报销"]

    def test_from_dict_tolerant(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase.from_dict({"query": "  q  ", "expected_doc_ids": ["a", None, 1]})
        assert case.query == "q"
        # None 被过滤，整数被转为字符串
        assert case.expected_doc_ids == ["a", "1"]

    def test_to_dict_roundtrip(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase(query="q", expected_doc_ids=["a"], tags=["t"])
        d = case.to_dict()
        assert d["query"] == "q"
        assert d["expected_doc_ids"] == ["a"]
        assert d["tags"] == ["t"]


class TestEvalDatasetLoad:
    """EvalDataset JSONL 加载测试。"""

    def test_load_normal(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.eval.dataset import EvalDataset

        path = tmp_path / "normal.jsonl"
        path.write_text(
            json.dumps({"query": "报销", "expected_doc_ids": ["d1"]}, ensure_ascii=False)
            + "\n"
            + json.dumps(
                {"query": "请假", "expected_doc_ids": ["d2"], "tags": ["hr"]},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        ds = EvalDataset.load(str(path))
        assert len(ds) == 2
        assert ds.cases[0].query == "报销"
        assert ds.cases[1].tags == ["hr"]
        # 迭代协议
        queries = [c.query for c in ds]
        assert queries == ["报销", "请假"]

    def test_load_empty_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.eval.dataset import EvalDataset

        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        ds = EvalDataset.load(str(path))
        assert len(ds) == 0
        assert bool(ds) is False

    def test_load_malformed_lines_skipped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.eval.dataset import EvalDataset

        path = tmp_path / "bad.jsonl"
        path.write_text(
            '{"query": "good"}\n'
            "not a json line\n"
            '{"query": ""}\n'  # 空 query 被跳过
            '["array", "not", "object"]\n'
            '{"query": "second"}\n',
            encoding="utf-8",
        )
        ds = EvalDataset.load(str(path))
        # 仅保留两条有效用例
        assert len(ds) == 2
        assert ds.cases[0].query == "good"
        assert ds.cases[1].query == "second"

    def test_load_nonexistent_file(self) -> None:
        from app.eval.dataset import EvalDataset

        ds = EvalDataset.load("/nonexistent/path/to/file.jsonl")
        assert len(ds) == 0

    def test_load_from_dir(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.eval.dataset import EvalDataset

        (tmp_path / "a.jsonl").write_text(
            json.dumps({"query": "a1"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "b.jsonl").write_text(
            json.dumps({"query": "b1"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"query": "b2"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        # 非 jsonl 文件应被忽略
        (tmp_path / "readme.txt").write_text("ignore me", encoding="utf-8")

        ds = EvalDataset.load_from_dir(str(tmp_path))
        assert len(ds) == 3
        queries = sorted(c.query for c in ds)
        assert queries == ["a1", "b1", "b2"]

    def test_load_from_nonexistent_dir(self) -> None:
        from app.eval.dataset import EvalDataset

        ds = EvalDataset.load_from_dir("/nonexistent/dir")
        assert len(ds) == 0


# ======================================================================
# runner.py 检索指标测试
# ======================================================================


class TestRecallAtK:
    """recall_at_k 计算测试。"""

    def test_perfect_match(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k(["a", "b", "c"], ["a", "b"], 5) == 1.0

    def test_partial_match(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k(["a", "x", "y"], ["a", "b"], 5) == 0.5

    def test_no_match(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k(["x", "y"], ["a", "b"], 5) == 0.0

    def test_k_limit(self) -> None:
        """相关文档在 K 之外不计入。"""
        from app.eval.runner import recall_at_k

        assert recall_at_k(["a", "b", "c"], ["c"], 2) == 0.0
        assert recall_at_k(["a", "b", "c"], ["c"], 3) == 1.0

    def test_empty_retrieved(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k([], ["a"], 5) == 0.0

    def test_empty_relevant(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k(["a", "b"], [], 5) == 0.0

    def test_k_zero_or_negative(self) -> None:
        from app.eval.runner import recall_at_k

        assert recall_at_k(["a"], ["a"], 0) == 0.0
        assert recall_at_k(["a"], ["a"], -1) == 0.0


class TestMRR:
    """mrr 计算测试。"""

    def test_first_position(self) -> None:
        from app.eval.runner import mrr

        assert mrr(["a", "b"], ["a"]) == 1.0

    def test_second_position(self) -> None:
        from app.eval.runner import mrr

        assert mrr(["x", "a"], ["a"]) == 0.5

    def test_third_position(self) -> None:
        from app.eval.runner import mrr

        assert mrr(["x", "y", "a"], ["a"]) == pytest.approx(1 / 3)

    def test_no_match(self) -> None:
        from app.eval.runner import mrr

        assert mrr(["x", "y"], ["a"]) == 0.0

    def test_empty_lists(self) -> None:
        from app.eval.runner import mrr

        assert mrr([], ["a"]) == 0.0
        assert mrr(["a"], []) == 0.0


class TestNDCG:
    """ndcg 计算测试。"""

    def test_perfect_order(self) -> None:
        from app.eval.runner import ndcg

        assert ndcg(["a", "b"], ["a", "b"], 5) == 1.0

    def test_partial_order(self) -> None:
        import math

        from app.eval.runner import ndcg

        # dcg = 1/log2(2) + 0 + 1/log2(4) = 1 + 0.5 = 1.5
        # idcg = 1/log2(2) + 1/log2(3) = 1 + 0.6309 ≈ 1.6309
        result = ndcg(["a", "x", "b"], ["a", "b"], 3)
        expected = 1.5 / (1.0 / math.log2(2) + 1.0 / math.log2(3))
        assert result == pytest.approx(expected, rel=1e-3)
        assert result < 1.0  # 非完美排序应小于 1

    def test_no_match(self) -> None:
        from app.eval.runner import ndcg

        assert ndcg(["x", "y"], ["a", "b"], 5) == 0.0

    def test_empty_relevant(self) -> None:
        from app.eval.runner import ndcg

        assert ndcg(["a", "b"], [], 5) == 0.0

    def test_empty_retrieved(self) -> None:
        from app.eval.runner import ndcg

        assert ndcg([], ["a"], 5) == 0.0

    def test_k_limit(self) -> None:
        """K 之外的相关文档不贡献 DCG，但 IDCG 仍按 |R| 计算。"""
        from app.eval.runner import ndcg

        # retrieved 长度 > k，只看前 k
        result = ndcg(["a", "b", "c"], ["b"], 1)
        # dcg = 0（b 不在前 1），idcg = 1/log2(2)=1
        assert result == 0.0


# ======================================================================
# runner.py EvalRunner 集成测试
# ======================================================================


class _FakeEngine:
    """用于测试的假 RAG 引擎 — _retrieve 写入预设文档，answer 产出固定 token。"""

    def __init__(self, docs_by_query: dict[str, list[dict]]) -> None:
        self._docs_by_query = docs_by_query
        self.answer_call_count = 0

    async def _retrieve(self, state: dict, kb_ids: list[str] | None) -> None:
        q = state.get("query", "")
        state["retrieved_docs"] = list(self._docs_by_query.get(q, []))

    async def answer(self, query, user_id, session_id, kb_ids=None, memory_context=""):  # type: ignore[no-untyped-def]
        self.answer_call_count += 1
        yield "这是"
        yield "答案"


class TestEvalRunnerRun:
    """EvalRunner.run 集成测试。"""

    @pytest.mark.asyncio
    async def test_run_with_generation_and_judge(self) -> None:
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner
        from app.observability.llm_judge import EvalResult

        docs = [
            {"doc_id": "doc_1", "content": "报销需填写报销单", "score": 0.9},
        ]
        engine = _FakeEngine({"报销流程": docs})

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(
            return_value=EvalResult(
                question="报销流程",
                answer="这是答案",
                citation_accuracy=4,
                completeness=5,
                hallucination_inverse=4,
                total_score=4.33,
            )
        )

        ds = EvalDataset(
            [EvalCase(query="报销流程", expected_doc_ids=["doc_1"])]
        )
        runner = EvalRunner(engine=engine, judge_service=mock_judge)
        result = await runner.run(ds, with_generation=True)

        assert result.total == 1
        assert result.passed == 1
        # 检索命中 → recall=1.0
        assert result.avg_recall_at_5 == 1.0
        assert result.avg_mrr == 1.0
        assert result.avg_ndcg_at_5 == 1.0
        # Judge 被调用
        assert engine.answer_call_count == 1
        mock_judge.evaluate_single.assert_called_once()
        case = result.case_results[0]
        assert case.answer == "这是答案"
        assert case.judge_scores is not None
        assert case.judge_scores["total_score"] == 4.33
        assert result.avg_judge_score == pytest.approx(4.33)
        assert result.run_id  # 自动生成

    @pytest.mark.asyncio
    async def test_run_no_generation(self) -> None:
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        docs = [{"doc_id": "d1", "content": "c1"}]
        engine = _FakeEngine({"q1": docs})
        ds = EvalDataset([EvalCase(query="q1", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=False)

        assert engine.answer_call_count == 0
        case = result.case_results[0]
        assert case.answer is None
        assert case.judge_scores is None
        assert case.recall_at_5 == 1.0
        assert result.avg_judge_score == 0.0

    @pytest.mark.asyncio
    async def test_run_partial_retrieval(self) -> None:
        """部分命中的指标计算。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        docs = [
            {"doc_id": "d1", "content": "c1"},
            {"doc_id": "d2", "content": "c2"},
            {"doc_id": "d3", "content": "c3"},
        ]
        engine = _FakeEngine({"q": docs})
        # expected 含 d1 和 d4（d4 未命中）
        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1", "d4"])])
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=False)

        case = result.case_results[0]
        assert case.recall_at_5 == 0.5  # 1/2
        assert case.mrr == 1.0  # d1 在第 1 位
        assert case.retrieved_doc_ids == ["d1", "d2", "d3"]

    @pytest.mark.asyncio
    async def test_run_engine_none_degrades(self) -> None:
        """engine 为 None 时优雅降级，指标为 0。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=None, judge_service=None)
        result = await runner.run(ds, with_generation=True)

        assert result.total == 1
        case = result.case_results[0]
        assert case.recall_at_5 == 0.0
        assert case.error == "engine_unavailable"
        assert case.answer is None
        # engine 不可用 → 不通过
        assert result.passed == 0

    @pytest.mark.asyncio
    async def test_run_judge_none_skips_scoring(self) -> None:
        """with_generation=True 但 judge_service=None 时生成答案但不评分。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        engine = _FakeEngine({"q": [{"doc_id": "d1", "content": "c"}]})
        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=True)

        case = result.case_results[0]
        assert case.answer == "这是答案"
        assert case.judge_scores is None
        assert result.avg_judge_score == 0.0

    @pytest.mark.asyncio
    async def test_run_retrieve_exception_handled(self) -> None:
        """_retrieve 抛异常时单条用例记录错误，不中断整体。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        engine = MagicMock()
        engine._retrieve = AsyncMock(side_effect=RuntimeError("boom"))
        ds = EvalDataset(
            [
                EvalCase(query="q1", expected_doc_ids=["d1"]),
                EvalCase(query="q2", expected_doc_ids=["d2"]),
            ]
        )
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=False)

        assert result.total == 2
        assert result.passed == 0
        for case in result.case_results:
            assert case.error is not None
            assert "retrieve_error" in case.error

    @pytest.mark.asyncio
    async def test_run_to_dict_serializable(self) -> None:
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        engine = _FakeEngine({"q": [{"doc_id": "d1", "content": "c"}]})
        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=False)

        d = result.to_dict()
        assert d["total"] == 1
        assert d["passed"] == 1
        assert json.dumps(d, ensure_ascii=False)  # 可 JSON 序列化


class TestMaxIterationsConfigurable:
    """max_iterations 不再硬编码为 1 — 构造参数化并透传到评测 state。"""

    @pytest.mark.asyncio
    async def test_default_max_iterations_is_five(self) -> None:
        """默认 max_iterations=5（与引擎默认值一致），不再是 1。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        captured: dict = {}

        class _CaptureEngine(_FakeEngine):
            async def _retrieve(self, state: dict, kb_ids: list[str] | None) -> None:
                captured["max_iterations"] = state.get("max_iterations")
                await super()._retrieve(state, kb_ids)

        engine = _CaptureEngine({"q": [{"doc_id": "d1", "content": "c"}]})
        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=engine, judge_service=None)
        result = await runner.run(ds, with_generation=False)

        assert runner.max_iterations == 5
        assert captured["max_iterations"] == 5
        assert result.max_iterations == 5
        assert result.to_dict()["max_iterations"] == 5

    @pytest.mark.asyncio
    async def test_custom_max_iterations_propagates_to_state(self) -> None:
        """自定义 max_iterations 透传到 _retrieve 的 state。"""
        from app.eval.dataset import EvalCase, EvalDataset
        from app.eval.runner import EvalRunner

        captured: dict = {}

        class _CaptureEngine(_FakeEngine):
            async def _retrieve(self, state: dict, kb_ids: list[str] | None) -> None:
                captured["max_iterations"] = state.get("max_iterations")
                await super()._retrieve(state, kb_ids)

        engine = _CaptureEngine({"q": [{"doc_id": "d1", "content": "c"}]})
        ds = EvalDataset([EvalCase(query="q", expected_doc_ids=["d1"])])
        runner = EvalRunner(engine=engine, judge_service=None, max_iterations=3)
        result = await runner.run(ds, with_generation=False)

        assert captured["max_iterations"] == 3
        assert result.max_iterations == 3

    @pytest.mark.asyncio
    async def test_max_iterations_floor_is_one(self) -> None:
        """max_iterations 下限保护为 1（0 或负数被钳制）。"""
        from app.eval.runner import EvalRunner

        assert EvalRunner(max_iterations=0).max_iterations == 1
        assert EvalRunner(max_iterations=-3).max_iterations == 1


# ======================================================================
# repository.py compare_with_baseline 测试
# ======================================================================


def _make_result(recall: float, mrr: float, ndcg: float, judge: float) -> object:  # type: ignore[no-untyped-def]
    from app.eval.runner import EvalRunResult

    return EvalRunResult(
        avg_recall_at_5=recall,
        avg_mrr=mrr,
        avg_ndcg_at_5=ndcg,
        avg_judge_score=judge,
        total=1,
        passed=1,
    )


class TestCompareWithBaseline:
    """compare_with_baseline 回归检测测试。"""

    def test_metric_drop_is_regression(self) -> None:
        from app.eval.repository import EvalRepository

        baseline = _make_result(1.0, 1.0, 1.0, 4.0)
        current = _make_result(0.5, 1.0, 1.0, 4.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        assert cmp["is_regression"] is True
        assert cmp["metrics"]["avg_recall_at_5"]["regressed"] is True
        assert cmp["metrics"]["avg_mrr"]["regressed"] is False

    def test_metric_improve_not_regression(self) -> None:
        from app.eval.repository import EvalRepository

        baseline = _make_result(0.5, 0.5, 0.5, 2.0)
        current = _make_result(1.0, 1.0, 1.0, 4.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        assert cmp["is_regression"] is False
        for m in cmp["metrics"].values():
            assert m["regressed"] is False

    def test_metric_flat_not_regression(self) -> None:
        from app.eval.repository import EvalRepository

        baseline = _make_result(0.8, 0.7, 0.6, 3.0)
        current = _make_result(0.8, 0.7, 0.6, 3.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        assert cmp["is_regression"] is False

    def test_small_drop_within_threshold(self) -> None:
        """下降比例未超阈值不算回归。"""
        from app.eval.repository import EvalRepository

        baseline = _make_result(1.0, 1.0, 1.0, 4.0)
        # 下降 2%，阈值默认 5%
        current = _make_result(0.98, 1.0, 1.0, 4.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        assert cmp["is_regression"] is False

    def test_baseline_zero_no_regression(self) -> None:
        """基线为 0 时不视为回归。"""
        from app.eval.repository import EvalRepository

        baseline = _make_result(0.0, 0.0, 0.0, 0.0)
        current = _make_result(0.0, 0.0, 0.0, 0.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        assert cmp["is_regression"] is False

    def test_threshold_from_config(self) -> None:
        from app.eval.repository import EvalRepository

        baseline = _make_result(1.0, 1.0, 1.0, 4.0)
        current = _make_result(0.5, 1.0, 1.0, 4.0)
        cmp = EvalRepository.compare_with_baseline(current, baseline)
        # 默认阈值 0.05
        assert cmp["threshold"] == pytest.approx(0.05)


# ======================================================================
# repository.py 数据库持久化测试（使用内存 SQLite）
# ======================================================================


class TestEvalRepositoryPersistence:
    """EvalRepository 持久化往返测试（注入 db_session）。"""

    @pytest.mark.asyncio
    async def test_save_and_get_baseline(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalCaseResult, EvalRunResult

        repo = EvalRepository(session=db_session)
        result = EvalRunResult(
            case_results=[EvalCaseResult(query="q", recall_at_5=1.0)],
            avg_recall_at_5=0.9,
            avg_mrr=0.8,
            avg_ndcg_at_5=0.7,
            avg_judge_score=4.0,
            total=1,
            passed=1,
            evaluated_at="2026-01-01T00:00:00",
        )
        run_id = await repo.save(result, "ds_test", is_baseline=True)
        assert run_id

        baseline = await repo.get_baseline("ds_test")
        assert baseline is not None
        assert baseline.run_id == run_id
        assert baseline.avg_recall_at_5 == pytest.approx(0.9)
        assert baseline.total == 1

    @pytest.mark.asyncio
    async def test_save_and_get_by_run_id(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        repo = EvalRepository(session=db_session)
        result = EvalRunResult(
            avg_recall_at_5=0.5,
            avg_mrr=0.4,
            avg_ndcg_at_5=0.3,
            avg_judge_score=2.0,
            total=2,
            passed=1,
            evaluated_at="2026-01-01T00:00:00",
        )
        run_id = await repo.save(result, "ds_by_id")
        fetched = await repo.get_by_run_id(run_id)
        assert fetched is not None
        assert fetched.avg_recall_at_5 == pytest.approx(0.5)
        assert fetched.total == 2

    @pytest.mark.asyncio
    async def test_set_baseline_replaces_old(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        repo = EvalRepository(session=db_session)
        r1 = EvalRunResult(avg_recall_at_5=0.5, total=1, passed=1, evaluated_at="t1")
        r2 = EvalRunResult(avg_recall_at_5=0.9, total=1, passed=1, evaluated_at="t2")
        id1 = await repo.save(r1, "ds_replace", is_baseline=True)
        id2 = await repo.save(r2, "ds_replace", is_baseline=False)

        # 初始基线是 r1
        baseline = await repo.get_baseline("ds_replace")
        assert baseline is not None
        assert baseline.run_id == id1

        # 将 r2 设为基线
        await repo.set_baseline(id2)
        baseline = await repo.get_baseline("ds_replace")
        assert baseline is not None
        assert baseline.run_id == id2
        assert baseline.avg_recall_at_5 == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_list_results(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        repo = EvalRepository(session=db_session)
        for i in range(3):
            await repo.save(
                EvalRunResult(avg_recall_at_5=0.1 * i, total=1, passed=1, evaluated_at="t"),
                "ds_list",
            )
        results = await repo.list_results("ds_list", limit=10)
        assert len(results) == 3
        assert all(r["dataset_name"] == "ds_list" for r in results)

    @pytest.mark.asyncio
    async def test_get_baseline_not_found(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.eval.repository import EvalRepository

        repo = EvalRepository(session=db_session)
        assert await repo.get_baseline("nonexistent_ds") is None


class TestEvalRepositoryDegradation:
    """数据库不可用时优雅降级测试。"""

    @pytest.mark.asyncio
    async def test_save_returns_run_id_without_db(self) -> None:
        """工厂不可用时 save 仍返回 run_id 但不持久化。"""
        from app.eval import repository as repo_mod
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        with patch.object(repo_mod, "_session_factory", None):
            repo = EvalRepository(session=None)
            result = EvalRunResult(avg_recall_at_5=0.5, total=1, passed=1, evaluated_at="t")
            run_id = await repo.save(result, "ds", is_baseline=True)
            assert run_id  # 仍返回 run_id
            # 未持久化 → 查不到
            assert await repo.get_baseline("ds") is None

    @pytest.mark.asyncio
    async def test_get_baseline_returns_none_without_db(self) -> None:
        from app.eval import repository as repo_mod
        from app.eval.repository import EvalRepository

        with patch.object(repo_mod, "_session_factory", None):
            repo = EvalRepository(session=None)
            assert await repo.get_baseline("ds") is None

    @pytest.mark.asyncio
    async def test_list_results_returns_empty_without_db(self) -> None:
        from app.eval import repository as repo_mod
        from app.eval.repository import EvalRepository

        with patch.object(repo_mod, "_session_factory", None):
            repo = EvalRepository(session=None)
            assert await repo.list_results("ds") == []

    @pytest.mark.asyncio
    async def test_set_baseline_silent_without_db(self) -> None:
        from app.eval import repository as repo_mod
        from app.eval.repository import EvalRepository

        with patch.object(repo_mod, "_session_factory", None):
            repo = EvalRepository(session=None)
            # 不应抛异常
            await repo.set_baseline("any-run-id")

    @pytest.mark.asyncio
    async def test_broken_session_degrades(self) -> None:
        """注入会话在执行时抛异常 → 降级不抛出。"""
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        broken = MagicMock()
        broken.add = MagicMock(side_effect=RuntimeError("db down"))
        broken.execute = AsyncMock(side_effect=RuntimeError("db down"))
        repo = EvalRepository(session=broken)
        result = EvalRunResult(avg_recall_at_5=0.5, total=1, passed=1, evaluated_at="t")
        # save 捕获异常仍返回 run_id
        run_id = await repo.save(result, "ds")
        assert run_id
        # get_baseline 捕获异常返回 None
        assert await repo.get_baseline("ds") is None


# ======================================================================
# CLI 测试（scripts/run_eval.py）
# ======================================================================


class TestRunEvalCLI:
    """run_eval.main() 退出码测试。"""

    def test_main_exit_zero_no_baseline(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        from scripts import run_eval

        # 准备数据集
        ds_path = tmp_path / "sample.jsonl"
        ds_path.write_text(
            json.dumps({"query": "q1", "expected_doc_ids": ["d1"]}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        with patch.object(run_eval, "_build_engine", return_value=None), \
             patch.object(run_eval, "_build_judge", return_value=None), \
             patch("app.eval.repository._session_factory", None):
            rc = run_eval.main(
                ["--dataset", str(ds_path), "--no-generation"]
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "离线评测报告" in out
        assert "avg_recall_at_5" in out

    def test_main_regression_exit_one(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        from scripts import run_eval
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        ds_path = tmp_path / "sample.jsonl"
        ds_path.write_text(
            json.dumps({"query": "q1", "expected_doc_ids": ["d1"]}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        # 高分基线 → 当前为 0 → 回归
        high_baseline = EvalRunResult(
            avg_recall_at_5=1.0,
            avg_mrr=1.0,
            avg_ndcg_at_5=1.0,
            avg_judge_score=4.0,
            total=1,
            passed=1,
            evaluated_at="2026-01-01T00:00:00",
        )

        with patch.object(run_eval, "_build_engine", return_value=None), \
             patch.object(run_eval, "_build_judge", return_value=None), \
             patch("app.eval.repository._session_factory", None), \
             patch.object(
                 EvalRepository,
                 "get_by_run_id",
                 new=AsyncMock(return_value=high_baseline),
             ):
            rc = run_eval.main(
                [
                    "--dataset",
                    str(ds_path),
                    "--no-generation",
                    "--baseline",
                    "some-run-id",
                ]
            )

        assert rc == 1
        err = capsys.readouterr().err
        assert "回归" in err

    def test_main_empty_dataset_exit_one(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from scripts import run_eval

        ds_path = tmp_path / "empty.jsonl"
        ds_path.write_text("", encoding="utf-8")

        with patch.object(run_eval, "_build_engine", return_value=None):
            rc = run_eval.main(["--dataset", str(ds_path), "--no-generation"])

        assert rc == 1

    def test_main_kb_ids_parsed(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        from scripts import run_eval

        ds_path = tmp_path / "sample.jsonl"
        ds_path.write_text(
            json.dumps({"query": "q1", "expected_doc_ids": ["d1"]}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        captured: dict = {}

        class _StubRunner:
            def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
                pass

            async def run(self, dataset, kb_ids=None, with_generation=True):  # type: ignore[no-untyped-def]
                captured["kb_ids"] = kb_ids
                captured["with_generation"] = with_generation
                from app.eval.runner import EvalRunResult

                return EvalRunResult(total=1, passed=1, evaluated_at="t")

        with patch.object(run_eval, "_build_engine", return_value=None), \
             patch.object(run_eval, "_build_judge", return_value=None), \
             patch.object(run_eval, "EvalRunner", _StubRunner), \
             patch("app.eval.repository._session_factory", None):
            rc = run_eval.main(
                [
                    "--dataset",
                    str(ds_path),
                    "--kb-ids",
                    "kb_a, kb_b",
                    "--no-generation",
                ]
            )

        assert rc == 0
        assert captured["kb_ids"] == ["kb_a", "kb_b"]
        assert captured["with_generation"] is False
