"""
文档解析器工厂 — 单一职责：按文档类型分发到对应的解析器。

解析器优先级：
    1. Docling 统一解析器（如果已安装且启用）— 覆盖 PDF/DOCX/PPTX/XLSX/HTML/图片/音频
    2. 原有专用解析器（pymupdf/python-docx/python-pptx/openpyxl）— 作为降级路径
    3. None — 不支持的类型，由调用方处理

遵循开闭原则：新增文档类型只需在 _PARSERS 注册表中添加映射，
无需修改 get_parser 分支逻辑。
"""

from __future__ import annotations

from functools import lru_cache

from app.document.base import DocumentParser
from app.utils.logger import get_logger

log = get_logger(__name__)

# 文档类型 → 原有解析器类（延迟实例化，作为 Docling 降级路径）
_PARSER_CLASSES: dict[str, type[DocumentParser]] = {}


def _register_parsers() -> None:
    """注册所有原有文档解析器 — 延迟导入避免循环依赖。"""
    from app.document.docx_parser import DOCXParser
    from app.document.pdf_parser import PDFParser
    from app.document.pptx_parser import PPTXParser
    from app.document.xlsx_parser import XLSXParser

    _PARSER_CLASSES["pdf"] = PDFParser
    _PARSER_CLASSES["pptx"] = PPTXParser
    _PARSER_CLASSES["docx"] = DOCXParser
    _PARSER_CLASSES["xlsx"] = XLSXParser
    _PARSER_CLASSES["xls"] = XLSXParser  # 别名
    # 注意：不注册 "doc" 和 "ppt" 旧格式别名
    # python-docx / python-pptx 只支持 OOXML (.docx/.pptx)，
    # 旧格式由 _parse_document 路由层做兜底提示


@lru_cache(maxsize=1)
def _get_parser_classes() -> dict[str, type[DocumentParser]]:
    """获取原有解析器注册表（单例初始化）。"""
    if not _PARSER_CLASSES:
        _register_parsers()
    return _PARSER_CLASSES


def _is_docling_enabled() -> bool:
    """检查 Docling 是否启用（配置开关 + 包已安装）。"""
    try:
        from app.config import get_settings

        settings = get_settings()
        if not getattr(settings, "DOCLING_ENABLED", False):
            return False
    except Exception:
        return False

    try:
        from app.document.docling_parser import DoclingParser

        return DoclingParser.is_available()
    except ImportError:
        return False


def get_parser(doc_type: str) -> DocumentParser | None:
    """根据文档类型获取对应的解析器实例。

    优先返回 DoclingParser（如果已安装且启用且支持该类型），
    降级返回原有专用解析器。

    Args:
        doc_type: 文档类型（pdf / pptx / docx / xlsx 等）。

    Returns:
        对应的 DocumentParser 实例。不支持该类型时返回 None，
        由调用方降级为原有解析逻辑。
    """
    doc_type_lower = doc_type.lower()

    # 1. 优先尝试 Docling 统一解析器
    if _is_docling_enabled():
        try:
            from app.document.docling_parser import DoclingParser

            if DoclingParser.is_supported(doc_type_lower):
                return DoclingParser()
        except ImportError:
            pass

    # 2. 降级到原有专用解析器
    parsers = _get_parser_classes()
    parser_cls = parsers.get(doc_type_lower)
    if parser_cls is None:
        return None

    return parser_cls()


def get_parser_with_fallback(doc_type: str) -> tuple[DocumentParser | None, str]:
    """获取解析器并返回解析器类型标识。

    用于 document_tasks 需要知道使用的是 Docling 还是原有解析器的场景。

    Args:
        doc_type: 文档类型。

    Returns:
        (parser_instance, parser_type) 二元组。
        parser_type 为 "docling" / "legacy" / "none"。
    """
    doc_type_lower = doc_type.lower()

    # 1. 优先 Docling
    if _is_docling_enabled():
        try:
            from app.document.docling_parser import DoclingParser

            if DoclingParser.is_supported(doc_type_lower):
                return DoclingParser(), "docling"
        except ImportError:
            pass

    # 2. 降级到原有解析器
    parsers = _get_parser_classes()
    parser_cls = parsers.get(doc_type_lower)
    if parser_cls is not None:
        return parser_cls(), "legacy"

    return None, "none"
