"""
引用标注 — 单一职责：从生成文本中提取引用标注并映射到检索文档。

实现逻辑：
    1. 从生成文本中提取 [1] [2] 等引用标注（含中英文方括号）；
    2. 将引用编号映射到对应的检索文档（sources）；
    3. 生成引用卡片数据（doc_id / title / snippet / url）。

遵循单一职责：本模块只负责引用解析与映射，不涉及生成与检索。
遵循开闭原则：引用格式匹配由正则驱动，新增引用样式只需扩展 _CITATION_PATTERN。
"""

from __future__ import annotations

import re
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 引用标注正则 — 匹配 [1] [2] 等（含中文全角方括号【1】）。
# 仅匹配 1~3 位数字，避免误匹配日期 / 编号列表。
_CITATION_PATTERN: re.Pattern[str] = re.compile(r"[\[【](\d{1,3})[\]】]")
# 引用 snippet 最大字符数
_SNIPPET_MAX: int = 200


class CitationExtractor:
    """引用标注提取器 — 将文本中的 [n] 标注映射到检索文档来源。

    使用方式::

        extractor = CitationExtractor()
        citations = extractor.extract(answer, sources=retrieved_docs)
    """

    def extract(
        self,
        text: str,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """从生成文本中提取引用标注并映射到检索文档。

        Args:
            text: LLM 生成的答案文本（可能含 [1] [2] 等标注）。
            sources: 检索到的文档来源列表，每项至少包含 doc_id，
                     可选 title / content / snippet / url。

        Returns:
            引用卡片列表，每项格式::

                {
                    "citation_id": int,    # 引用编号
                    "doc_id": str,
                    "title": str | None,
                    "snippet": str,        # 文档内容摘要
                    "url": str | None,
                }
        """
        citation_ids = self._find_citation_ids(text)
        if not citation_ids:
            return []

        citations: list[dict[str, Any]] = []
        for cid in citation_ids:
            # 引用编号从 1 开始，映射到 sources 列表索引（0-based）
            source = self._map_to_source(cid, sources)
            if source is None:
                log.debug("citation.unmapped", citation_id=cid, sources_count=len(sources))
                continue
            snippet = self._make_snippet(source)
            citations.append(
                {
                    "citation_id": cid,
                    "doc_id": str(source.get("doc_id") or ""),
                    "title": source.get("title"),
                    "snippet": snippet,
                    "url": source.get("url"),
                }
            )
        log.debug("citation.extracted", count=len(citations), text_len=len(text))
        return citations

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _find_citation_ids(text: str) -> list[int]:
        """提取文本中出现的所有引用编号，按首次出现顺序去重。"""
        seen: set[int] = set()
        ordered: list[int] = []
        for match in _CITATION_PATTERN.finditer(text):
            cid = int(match.group(1))
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
        return ordered

    @staticmethod
    def _map_to_source(
        citation_id: int,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """将引用编号映射到检索文档。

        映射规则：引用编号 n 对应 sources[n-1]；越界时返回 None。
        """
        if not sources:
            return None
        index = citation_id - 1
        if 0 <= index < len(sources):
            return sources[index]
        return None

    @staticmethod
    def _make_snippet(source: dict[str, Any]) -> str:
        """从来源文档生成内容摘要（截断到 _SNIPPET_MAX 字符）。"""
        content = (
            source.get("snippet")
            or source.get("content")
            or source.get("chunk_text")
            or ""
        )
        content = str(content).strip()
        if len(content) <= _SNIPPET_MAX:
            return content
        return content[:_SNIPPET_MAX] + "..."
