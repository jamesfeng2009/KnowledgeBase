"""
文件类型嗅探单元测试 — sniff_file_type / resolve_upload_doc_type。

覆盖目标：
    1. 各文档格式魔数正检（pdf/docx/xlsx/pptx）+ 文本兜底；
    2. 伪装/非文档格式（图片/可执行/归档/数据库）应被判定为需拒绝；
    3. "改后缀"文件应被纠正或保留声明的策略。
"""

import pytest

from app.document.sniff import (
    DOC_TYPES_BY_SNIFF,
    resolve_upload_doc_type,
    sniff_file_type,
)

# 常用魔数（与 sniff.py 内定义保持一致）
PDF = b"%PDF-1.7\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
ELF = b"\x7fELF" + b"\x02\x01\x01\x00" + b"\x00" * 8
PE = b"MZ" + b"\x90\x00\x03\x00" + b"\x00" * 16
GZIP = b"\x1f\x8b" + b"\x08\x00" + b"\x00" * 8
SQLITE = b"SQLite format 3\x00" + b"\x00" * 8


def _pk(inner: bytes) -> bytes:
    """构造一个 zip 头 + 内容，模拟 OOXML 的本地文件头。"""
    return b"PK\x03\x04" + b"\x14\x00\x00\x00" + b"\x00" * 22 + inner


class TestSniffFileType:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (PDF, "pdf"),
            (PNG, "png"),
            (JPEG, "jpeg"),
            (ELF, "elf"),
            (PE, "pe"),
            (GZIP, "gzip"),
            (SQLITE, "sqlite"),
        ],
    )
    def test_magic_detection(self, raw: bytes, expected: str) -> None:
        assert sniff_file_type(raw).format == expected

    def test_pdf_requires_head(self) -> None:
        # 非头部位置的 %PDF- 不应命中
        ok = b"junk-prefix" + PDF
        assert sniff_file_type(ok).format in ("text", "unknown")

    def test_webp_riff(self) -> None:
        webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 "
        assert sniff_file_type(webp).format == "webp"

    def test_tar_magic_at_offset(self) -> None:
        tar = b"\x00" * 257 + b"ustar\x0000"
        assert sniff_file_type(tar).format == "tar"

    def test_ooxml_docx(self) -> None:
        assert sniff_file_type(_pk(b"word/document.xml")).format == "docx"

    def test_ooxml_xlsx(self) -> None:
        assert sniff_file_type(_pk(b"xl/workbook.xml")).format == "xlsx"

    def test_ooxml_pptx(self) -> None:
        assert sniff_file_type(_pk(b"ppt/presentation.xml")).format == "pptx"

    def test_plain_text(self) -> None:
        assert sniff_file_type("这是一段中文文本\n".encode("utf-8")).is_text is True

    def test_empty_is_unknown(self) -> None:
        assert sniff_file_type(b"").format == "unknown"


class TestResolveUploadDocType:
    def test_md_text_accepted_keeps_declared(self) -> None:
        doc_type, reason = resolve_upload_doc_type("# 标题\n正文".encode("utf-8"), "md")
        assert doc_type == "md" and reason is None

    def test_pdf_accepted(self) -> None:
        doc_type, reason = resolve_upload_doc_type(PDF + b"x" * 8, "pdf")
        assert doc_type == "pdf" and reason is None

    def test_masked_docx_corrected_to_docx(self) -> None:
        # 声称为 pdf，实际是 docx → 纠正为 docx
        doc_type, reason = resolve_upload_doc_type(_pk(b"word/document.xml"), "pdf")
        assert reason is None and doc_type == DOC_TYPES_BY_SNIFF["docx"]

    def test_image_masquerade_rejected(self) -> None:
        # 声称为 pdf，实际是 PNG 图片 → 拒绝
        _, reason = resolve_upload_doc_type(PNG + b"x" * 8, "pdf")
        assert reason is not None

    def test_executable_masquerade_rejected(self) -> None:
        # 伪装成文档的可执行文件 → 拒绝
        _, reason = resolve_upload_doc_type(ELF + b"x" * 8, "md")
        assert reason is not None and "ELF" in reason

    def test_archive_rejected(self) -> None:
        _, reason = resolve_upload_doc_type(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8, "txt")
        assert reason is not None