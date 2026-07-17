"""
文档解析器抽象层 — 单一职责：将原始文档解析为增强文本。

增强文本格式（统一返回 str）：
    纯文本 + HTML <table> 标签 + [图片描述: ...] 内联标注

遵循开闭原则：新增文档类型只需新增 Parser 并在 factory 注册，
无需修改既有解析器或 document_tasks 主流程。
遵循优雅降级：第三方库未安装时返回 content_text，不阻断主流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ParsedSection:
    """解析后的文档片段 — 带类型标签，用于交错排列。

    Attributes:
        kind: 片段类型（text / table / image_desc）。
        content: 片段文本内容。
            - text: 纯文本
            - table: HTML <table> 标签
            - image_desc: "[图片描述: ...]" 内联标注
        page: 页码或 slide 编号（用于排序）。
    """

    kind: str
    content: str
    page: int = 0


class DocumentParser(ABC):
    """文档解析器统一接口 — 所有格式解析器继承本抽象。"""

    @abstractmethod
    async def parse(self, file_path: str) -> str:
        """解析文档，返回增强文本。

        Args:
            file_path: 文档文件路径。

        Returns:
            增强文本字符串（纯文本 + HTML 表格 + 图片描述）。
            解析失败时返回空字符串，由调用方降级处理。
        """
        raise NotImplementedError

    @staticmethod
    def sections_to_text(sections: list[ParsedSection]) -> str:
        """将 ParsedSection 列表合并为增强文本字符串。

        按页码排序，各片段用空行分隔。表格和图片描述保持原样，
        纯文本片段直接拼接。
        """
        if not sections:
            return ""

        sorted_sections = sorted(sections, key=lambda s: s.page)
        parts: list[str] = []
        for sec in sorted_sections:
            text = sec.content.strip()
            if not text:
                continue
            parts.append(text)

        return "\n\n".join(parts)
