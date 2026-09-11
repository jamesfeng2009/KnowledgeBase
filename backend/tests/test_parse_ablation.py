"""解析消融管线集成测试 — 用合成语料 + 强制自检，确定性验证四维指标全链路。

不依赖真实 Docling/MinerU（避免模型下载），CI 可回归。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from evals.parse_ablation import _route_recommendation, _run_variant
from evals.parse_eval.smoke_corpus import generate as generate_corpus
from evals.parse_eval.schema import load_manifest


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> tuple[str, object]:
    d = tempfile.mkdtemp(prefix="parse_corpus_")
    truths = generate_corpus(d)
    return d, truths


@pytest.mark.asyncio
async def test_selfcheck_pipeline_covers_all_four_metrics(corpus) -> None:
    dir_, truths = corpus
    agg, _, _ = await _run_variant(
        "docling", truths, dir_, force_selfcheck=True
    )
    # 理想还原的正样板：四维指标都应注册并接近饱和
    assert agg["text_extraction"]["n"] == 3
    assert agg["table_restoration"]["n"] == 2
    assert agg["formula"]["block_hit_rate"] == pytest.approx(1.0)
    assert agg["ocr"]["avg_cer"] == pytest.approx(0.0)
    assert agg["n_docs"] == 4


@pytest.mark.asyncio
async def test_both_variants_produce_identical_dict_in_selfcheck(corpus) -> None:
    dir_, truths = corpus
    a, _av_a, docs_a = await _run_variant("docling", truths, dir_, force_selfcheck=True)
    b, _av_b, docs_b = await _run_variant("docling+mineru", truths, dir_, force_selfcheck=True)
    assert a["formula"]["block_hit_rate"] == b["formula"]["block_hit_rate"]
    assert a["ocr"]["avg_cer"] == b["ocr"]["avg_cer"]
    # 自检下双变体输出一致 → route 建议应维持默认，且 per-doc 命中数一致
    assert len(docs_a) == len(docs_b) == 4
    assert all(0.0 <= d["complexity"] <= 1.0 for d in docs_a)


def test_route_recommendation_keeps_default_when_no_gain() -> None:
    # Docling 与 MinerU 输出完全一致（自检场景）→ 无质量增益 → 维持默认路由
    shared = [
        {"doc_id": "d1", "complexity": 0.9, "table": {"f1": 0.6},
         "formula": {"block_hit_rate": 0.5}, "scan": True},
        {"doc_id": "d2", "complexity": 0.9, "table": {"f1": 0.6},
         "formula": {"block_hit_rate": 0.5}, "scan": True},
    ]
    rec = _route_recommendation(shared, shared)
    assert rec["recommended_route"] == "docling_default_conditional_mineru"
    assert rec["n_docs"] == 2
    assert rec["n_beneficial"] == 0


def test_route_recommendation_escalates_when_mineru_gains_on_complex_docs() -> None:
    docling = [
        {"doc_id": "d1", "complexity": 0.9, "table": {"f1": 0.4},
         "formula": {"block_hit_rate": 0.3}, "scan": True},
        {"doc_id": "d2", "complexity": 0.55, "table": {"f1": 0.5},
         "formula": {"block_hit_rate": 0.5}, "scan": False},
    ]
    mineru = [
        {"doc_id": "d1", "complexity": 0.9, "table": {"f1": 0.9},
         "formula": {"block_hit_rate": 0.9}, "scan": True},
        {"doc_id": "d2", "complexity": 0.55, "table": {"f1": 0.5},
         "formula": {"block_hit_rate": 0.5}, "scan": False},
    ]
    rec = _route_recommendation(docling, mineru)
    assert rec["recommended_route"] == "mineru_by_doc_complexity"
    assert rec["n_beneficial"] == 1
    # 阈值取 beneficial 文档复杂度下界
    assert rec["recommended_threshold"] == pytest.approx(0.9)


def test_manifest_roundtrip(corpus) -> None:
    dir_, truths = corpus
    from evals.parse_eval.schema import load_manifest

    loaded = load_manifest(os.path.join(dir_, "manifest.jsonl"))
    assert len(loaded) == 4
    ids = {t.doc_id for t in loaded}
    assert {"fin_report", "contract_scan", "statements", "whitepaper"} <= ids
    wp = next(t for t in loaded if t.doc_id == "whitepaper")
    assert wp.formulas
    assert wp.tables[0].header_rows == 1
    st = next(t for t in loaded if t.doc_id == "statements")
    assert st.tables[0].header_rows == 2
    assert st.tables[0].spans