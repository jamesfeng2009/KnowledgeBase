"""
Docling 统一文档解析器 — 基于 IBM Granite-Docling-258M 模型。

核心能力：
    - 统一解析 PDF / DOCX / PPTX / XLSX / HTML / 图片 / 音频，输出 HTML；
    - AI 驱动版面分析（多栏布局理解、阅读顺序还原）；
    - 表格识别（无边框表格也能识别，保留行列结构）；
    - 公式识别（→ LaTeX）；
    - 扫描 PDF OCR（PaddleOCR 内置，自动选择 text/OCR 模式）。

设计要点：
    - 统一输出 HTML（含 <h1>/<h2>/<h3> 标题 + <table> 表格），
      与原有解析器（pymupdf/python-docx/python-pptx/openpyxl）格式一致，
      chunker 的 _split_html() 直接按 <h> 标签分块，无需格式检测；
    - Docling 不可用时优雅降级（返回空字符串，由 factory 路由到原有解析器）；
    - 支持 VLM 图片描述增强（Docling 提取图片位置 → VLM 生成描述注入 HTML）。

许可证：MIT（免费商用），pip install docling 安装。
"""

from __future__ import annotations

import os
from typing import Any

from app.config import get_settings
from app.document.base import DocumentParser
from app.utils.logger import get_logger

log = get_logger(__name__)

#: Docling 支持的文档类型（视频不在列 — 视频保留 ffmpeg→ASR 管线）
_DOCLING_SUPPORTED_TYPES: set[str] = {
    "pdf", "docx", "pptx", "xlsx", "xls",
    "html", "htm",
    "png", "jpg", "jpeg", "gif", "webp", "tiff", "bmp",
    "audio", "mp3", "wav", "m4a", "aac", "flac", "ogg",
    "md", "txt",
}


class DoclingParser(DocumentParser):
    """Docling 统一解析器 — 一行代码解析多格式文档为 HTML。

    使用方式::

        parser = DoclingParser()
        html = await parser.parse("path/to/document.pdf")

    统一输出 HTML（含 <h1>/<h2>/<h3> 标题标签 + <table> 表格），
    与原有解析器格式一致，chunker 的 _split_html() 直接按 <h> 标签分块。

    Docling 内部自动选择解析策略：
        - 数字 PDF → text 模式（快速提取文本层）；
        - 扫描 PDF → OCR 模式（PaddleOCR 版面分析 + 文字识别）；
        - DOCX/PPTX/XLSX → OOXML 解析 + 版面理解；
        - 图片 → OCR + 版面分析；
        - 音频 → 语音转文本。

    可选增强：
        - VLM 图片描述：Docling 提取图片位置后，用 VLM 生成图片描述注入 HTML。
    """

    def __init__(self) -> None:
        self._converter: Any | None = None
        self._init_checked: bool = False

    def _get_converter(self) -> Any | None:
        """延迟初始化 Docling DocumentConverter。

        第一次调用时创建实例并缓存，后续复用。
        Docling 未安装时返回 None。
        """
        if self._init_checked and self._converter is not None:
            return self._converter

        try:
            from docling.document_converter import DocumentConverter

            self._converter = DocumentConverter()
            self._init_checked = True
            log.info("docling.initialized")
            return self._converter
        except ImportError:
            log.warning("docling.not_installed", reason="pip install docling")
            self._init_checked = True
            return None
        except Exception as exc:
            log.warning("docling.init_failed", error=str(exc))
            self._init_checked = True
            return None

    async def parse(self, file_path: str) -> str:
        """解析文档为 HTML 格式。

        统一输出 HTML（含 <h1>/<h2>/<h3> 标题标签 + <table> 表格），
        与原有解析器（pymupdf/python-docx/python-pptx/openpyxl）输出格式一致，
        chunker 的 _split_html() 可直接按 <h> 标签分块，无需格式检测。

        Args:
            file_path: 文档文件路径。

        Returns:
            HTML 格式的文档内容。Docling 不可用或解析失败时返回空字符串。
        """
        # 0. 扫描件/图片 OCR — 若启用 MinerU 且命中扫描 PDF / 图片，走 MinerU。
        #    Office（docx/pptx/xlsx）与音频仍走 Docling，仅替换 OCR 路径。
        if self._should_use_mineru(file_path):
            html = await self._parse_with_mineru(file_path)
            if html:
                log.info(
                    "mineru.parsed",
                    file_path=file_path,
                    html_len=len(html),
                )
                return html

        converter = self._get_converter()
        if converter is None:
            return ""

        try:
            result = converter.convert(file_path)
            html: str = result.document.export_to_html()

            if not html or not html.strip():
                log.warning("docling.empty_result", file_path=file_path)
                return ""

            # 可选：VLM 图片描述增强
            settings = get_settings()
            vlm_enhance = getattr(settings, "DOCLING_VLM_IMAGE_ENHANCE", False)
            if vlm_enhance:
                html = await self._enhance_with_vlm(html, result)

            log.info(
                "docling.parsed",
                file_path=file_path,
                html_len=len(html),
            )
            return html

        except Exception as exc:
            log.warning("docling.parse_failed", file_path=file_path, error=str(exc))
            return ""

    async def _parse_raw(self, file_path: str) -> Any | None:
        """解析文档并返回原始 Docling result 对象（含图片数据）。

        与 parse() 不同，本方法返回 Docling ConversionResult 原始对象，
        供 _extract_pictures() 提取图片二进制数据。

        Args:
            file_path: 文档文件路径。

        Returns:
            Docling ConversionResult 对象，或 None（不可用/失败）。
        """
        converter = self._get_converter()
        if converter is None:
            return None

        try:
            result = converter.convert(file_path)
            return result
        except Exception as exc:
            log.warning("docling.parse_raw_failed", file_path=file_path, error=str(exc))
            return None

    async def _enhance_with_vlm(self, html: str, result: Any) -> str:
        """VLM 图片描述增强 — 在 HTML 中注入图片描述。

        Docling 提取文档结构后，对图片位置调用 VLM 生成描述，
        将描述文本注入到 HTML 对应位置。

        Args:
            html: Docling 生成的原始 HTML。
            result: Docling 转换结果对象。

        Returns:
            增强后的 HTML（图片描述注入到对应位置）。
        """
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            # 尝试从 Docling 结果中提取图片
            pictures = self._extract_pictures(result)
            if not pictures:
                return html

            enhanced_parts: list[str] = []
            for pic_info in pictures:
                try:
                    # P0-5: 结构化输出 + 校验层 — 越界数值/非法输出标记
                    # low_confidence，注入时显式标注待复核而非直接入库
                    result_dict = await vlm.understand_structured(
                        image=pic_info["data"],
                        image_type="general",
                        mime_type=pic_info.get("mime_type", "image/png"),
                    )
                    desc = result_dict.get("description") or ""
                    if desc:
                        if result_dict.get("low_confidence"):
                            enhanced_parts.append(
                                f"<p>[图片描述（低置信度，待人工复核）: {desc}]</p>"
                            )
                        else:
                            enhanced_parts.append(f"<p>[图片描述: {desc}]</p>")
                except Exception as exc:
                    log.debug("docling.vlm_enhance_failed", error=str(exc))

            if enhanced_parts:
                return html + "\n" + "\n".join(enhanced_parts)
            return html

        except ImportError:
            log.debug("docling.vlm_not_available_for_enhance")
            return html
        except Exception as exc:
            log.debug("docling.vlm_enhance_error", error=str(exc))
            return html

    @staticmethod
    def _extract_pictures(result: Any) -> list[dict[str, Any]]:
        """从 Docling 转换结果中提取图片数据。

        Args:
            result: Docling 转换结果对象。

        Returns:
            图片信息列表，每项包含 data（bytes）和 mime_type。
        """
        pictures: list[dict[str, Any]] = []
        try:
            # Docling v2.x API：result.document.pictures
            doc = getattr(result, "document", None)
            if doc is None:
                return pictures

            pics = getattr(doc, "pictures", None)
            if pics is None:
                return pictures

            for pic in pics:
                # 尝试获取图片二进制数据
                image_data = None
                mime_type = "image/png"

                # 方式 1：pic.image.uri → 读取文件
                uri = getattr(getattr(pic, "image", None), "uri", None)
                if uri:
                    import os
                    if os.path.exists(str(uri)):
                        with open(str(uri), "rb") as f:
                            image_data = f.read()

                # 方式 2：pic.image.data → 直接二进制
                if image_data is None:
                    image_data = getattr(getattr(pic, "image", None), "data", None)

                if image_data:
                    pictures.append({"data": image_data, "mime_type": mime_type})

        except Exception as exc:
            log.debug("docling.extract_pictures_failed", error=str(exc))

        return pictures

    #: 交由 MinerU 处理（替换 Docling OCR 路径）的图片类型
    _MINERU_IMAGE_TYPES: set[str] = {
        "png", "jpg", "jpeg", "gif", "webp", "tiff", "bmp",
    }

    def _mineru_python(self) -> str:
        """读取 MinerU 独立 venv 的 python 路径（缓存，缺省返回空串）。"""
        if getattr(self, "_mineru_python_path", None) is None:
            settings = get_settings()
            path = (getattr(settings, "MINERU_PYTHON", "") or "").strip()
            self._mineru_python_path = path if path else ""
        return self._mineru_python_path

    def _should_use_mineru(self, file_path: str) -> bool:
        """判断是否应走 MinerU 替换 Docling 的 OCR 路径。

        规则：启用 MinerU 且配置了独立 venv 时，
            - 图片类型 → MinerU；
            - PDF 且为扫描件（无文本层）→ MinerU；
            - Office（docx/pptx/xlsx）与音频 → 仍走 Docling。
        """
        settings = get_settings()
        if not self._bool(getattr(settings, "MINERU_ENABLED", False), False):
            return False
        if not self._mineru_python():
            log.debug("mineru.python_not_configured")
            return False

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        if ext in self._MINERU_IMAGE_TYPES:
            return True
        if ext == "pdf" and self._is_scanned_pdf(file_path):
            return True
        return False

    @staticmethod
    def _is_scanned_pdf(file_path: str) -> bool:
        """用 PyMuPDF 检测 PDF 是否为扫描件（整份无文本层）。"""
        try:
            import fitz

            with fitz.open(file_path) as doc:
                if doc.page_count == 0:
                    return False
                for page in doc:
                    if page.get_text().strip():
                        return False
                return True
        except Exception:
            # 无法读取文本层时保守返回 False（仍走 Docling）
            return False

    async def _parse_with_mineru(self, file_path: str) -> str:
        """在独立 venv 中调用 MinerU CLI 解析扫描 PDF / 图片，返回 HTML。

        采用子进程隔离：MinerU 依赖（transformers 5.15 + mlx-vlm）与
        Docling 依赖存在版本冲突，独立 venv 可避免污染主后端环境。
        """
        import asyncio
        import glob
        import tempfile

        settings = get_settings()
        python = self._mineru_python()
        backend = getattr(settings, "MINERU_BACKEND", "vlm-engine") or "vlm-engine"
        lang = getattr(settings, "MINERU_LANG", "ch") or "ch"
        timeout = int(getattr(settings, "MINERU_TIMEOUT", 600) or 600)

        cmd = [
            python, "-c",
            "from mineru.cli.client import main; import sys; sys.exit(main())",
            "-p", file_path, "-o", "{out_dir}",
            "-b", backend, "-l", lang,
        ]

        try:
            with tempfile.TemporaryDirectory() as out_dir:
                cmd[cmd.index("{out_dir}")] = out_dir
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    log.warning("mineru.timeout", file_path=file_path)
                    return ""

                if proc.returncode != 0:
                    log.warning(
                        "mineru.failed",
                        file_path=file_path,
                        code=proc.returncode,
                        stderr=(stderr or b"")[-800:].decode("utf-8", "ignore"),
                    )
                    return ""

                md_files = glob.glob(
                    os.path.join(out_dir, "**", "*.md"), recursive=True
                )
                if not md_files:
                    log.warning("mineru.no_md_output", file_path=file_path)
                    return ""

                with open(md_files[0], "r", encoding="utf-8") as f:
                    md = f.read()
                if not md or not md.strip():
                    return ""
                return self._markdown_to_html(md)
        except Exception as exc:
            log.warning("mineru.error", file_path=file_path, error=str(exc))
            return ""

    @staticmethod
    def _markdown_to_html(md: str) -> str:
        """将 MinerU 输出的 Markdown 转 HTML（标题/表格/图片）。

        优先用 markdown-it-py（Docling 依赖链已含），不可用时退化为段落包裹，
        保证 chunker 能按 <h> 标签分块。
        """
        try:
            from markdown_it import MarkdownIt

            # html=True 让 MinerU 输出中的原生 <table> 原样透传，
            # 否则会被转义成 &lt;table&gt; 导致表格结构丢失。
            return MarkdownIt("js-default", {"html": True}).render(md)
        except Exception:
            # 极简降级：保留标题与表格，其余按行转 <p>
            import re

            lines = md.splitlines()
            out: list[str] = []
            in_code = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    out.append(f"<pre>{line}</pre>")
                    continue
                m = re.match(r"^(#{1,6})\s+(.*)$", line)
                if m:
                    level = len(m.group(1))
                    out.append(f"<h{level}>{m.group(2)}</h{level}>")
                elif line.strip().startswith("|") and line.strip().endswith("|"):
                    out.append(line)  # 表格原样保留，交由上层处理
                elif line.strip():
                    out.append(f"<p>{line.strip()}</p>")
            return "\n".join(out)

    @staticmethod
    def is_supported(doc_type: str) -> bool:
        """检查 Docling 是否支持该文档类型。

        Args:
            doc_type: 文档类型标识（如 "pdf", "docx", "xlsx"）。

        Returns:
            True 如果 Docling 支持该类型。
        """
        return doc_type.lower() in _DOCLING_SUPPORTED_TYPES

    @staticmethod
    def is_available() -> bool:
        """检查 Docling 是否已安装且可用。

        Returns:
            True 如果 docling 包已安装。
        """
        try:
            import docling  # noqa: F401
            return True
        except ImportError:
            return False
