"""
文件类型嗅探 — 用二进制魔数识别文件的真实类型。

用途：解析静态分发前先校验实际类型，避免"改后缀"文件被错误分发，
以及伪装成文档/图片的可执行文件进入解析链路。

设计约定（与项目零外部依赖、手写头部解析的风格一致，见 image_storage.py）：
    - 不引入 libmagic，用字节探针识别常见格式；
    - 只读文件头部一小段，不做全量扫描；
    - sniff_file_type() 纯函数、无 IO，便于单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 在分发前可识别为文档解析类型的真实格式 → doc_type
DOC_TYPES_BY_SNIFF: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "xls": "xls",  # OLE2 且含 Workbook 表
}

# 合法 OOXML 办公容器但无法区分 docx/xlsx/pptx 子类型：保留声明类型，不拒绝
_OOXML_AMBIGUOUS = "ooxml_unknown"

# 被识别为非文档解析目标（可执行/归档/图片等）的格式，上传时应拒绝
NON_DOCUMENT_FORMATS = frozenset(
    {
        "zip",      # 无 OOXML 标记的通用 ZIP
        "7z",
        "rar",
        "gzip",
        "tar",
        "sqlite",
        "png",
        "jpeg",
        "webp",
        "gif",
        "bmp",
        "ole2",     # 旧版 Office 二进制复合文档（.doc/.ppt）
        "elf",
        "pe",
        "unknown",
    }
)


@dataclass(frozen=True)
class FileSniff:
    """文件实际类型的嗅探结果。"""

    format: str            # 规范化格式标识（见 DOC_TYPES_BY_SNIFF / NON_DOCUMENT_FORMATS / "text"）
    is_text: bool          # 是否为纯文本（md/txt/csv/html 无魔数）
    comment: str = ""      # 供日志/错误提示的可读说明


def sniff_file_type(data: bytes, max_head: int = 1024) -> FileSniff:
    """嗅探字节流的真实文件类型。

    Args:
        data: 文件字节内容（可为空）。
        max_head: 参与判断的头部字节数上限。

    Returns:
        FileSniff。format 为 "text" 时表示无魔数的纯文本，
        调用方应保留按扩展名推导的类型。
    """
    if not data:
        return FileSniff(format="unknown", is_text=False, comment="空文件")

    head = data[:max_head]

    # 1) 二进制魔数精确匹配（按常用文档/高危格式优先）
    if _starts_with(head, b"%PDF-"):
        return FileSniff("pdf", False, "PDF 文档")
    if _starts_with(head, b"\x89PNG\r\n\x1a\n"):
        return FileSniff("png", False, "PNG 图片")
    if _starts_with(head, b"\xff\xd8\xff"):
        return FileSniff("jpeg", False, "JPEG 图片")
    if _starts_with(head, b"GIF87a") or _starts_with(head, b"GIF89a"):
        return FileSniff("gif", False, "GIF 图片")
    if _starts_with(head, b"BM"):
        return FileSniff("bmp", False, "BMP 图片")
    if _starts_with(head, b"\x7fELF"):
        return FileSniff("elf", False, "Linux ELF 可执行文件")
    if _starts_with(head, b"7z\xbc\xaf\x27\x1c"):
        return FileSniff("7z", False, "7z 压缩包")
    if _starts_with(head, b"Rar!\x1a\x07"):
        return FileSniff("rar", False, "RAR 压缩包")
    if _starts_with(head, b"\x1f\x8b"):
        return FileSniff("gzip", False, "gzip 压缩包")
    if _starts_with(head, b"SQLite format 3\x00"):
        return FileSniff("sqlite", False, "SQLite 数据库")

    # 2) WebP：RIFF 容器 + "WEBP" 子类型，避免与 RIFF 其它变体误判
    if len(head) >= 12 and _starts_with(head, b"RIFF") and head[8:12] == b"WEBP":
        return FileSniff("webp", False, "WebP 图片")

    # 3) tar：magic 出现在偏移 257
    if len(head) >= 262 and head[257:262] == b"ustar":
        return FileSniff("tar", False, "tar 归档")

    # 4) PE 可执行（MZ 头；无其它魔数命中时判为可执行）
    if _starts_with(head, b"MZ"):
        return FileSniff("pe", False, "Windows PE 可执行文件")

    # 5) ZIP 容器：OOXML（docx/xlsx/pptx）都是 zip，读包内标记区分；
    #    其余 zip（普通包/Page 加密等）归为 NON_DOCUMENT。
    if _starts_with(head, b"PK\x03\x04"):
        return _sniff_ooxml(data)
    if _starts_with(head, b"PK\x05\x06") or _starts_with(head, b"PK\x07\x08"):
        return FileSniff("zip", False, "ZIP 归档（分割/稀疏）")

    # 6) OLE2 复合文档：旧版 Office 二进制格式
    if _starts_with(head, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _sniff_ole(head)

    # 7) 文本兜底：头部无空字节且可用 UTF-8 解码 → 视为纯文本
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return FileSniff("unknown", False, "无法识别的二进制内容")
        return FileSniff("text", True, "文本文件")

    return FileSniff("unknown", False, "无法识别的二进制内容")


def _starts_with(data: bytes, magic: bytes) -> bool:
    return data.startswith(magic)


def _sniff_ooxml(data: bytes) -> FileSniff:
    """在 ZIP 容器内通过打包路径标记区分 docx/xlsx/pptx。

    子类型标记可能位于文件靠后位置，故扫描传入的完整字节，
    而非仅文件头（大型 docx 的 word/document.xml 可能晚于首 KB）。
    """
    for marker, fmt, label in (
        (b"word/document.xml", "docx", "Word 文档 (docx)"),
        (b"xl/workbook.xml", "xlsx", "Excel 工作簿 (xlsx)"),
        (b"ppt/presentation.xml", "pptx", "PowerPoint 演示文稿 (pptx)"),
    ):
        if marker in data:
            return FileSniff(fmt, False, label)

    # 含 [Content_Types].xml 即合法的 OOXML 办公文档，但子类型无法区分时保留声明类型
    if b"[Content_Types].xml" in data:
        return FileSniff("ooxml_unknown", False, "OOXML 办公文档（子类型无法区分）")

    return FileSniff("zip", False, "ZIP 归档（非 OOXML 文档）")


def _sniff_ole(head: bytes) -> FileSniff:
    """在 OLE2 复合文档中识别旧版 Office 子类型，含 Workbook 表判为 xls。"""
    if b"Workbook" in head:
        return FileSniff("xls", False, "旧版 Excel 工作簿 (xls)")
    if b"WordDocument" in head:
        return FileSniff("ole2", False, "旧版 Word 二进制文档 (doc)")
    if b"PowerPoint Document" in head:
        return FileSniff("ole2", False, "旧版 PowerPoint 二进制文档 (ppt)")
    return FileSniff("ole2", False, "OLE2 复合文档")


def resolve_upload_doc_type(data: bytes, declared_ext: str) -> tuple[str, Optional[str]]:
    """对上传播放字节做类型校验并纠正 doc_type。

    Args:
        data: 已读取的文件内容。
        declared_ext: 按文件名扩展名推断的 doc_type（可能失真）。

    Returns:
        (final_doc_type, reject_reason) 二元组。
        - reject_reason 为空表示可接收，final_doc_type 为应写入的 doc_type；
        - reject_reason 非空表示应拒绝该上传，final_doc_type 无意义。
    """
    sniff = sniff_file_type(data)

    # 纯文本（md/txt/csv/html 等）：无魔数，保留声明类型
    if sniff.is_text:
        return declared_ext, None

    # 识别为支持的文档解析类型：用真实类型纠正分发
    mapping = DOC_TYPES_BY_SNIFF.get(sniff.format)
    if mapping is not None:
        return mapping, None

    # 合法 OOXML 办公文档但子类型无法区分：保留声明类型，不拒绝
    if sniff.format == _OOXML_AMBIGUOUS:
        return declared_ext, None

    # 其余（图片/归档/数据库/可执行/无法识别）一律拒绝，防止伪装文件进入解析链路
    return declared_ext, (
        sniff.comment or f"不支持的文件类型：{sniff.format}"
    )