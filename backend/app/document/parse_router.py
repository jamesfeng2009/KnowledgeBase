"""解析路由决策 — DOC_PARSER_ROUTE 的纯函数实现，可单测、可被消融数据标定。

两条路由策略：
    - "docling_default_conditional_mineru"：Docling 主引擎；仅图片与纯扫描 PDF
      （无文本层）在 MinerU 可用时升级。等价于既有行为。
    - "mineru_by_doc_complexity"：compute_complexity() 达到阈值即升级 MinerU，
      供客户语料中扫描件/图片密集/公式表格重文档占比高时启用。

复杂度信号既可在解析前用廉价手段初估（text_layer_ratio / 图片数），也可在解析
后用 complexity_from_html() 从解析输出统计（表格/公式/图片密度），由消融报告
据此给出路由与阈值建议——"用数据落配置"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 图片类型 — 命中即强制走 OCR（MinerU）
IMAGE_TYPES: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "tiff", "bmp"}
)

#: 文档类型 → 复杂度基分（无结构信号的先验权重）
_KIND_BASE: dict[str, float] = {
    "pdf": 0.45,
    "docx": 0.25,
    "pptx": 0.25,
    "xlsx": 0.30,
    "html": 0.15,
    "htm": 0.15,
    "md": 0.10,
    "txt": 0.10,
}


@dataclass
class ParseSignals:
    """解析复杂度信号（0~1 标度，越大越复杂/越需要 MinerU）。"""

    doc_type: str = "pdf"
    text_layer_ratio: float = 1.0   # 0=纯扫描(无文本层) 1=全文本
    n_images: int = 0
    formula_markers: int = 0
    complex_table: bool = False     # 含双层表头/合并单元格等复杂表结构


def compute_complexity(s: ParseSignals) -> float:
    """把信号综合为 0~1 复杂度分。权重可解释、确定性：

    base（类型先验）  + 扫描程度 + 图片密度 + 公式 + 复杂表格标记。
    """
    kind = s.doc_type.lower()
    score = _KIND_BASE.get(kind, 0.40)
    # 纯扫描 PDF 文本层为 0 → 显著抬高复杂度
    score += (1.0 - max(0.0, min(1.0, s.text_layer_ratio))) * 0.30
    score += min(s.n_images / 20.0, 1.0) * 0.15
    score += min(s.formula_markers / 5.0, 1.0) * 0.05
    score += 0.05 if s.complex_table else 0.0
    return max(0.0, min(1.0, score))


def choose_engine(
    route: str,
    s: ParseSignals,
    *,
    mineru_available: bool,
    threshold: float = 0.6,
) -> str:
    """决策返回 "docling" | "mineru"。

    route 非法时回退默认策略，避免生产环境因拼写/脏配置直接报错。
    """
    if not mineru_available:
        return "docling"

    if route == "mineru_by_doc_complexity":
        # 复杂度达到阈值的文档交给 MinerU；图片无条件走 MinerU（OCR 强依赖路径）
        if s.doc_type.lower() in IMAGE_TYPES:
            return "mineru"
        return "mineru" if compute_complexity(s) >= threshold else "docling"

    # 默认：docling_default_conditional_mineru（也兜底所有未知 route）
    if s.doc_type.lower() in IMAGE_TYPES:
        return "mineru"
    if s.doc_type.lower() == "pdf" and s.text_layer_ratio < 0.05:
        return "mineru"
    return "docling"


# ======================================================================
# 解析后复杂度统计 — 供消融报告把 DOC_PARSER_ROUTE 落到数据
# ======================================================================

import re as _re  # noqa: E402

_TABLE_TAG_RE = _re.compile(r"<table[ >]", _re.IGNORECASE)


def complexity_from_html(html: str, text_layer_ratio: float = 1.0) -> ParseSignals:
    """从解析输出统计公式/图片/表格信号，反推该文档的复杂度信号。

    用于消融：同一篇文档在 Docling 与 MinerU 两种解析下都产出 HTML，据此判定
    "该文档复杂度是否达到应升级 MinerU 的阈值"，把路由决策建立在实际指标上。
    """
    n_tables = len(_TABLE_TAG_RE.findall(html))
    # 公式/Latex 标记：反斜杠序列、下标花括号等粗粒度启发式
    n_formula = len(_re.findall(r"\\[a-zA-Z]+|\{[^}]*\}_\{", html))
    n_images = len(_re.findall(r"<img|\[图片描述:|!\[", html))
    return ParseSignals(
        doc_type="pdf",
        text_layer_ratio=text_layer_ratio,
        n_images=n_images,
        formula_markers=n_formula,
        complex_table=n_tables >= 2,
    )