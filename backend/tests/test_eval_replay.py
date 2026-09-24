"""
轨迹回放导出测试 — app/eval/replay.py。

覆盖：
    - 从本地 SpanRecord / LangFuse 导出两种形态还原 query、检索文档、答案
    - bad case 判定信号（负反馈 / 错误 / 跑满迭代 / 空召回 / 拒答 / judge 低分）
    - 自动产物必须是 needs_review 候选，且不携带伪造的 ground truth
    - 去重（归一化 query + 内容哈希）与 only_bad / limit
    - promote 只放人工标注过的用例进门禁集（核心诚实性约束）
    - 脏数据容错（坏行、缺字段、非法 JSON）
"""

import json

from app.eval.dataset import EvalDataset
from app.eval.replay import (
    NEEDS_REVIEW_TAG,
    REPLAY_TAG,
    ReplayExporter,
    TraceSnapshot,
    case_from_trace,
    detect_bad_case,
    load_traces,
)


def _span(span_type, name, **kw):
    base = {
        "span_id": kw.pop("span_id", "s1"),
        "parent_span_id": kw.pop("parent_span_id", None),
        "span_type": span_type,
        "name": name,
        "start_time": 1.0,
        "end_time": 2.0,
        "status": "ok",
    }
    base.update(kw)
    return base


class TestTraceParsing:
    def test_from_plain_dict(self):
        snap = TraceSnapshot.from_dict(
            {
                "trace_id": "t1",
                "query": "报销上限是多少",
                "answer": "8000 元",
                "retrieved_doc_ids": ["doc_a", "doc_b"],
                "feedback": "complaint",
            }
        )
        assert snap.trace_id == "t1"
        assert snap.query == "报销上限是多少"
        assert snap.retrieved_doc_ids == ["doc_a", "doc_b"]
        assert snap.feedback == "complaint"

    def test_query_and_docs_recovered_from_spans(self):
        spans = [
            _span("task.run", "task.run", metadata={"query": "会议室怎么订", "iterations": 3}),
            _span(
                "retrieve",
                "retrieve_iter1",
                parent_span_id="s1",
                metadata={"doc_ids": ["doc_x"]},
            ),
            _span("generate", "generate", output_ref="在 OA 里提交申请"),
        ]
        snap = TraceSnapshot.from_dict({"trace_id": "t2", "spans": spans})
        assert snap.query == "会议室怎么订"
        assert snap.retrieved_doc_ids == ["doc_x"]
        assert snap.answer == "在 OA 里提交申请"
        assert snap.iterations == 3

    def test_langfuse_export_shape(self):
        # LangFuse 导出用 observations / input / output 命名
        snap = TraceSnapshot.from_dict(
            {
                "id": "lf-1",
                "input": "年假几天",
                "output": "5 天",
                "observations": [
                    _span("retrieve", "retrieve", metadata={"doc_ids": ["doc_h"]})
                ],
            }
        )
        assert snap.trace_id == "lf-1"
        assert snap.query == "年假几天"
        assert snap.retrieved_doc_ids == ["doc_h"]

    def test_error_span_becomes_error(self):
        spans = [_span("tool.call", "tool:erp", status="error", error="timeout")]
        snap = TraceSnapshot.from_dict({"query": "q", "spans": spans})
        assert snap.error == "timeout"

    def test_ab_group_carried_from_root_span(self):
        spans = [
            _span(
                "task.run",
                "task.run",
                metadata={
                    "ab_experiment": "prompt_v2",
                    "ab_arm": "treatment",
                    "ab_prompt_variant": "answer_first",
                },
            )
        ]
        snap = TraceSnapshot.from_dict({"query": "q", "spans": spans})
        assert snap.ab_arm == "treatment"
        assert snap.prompt_variant == "answer_first"

    def test_nested_online_quality_score_is_used_as_judge_signal(self):
        """engine 把自评分写在根 span 的 quality 子字典里，不是顶层字段。"""
        spans = [
            _span(
                "task.run",
                "task.run",
                metadata={
                    "iterations": 2,
                    "retrieved_docs": 3,
                    "quality": {"total_score": 0.31, "passed": False},
                },
            )
        ]
        snap = TraceSnapshot.from_dict(
            {
                "query": "q",
                "answer": "先填单再审批",
                "spans": spans
                + [_span("retrieve", "retrieve", evidence_ref="doc_a,doc_b")],
            }
        )
        assert snap.judge_score == 0.31
        assert detect_bad_case(snap, judge_floor=0.6) == ["low_judge_score"]


class TestBadCaseDetection:
    def test_good_trace_has_no_reasons(self):
        snap = TraceSnapshot(
            query="报销流程",
            answer="先填单再审批",
            retrieved_doc_ids=["doc_a"],
            judge_score=0.9,
        )
        assert detect_bad_case(snap) == []

    def test_signals_are_detected(self):
        assert "negative_feedback" in detect_bad_case(
            TraceSnapshot(query="q", feedback="complaint", retrieved_doc_ids=["d"])
        )
        assert "empty_retrieval" in detect_bad_case(TraceSnapshot(query="q"))
        assert "max_iterations_reached" in detect_bad_case(
            TraceSnapshot(query="q", retrieved_doc_ids=["d"], max_iterations_reached=True)
        )
        assert "refusal" in detect_bad_case(
            TraceSnapshot(query="q", answer="抱歉，没有找到相关信息", retrieved_doc_ids=["d"])
        )
        assert "low_judge_score" in detect_bad_case(
            TraceSnapshot(query="q", answer="a", retrieved_doc_ids=["d"], judge_score=0.2)
        )

    def test_judge_score_accepts_100_scale(self):
        snap = TraceSnapshot(
            query="q", answer="a", retrieved_doc_ids=["d"], judge_score=25.0
        )
        assert "low_judge_score" in detect_bad_case(snap)

    def test_empty_query_never_a_bad_case(self):
        assert detect_bad_case(TraceSnapshot(query="", feedback="complaint")) == []


class TestCaseConstruction:
    def test_candidate_has_no_fabricated_ground_truth(self):
        """自动导出的用例绝不能自带 expected_doc_ids —— 否则评测变成自我确认。"""
        snap = TraceSnapshot(
            query="报销上限",
            answer="8000 元",
            retrieved_doc_ids=["doc_a"],
            cited_doc_ids=["doc_a"],
            feedback="complaint",
        )
        case = case_from_trace(snap)
        assert case.expected_doc_ids == []
        assert REPLAY_TAG in case.tags
        assert NEEDS_REVIEW_TAG in case.tags
        assert "label_source:unlabeled" in case.tags
        assert case.case_id.startswith("replay-")

    def test_citation_weak_label_is_marked(self):
        snap = TraceSnapshot(
            query="报销上限", answer="见 doc_a", cited_doc_ids=["doc_a"]
        )
        case = case_from_trace(snap, use_citations_as_expected=True)
        assert case.expected_doc_ids == ["doc_a"]
        assert "label_source:auto_citation" in case.tags
        assert NEEDS_REVIEW_TAG in case.tags

    def test_provenance_in_tags(self):
        snap = TraceSnapshot(
            query="q", trace_id="t9", created_at="2026-09-01", ab_arm="control"
        )
        case = case_from_trace(snap, reasons=["error"])
        assert "trace:t9" in case.tags
        assert "at:2026-09-01" in case.tags
        assert "ab_arm:control" in case.tags
        assert "error" in case.tags

    def test_replay_context_keeps_original_retrieval(self):
        snap = TraceSnapshot(query="q", answer="a", retrieved_doc_ids=["d1", "d2"])
        case = case_from_trace(snap, reasons=[])
        assert case.context_expect["replayed_doc_ids"] == ["d1", "d2"]

    def test_correlation_id_survives_to_candidate(self):
        """关联键要一路带到标注队列，否则无法回查这次决策的下游结果。"""
        spans = [_span("task.run", "task.run", metadata={"ab_correlation_id": "conv-42"})]
        snap = TraceSnapshot.from_dict({"query": "q", "spans": spans})
        assert snap.correlation_id == "conv-42"
        case = case_from_trace(snap, reasons=["error"])
        assert case.context_expect["correlation_id"] == "conv-42"


class TestExporter:
    def _write_traces(self, tmp_path, rows):
        path = tmp_path / "traces.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)

    def test_export_dedupes_against_existing_dataset(self, tmp_path):
        existing = tmp_path / "p0.jsonl"
        existing.write_text(
            json.dumps({"query": "报销上限是多少", "expected_doc_ids": ["d"]}) + "\n",
            encoding="utf-8",
        )
        traces = self._write_traces(
            tmp_path,
            [
                {"query": "报销上限是多少", "feedback": "complaint"},
                {"query": "差旅标准呢", "feedback": "complaint"},
            ],
        )
        out = str(tmp_path / "candidates.jsonl")
        exporter = ReplayExporter(existing_paths=[str(existing)])
        stats = exporter.export(load_traces(traces), out)
        assert stats["skipped_duplicate"] == 1
        assert stats["written"] == 1

    def test_export_skips_good_traces_by_default(self, tmp_path):
        traces = self._write_traces(
            tmp_path,
            [
                {"query": "好问题", "answer": "好答案", "retrieved_doc_ids": ["d"]},
                {"query": "坏问题", "feedback": "complaint", "retrieved_doc_ids": ["d"]},
            ],
        )
        out = str(tmp_path / "c.jsonl")
        stats = ReplayExporter().export(load_traces(traces), out)
        assert stats["written"] == 1
        assert stats["skipped_not_bad"] == 1

    def test_all_traces_flag_overrides(self, tmp_path):
        traces = self._write_traces(
            tmp_path, [{"query": "好问题", "retrieved_doc_ids": ["d"]}]
        )
        out = str(tmp_path / "c.jsonl")
        stats = ReplayExporter().export(
            load_traces(traces), out, only_bad=False
        )
        assert stats["written"] == 1

    def test_same_query_different_trace_not_deduped_by_case_id(self, tmp_path):
        """同一句话被问两次是重复；不同 trace 内容不同也不该无限堆叠。

        去重按归一化 query 生效 —— 高频问题不该变成 N 条用例。
        """
        traces = self._write_traces(
            tmp_path,
            [
                {"query": "WiFi 密码？", "answer": "a1", "feedback": "complaint"},
                {"query": "WiFi 密码? ", "answer": "a2", "feedback": "complaint"},
            ],
        )
        out = str(tmp_path / "c.jsonl")
        stats = ReplayExporter().export(load_traces(traces), out)
        assert stats["written"] == 1
        assert stats["skipped_duplicate"] == 1

    def test_limit_respected(self, tmp_path):
        traces = self._write_traces(
            tmp_path,
            [{"query": f"问题{i}", "feedback": "complaint"} for i in range(10)],
        )
        out = str(tmp_path / "c.jsonl")
        stats = ReplayExporter().export(load_traces(traces), out, limit=3)
        assert stats["written"] == 3

    def test_exported_file_is_loadable_as_dataset(self, tmp_path):
        traces = self._write_traces(
            tmp_path, [{"query": "报销", "feedback": "complaint"}]
        )
        out = str(tmp_path / "c.jsonl")
        ReplayExporter().export(load_traces(traces), out)
        ds = EvalDataset.load(out)
        assert len(ds) == 1
        assert ds.cases[0].case_id.startswith("replay-")


class TestPromote:
    """合入门禁 —— 本模块存在的意义所在。"""

    def _candidate(self, *, source="unlabeled", docs=None, query="报销上限"):
        snap = TraceSnapshot(
            query=query, answer="8000", retrieved_doc_ids=["doc_a"], feedback="complaint"
        )
        case = case_from_trace(snap, use_citations_as_expected=(source == "auto_citation"))
        case.context_expect["label_source"] = source
        if docs is not None:
            case.expected_doc_ids = docs
        return case

    def _write(self, path, cases):
        with open(path, "w", encoding="utf-8") as fh:
            for c in cases:
                fh.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

    def test_unlabeled_candidates_are_rejected(self, tmp_path):
        cand = str(tmp_path / "c.jsonl")
        target = str(tmp_path / "p6.jsonl")
        self._write(cand, [self._candidate(docs=["doc_a"])])
        stats = ReplayExporter.promote(cand, target)
        assert stats["promoted"] == 0
        assert stats["rejected_unlabeled"] == 1
        assert not open(target, encoding="utf-8").read().strip()

    def test_auto_citation_still_rejected(self, tmp_path):
        """弱标注不能混进门禁集 —— 否则 ground truth 就是系统自己的检索结果。"""
        cand = str(tmp_path / "c.jsonl")
        target = str(tmp_path / "p6.jsonl")
        self._write(cand, [self._candidate(source="auto_citation", docs=["doc_a"])])
        stats = ReplayExporter.promote(cand, target)
        assert stats["promoted"] == 0

    def test_human_labeled_promoted_and_review_flag_cleared(self, tmp_path):
        cand = str(tmp_path / "c.jsonl")
        target = str(tmp_path / "p6.jsonl")
        self._write(
            cand, [self._candidate(source="human", docs=["doc_a", "doc_b"])]
        )
        stats = ReplayExporter.promote(cand, target)
        assert stats["promoted"] == 1
        ds = EvalDataset.load(target)
        assert len(ds) == 1
        assert NEEDS_REVIEW_TAG not in ds.cases[0].tags
        assert REPLAY_TAG in ds.cases[0].tags
        assert ds.cases[0].expected_doc_ids == ["doc_a", "doc_b"]
        # tags 与 context_expect 不能各说各话：候选期的 label_source 必须被改写
        assert "label_source:unlabeled" not in ds.cases[0].tags
        assert "label_source:human" in ds.cases[0].tags

    def test_human_label_without_docs_rejected(self, tmp_path):
        cand = str(tmp_path / "c.jsonl")
        target = str(tmp_path / "p6.jsonl")
        self._write(cand, [self._candidate(source="human", docs=[])])
        stats = ReplayExporter.promote(cand, target)
        assert stats["promoted"] == 0

    def test_promote_is_idempotent(self, tmp_path):
        cand = str(tmp_path / "c.jsonl")
        target = str(tmp_path / "p6.jsonl")
        case = self._candidate(source="human", docs=["doc_a"])
        self._write(cand, [case])
        ReplayExporter.promote(cand, target)
        stats = ReplayExporter.promote(cand, target)
        assert stats["promoted"] == 0
        assert stats["already_present"] == 1
        assert len(EvalDataset.load(target)) == 1


class TestLoadTracesTolerance:
    def test_bad_lines_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        path.write_text(
            '{"query": "ok"}\nnot-json\n[1,2]\n\n{"query": "ok2"}\n', encoding="utf-8"
        )
        snaps = load_traces(str(path))
        assert [s.query for s in snaps] == ["ok", "ok2"]

    def test_json_array_supported(self, tmp_path):
        path = tmp_path / "export.json"
        path.write_text(json.dumps([{"query": "a"}, {"query": "b"}]), encoding="utf-8")
        assert len(load_traces(str(path))) == 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_traces(str(tmp_path / "nope.jsonl")) == []
