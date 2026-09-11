"""解析四维指标 — 全部为纯函数，可用单测直接校验。

1. text_extraction_pr  — 文本抽取查准率/召回率/F1（token 级，中文按字切分）
2. table_structure_pr  — 表格还原结构级 P·R（合并单元格/双层表头/单元格文本覆盖）
3. formula_metrics     — 公式 LaTeX 还原（块命中率 + 串相似度）
4. char_error_rate     — 扫描件 OCR 字符错误率 CER（Levenshtein）

设计约束：不引入任何重依赖（jieba/jieba/reportlab 均不依赖），直接用纯文本
与轻量 HTML 表格解析，保证单测快、真实语料可替换。
"""

from __future__ import annotations

import re
import unicodedata

from .schema import TableTruth


def _tokenize(text: str) -> list[str]:
    """tokenize — ASCII 词按空白/连字符切分，CJK 中日韩字逐字为 token。"""
    # 将 CJK 与 ASCII 边界用空格断开，保证中日韩字符单独成 token
    spaced = re.sub(r"([\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af])", r" \1 ", text)
    tokens = [
        t for t in re.split(r"[\s，。；：、（）《》〈〉“”‘’「」【】,—:;,.!?()\[\]{}/\\|+\-=*^<>]+", spaced)
        if t and not t.isspace()
    ]
    return tokens


def _detok(tokens: list[str]) -> str:
    return "".join(tokens)


def text_extraction_pr(pred: str, ref: str) -> dict[str, float]:
    """token 级文本抽取 P·R·F1。pred 为解析器输出，ref 为参考文本。"""
    if not ref:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    pt, rt = set(_tokenize(pred)), set(_tokenize(ref))
    if not rt:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    if not pt:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    hit = len(pt & rt)
    p = hit / len(pt)
    r = hit / len(rt)
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": p, "recall": r, "f1": f1}


# ---------------------------------------------------------------- 表格解析

_TABLE_RE = re.compile(r"<table.*?</table>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr.*?</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<(td|th)(.*?)>(.*?)</(?:td|th)>", re.IGNORECASE | re.DOTALL)


def _get_table_count(html: str) -> int:
    return len(_TABLE_RE.findall(html))


def _extract_tables(html: str) -> list[list[list[str]]]:
    """把解析器输出的 <table> 抽取为 行→单元格文本 结构（不追合并语义）。"""
    tables: list[list[list[str]]] = []
    for tmatch in _TABLE_RE.finditer(html):
        rows: list[list[str]] = []
        for rmatch in _TR_RE.finditer(tmatch.group(0)):
            cells = []
            for cmatch in _TD_RE.finditer(rmatch.group(0)):
                cells.append(re.sub(r"<[^>]+>", "", cmatch.group(3)).strip())
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def table_structure_pr(pred_html: str, truth: TableTruth) -> dict[str, float]:
    """表格还原结构级 P·R·F1。

    对齐口径（务实且可审计）：
      - 行覆盖 / 列覆盖：解析表格维度与真值维度的比例（矩形覆盖）;
      - 单元格文本召回：真值非空单元格在解析表格中出现的比例；
      - 表头还原：解析表中位于顶部一行（header_rows 范围内）与真值表头文本的匹配率。
    合并单元格（row_span/col_span）作为单独布尔：存在合并且解析维度正确时记为恢复。
    """
    if not truth.cells:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "span_recovered": False}

    tables = _extract_tables(pred_html)
    if not tables:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "span_recovered": False}

    # 取与真值维度最接近的解析表
    best = None
    best_gap = float("inf")
    for tb in tables:
        gap = abs(len(tb) - truth.rows) + abs((len(tb[0]) if tb else 0) - truth.cols)
        if gap < best_gap:
            best = tb
            best_gap = gap
    if best is None:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "span_recovered": False}

    true_tokens = set(t for row in truth.cells for t in row if t)
    pred_tokens = set(t for row in best for t in row if t)
    if not true_tokens:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "span_recovered": False}

    row_cover = min(len(best), truth.rows) / max(truth.rows, 1)
    # 物理抽取中 colspan 会让首行单元格数偏少，取最宽行宽度作为列宽估计
    pred_cols = max((len(r) for r in best), default=0)
    col_cover = min(pred_cols, truth.cols) / max(truth.cols, 1)
    recall = len(true_tokens & pred_tokens) / len(true_tokens)
    precision = len(true_tokens & pred_tokens) / len(pred_tokens) if pred_tokens else 0.0

    # 表头还原（双层表头 → 取 top header_rows 行）
    header_true = {
        t for r in truth.cells[: truth.header_rows] if r for t in r
        if _tokenize(t)
    }
    header_pred = {
        t for r in best[: truth.header_rows] if r for t in r
        if _tokenize(t)
    }
    header_recall = (
        len(header_true & header_pred) / len(header_true) if header_true else 1.0
    )

    span_recovered = bool(truth.spans) and len(best) == truth.rows and pred_cols == truth.cols

    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "header_recall": header_recall,
        "row_cover": row_cover,
        "col_cover": col_cover,
        "span_recovered": span_recovered,
    }


# ---------------------------------------------------------------- 公式

def _norm_formula(s: str) -> str:
    """规范化公式字符串 — 去空白、单双引号，仅用于串相似度比较。"""
    s = re.sub(r"\\\s+", "\\\\", s)
    return re.sub(r"\s+", "", s.replace("“", '"').replace("”", '"')).strip()


def formula_metrics(pred_text: str, expected_forms: list[str]) -> dict[str, float]:
    """公式 LaTeX 还原 — 块命中率 + 平均串相似度（字符级编辑距离）。"""
    if not expected_forms:
        return {"block_hit_rate": 1.0, "avg_string_sim": 1.0, "sim": 1.0}
    pred_norm = _norm_formula(pred_text)
    hits = 0
    sims = []
    for f in expected_forms:
        fn = _norm_formula(f)
        if not fn:
            hits += 1
            sims.append(1.0)
            continue
        if fn in pred_norm:
            hits += 1
            sims.append(1.0)
            continue
        sims.append(_edit_sim(fn, pred_norm))
    block_hit = hits / len(expected_forms)
    avg = sum(sims) / len(sims) if sims else 0.0
    return {"block_hit_rate": block_hit, "avg_string_sim": avg, "sim": avg}


def _edit_dist(a: str, b: str) -> int:
    """Levenshtein 编辑距离（DP，纯内存）。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,          # 删除
                cur[j - 1] + 1,       # 插入
                prev[j - 1] + (ca != cb),  # 替换
            )
        prev = cur
    return prev[-1]


def _edit_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 - (_edit_dist(a, b) / max(len(a), len(b)))


# ---------------------------------------------------------------- OCR CER

def _normalize_chars(text: str) -> list[str]:
    """OCR 归一化 — 规范化 Unicode，保留可见字符。"""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        if ch == "\n":
            continue
        if ch.isspace():
            ch = ""
        out.append(ch)
    return out


def char_error_rate(pred: str, ref: str) -> float:
    """扫描件 OCR 字符错误率 CER — 1-编辑距离/参考长度。"""
    pa, ra = _normalize_chars(pred), _normalize_chars(ref)
    if not ra:
        return 0.0 if not pa else 1.0
    ed = _edit_dist("".join(pa), "".join(ra))
    return min(1.0, ed / len(ra))