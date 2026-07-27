"""
引用标注 — 单一职责：从生成文本中提取引用标注并映射到检索文档。

实现逻辑：
    1. 从生成文本中提取 [1] [2] 等引用标注（含中英文方括号）；
    2. 将引用编号映射到对应的检索文档（sources）；
    3. 生成引用卡片数据（doc_id / title / snippet / url）；
    4. 强制校验：答案必须包含引用标注，否则视为幻觉风险。

遵循单一职责：本模块只负责引用解析与映射，不涉及生成与检索。
遵循开闭原则：引用格式匹配由正则驱动，新增引用样式只需扩展 _CITATION_PATTERN。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 引用标注正则 — 匹配 [1] [2] 等（含中文全角方括号【1】）。
# 仅匹配 1~3 位数字，避免误匹配日期 / 编号列表。
_CITATION_PATTERN: re.Pattern[str] = re.compile(r"[\[【](\d{1,3})[\]】]")
# 引用 snippet 最大字符数
_SNIPPET_MAX: int = 200


@dataclass
class CitationValidationResult:
    """引用校验结果。

    Attributes:
        valid: 是否通过校验（答案包含有效引用标注）。
        has_citations: 答案中是否包含 [n] 引用标注。
        citation_count: 提取到的引用编号数量。
        source_count: 可用来源文档数量。
        unmapped_ids: 无法映射到来源的引用编号列表。
        reason: 校验失败原因（valid=True 时为空）。
    """

    valid: bool
    has_citations: bool
    citation_count: int
    source_count: int
    unmapped_ids: list[int]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "valid": self.valid,
            "has_citations": self.has_citations,
            "citation_count": self.citation_count,
            "source_count": self.source_count,
            "unmapped_ids": self.unmapped_ids,
            "reason": self.reason,
        }


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
    # 引用强制校验 — 拦截无引用标注的答案
    # ------------------------------------------------------------------

    def has_citations(self, text: str) -> bool:
        """检查文本中是否包含 [n] 引用标注。

        Args:
            text: 待检查的答案文本。

        Returns:
            True 如果文本中至少包含一个 [n] 引用标注。
        """
        return bool(_CITATION_PATTERN.search(text))

    def validate_citations(
        self,
        text: str,
        sources: list[dict[str, Any]],
    ) -> CitationValidationResult:
        """强制校验答案是否包含有效引用标注。

        校验规则：
            1. 如果有来源文档但答案中无任何 [n] 引用标注 → 无效（幻觉风险）；
            2. 如果引用编号超出来源范围 → 记录 unmapped_ids 但不阻断
               （LLM 可能引用了不存在的文档，需人工核查）；
            3. 无来源文档时跳过校验（非 RAG 场景，如纯对话）。

        Args:
            text: LLM 生成的答案文本。
            sources: 检索到的文档来源列表。

        Returns:
            CitationValidationResult: 校验结果。
        """
        citation_ids = self._find_citation_ids(text)
        has_cites = len(citation_ids) > 0
        source_count = len(sources)

        # 无来源文档时跳过校验
        if source_count == 0:
            return CitationValidationResult(
                valid=True,
                has_citations=has_cites,
                citation_count=len(citation_ids),
                source_count=source_count,
                unmapped_ids=[],
                reason="",
            )

        # 有来源但无引用标注 → 校验失败
        if not has_cites:
            log.warning(
                "citation.validation_failed",
                reason="答案未包含任何引用标注",
                source_count=source_count,
                text_len=len(text),
            )
            return CitationValidationResult(
                valid=False,
                has_citations=False,
                citation_count=0,
                source_count=source_count,
                unmapped_ids=[],
                reason="答案未包含任何 [n] 引用标注，存在幻觉风险",
            )

        # 检查引用编号是否都能映射到来源
        unmapped: list[int] = []
        for cid in citation_ids:
            if self._map_to_source(cid, sources) is None:
                unmapped.append(cid)

        if unmapped:
            log.warning(
                "citation.unmapped_ids",
                unmapped=unmapped,
                source_count=source_count,
            )
            # 有未映射的引用编号，但仍认为校验通过（LLM 引用了额外信息）
            # 只是记录告警，不阻断 — 避免误杀有效答案
            return CitationValidationResult(
                valid=True,
                has_citations=True,
                citation_count=len(citation_ids),
                source_count=source_count,
                unmapped_ids=unmapped,
                reason=f"引用编号 {unmapped} 超出来源范围，建议核查",
            )

        log.debug(
            "citation.validation_passed",
            citation_count=len(citation_ids),
            source_count=source_count,
        )
        return CitationValidationResult(
            valid=True,
            has_citations=True,
            citation_count=len(citation_ids),
            source_count=source_count,
            unmapped_ids=[],
            reason="",
        )

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
