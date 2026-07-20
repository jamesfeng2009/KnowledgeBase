"""
语义分块器 — 单一职责：将长文档切分为语义连贯的 Chunk 序列。

采用内容类型路由 + 四级优先级分块策略：
    0. 内容类型路由（P1）：根据 content_type 主动选择最优分块策略；
       - faq: Q&A 对分块（P0），一个问答对 = 一个 chunk；
       - tutorial/specification/report: 结构化分块（带标题路径锚点 P3）；
       - plain: 语义分块（TextTiling）；
       - 未指定时走四级兜底链。
    1. 结构化分块：按 Markdown 标题（# / ## / ###）或 HTML 标签（<h1>/<h2>）分割，
       每个 chunk 携带完整标题路径作为上下文锚点（P3），
       title_path 作为 [标题路径] 前缀拼入 content 增强 embedding 上下文感知；
    2. 语义分块：基于 TextTiling 相似度算法，滑动窗口计算相邻段落相似度，
       在相似度谷底分割（话题边界）；
    3. 父子索引：小块检索、大块上下文（小块 256 tokens，父块 1024 tokens）；
    4. 固定长度兜底：512 tokens 固定分割，可选 Overlap 重叠弥补硬切边界丢失。

遵循单一职责：本模块只负责文本切分，不涉及向量化和存储。
遵循开闭原则：新增分块策略只需新增私有方法并在 chunk 中按优先级追加调用，
无需修改既有策略实现。
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Sequence

from app.utils.logger import get_logger

log = get_logger(__name__)

# 父子索引参数
_CHILD_TOKENS: int = 256
_PARENT_TOKENS: int = 1024
# 固定长度兜底参数
_FALLBACK_TOKENS: int = 512
# 结构化分块产出有效性的最小块数
_MIN_STRUCTURAL_CHUNKS: int = 2
# 语义分块 TextTiling 滑动窗口（按句/段计）
_SEMANTIC_WINDOW: int = 3
# 中英文混合 token 估算系数：约 1 token ≈ 3.5 字符（保守取值）。
_CHARS_PER_TOKEN: float = 3.5
# Q&A 对分块 — 单个 Q&A chunk 的最大 token 数
_QA_MAX_TOKENS: int = 1200
# 结构化分块 — chunk 软上限（字符数）
_STRUCTURAL_MAX_CHARS: int = 2800  # ~800 tokens
# 固定长度兜底 — Overlap 重叠字符数（~50 tokens），弥补硬切的边界信息丢失
_OVERLAP_CHARS: int = 175
# 固定长度兜底 — 是否启用 Overlap（默认关闭，仅硬切兜底场景需要）
_CHUNK_OVERLAP_ENABLED: bool = False
# 支持的内容类型标签
_VALID_CONTENT_TYPES: frozenset[str] = frozenset(
    {"faq", "tutorial", "specification", "report", "plain", "auto"}
)


@dataclass(frozen=True)
class Chunk:
    """文档分块 — 检索与上下文注入的最小单元。

    Attributes:
        id: 分块唯一标识（UUID）。
        doc_id: 所属文档 ID。
        content: 分块文本内容。
        parent_id: 父块 ID（父子索引模式下，小块指向大块以扩充上下文）；
                   无父块时为 None。
        start_pos: 分块在原文中的起始字符偏移。
        end_pos: 分块在原文中的结束字符偏移。
        token_count: 分块估算 token 数。
        title_path: 标题路径锚点（P3），如 "Redis 深度解析 > 集群 > 哈希槽分配"；
                    结构化分块时自动提取，非结构化内容为空字符串。
        content_type: 内容类型标签（P1），标记分块来源的内容类型。
        chunk_strategy: 实际使用的分块策略名称（qa / structural / semantic / fallback）。
    """

    id: str
    doc_id: str
    content: str
    parent_id: str | None = None
    start_pos: int = 0
    end_pos: int = 0
    token_count: int = 0
    title_path: str = ""
    content_type: str = ""
    chunk_strategy: str = ""


@dataclass(frozen=True)
class _QAPair:
    """Q&A 对内部表示 — 用于 P0 Q&A 分块中间结果。

    Attributes:
        question: 问题文本。
        answer: 答案文本。
        start_pos: 在原文中的起始偏移。
        end_pos: 在原文中的结束偏移。
    """

    question: str
    answer: str
    start_pos: int
    end_pos: int


def estimate_tokens(text: str) -> int:
    """估算文本 token 数（中英文混合启发式）。

    不依赖 tiktoken 等外部库，按字符数 / 3.5 估算，保证 0 外部依赖。
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


class SemanticChunker:
    """语义分块器 — 内容类型路由 + 四级优先级策略，逐级降级保证总能产出有效分块。

    P1 内容类型路由：当 content_type 明确指定时，主动路由到最优分块策略，
    而非被动走兜底链。未指定（"auto"）时保持原有四级兜底行为。

    使用方式::

        chunker = SemanticChunker()
        # P1: 按内容类型路由
        chunks = chunker.chunk(content, doc_type="md", content_type="faq")
        # 兼容：不传 content_type 时走四级兜底链
        chunks = chunker.chunk(content, doc_type="md")
    """

    def __init__(
        self,
        child_tokens: int = _CHILD_TOKENS,
        parent_tokens: int = _PARENT_TOKENS,
        fallback_tokens: int = _FALLBACK_TOKENS,
    ) -> None:
        self.child_tokens = child_tokens
        self.parent_tokens = parent_tokens
        self.fallback_tokens = fallback_tokens

    def chunk(
        self,
        content: str,
        doc_type: str,
        content_type: str = "auto",
    ) -> list[Chunk]:
        """对文档内容执行分块，返回 Chunk 列表。

        P1 内容类型路由优先：当 content_type 明确时主动选择分块策略，
        否则按优先级依次尝试结构化 / 语义 / 父子索引 / 固定长度兜底。

        Args:
            content: 文档纯文本内容。
            doc_type: 文档类型（md / html / docx / pdf / txt 等）。
            content_type: 内容类型标签（P1），可选值：
                - "faq": Q&A 对分块（P0），一个问答对 = 一个 chunk；
                - "tutorial"/"specification"/"report": 结构化分块（带标题路径 P3）；
                - "plain": 语义分块（TextTiling）；
                - "auto"（默认）: 走四级兜底链。

        Returns:
            Chunk 列表，至少包含一个分块。
        """
        if not content or not content.strip():
            return []

        doc_id = str(uuid.uuid4())
        ct = content_type if content_type in _VALID_CONTENT_TYPES else "auto"

        # P1: 内容类型显式路由
        if ct == "faq":
            chunks = self._qa_split(content, doc_id)
            if chunks:
                log.debug("chunker.qa", count=len(chunks))
                return chunks
            # QA 分块未产出有效结果，降级到兜底链

        if ct in ("tutorial", "specification", "report"):
            chunks = self._structural_split(content, doc_id, doc_type)
            if self._is_valid(chunks):
                log.debug("chunker.structural_by_type", count=len(chunks), content_type=ct)
                return chunks
            # 结构化分块无效，降级到语义分块

        if ct == "plain":
            chunks = self._semantic_split(content, doc_id)
            if self._is_valid(chunks):
                log.debug("chunker.semantic_by_type", count=len(chunks))
                return self._parent_child_index(content, chunks, doc_id)
            # 语义分块无效，降级到兜底

        # auto 模式 — 四级兜底链
        # 1. 结构化分块：检测内容格式（Markdown # 标题 / HTML <h> 标签）
        #    不再依赖 doc_type — Docling 输出 Markdown 但 doc_type 可能是 pdf/docx
        chunks = self._structural_split(content, doc_id, doc_type)
        if self._is_valid(chunks):
            log.debug("chunker.structural", count=len(chunks), doc_type=doc_type)
            return chunks

        # 2. 语义分块：TextTiling 相似度
        chunks = self._semantic_split(content, doc_id)
        if self._is_valid(chunks):
            log.debug("chunker.semantic", count=len(chunks))
            return self._parent_child_index(content, chunks, doc_id)

        # 3. 父子索引在语义分块后已构建，若仍无效则走兜底
        # 4. 固定长度兜底
        chunks = self._fixed_split(content, doc_id)
        log.debug("chunker.fallback", count=len(chunks))
        return chunks

    # ------------------------------------------------------------------
    # 视频转写分块 — P0 ASR + P1 关键帧 VLM
    # ------------------------------------------------------------------

    def chunk_video_transcript(
        self,
        segments: list[dict[str, Any]],
        keyframe_descriptions: list[dict[str, Any]] | None = None,
    ) -> list[Chunk]:
        """对视频 ASR 转写片段执行语义分块 — 按章节/时间窗口合并。

        P0: ASR 转写片段按时间窗口合并为语义块，title_path 存时间戳。
        P1: 关键帧 VLM 描述按时间戳对齐到最近的转写块，追加为视觉上下文。

        Args:
            segments: ASR 转写片段列表，每项格式::

                {"start": 0.0, "end": 15.2, "text": "..."}

            keyframe_descriptions: 关键帧 VLM 描述列表（P1，可选），每项格式::

                {"timestamp": 30.0, "description": "幻灯片显示三层架构图"}

        Returns:
            Chunk 列表，每个 Chunk 的 title_path 存时间戳标签（如 "00:00-02:15"），
            content 为合并后的转写文本（+ 关键帧描述），chunk_strategy 为 "video_semantic"。
        """
        if not segments:
            return []

        doc_id = str(uuid.uuid4())
        kf_map = self._build_keyframe_map(keyframe_descriptions)

        # 按时间窗口合并转写片段 — 默认 120 秒一个块
        WINDOW_SECONDS = 120
        chunks: list[Chunk] = []
        current_texts: list[str] = []
        window_start = float(segments[0].get("start", 0))
        window_end = window_start + WINDOW_SECONDS

        for seg in segments:
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", 0))
            seg_text = str(seg.get("text", "")).strip()
            if not seg_text:
                continue

            # 如果当前片段超出窗口，先保存当前块
            if seg_start >= window_end and current_texts:
                chunk = self._make_video_chunk(
                    doc_id, current_texts, window_start, seg_start, kf_map
                )
                if chunk:
                    chunks.append(chunk)
                current_texts = []
                window_start = seg_start
                window_end = window_start + WINDOW_SECONDS

            current_texts.append(seg_text)

        # 保存最后一个块
        if current_texts:
            last_end = float(segments[-1].get("end", window_start))
            chunk = self._make_video_chunk(
                doc_id, current_texts, window_start, last_end, kf_map
            )
            if chunk:
                chunks.append(chunk)

        # 如果合并后只有一个大块且文本很长，进一步用语义分块拆分
        if len(chunks) == 1 and estimate_tokens(chunks[0].content) > self.fallback_tokens:
            sub_chunks = self._semantic_split(chunks[0].content, doc_id)
            if self._is_valid(sub_chunks):
                # 保留原始时间戳在 title_path
                ts_label = chunks[0].title_path
                for i, sc in enumerate(sub_chunks):
                    chunks[i] = replace(
                        sc,
                        title_path=f"{ts_label} (part {i+1})",
                        content_type="video",
                        chunk_strategy="video_semantic",
                    )
                chunks = chunks[:len(sub_chunks)]

        log.info(
            "chunker.video",
            segments=len(segments),
            chunks=len(chunks),
            keyframes=len(kf_map),
        )
        return chunks

    def _build_keyframe_map(
        self,
        keyframe_descriptions: list[dict[str, Any]] | None,
    ) -> dict[float, str]:
        """构建关键帧时间戳 → 描述的映射。"""
        kf_map: dict[float, str] = {}
        if not keyframe_descriptions:
            return kf_map
        for kf in keyframe_descriptions:
            ts = float(kf.get("timestamp", 0))
            desc = str(kf.get("description", "")).strip()
            if desc:
                kf_map[ts] = desc
        return kf_map

    def _make_video_chunk(
        self,
        doc_id: str,
        texts: list[str],
        start: float,
        end: float,
        kf_map: dict[float, str],
    ) -> Chunk | None:
        """创建一个视频转写 Chunk — 合并文本 + 附带关键帧描述。"""
        content_parts: list[str] = list(texts)

        # 查找时间窗口内的关键帧描述
        kf_descs: list[str] = []
        for kf_ts, kf_desc in sorted(kf_map.items()):
            if start <= kf_ts <= end:
                kf_label = f"{int(kf_ts // 60):02d}:{int(kf_ts % 60):02d}"
                kf_descs.append(f"[画面 {kf_label}] {kf_desc}")

        if kf_descs:
            content_parts.append("\n--- 视觉描述 ---\n" + "\n".join(kf_descs))

        content = "\n".join(content_parts).strip()
        if not content:
            return None

        ts_label = (
            f"{int(start // 60):02d}:{int(start % 60):02d}"
            f"-{int(end // 60):02d}:{int(end % 60):02d}"
        )

        return Chunk(
            id=str(uuid.uuid4()),
            doc_id=doc_id,
            content=content,
            parent_id=None,
            start_pos=int(start),
            end_pos=int(end),
            token_count=estimate_tokens(content),
            title_path=ts_label,
            content_type="video",
            chunk_strategy="video_semantic",
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid(chunks: Sequence[Chunk]) -> bool:
        """判断分块结果是否有效 — 块数 >= 2 且非空。"""
        return len(chunks) >= _MIN_STRUCTURAL_CHUNKS and all(c.content.strip() for c in chunks)

    # ------------------------------------------------------------------
    # 策略 0：Q&A 对分块（P0）
    # ------------------------------------------------------------------

    def _qa_split(self, content: str, doc_id: str) -> list[Chunk]:
        """Q&A 对分块 — 一个问答对 = 一个 chunk（P0）。

        识别文本中的 Q&A 模式（Q:/A:、问:/答:、## 问题/## 回答 等），
        将每个问题-答案对作为一个完整的 chunk，确保问题和答案不被拆散。

        支持的 Q&A 格式：
            Q: 问题内容
            A: 答案内容

            问：问题内容
            答：答案内容

            ## 问题：xxx
            ## 回答：xxx
        """
        # 尝试多种 Q&A 模式匹配
        qa_pairs = self._extract_qa_pairs(content)
        if not qa_pairs:
            return []

        chunks: list[Chunk] = []
        for pair in qa_pairs:
            # 组装 Q&A chunk：问题 + 答案合并为一段
            text = f"问：{pair.question}\n\n答：{pair.answer}".strip()
            if not text:
                continue

            # 超长 Q&A 对按 _QA_MAX_TOKENS 切分（保持 Q&A 上下文前缀）
            if estimate_tokens(text) > _QA_MAX_TOKENS:
                sub_texts = self._split_by_tokens(text, _QA_MAX_TOKENS)
                for sub in sub_texts:
                    if sub.strip():
                        chunks.append(
                            Chunk(
                                id=str(uuid.uuid4()),
                                doc_id=doc_id,
                                content=sub.strip(),
                                start_pos=pair.start_pos,
                                end_pos=pair.end_pos,
                                token_count=estimate_tokens(sub),
                                title_path=pair.question[:80],
                                content_type="faq",
                                chunk_strategy="qa",
                            )
                        )
            else:
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=text,
                        start_pos=pair.start_pos,
                        end_pos=pair.end_pos,
                        token_count=estimate_tokens(text),
                        title_path=pair.question[:80],
                        content_type="faq",
                        chunk_strategy="qa",
                    )
                )

        return chunks

    @staticmethod
    def _extract_qa_pairs(content: str) -> list[_QAPair]:
        """从文本中提取 Q&A 对，支持多种格式。"""
        pairs: list[_QAPair] = []

        # 模式 1: Q: ... A: ...（英文格式）
        # 模式 2: 问：... 答：...（中文格式）
        # 统一匹配：以 Q:/A: 或 问：/答： 或 ## 问题/## 回答 开头
        patterns = [
            # Q:/A: 格式
            (
                re.compile(r"(?:^|\n)\s*Q[：:]\s*(.+?)(?=\n\s*A[：:]|\Z)", re.DOTALL),
                re.compile(r"(?:^|\n)\s*A[：:]\s*(.+?)(?=\n\s*Q[：:]|\Z)", re.DOTALL),
            ),
            # 问：/答： 格式
            (
                re.compile(r"(?:^|\n)\s*问[：:]\s*(.+?)(?=\n\s*答[：:]|\Z)", re.DOTALL),
                re.compile(r"(?:^|\n)\s*答[：:]\s*(.+?)(?=\n\s*问[：:]|\Z)", re.DOTALL),
            ),
        ]

        for q_pattern, a_pattern in patterns:
            q_matches = list(q_pattern.finditer(content))
            a_matches = list(a_pattern.finditer(content))

            # 配对 Q 和 A
            for i, q_match in enumerate(q_matches):
                question = q_match.group(1).strip()
                # 找到对应的 A — 紧跟在 Q 后面
                answer = ""
                a_start = q_match.end()
                for a_match in a_matches:
                    if a_match.start() >= a_start - 5:
                        answer = a_match.group(1).strip()
                        break

                if question and answer:
                    start_pos = q_match.start()
                    end_pos = a_match.end() if answer else q_match.end()
                    pairs.append(_QAPair(question, answer, start_pos, end_pos))

        # 模式 3: ## 问题：xxx / ## 回答：xxx（Markdown 标题格式）
        md_q_pattern = re.compile(r"^#{1,3}\s*(?:问题|Question)[：:]\s*(.+)$", re.MULTILINE | re.IGNORECASE)
        md_a_pattern = re.compile(r"^#{1,3}\s*(?:回答|答案|Answer)[：:]\s*(.+)$", re.MULTILINE | re.IGNORECASE)

        md_q_matches = list(md_q_pattern.finditer(content))
        md_a_matches = list(md_a_pattern.finditer(content))

        for i, q_match in enumerate(md_q_matches):
            question = q_match.group(1).strip()
            # 对应的 A 在 Q 之后
            answer = ""
            end_pos = q_match.end()
            for a_match in md_a_matches:
                if a_match.start() > q_match.start():
                    # 答案文本：优先取标题行内文本（group 1）
                    # 再补充标题行之后到下一个 Q 之间的正文
                    next_q_start = (
                        md_q_matches[i + 1].start()
                        if i + 1 < len(md_q_matches)
                        else len(content)
                    )
                    answer_inline = a_match.group(1).strip()
                    answer_body = content[a_match.end() : next_q_start].strip()
                    answer = f"{answer_inline}\n{answer_body}".strip() if answer_body else answer_inline
                    end_pos = next_q_start
                    break

            if question and answer:
                pairs.append(_QAPair(question, answer, q_match.start(), end_pos))

        # 去重（不同模式可能匹配到相同的 Q&A）
        seen_questions: set[str] = set()
        unique_pairs: list[_QAPair] = []
        for p in pairs:
            key = p.question[:50]
            if key not in seen_questions:
                seen_questions.add(key)
                unique_pairs.append(p)

        return unique_pairs

    # ------------------------------------------------------------------
    # 策略 1：结构化分块（P3: 标题路径锚点）
    # ------------------------------------------------------------------

    def _structural_split(
        self,
        content: str,
        doc_id: str,
        doc_type: str,
    ) -> list[Chunk]:
        """按内容格式智能分割 — 检测 Markdown 或 HTML，不依赖 doc_type。

        Docling 输出 Markdown（# 标题 / | 表格），原有解析器输出 HTML
        （<h2> 标签 / <table>），需要按实际内容格式选择分块策略，
        否则 Docling 解析的 PDF/DOCX 会因 doc_type != "md" 走 _split_html，
        找不到 HTML 标签导致结构化分块失效。
        """
        if self._is_markdown(content):
            return self._split_markdown(content, doc_id)
        return self._split_html(content, doc_id)

    @staticmethod
    def _is_markdown(content: str) -> bool:
        """检测内容是否为 Markdown 格式。

        判断依据（满足任一即视为 Markdown）：
            - 行首 1~3 个 # 标题（# / ## / ###）
            - Markdown 表格语法（| 列1 | 列2 |）
            - Markdown 列表语法（- / * / 1.）

        Args:
            content: 待检测的文本内容。

        Returns:
            True 如果内容包含 Markdown 结构标记。
        """
        # Markdown 标题：# / ## / ###
        if re.search(r"^#{1,3}\s+", content, re.MULTILINE):
            return True
        # Markdown 表格：| header | header |
        if re.search(r"^\|.+\|\s*$", content, re.MULTILINE):
            return True
        # Markdown 表格分隔行：|---|---|
        if re.search(r"^\|[\s\-:|]+\|\s*$", content, re.MULTILINE):
            return True
        return False

    @staticmethod
    def _split_markdown(content: str, doc_id: str) -> list[Chunk]:
        """按 Markdown 标题（# / ## / ###）分割，提取标题路径作为上下文锚点（P3）。

        P3 标题路径：每个 chunk 的 title_path 字段记录从根标题到当前标题的完整路径，
        如 "Redis 深度解析 > 集群架构 > 哈希槽分配"，用于：
        - embedding 时拼接为前缀增强语义锚定；
        - 检索时提供来源层级信息；
        - 生成时帮助 LLM 理解信息在文档中的位置。
        """
        # 匹配行首 1~3 个 # 的标题
        pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
        matches = list(pattern.finditer(content))

        if len(matches) < _MIN_STRUCTURAL_CHUNKS:
            return []

        chunks: list[Chunk] = []
        # P3: 维护标题层级栈，用于构建 title_path
        title_stack: list[tuple[int, str]] = []  # [(level, title), ...]

        for idx, match in enumerate(matches):
            level = len(match.group(1))  # # 的个数 = 层级
            title = match.group(2).strip()

            # 更新标题栈：弹出层级 >= 当前的标题
            while title_stack and title_stack[-1][0] >= level:
                title_stack.pop()
            title_stack.append((level, title))

            # 构建 title_path: "根标题 > 二级标题 > 三级标题"
            title_path = " > ".join(t for _, t in title_stack)

            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            text = content[start:end].strip()
            if not text:
                continue

            # 补章节标题：title_path 作为前缀拼入 content，增强 embedding 上下文感知
            prefixed_text = f"[{title_path}]\n{text}" if title_path else text

            # P3: 超长 chunk 在 H4/段落级别进一步拆分，保持标题路径前缀
            if len(prefixed_text) > _STRUCTURAL_MAX_CHARS:
                sub_texts = SemanticChunker._split_by_tokens(
                    prefixed_text, int(_STRUCTURAL_MAX_CHARS / _CHARS_PER_TOKEN)
                )
                for sub in sub_texts:
                    if sub.strip():
                        chunks.append(
                            Chunk(
                                id=str(uuid.uuid4()),
                                doc_id=doc_id,
                                content=sub.strip(),
                                start_pos=start,
                                end_pos=end,
                                token_count=estimate_tokens(sub),
                                title_path=title_path,
                                chunk_strategy="structural",
                            )
                        )
            else:
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=prefixed_text,
                        start_pos=start,
                        end_pos=end,
                        token_count=estimate_tokens(prefixed_text),
                        title_path=title_path,
                        chunk_strategy="structural",
                    )
                )
        return chunks

    @staticmethod
    def _split_html(content: str, doc_id: str) -> list[Chunk]:
        """按 HTML 标题标签（<h1>/<h2>/<h3>）分割，提取标题路径（P3）。

        改进：
        - 补章节标题：title_path 作为 [标题路径] 前缀拼入 content，
          让 embedding 阶段即可感知上下文层级，检索精度提升；
        - 超长拆分：超过 _STRUCTURAL_MAX_CHARS 的章节按 token 上限拆分，
          与 _split_markdown 保持一致，保持 title_path 前缀。
        """
        # 同时匹配标签和标题文本
        pattern = re.compile(r"<(h[1-3])[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
        matches = list(pattern.finditer(content))

        if len(matches) < _MIN_STRUCTURAL_CHUNKS:
            return []

        chunks: list[Chunk] = []
        # P3: 维护标题层级栈
        title_stack: list[tuple[int, str]] = []

        for idx, match in enumerate(matches):
            tag = match.group(1).lower()
            level = int(tag[1])  # h1 -> 1, h2 -> 2, h3 -> 3
            # 提取标题文本（去除嵌套标签）
            raw_title = match.group(2)
            title = re.sub(r"<[^>]+>", "", raw_title).strip()

            # 更新标题栈
            while title_stack and title_stack[-1][0] >= level:
                title_stack.pop()
            title_stack.append((level, title))

            title_path = " > ".join(t for _, t in title_stack)

            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            text = content[start:end].strip()
            if not text:
                continue

            # 补章节标题：title_path 作为前缀拼入 content，增强 embedding 上下文感知
            prefixed_text = f"[{title_path}]\n{text}" if title_path else text

            # 超长拆分：与 _split_markdown 保持一致
            if len(prefixed_text) > _STRUCTURAL_MAX_CHARS:
                sub_texts = SemanticChunker._split_by_tokens(
                    prefixed_text, int(_STRUCTURAL_MAX_CHARS / _CHARS_PER_TOKEN)
                )
                for sub in sub_texts:
                    if sub.strip():
                        chunks.append(
                            Chunk(
                                id=str(uuid.uuid4()),
                                doc_id=doc_id,
                                content=sub.strip(),
                                start_pos=start,
                                end_pos=end,
                                token_count=estimate_tokens(sub),
                                title_path=title_path,
                                chunk_strategy="structural",
                            )
                        )
            else:
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=prefixed_text,
                        start_pos=start,
                        end_pos=end,
                        token_count=estimate_tokens(prefixed_text),
                        title_path=title_path,
                        chunk_strategy="structural",
                    )
                )
        return chunks

    # ------------------------------------------------------------------
    # 策略 2：语义分块（TextTiling 相似度）
    # ------------------------------------------------------------------

    def _semantic_split(self, content: str, doc_id: str) -> list[Chunk]:
        """基于 TextTiling 相似度算法在话题边界分割。

        滑动窗口计算相邻段落相似度，在相似度谷底（depth score 高）处分割。
        """
        # 按空行 / 句号切分为段落单元
        units = self._split_into_units(content)
        if len(units) < _SEMANTIC_WINDOW * 2 + 1:
            return []

        # 计算相邻单元对的相似度序列
        scores = self._compute_similarity_scores(units)
        if not scores:
            return []

        # 在相似度谷底（depth score）处确定分割点
        boundaries = self._find_boundaries(scores)
        if not boundaries:
            return []

        # 根据分割点组装 Chunk
        chunks: list[Chunk] = []
        prev = 0
        offset = 0
        unit_starts: list[int] = []
        pos = 0
        for unit in units:
            unit_starts.append(pos)
            pos += len(unit) + 1  # +1 for separator

        for boundary in boundaries:
            end_unit = boundary + 1
            text = "".join(units[prev:end_unit]).strip()
            if text:
                start_pos = unit_starts[prev] if prev < len(unit_starts) else offset
                end_pos = (
                    unit_starts[end_unit] if end_unit < len(unit_starts) else len(content)
                )
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=text,
                        start_pos=start_pos,
                        end_pos=min(end_pos, len(content)),
                        token_count=estimate_tokens(text),
                        chunk_strategy="semantic",
                    )
                )
            offset = end_pos
            prev = end_unit

        # 尾部剩余
        if prev < len(units):
            text = "".join(units[prev:]).strip()
            if text:
                start_pos = unit_starts[prev] if prev < len(unit_starts) else offset
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=text,
                        start_pos=start_pos,
                        end_pos=len(content),
                        token_count=estimate_tokens(text),
                        chunk_strategy="semantic",
                    )
                )
        return chunks

    @staticmethod
    def _split_into_units(content: str) -> list[str]:
        """将内容切分为段落 / 句子单元（TextTiling 的 token 序列近似）。"""
        # 优先按空行分段，无空行则按句号分段
        units = [u.strip() for u in re.split(r"\n\s*\n", content) if u.strip()]
        if len(units) <= 1:
            units = [u.strip() for u in re.split(r"(?<=[。！？.!?])\s*", content) if u.strip()]
        return units

    @staticmethod
    def _tokenize_for_similarity(text: str) -> set[str]:
        """提取关键词集合用于相似度计算（Jaccard 近似）。"""
        # 中英文混合分词：英文按空格，中文按字
        tokens: set[str] = set()
        for word in re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]", text.lower()):
            tokens.add(word)
        return tokens

    def _compute_similarity_scores(self, units: list[str]) -> list[float]:
        """计算相邻窗口对的相似度得分序列。"""
        w = _SEMANTIC_WINDOW
        scores: list[float] = []
        for i in range(w, len(units) - w):
            left = self._tokenize_for_similarity("".join(units[i - w : i]))
            right = self._tokenize_for_similarity("".join(units[i : i + w]))
            if not left and not right:
                scores.append(0.0)
                continue
            union = left | right
            if not union:
                scores.append(0.0)
                continue
            scores.append(len(left & right) / len(union))
        return scores

    @staticmethod
    def _find_boundaries(scores: list[float]) -> list[int]:
        """在相似度谷底确定分割点 — depth score 超过均值+标准差的位置。"""
        if not scores:
            return []
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = math.sqrt(variance)
        threshold = mean - std

        boundaries: list[int] = []
        w = _SEMANTIC_WINDOW
        for i, score in enumerate(scores):
            if score <= threshold:
                boundaries.append(i + w)
        return boundaries

    # ------------------------------------------------------------------
    # 策略 3：父子索引
    # ------------------------------------------------------------------

    def _parent_child_index(
        self,
        content: str,
        chunks: list[Chunk],
        doc_id: str,
    ) -> list[Chunk]:
        """构建父子索引 — 小块检索、父块提供上下文。

        将每个语义块进一步切为 ~child_tokens 的小块（子），原块作为父；
        子块通过 parent_id 指向父块，检索命中子块后可回取父块扩充上下文。
        """
        result: list[Chunk] = []
        for parent in chunks:
            if parent.token_count <= self.child_tokens:
                # 小于阈值无需再切，直接作为叶子块
                result.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=parent.content,
                        parent_id=parent.id,
                        start_pos=parent.start_pos,
                        end_pos=parent.end_pos,
                        token_count=parent.token_count,
                        chunk_strategy=parent.chunk_strategy or "semantic",
                    )
                )
                # 父块本身也保留，供上下文扩充
                result.append(parent)
                continue

            # 按 child_tokens 切分子块
            child_texts = self._split_by_tokens(parent.content, self.child_tokens)
            for text in child_texts:
                if not text.strip():
                    continue
                result.append(
                    Chunk(
                        id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        content=text,
                        parent_id=parent.id,
                        start_pos=parent.start_pos,
                        end_pos=parent.start_pos + len(text),
                        token_count=estimate_tokens(text),
                        chunk_strategy=parent.chunk_strategy or "semantic",
                    )
                )
            # 父块保留
            result.append(parent)
        return result

    # ------------------------------------------------------------------
    # 策略 4：固定长度兜底
    # ------------------------------------------------------------------

    def _fixed_split(self, content: str, doc_id: str) -> list[Chunk]:
        """按固定 token 数切分（兜底策略）。

        改进：可选 Overlap — 相邻块共享前一块末尾内容（~50 tokens），
        弥补硬切的边界信息丢失。通过 _CHUNK_OVERLAP_ENABLED 开关控制（默认关闭）。
        仅兜底策略使用 Overlap，因为高级策略（结构化分块、TextTiling）在语义边界
        切分，天然保留上下文；父子索引更提供 parent_id 回取机制，优于 Overlap。
        """
        texts = self._split_by_tokens(content, self.fallback_tokens)

        # 可选 Overlap：相邻块共享前一块末尾内容，防止边界信息丢失
        if _CHUNK_OVERLAP_ENABLED and len(texts) > 1:
            overlapped: list[str] = []
            for i, text in enumerate(texts):
                if i == 0:
                    overlapped.append(text)
                else:
                    # 取前一块末尾 _OVERLAP_CHARS 字符作为当前块开头
                    prev = texts[i - 1]
                    prev_tail = prev[-_OVERLAP_CHARS:] if len(prev) > _OVERLAP_CHARS else prev
                    overlapped.append(prev_tail + text)
            texts = overlapped

        chunks: list[Chunk] = []
        offset = 0
        for text in texts:
            if not text.strip():
                offset += len(text)
                continue
            chunks.append(
                Chunk(
                    id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    content=text,
                    start_pos=offset,
                    end_pos=offset + len(text),
                    token_count=estimate_tokens(text),
                    chunk_strategy="fallback",
                )
            )
            offset += len(text)
        return chunks if chunks else [
            Chunk(
                id=str(uuid.uuid4()),
                doc_id=doc_id,
                content=content.strip(),
                start_pos=0,
                end_pos=len(content),
                token_count=estimate_tokens(content),
                chunk_strategy="fallback",
            )
        ]

    @staticmethod
    def _split_by_tokens(text: str, max_tokens: int) -> list[str]:
        """按估算 token 数上限切分文本，尽量在段落 / 句子边界断开。"""
        if max_tokens <= 0:
            return [text]
        max_chars = int(max_tokens * _CHARS_PER_TOKEN)
        if len(text) <= max_chars:
            return [text]

        parts: list[str] = []
        # 优先在段落边界切分
        blocks = re.split(r"(\n\s*\n)", text)
        buf = ""
        for block in blocks:
            if len(buf) + len(block) <= max_chars:
                buf += block
                continue
            if buf:
                parts.append(buf)
                buf = ""
            # 单个 block 仍超长，按句子切分
            if len(block) > max_chars:
                sentences = re.split(r"(?<=[。！？.!?])\s*", block)
                for sent in sentences:
                    if len(buf) + len(sent) <= max_chars:
                        buf += sent
                    else:
                        if buf:
                            parts.append(buf)
                        # 句子本身超长则硬切
                        if len(sent) > max_chars:
                            for i in range(0, len(sent), max_chars):
                                parts.append(sent[i : i + max_chars])
                        else:
                            buf = sent
            else:
                buf = block
        if buf:
            parts.append(buf)
        return parts
