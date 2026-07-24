"""
文档解析质量指标库 — 单一职责：纯函数计算解析结果与标注答案的质量指标。

四大维度（参考 test.md「文档解析测试指标体系」）：
    1. 文本相似度（text_similarity）— 编辑距离 + 字符错误率 CER，目标 >95%
    2. 表格准确率（table_similarity）— 表格数量 + 单元格匹配率，目标 >90%
    3. 公式准确率（formula_similarity）— 符号匹配 + LaTeX 格式，目标 >85%
    4. 版面还原度（layout_similarity）— 标题/段落/列表结构识别，目标 >90%

所有函数均为纯函数（无副作用、无 IO），便于单元测试与独立复用。
参考实现源自 test.md 第六部分，已做规范化与版面还原维度补充。
"""

from __future__ import annotations

import re
from typing import Any

# ======================================================================
# 1. 文本相似度 — 编辑距离 / CER / 词级 Jaccard
# ======================================================================


def levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串的编辑距离（Levenshtein Distance）。

    将 s1 转换为 s2 所需的最少操作次数（插入、删除、替换）。
    """
    m, n = len(s1), len(s2)
    # 空间优化：仅保留上一行
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            if s1[i - 1] == s2[j - 1]:
                curr[j] = prev[j - 1]
            else:
                curr[j] = 1 + min(prev[j], curr[j - 1], prev[j - 1])
        prev = curr
    return prev[n]


def text_similarity(text1: str, text2: str) -> float:
    """字符级文本相似度（0-1，1 表示完全相同）。

    similarity = 1 - levenshtein / max(len1, len2)
    """
    if not text1 and not text2:
        return 1.0
    max_len = max(len(text1), len(text2))
    if max_len == 0:
        return 1.0
    distance = levenshtein_distance(text1, text2)
    return round(1 - distance / max_len, 4)


def char_error_rate(standard: str, parsed: str) -> float:
    """字符错误率 CER（0-1，越低越好）。

    CER = 编辑距离 / 标准文本长度
    """
    if not standard:
        return 0.0 if not parsed else 1.0
    distance = levenshtein_distance(standard, parsed)
    return round(distance / len(standard), 4)


def _tokenize(text: str) -> list[str]:
    """简单分词 — 按标点和空格切分。"""
    return re.findall(r"\w+|[^\w\s]", text)


def token_similarity(text1: str, text2: str) -> float:
    """词级 Jaccard 相似度（0-1）。"""
    tokens1 = set(_tokenize(text1))
    tokens2 = set(_tokenize(text2))
    if not tokens1 and not tokens2:
        return 1.0
    union = tokens1 | tokens2
    if not union:
        return 1.0
    return round(len(tokens1 & tokens2) / len(union), 4)


# ======================================================================
# 2. 表格准确率 — 数量匹配 + 单元格匹配
# ======================================================================

# Markdown 表格正则：| col | col | 换行 |---|---| 换行若干数据行
_MARKDOWN_TABLE_PATTERN = re.compile(
    r"\|[^\n]+\|[\n\r]+\|[-\s|:]+\|[\n\r]+(?:\|[^\n]+\|[\n\r]+)+",
    re.MULTILINE,
)


def extract_tables(text: str) -> list[str]:
    """从文本中提取 Markdown 表格。

    识别 ``| 列 | 列 |`` 格式并带分隔线的表格块。
    """
    if not text:
        return []
    tables = _MARKDOWN_TABLE_PATTERN.findall(text)
    # 补充：连续多行含 | 或 \t 的也视为表格（至少 2 行）
    current: list[str] = []
    for line in text.split("\n"):
        if "|" in line or "\t" in line:
            current.append(line)
        else:
            if len(current) >= 2:
                tables.append("\n".join(current))
            current = []
    if len(current) >= 2:
        tables.append("\n".join(current))
    return tables


def parse_table_cells(table_text: str) -> list[str]:
    """解析表格单元格（展平为一维列表）。"""
    cells: list[str] = []
    for line in table_text.strip().split("\n"):
        if "|" not in line:
            continue
        row_cells = [c.strip() for c in line.split("|")]
        # 过滤空单元格和分隔线（如 ---）
        row_cells = [c for c in row_cells if c and not re.match(r"^[-:\s]+$", c)]
        cells.extend(row_cells)
    return cells


def table_similarity(standard_text: str, parsed_text: str) -> dict[str, Any]:
    """计算表格相似度。

    Returns:
        ``{table_count, expected_table_count, table_count_score,
        cell_match_score, overall_score}``

    overall = (数量得分 + 单元格匹配率) / 2
    """
    standard_tables = extract_tables(standard_text)
    parsed_tables = extract_tables(parsed_text)

    # 1. 表格数量匹配度
    if len(standard_tables) == 0:
        table_count_score = 1.0 if len(parsed_tables) == 0 else 0.0
    else:
        table_count_score = min(len(parsed_tables), len(standard_tables)) / len(
            standard_tables
        )

    # 2. 单元格内容匹配度
    if len(standard_tables) == 0:
        cell_match_score = 1.0
    else:
        standard_cells: list[str] = []
        for t in standard_tables:
            standard_cells.extend(parse_table_cells(t))
        parsed_cells: list[str] = []
        for t in parsed_tables:
            parsed_cells.extend(parse_table_cells(t))

        matched = 0
        for std_cell in standard_cells:
            for parsed_cell in parsed_cells:
                if std_cell == parsed_cell or text_similarity(std_cell, parsed_cell) > 0.9:
                    matched += 1
                    break
        cell_match_score = matched / len(standard_cells) if standard_cells else 1.0

    overall = round((table_count_score + cell_match_score) / 2, 4)
    return {
        "table_count": len(parsed_tables),
        "expected_table_count": len(standard_tables),
        "table_count_score": round(table_count_score, 4),
        "cell_match_score": round(cell_match_score, 4),
        "overall_score": overall,
    }


# ======================================================================
# 3. 公式准确率 — LaTeX/数学符号提取 + 归一化匹配
# ======================================================================

# LaTeX 公式：$...$
_LATEX_PATTERN = re.compile(r"\$([^\$]+)\$")
# 数学表达式：A = B² 形式
_MATH_PATTERN = re.compile(
    r"[A-Za-z]+\s*[=≈≠<>≤≥]\s*[A-Za-z0-9+\-*/^()²³√]+"
)
_SPECIAL_SYMBOLS = [
    "∑", "∫", "∂", "π", "√", "∞", "±", "×", "÷", "≈", "≠", "≤", "≥",
    "²", "³", "α", "β", "γ", "θ", "λ", "μ", "Ω",
]


def extract_formulas(text: str) -> list[str]:
    """从文本中提取公式（LaTeX / 内联数学表达式 / 特殊符号上下文）。"""
    if not text:
        return []
    formulas: list[str] = []
    formulas.extend(_LATEX_PATTERN.findall(text))
    formulas.extend(_MATH_PATTERN.findall(text))
    for symbol in _SPECIAL_SYMBOLS:
        if symbol in text:
            contexts = re.findall(rf".{{0,20}}{re.escape(symbol)}.{{0,20}}", text)
            formulas.extend(contexts)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for f in formulas:
        if f and f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def normalize_formula(formula: str) -> str:
    """标准化公式 — 去空格、统一符号，便于比较。"""
    formula = formula.replace(" ", "")
    formula = formula.replace("*", "×")
    formula = formula.replace("/", "÷")
    formula = formula.replace(">=", "≥")
    formula = formula.replace("<=", "≤")
    formula = formula.replace("!=", "≠")
    formula = formula.replace("~=", "≈")
    return formula


def formula_similarity(standard_text: str, parsed_text: str) -> dict[str, Any]:
    """计算公式相似度。

    Returns:
        ``{formula_count, expected_formula_count, formula_count_score,
        formula_match_score, overall_score}``
    """
    standard_formulas = extract_formulas(standard_text)
    parsed_formulas = extract_formulas(parsed_text)

    if len(standard_formulas) == 0:
        if len(parsed_formulas) == 0:
            return {
                "formula_count": 0,
                "expected_formula_count": 0,
                "formula_count_score": 1.0,
                "formula_match_score": 1.0,
                "overall_score": 1.0,
                "details": "无公式",
            }
        return {
            "formula_count": len(parsed_formulas),
            "expected_formula_count": 0,
            "formula_count_score": 0.0,
            "formula_match_score": 0.0,
            "overall_score": 0.0,
            "details": "误识别公式",
        }

    # 1. 公式数量得分
    formula_count_score = min(len(parsed_formulas), len(standard_formulas)) / len(
        standard_formulas
    )

    # 2. 公式匹配得分（归一化后文本相似度 > 0.8 视为匹配）
    matched_count = 0
    match_details: list[dict] = []
    for std_formula in standard_formulas:
        std_norm = normalize_formula(std_formula)
        best_match = 0.0
        best_parsed = ""
        for parsed_formula in parsed_formulas:
            parsed_norm = normalize_formula(parsed_formula)
            similarity = text_similarity(std_norm, parsed_norm)
            if similarity > best_match:
                best_match = similarity
                best_parsed = parsed_formula
        if best_match > 0.8:
            matched_count += 1
            match_details.append({
                "standard": std_formula,
                "parsed": best_parsed,
                "similarity": round(best_match, 4),
            })

    formula_match_score = matched_count / len(standard_formulas)
    overall = round((formula_count_score + formula_match_score) / 2, 4)
    return {
        "formula_count": len(parsed_formulas),
        "expected_formula_count": len(standard_formulas),
        "formula_count_score": round(formula_count_score, 4),
        "formula_match_score": round(formula_match_score, 4),
        "overall_score": overall,
        "match_details": match_details,
    }


# ======================================================================
# 4. 版面还原度 — 标题/段落/列表结构识别
# ======================================================================

# HTML 标题标签
_H_PATTERN = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
# HTML 列表项
_LI_PATTERN = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
# HTML 段落
_P_PATTERN = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
# Markdown 标题
_MD_H_PATTERN = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _strip_tags(html: str) -> str:
    """去除 HTML 标签，保留纯文本。"""
    return re.sub(r"<[^>]+>", "", html)


def extract_layout_features(text: str) -> dict[str, int]:
    """提取版面结构特征 — 标题/段落/列表数量。

    同时识别 HTML（Docling 输出）与 Markdown 两种格式。
    """
    headings = len(_H_PATTERN.findall(text)) + len(_MD_H_PATTERN.findall(text))
    paragraphs = len(_P_PATTERN.findall(text))
    list_items = len(_LI_PATTERN.findall(text))

    # 若无 HTML 标签，按行估算段落（非空行块）
    if paragraphs == 0 and "<" not in text:
        paragraphs = len([ln for ln in text.split("\n") if ln.strip()])

    return {
        "headings": headings,
        "paragraphs": paragraphs,
        "list_items": list_items,
    }


def layout_similarity(standard_text: str, parsed_text: str) -> dict[str, Any]:
    """计算版面还原度。

    比较标题/段落/列表三类结构元素的数量匹配度。
    overall = 三类结构匹配度的均值
    """
    std_features = extract_layout_features(standard_text)
    parsed_features = extract_layout_features(parsed_text)

    scores: list[float] = []
    for key in ("headings", "paragraphs", "list_items"):
        std_count = std_features[key]
        parsed_count = parsed_features[key]
        if std_count == 0:
            # 标准无该结构：解析也无则满分，有则扣分（误识别）
            scores.append(1.0 if parsed_count == 0 else 0.5)
        else:
            scores.append(min(parsed_count, std_count) / std_count)

    overall = round(sum(scores) / len(scores), 4) if scores else 0.0
    return {
        "standard_features": std_features,
        "parsed_features": parsed_features,
        "heading_score": round(scores[0], 4),
        "paragraph_score": round(scores[1], 4),
        "list_score": round(scores[2], 4),
        "overall_score": overall,
    }


# ======================================================================
# 5. 综合指标 — 四维度聚合
# ======================================================================

# 综合权重（参考 test.md 目标值的重要度，文本为主）
_WEIGHTS = {
    "text": 0.40,
    "table": 0.20,
    "formula": 0.20,
    "layout": 0.20,
}


def compute_parse_metrics(standard_text: str, parsed_text: str) -> dict[str, Any]:
    """计算单条文档解析的综合质量指标。

    Args:
        standard_text: 人工标注的标准答案文本（ground truth）。
        parsed_text: 解析器实际输出的文本（Docling HTML 或纯文本）。

    Returns:
        综合指标 dict::

            {
                "text_similarity": float,      # 文本相似度（0-1）
                "cer": float,                  # 字符错误率（0-1，越低越好）
                "token_similarity": float,     # 词级 Jaccard 相似度
                "table": {...},                # 表格准确率
                "formula": {...},              # 公式准确率
                "layout": {...},               # 版面还原度
                "overall_score": float,        # 综合得分（0-1）
            }
    """
    text_sim = text_similarity(standard_text, parsed_text)
    cer = char_error_rate(standard_text, parsed_text)
    token_sim = token_similarity(standard_text, parsed_text)
    table = table_similarity(standard_text, parsed_text)
    formula = formula_similarity(standard_text, parsed_text)
    layout = layout_similarity(standard_text, parsed_text)

    overall = round(
        _WEIGHTS["text"] * text_sim
        + _WEIGHTS["table"] * table["overall_score"]
        + _WEIGHTS["formula"] * formula["overall_score"]
        + _WEIGHTS["layout"] * layout["overall_score"],
        4,
    )

    return {
        "text_similarity": text_sim,
        "cer": cer,
        "token_similarity": token_sim,
        "table": table,
        "formula": formula,
        "layout": layout,
        "overall_score": overall,
    }
