"""
文档解析器工厂 — 单一职责：按文档类型分发到对应的解析器。

遵循开闭原则：新增文档类型只需在 _PARSERS 注册表中添加映射，
无需修改 get_parser 分支逻辑。
"""

from __future__ import annotations

from functools import lru_cache

from app.document.base import DocumentParser
from app.utils.logger import get_logger

log = get_logger(__name__)

# 文档类型 → 解析器类（延迟实例化）
_PARSER_CLASSES: dict[str, type[DocumentParser]] = {}


def _register_parsers() -> None:
    """注册所有文档解析器 — 延迟导入避免循环依赖。"""
    from app.document.docx_parser import DOCXParser
    from app.document.pdf_parser import PDFParser
    from app.document.pptx_parser import PPTXParser

    _PARSER_CLASSES["pdf"] = PDFParser
    _PARSER_CLASSES["pptx"] = PPTXParser
    _PARSER_CLASSES["ppt"] = PPTXParser  # 别名
    _PARSER_CLASSES["docx"] = DOCXParser


@lru_cache(maxsize=1)
def _get_parser_classes() -> dict[str, type[DocumentParser]]:
    """获取解析器注册表（单例初始化）。"""
    if not _PARSER_CLASSES:
        _register_parsers()
    return _PARSER_CLASSES


def get_parser(doc_type: str) -> DocumentParser | None:
    """根据文档类型获取对应的解析器实例。

    Args:
        doc_type: 文档类型（pdf / pptx / ppt 等）。

    Returns:
        对应的 DocumentParser 实例。不支持该类型时返回 None，
        由调用方降级为原有解析逻辑。
    """
    parsers = _get_parser_classes()
    parser_cls = parsers.get(doc_type.lower())
    if parser_cls is None:
        return None

    return parser_cls()
