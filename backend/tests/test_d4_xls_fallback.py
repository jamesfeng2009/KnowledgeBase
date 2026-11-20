"""D4 闸门 — .xls 上传可进、解析可出（sniff OLE2 子类型 + xlrd 降级依赖）。

红基线双缺陷（2026-11-19 实测取证）：
1. sniff._sniff_ole 用 ASCII b"Workbook" 在前 1024 字节搜索，而 OLE2 目录流名
   为 UTF-16LE 且目录扇区常在文件尾部 → 真实 .xls 恒判 ole2 被上传拒绝；
2. openpyxl 不支持 .xls → pandas.read_excel 兜底需 xlrd，requirements 未声明、
   venv 未安装 → 即使进入解析也必败返空。

真实边界：真实 xlwt 生成的 BIFF8 .xls 样例（test_documents/xls/）、
真实 sniff 纯函数、真实 XLSXParser.parse。
"""

from __future__ import annotations

import glob
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_XLS = _REPO_ROOT / "test_documents" / "xls" / "01_员工花名册.xls"
_REAL_XLSX = sorted(glob.glob(str(_REPO_ROOT / "test_documents" / "xlsx" / "*.xlsx")))

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _u16(name: str) -> bytes:
    return name.encode("utf-16-le")


class TestD4SniffOleSubtype:
    def test_real_xls_accepted_as_xls(self):
        from app.document.sniff import resolve_upload_doc_type

        data = _REAL_XLS.read_bytes()
        doc_type, reject = resolve_upload_doc_type(data, "xls")
        assert reject is None
        assert doc_type == "xls"

    def test_ole2_word_still_rejected(self):
        """正向证人：.doc（WordDocument 流）仍判 ole2 拒绝，D5 默认不变。"""
        from app.document.sniff import resolve_upload_doc_type

        data = _OLE2_MAGIC + b"\x00" * 512 + _u16("Root Entry") + _u16("WordDocument")
        _, reject = resolve_upload_doc_type(data, "doc")
        assert reject is not None

    def test_ole2_ppt_still_rejected(self):
        from app.document.sniff import resolve_upload_doc_type

        data = (
            _OLE2_MAGIC
            + b"\x00" * 512
            + _u16("Root Entry")
            + _u16("PowerPoint Document")
        )
        _, reject = resolve_upload_doc_type(data, "ppt")
        assert reject is not None

    def test_xls_marker_in_late_directory_found(self):
        """目录在 1024 字节之后也能识别（红基线缺陷点）。"""
        from app.document.sniff import sniff_file_type

        data = _OLE2_MAGIC + b"\x00" * 4096 + _u16("Root Entry") + _u16("Workbook")
        assert sniff_file_type(data).format == "xls"


class TestD4XlsParse:
    async def test_real_xls_parses_nonempty(self):
        from app.document.xlsx_parser import XLSXParser

        html = await XLSXParser().parse(str(_REAL_XLS))
        assert html != ""
        assert "<h2>" in html
        assert "张三" in html
        assert "研发部" in html
        # 第二个 sheet 也在
        assert "设备清单" in html

    async def test_real_xlsx_still_parses(self):
        """正向证人：.xlsx openpyxl 主路径不回归。"""
        from app.document.xlsx_parser import XLSXParser

        assert _REAL_XLSX, "存量 xlsx 样例缺失"
        html = await XLSXParser().parse(_REAL_XLSX[0])
        assert html != ""
        assert "<table>" in html

    async def test_corrupt_xls_returns_empty_honestly(self):
        """双引擎都失败 → 空串（管线 A2 契约负责标 failed，解析器不冒充成功）。"""
        import tempfile

        from app.document.xlsx_parser import XLSXParser

        with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as f:
            f.write(_OLE2_MAGIC + b"\x00" * 2048)
            path = f.name
        try:
            html = await XLSXParser().parse(path)
            assert html == ""
        finally:
            Path(path).unlink(missing_ok=True)


class TestD4DependencyDeclaration:
    def test_requirements_declares_xlrd(self):
        """依赖声明证人：全新环境按 requirements 安装后降级路径可用。"""
        req = (_REPO_ROOT / "backend" / "requirements.txt").read_text()
        lines = [
            ln.strip()
            for ln in req.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert any(ln.lower().startswith("xlrd") for ln in lines)
