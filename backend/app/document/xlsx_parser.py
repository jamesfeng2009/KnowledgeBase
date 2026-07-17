"""
XLSX 电子表格解析器 — 单一职责：将 Excel 解析为增强文本（sheet 标题 + HTML 表格）。

使用 openpyxl 读取 .xlsx / .xls 文件：
    - 每个 sheet 输出为 <h2>sheet 名</h2> 标题 + HTML <table>；
    - 第一行视为表头（<th>），其余为数据行（<td>）；
    - 空行自动跳过，纯空白 sheet 只输出标题。

遵循优雅降级：
    - openpyxl 未安装 → 返回空字符串，调用方降级；
    - 单个 sheet 解析异常 → 跳过该 sheet，继续处理其余 sheet；
    - 配置开关可控制表格提取、行数和 sheet 数量上限。
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.document.base import DocumentParser, ParsedSection
from app.utils.logger import get_logger

log = get_logger(__name__)


class XLSXParser(DocumentParser):
    """XLSX 解析器 — openpyxl 读取，每 sheet 转 HTML 表格。"""

    async def parse(self, file_path: str) -> str:
        """解析 XLSX 文档，返回增强文本。

        Args:
            file_path: XLSX 文件路径。

        Returns:
            增强文本（<h2>sheet 名</h2> + HTML 表格）。
            openpyxl 未安装或解析失败时返回空字符串。
        """
        try:
            import openpyxl
        except ImportError:
            log.warning("xlsx.parser_skipped", reason="openpyxl not installed")
            return ""

        try:
            workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        except Exception as exc:
            log.warning("xlsx.open_failed", file_path=file_path, error=str(exc))
            return ""

        settings = get_settings()
        table_enabled = getattr(settings, "XLSX_TABLE_EXTRACTION_ENABLED", True)
        max_rows = getattr(settings, "XLSX_MAX_ROWS_PER_SHEET", 500)
        max_sheets = getattr(settings, "XLSX_MAX_SHEETS", 20)

        sections: list[ParsedSection] = []
        sheet_count = 0

        for sheet_name in workbook.sheetnames:
            if sheet_count >= max_sheets:
                log.info("xlsx.max_sheets_reached", limit=max_sheets)
                break

            sheet_count += 1
            seq = sheet_count

            # 输出 sheet 标题（作为分块锚点）
            sections.append(
                ParsedSection(
                    kind="text",
                    content=f"<h2>{self._escape_html(sheet_name)}</h2>",
                    page=seq,
                )
            )

            if not table_enabled:
                continue

            # 提取 sheet 数据为 HTML 表格
            try:
                worksheet = workbook[sheet_name]
                html = self._extract_sheet_html(worksheet, max_rows)
                if html:
                    sections.append(
                        ParsedSection(kind="table", content=html, page=seq)
                    )
            except Exception as exc:
                log.debug("xlsx.sheet_extract_failed", sheet=sheet_name, error=str(exc))

        workbook.close()

        log.info(
            "xlsx.parsed",
            file_path=file_path,
            sheets=sheet_count,
            sections=len(sections),
            tables=sum(1 for s in sections if s.kind == "table"),
        )
        return self.sections_to_text(sections)

    def _extract_sheet_html(self, worksheet: Any, max_rows: int) -> str:
        """将 worksheet 转为 HTML <table>。

        Args:
            worksheet: openpyxl Worksheet 对象。
            max_rows: 最大提取行数。

        Returns:
            HTML <table> 标签字符串。空 sheet 返回空字符串。
        """
        rows: list[list[str | None]] = []
        row_count = 0

        for row in worksheet.iter_rows(values_only=True):
            if row_count >= max_rows:
                log.info("xlsx.max_rows_reached", limit=max_rows)
                break
            row_count += 1

            # 跳过全空行
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue

            # 将所有单元格转为字符串
            cells = [str(cell).strip() if cell is not None else "" for cell in row]
            rows.append(cells)

        if not rows:
            return ""

        return self._rows_to_html(rows)

    @staticmethod
    def _rows_to_html(rows: list[list[str | None]]) -> str:
        """将二维数据转为 HTML <table> 标签。

        第一行视为表头（<th>），其余为数据行（<td>）。
        """
        if not rows:
            return ""

        lines: list[str] = ["<table>"]

        for i, row in enumerate(rows):
            lines.append("<tr>")
            tag = "th" if i == 0 else "td"
            for cell in row:
                cell_text = (cell or "").strip()
                cell_text = XLSXParser._escape_html(cell_text)
                lines.append(f"<{tag}>{cell_text}</{tag}>")
            lines.append("</tr>")

        lines.append("</table>")
        return "\n".join(lines)

    @staticmethod
    def _escape_html(text: str) -> str:
        """转义 HTML 特殊字符。"""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
