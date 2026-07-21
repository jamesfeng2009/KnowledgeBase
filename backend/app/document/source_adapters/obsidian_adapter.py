"""
Obsidian 适配器 — 读取本地 Obsidian vault 中的 Markdown 文件。

Obsidian vault 就是一个包含 .md 文件的目录，适配器只需：
    1. 读取指定 .md 文件内容
    2. 列举 vault 目录下所有 .md 文件

凭证格式（credentials dict）::
    {"vault_path": "/Users/user/Documents/MyVault"}

输出格式：Markdown，由 MarkdownParser 后续解析，chunker._split_markdown 分块。

支持 Obsidian 特性：
    - [[wiki links]] 保持原样（chunker 不处理，向量模型可理解上下文）
    - ![](image.png) 嵌入保持原样
    - YAML frontmatter（MarkdownParser 会提取 title）
    - 嵌套目录结构（list_documents 递归列举）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.document.source_adapters.base import (
    AdapterError,
    DocumentSourceAdapter,
    FetchedDocument,
    SourceDocumentInfo,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# 最大递归深度（Obsidian vault 通常不超过 5 层嵌套）
_MAX_DEPTH: int = 10
# 列举文件数量上限（防止超大型 vault 性能问题）
_MAX_LIST_COUNT: int = 5000


class ObsidianAdapter(DocumentSourceAdapter):
    """Obsidian vault 文档来源适配器 — 读取本地 Markdown 文件。

    使用方式::

        adapter = ObsidianAdapter()
        doc = await adapter.fetch(
            "Projects/Architecture.md",
            credentials={"vault_path": "/Users/user/Documents/MyVault"},
        )
    """

    adapter_id = "obsidian"
    display_name = "Obsidian"
    supported_formats = ("markdown",)

    async def fetch(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> FetchedDocument:
        """读取 Obsidian vault 中的 Markdown 文件。

        Args:
            doc_url_or_id: vault 内的相对路径（如 ``"Projects/Architecture.md"``）
                或绝对路径。支持 ``obsidian://open?vault=MyVault&file=Projects/Architecture``
                URI 格式。
            credentials: 必须包含 ``vault_path``。

        Returns:
            FetchedDocument，format 为 ``"markdown"``。

        Raises:
            AdapterError: 文件不存在或 vault_path 缺失。
        """
        vault_path = credentials.get("vault_path", "")
        if not vault_path:
            raise AdapterError(self.adapter_id, "缺少 vault_path 配置")

        vault = Path(vault_path).expanduser().resolve()
        if not vault.is_dir():
            raise AdapterError(
                self.adapter_id,
                f"vault 目录不存在: {vault}",
            )

        # 从 obsidian:// URI 或路径中提取文件路径
        file_rel = self._extract_file_path(doc_url_or_id)

        # 安全检查：防止路径穿越
        target = (vault / file_rel).resolve()
        try:
            target.relative_to(vault)
        except ValueError:
            raise AdapterError(
                self.adapter_id,
                f"路径越界，禁止访问 vault 外文件: {file_rel}",
            ) from None

        if not target.is_file():
            raise AdapterError(
                self.adapter_id,
                f"文件不存在: {file_rel}",
                status_code=404,
            )

        if target.suffix.lower() not in (".md", ".markdown"):
            raise AdapterError(
                self.adapter_id,
                f"仅支持 .md 文件，当前文件: {target.name}",
            )

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            raise AdapterError(
                self.adapter_id,
                f"读取文件失败: {exc}",
            ) from exc

        # 标题：文件名（去扩展名）
        title = target.stem

        log.info(
            "obsidian.fetched",
            vault=str(vault),
            file=file_rel,
            chars=len(content),
        )

        return FetchedDocument(
            source=self.adapter_id,
            title=title,
            content=content,
            format="markdown",
            source_url=f"obsidian://open?vault={vault.name}&file={file_rel}",
            doc_id=str(target),
            metadata={
                "vault_path": str(vault),
                "relative_path": file_rel,
                "file_name": target.name,
            },
        )

    async def list_documents(
        self,
        space_or_root: str,
        credentials: dict[str, Any],
    ) -> list[SourceDocumentInfo]:
        """递归列举 vault 下所有 .md 文件。

        Args:
            space_or_root: vault 根目录路径（覆盖 credentials 中的 vault_path）。
            credentials: 可包含 ``vault_path``（space_or_root 优先）。

        Returns:
            .md 文件信息列表（相对路径作为 doc_id）。

        Raises:
            AdapterError: 目录不存在。
        """
        vault_path = space_or_root or credentials.get("vault_path", "")
        if not vault_path:
            raise AdapterError(self.adapter_id, "缺少 vault_path")

        vault = Path(vault_path).expanduser().resolve()
        if not vault.is_dir():
            raise AdapterError(
                self.adapter_id,
                f"vault 目录不存在: {vault}",
            )

        results: list[SourceDocumentInfo] = []
        count = 0

        for md_file in vault.rglob("*.md"):
            # 排除 .obsidian 配置目录
            if ".obsidian" in md_file.parts:
                continue
            if count >= _MAX_LIST_COUNT:
                log.warning(
                    "obsidian.list_truncated",
                    vault=str(vault),
                    max=_MAX_LIST_COUNT,
                )
                break

            rel_path = str(md_file.relative_to(vault))
            results.append(
                SourceDocumentInfo(
                    doc_id=rel_path,
                    title=md_file.stem,
                    url=f"obsidian://open?vault={vault.name}&file={rel_path}",
                    updated_at="",
                    author="",
                    metadata={
                        "relative_path": rel_path,
                        "file_size": md_file.stat().st_size,
                    },
                )
            )
            count += 1

        log.info(
            "obsidian.listed",
            vault=str(vault),
            count=len(results),
        )
        return results

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        """测试 vault 目录是否存在且可读。"""
        vault_path = credentials.get("vault_path", "")
        if not vault_path:
            return False
        vault = Path(vault_path).expanduser().resolve()
        return vault.is_dir()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_file_path(doc_url_or_id: str) -> str:
        """从输入中提取 Markdown 文件路径。

        支持格式：
            - 相对路径: ``"Projects/Architecture.md"``
            - 绝对路径: ``"/Users/user/.../Projects/Architecture.md"``
            - obsidian:// URI: ``"obsidian://open?vault=MyVault&file=Projects/Architecture.md"``
        """
        import re
        from urllib.parse import unquote, urlparse

        # obsidian:// URI
        if doc_url_or_id.startswith("obsidian://"):
            parsed = urlparse(doc_url_or_id)
            params = dict(re.findall(r"(\w+)=([^&]+)", parsed.query))
            file_param = params.get("file", "")
            if file_param:
                return unquote(file_param)
            return ""

        # 普通路径 — 去除可能的 URL 编码
        return unquote(doc_url_or_id) if "%" in doc_url_or_id else doc_url_or_id
