"""
文档解析模块 — 统一的文档解析入口。

支持 PDF（表格 + 图片 + 文本）和 PPTX（文本 + 表格 + 图片）格式，
返回增强文本（纯文本 + HTML 表格 + [图片描述]）。

遵循开闭原则：新增文档类型只需新增 Parser 并在 factory 注册。
"""

from app.document.base import DocumentParser, ParsedSection
from app.document.factory import get_parser

__all__ = [
    "DocumentParser",
    "ParsedSection",
    "get_parser",
]
