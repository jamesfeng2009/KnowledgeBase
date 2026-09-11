"""Docling vs MinerU 解析消融 — 用同一批评测集跑两种引擎，产出四维对照指标。

运行：cd backend && python -m evals.parse_ablation
      可选 --corpus <目录>（含 manifest.jsonl） --output <路径> --ids a,b,c

引擎调度：
    - docling        ：Docling 全路径（DoclingParser，含其扫描件内部 OCR 分支）；
    - docling+mineru ：扫描件强制走 MinerU 子进程路径，Office/HTML 仍走 Docling。
    两种引擎都不可用时，退化为 identity_selfcheck（用真值文本代跑解析输出），
    以保证"指标 + 管线"在零重依赖环境下可端到端验证（输出中用 availability 标注）。

产出：results/parse_ablation.json（嵌套 JSON，与既有消融存档约定一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 允许从 repo 根直接运行 `python evals/parse_ablation.py` 或 `python -m evals.parse_ablation`
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.parse_eval.parse_metrics import (  # noqa: E402
    char_error_rate,
    formula_metrics,
    table_structure_pr,
    text_extraction_pr,
)
from evals.parse_eval.schema import ParseTruth, load_manifest  # noqa: E402
from evals.parse_eval.smoke_corpus import generate as generate_corpus  # noqa: E402

# 路由决策（P1.5）：DOC_PARSER_ROUTE 依据消融真实指标标定
from app.document.parse_router import (  # noqa: E402
    complexity_from_html,
    compute_complexity,
)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DEFAULT_CORPUS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "eval_datasets", "parse_corpus"
)


# ======================================================================
# 引擎后端 — 统一返回 (html, availability, engine)
# ======================================================================

def _docling_available() -> bool:
    try:
        from app.document.docling_parser import DoclingParser

        return bool(DoclingParser.is_available())
    except Exception:
        return False


def _mineru_configured() -> bool:
    try:
        from app.config import get_settings

        s = get_settings()
        return bool((getattr(s, "MINERU_ENABLED", False))
                    and (getattr(s, "MINERU_PYTHON", "") or "").strip())
    except Exception:
        return False


async def _run_docling(file_path: str) -> str:
    """Docling 全路径。不可用时抛 RuntimeError。"""
    if not _docling_available():
        raise RuntimeError("docling_unavailable")
    from app.document.docling_parser import DoclingParser

    parser = DoclingParser()
    html = await parser.parse(file_path)
    return html or ""


async def _run_docling_plus_mineru(file_path: str, truth: ParseTruth) -> str:
    """扫描件强制 MinerU，其余走 Docling。"""
    if truth.scan:
        if not _mineru_configured():
            raise RuntimeError("mineru_unconfigured")
        if not _docling_available():
            raise RuntimeError("docling_unavailable")
        from app.document.docling_parser import DoclingParser

        parser = DoclingParser()
        return await parser._parse_with_mineru(file_path)  # noqa: SLF001
    if not _docling_available():
        raise RuntimeError("docling_unavailable")
    from app.document.docling_parser import DoclingParser

    parser = DoclingParser()
    html = await parser.parse(file_path)
    return html or ""


def _identity_selfcheck(file_path: str, truth: ParseTruth) -> str:
    """无真实引擎时的链路自检（positive control）：用真值代跑解析输出，
    校验四个指标在"理想还原"下应接近饱和。

    HTML 回读源码（保留 <table>），PDF 用参考文本；再追加真值公式使公式指标
    得到正样本（HTML 内联公式用 ASCII，与真值 LaTeX 不同，需显式喂真值）。
    """
    if truth.kind == "html":
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                base = fh.read()
        except OSError:
            base = truth.text
    else:
        base = truth.text
    if truth.formulas:
        base += "\n" + " ".join(truth.formulas)
    return base


ENGINES: dict[str, object] = {
    "docling": _run_docling,
    "docling+mineru": _run_docling_plus_mineru,
}


# ======================================================================
# 单文档评测 + 聚合
# ======================================================================

def _eval_doc(html: str, truth: ParseTruth) -> dict:
    m: dict = {"doc_id": truth.doc_id, "kind": truth.kind, "scan": truth.scan}
    m["complexity"] = compute_complexity(complexity_from_html(html, text_layer_ratio=1.0))
    if truth.formulas:
        m["formula"] = formula_metrics(html, truth.formulas)
    else:
        m["formula"] = None
    if truth.tables:
        m["table"] = table_structure_pr(html, truth.tables[0])
    else:
        m["table"] = None
    if truth.scan:
        m["cer"] = char_error_rate(html, truth.text)
    else:
        m["text"] = text_extraction_pr(html, truth.text)
    return m


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _aggregate(doc_metrics: list[dict]) -> dict:
    a: dict = {"n_docs": len(doc_metrics)}
    texts = [d["text"]["f1"] for d in doc_metrics if d.get("text")]
    if texts:
        _prec = [d["text"]["precision"] for d in doc_metrics if d.get("text")]
        _rec = [d["text"]["recall"] for d in doc_metrics if d.get("text")]
        a["text_extraction"] = {
            "n": len(texts),
            "avg_f1": _avg(texts),
            "avg_precision": _avg(_prec),
            "avg_recall": _avg(_rec),
        }
    tabs = [d["table"]["f1"] for d in doc_metrics if d.get("table")]
    if tabs:
        _hdr = [
            d["table"]["header_recall"] for d in doc_metrics if d.get("table")
        ]
        a["table_restoration"] = {
            "n": len(tabs),
            "avg_f1": _avg(tabs),
            "avg_header_recall": _avg(_hdr),
            "span_recovered_docs": sum(
                1 for d in doc_metrics if d.get("table") and d["table"]["span_recovered"]
            ),
        }
    forms = [d["formula"]["block_hit_rate"] for d in doc_metrics if d.get("formula")]
    if forms:
        _sim = [
            d["formula"]["avg_string_sim"] for d in doc_metrics if d.get("formula")
        ]
        a["formula"] = {
            "n": len(forms),
            "block_hit_rate": _avg(forms),
            "avg_string_sim": _avg(_sim),
        }
    cers = [d["cer"] for d in doc_metrics if "cer" in d]
    if cers:
        a["ocr"] = {"n": len(cers), "avg_cer": _avg(cers)}
    return a


async def _run_variant(
    engine_key: str,
    truths: list[ParseTruth],
    corpus_dir: str,
    *,
    force_selfcheck: bool = False,
) -> tuple[dict, dict, list[dict]]:
    """跑单个引擎变体，返回 (聚合, availability 元信息, 文档级指标)。

    force_selfcheck=True 时跳过全部真实引擎，直接用真值回退自检——用于 CI / 快速
    复现，产出确定且不依赖模型下载；结果以 identity_selfcheck 标注。
    """
    fn = ENGINES[engine_key]
    per_doc: list[dict] = []
    used_engines: set[str] = set()
    identity_fallback = False
    for truth in truths:
        src = os.path.join(corpus_dir, truth.source)
        if not os.path.exists(src):
            raise FileNotFoundError(f"语料缺失：{src}（需先 python -m evals.parse_eval.smoke_corpus）")
        try:
            if force_selfcheck:
                raise RuntimeError("selfcheck_forced")
            if engine_key == "docling+mineru":
                html = await fn(src, truth)
            else:
                html = await fn(src)
            used_engines.add("docling")
            if engine_key == "docling+mineru" and truth.scan:
                used_engines.add("mineru")
        except RuntimeError:
            # 引擎不可用 / 强制自检 → 用真值回退，保证链路口径可跑
            identity_fallback = True
            used_engines.add("identity_selfcheck")
            html = _identity_selfcheck(src, truth)
        per_doc.append(_eval_doc(html, truth))

    agg = _aggregate(per_doc)
    availability = {
        "engines": sorted(used_engines),
        "identity_fallback": identity_fallback,
        "note": (
            "当前环境未提供真实 Docling/MinerU（或 --selfcheck 强制），指标来自真值"
            "回退自检，仅验证链路。"
            if identity_fallback else
            "真实引擎已接入。"
        ),
    }
    return agg, availability, per_doc


# ======================================================================
# 路由决策（P1.5）— 依据消融指标标定 DOC_PARSER_ROUTE
# ======================================================================

def _doc_quality(d: dict) -> float:
    """单文档综合质量：表格 F1 / 公式命中 / (1-CER) 的均值（可及维度）。"""
    parts: list[float] = []
    if d.get("table"):
        parts.append(d["table"]["f1"])
    if d.get("formula"):
        parts.append(d["formula"]["block_hit_rate"])
    if "cer" in d:
        parts.append(1.0 - d["cer"])
    return sum(parts) / len(parts) if parts else 0.0


def _route_recommendation(
    docs_docling: list[dict], docs_mineru: list[dict], default_threshold: float = 0.6
) -> dict:
    """基于单文档 Docling vs MinerU 质量差，给出 DOC_PARSER_ROUTE 建议。

    判定规则（务实、可审计）：若"非扫描或扫描"文档中，MinerU 质量相比 Docling
    提升 >0.05 且该文档复杂度 ≥0.4 的占比 ≥50%，且存在复杂度 ≥0.6 的文档，则
    建议按复杂度路由（mineru_by_doc_complexity），升级阈值取此类文档复杂度下界；
    否则维持 Docling 默认条件路由。
    """
    by_id = {d["doc_id"]: d for d in docs_mineru}
    gains: list[dict] = []
    for d in docs_docling:
        m = by_id.get(d["doc_id"])
        if not m:
            continue
        gains.append(
            {
                "doc_id": d["doc_id"],
                "complexity": d.get("complexity", 0.0),
                "docling_q": _doc_quality(d),
                "mineru_q": _doc_quality(m),
                "delta": _doc_quality(m) - _doc_quality(d),
            }
        )
    n = len(gains)
    beneficial = [
        g for g in gains
        if g["delta"] > 0.05 and g["complexity"] >= 0.4
    ]
    high_complex = [g for g in gains if g["complexity"] >= 0.6]

    if n and len(beneficial) / n >= 0.5 and high_complex:
        route = "mineru_by_doc_complexity"
        rec_threshold = min(g["complexity"] for g in beneficial)
        rationale = (
            "评测集中扫描/图片密集文档在 MinerU 下质量增益显著（delta>0.05）且占比过半，"
            "建议按复杂度路由升级；升级阈值取此类文档复杂度下界。"
        )
    else:
        route = "docling_default_conditional_mineru"
        rec_threshold = default_threshold
        rationale = (
            "未观测到 MinerU 在复杂文档上的显著质量增益（当前为自检/无真实 MinerU 数据），"
            "维持 Docling 默认条件路由。"
        )

    return {
        "recommended_route": route,
        "recommended_threshold": rec_threshold,
        "n_docs": n,
        "n_beneficial": len(beneficial),
        "n_high_complexity": len(high_complex),
        "rationale": rationale,
        "per_doc_gains": gains,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Docling vs MinerU 解析消融")
    parser.add_argument("--corpus", default=DEFAULT_CORPUS, help="语料目录（含 manifest.jsonl）")
    parser.add_argument("--output", default="", help="结果 JSON 路径")
    parser.add_argument("--regen", action="store_true", help="重新生成合成语料后再跑")
    parser.add_argument("--docling-only", action="store_true", help="只跑 docling 变体")
    parser.add_argument("--selfcheck", action="store_true",
                        help="跳过真实引擎，用真值自检（CI/快速复现，确定且不下载模型）")
    args = parser.parse_args()

    corpus_dir = os.path.abspath(args.corpus)
    manifest = os.path.join(corpus_dir, "manifest.jsonl")
    if args.regen or not os.path.exists(manifest):
        generate_corpus(corpus_dir)
    truths = load_manifest(manifest)

    variants = ["docling"] if args.docling_only else list(ENGINES.keys())
    per_docs: dict[str, list[dict]] = {}
    report: dict = {
        "corpus_dir": corpus_dir,
        "n_docs": len(truths),
        "selfcheck": args.selfcheck,
        "variants": {},
    }
    for v in variants:
        agg, availability, per_doc = await _run_variant(
            v, truths, corpus_dir, force_selfcheck=args.selfcheck
        )
        per_docs[v] = per_doc
        report["variants"][v] = {"availability": availability, **agg}

    # P1.5：依据消融指标给出 DOC_PARSER_ROUTE 建议
    if "docling" in per_docs and "docling+mineru" in per_docs:
        report["route_recommendation"] = _route_recommendation(
            per_docs["docling"], per_docs["docling+mineru"]
        )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = args.output or os.path.join(RESULTS_DIR, "parse_ablation.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())