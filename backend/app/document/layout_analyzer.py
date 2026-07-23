"""PDF 版式分析层 — 借鉴 pdfminer group_text_boxes 思路的阅读顺序重建。

职责单一：接收 pymupdf ``get_text("dict")`` 输出的 block 列表，
输出按视觉阅读顺序排列的文本 block 列表。不修改 pymupdf 本身，
仅消费其提供的 bbox 数据，性能开销为纯 Python block 排序（每页约 1-5ms）。

设计要点（对照 pdfminer.six ``pdfminer/converter.py``）：
    - 栏检测：按 block 的水平位置聚类成栏，借鉴 pdfminer 的
      "新 block 与当前栏垂直重叠且水平超出栏边界 → 新栏" 判定；
    - 通栏块处理：跨页宽的标题/脚注不参与栏聚类，按 y 位置插入输出；
    - 栏分隔线辅助：可选传入 ``page.get_drawings()`` 的垂直线段，
      作为栏边界的硬约束（pdfminer 用 LTLine 做同样的事）；
    - 容错：任何异常（脏数据、极端版式）都不影响主流程，
      调用方负责降级为 ``get_text()`` 纯文本。

坐标系约定：pymupdf 与 PDF 原生一致，原点在页面**左上**，y 轴向下。
    阅读顺序 = 栏间从左到右，栏内从上到下（y0 升序）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 通栏判定阈值：block 宽度 >= 页面宽度 * 此值 视为通栏（标题/脚注）
_FULL_WIDTH_RATIO: float = 0.7
# 栏聚类的水平容差（pt）：x0 差在此范围内的 block 视为同栏
_COLUMN_X_TOLERANCE: float = 20.0
# 栏间最小水平间隙（占页宽比例）：小于此值不认为是多栏
_MIN_COLUMN_GAP_RATIO: float = 0.08


@dataclass
class TextBlock:
    """版式分析用的轻量文本 block。

    从 pymupdf dict block 提取的最小字段集，与 pymupdf 数据结构解耦，
    便于单元测试构造和将来替换数据源。
    """

    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1)
    text: str = ""
    # 原始 pymupdf block（保留 lines/spans 以便调用方取字体等信息）
    raw: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @classmethod
    def from_pymupdf(cls, block: dict[str, Any]) -> TextBlock | None:
        """从 pymupdf ``get_text("dict")`` 的 block 构造，非文本 block 返回 None。"""
        if block.get("type") != 0:
            return None
        bbox_raw = block.get("bbox")
        if not bbox_raw or len(bbox_raw) != 4:
            return None
        bbox = tuple(float(v) for v in bbox_raw)
        # 汇总 block 内全部 span 文本（行内按顺序，行间加换行）
        lines: list[str] = []
        for line in block.get("lines", []):
            spans_text = "".join(s.get("text", "") for s in line.get("spans", []))
            if spans_text.strip():
                lines.append(spans_text)
        text = "\n".join(lines)
        return cls(bbox=bbox, text=text, raw=block)


# 栏内 y 间隙断段阈值：相邻块 y 间隙超过前一块高度的此倍数 → 断段
# （标题/脚注与正文栏之间通常有明显空行，借此把离群块从栏中剥离）
_COLUMN_Y_GAP_RATIO: float = 2.5


def _vertically_overlap(a: TextBlock, b: TextBlock, tolerance: float = 3.0) -> bool:
    """两个 block 是否在垂直方向有重叠（容差 tolerance pt）。"""
    return a.y0 < b.y1 + tolerance and b.y0 < a.y1 + tolerance


def _is_same_column(x0_a: float, x0_b: float) -> bool:
    """两个 x 起点是否属于同一栏（容差内）。"""
    return abs(x0_a - x0_b) <= _COLUMN_X_TOLERANCE


def _detect_columns(
    blocks: list[TextBlock],
    page_width: float,
) -> list[list[TextBlock]]:
    """将正文 block 聚类成栏（借鉴 pdfminer group_text_boxes）。

    算法：
        1. 按 y0（上→下）遍历 block；
        2. 维护"当前栏"的 x 区间，新 block 与当前栏垂直重叠但
           水平位于栏右侧（且间隙 >= 最小栏间隙）→ 判定为新栏；
        3. 无重叠的新 block → 归入水平位置最接近的已有栏；
        4. 栏间按 x0 从左到右排序。

    Args:
        blocks: 已剔除通栏块的正文 block 列表。
        page_width: 页面宽度（pt），用于计算最小栏间隙。

    Returns:
        栏列表，每栏是按 y0 升序的 block 列表。栏间从左到右排列。
    """
    if not blocks:
        return []

    min_gap = page_width * _MIN_COLUMN_GAP_RATIO
    # 按 y0 升序遍历（同 y 按 x0）
    sorted_blocks = sorted(blocks, key=lambda b: (b.y0, b.x0))

    columns: list[list[TextBlock]] = []
    # 每栏的 x 代表起点（用栏内 block 的最小 x0）
    column_x0: list[float] = []

    for block in sorted_blocks:
        placed = False
        # 尝试归入已有栏：x0 接近某栏起点 → 同栏
        for idx, cx0 in enumerate(column_x0):
            if _is_same_column(block.x0, cx0):
                columns[idx].append(block)
                column_x0[idx] = min(cx0, block.x0)
                placed = True
                break
        if not placed:
            # 与所有已有栏起点都不接近 → 新栏
            columns.append([block])
            column_x0.append(block.x0)

    # 栏间按 x0 从左到右排序，栏内按 y0 升序
    ordered_pairs = sorted(zip(column_x0, columns), key=lambda p: p[0])
    result: list[list[TextBlock]] = []
    for _, col in ordered_pairs:
        result.append(sorted(col, key=lambda b: (b.y0, b.x0)))
    return result


def sort_reading_order(
    blocks: list[TextBlock],
    page_width: float,
) -> list[TextBlock]:
    """将文本 block 按视觉阅读顺序排序（版式分层主入口）。

    流程：
        1. 初分：宽度 >= 页宽 70% 的块直接判为通栏；
        2. 对剩余正文块做栏检测；
        3. 二次判定：在多栏情况下，与多个栏的 x 区间都水平重叠、
           且不与任何栏"同栏对齐"的块（如短标题/短脚注）也归为通栏——
           它们虽不满 70% 页宽，但横跨栏边界，应脱离栏流按 y 插入；
        4. 通栏块按 y 区间插入栏流（标题在栏上、脚注在所有栏下）。

    Args:
        blocks: 本页全部文本 block。
        page_width: 页面宽度（pt）。

    Returns:
        按阅读顺序排列的 block 列表。输入为空时返回空列表。
    """
    if not blocks:
        return []
    if len(blocks) == 1:
        return list(blocks)

    full_width = page_width * _FULL_WIDTH_RATIO
    wide_full = [b for b in blocks if b.width >= full_width]
    candidates = [b for b in blocks if b.width < full_width]

    # 先做一轮栏检测，用栏结构辅助二次通栏判定
    columns = _detect_columns(candidates, page_width)

    # 二次判定：多栏时，检测"伪正文块"——实际是短通栏块（标题/脚注）。
    # 挑战：第一轮栏检测会把与某栏 x0 对齐的标题/脚注也聚进该栏，
    # 导致栏的 min/max y 覆盖全页，无法用"块在栏 y 范围外"判定。
    # 解法：对每栏内部按 y 间隙断段——标题/脚注与正文之间有明显空行
    # （间隙 > 前块高度 2.5 倍），借此把栏首/栏尾的离群单块段剥离为通栏块。
    extra_full: list[TextBlock] = []
    if len(columns) >= 2:
        refined_columns: list[list[TextBlock]] = []
        for col in columns:
            if not col:
                continue
            # 栏内按 y 间隙断段
            segments: list[list[TextBlock]] = [[col[0]]]
            for prev, cur in zip(col, col[1:]):
                gap = cur.y0 - prev.y1
                threshold = max(prev.height, 1.0) * _COLUMN_Y_GAP_RATIO
                if gap > threshold:
                    segments.append([cur])  # 新段
                else:
                    segments[-1].append(cur)
            # 段数 >= 2 时，首段/尾段若为单块（孤立标题/脚注）则剥离
            if len(segments) >= 2:
                # 首段单块且明显在栏顶（标题）
                first = segments[0]
                if len(first) == 1:
                    extra_full.append(first[0])
                    segments = segments[1:]
            if len(segments) >= 2:
                # 尾段单块且明显在栏底（脚注）
                last = segments[-1]
                if len(last) == 1:
                    extra_full.append(last[0])
                    segments = segments[:-1]
            # 剩余段合并回本栏正文
            body_col = [b for seg in segments for b in seg]
            if body_col:
                refined_columns.append(body_col)
        if extra_full:
            columns = _detect_columns(
                [b for col in refined_columns for b in col], page_width
            )

    full_blocks = sorted(wide_full + extra_full, key=lambda b: b.y0)
    body_blocks = [b for col in columns for b in col]

    # 正文栏检测
    columns = _detect_columns(body_blocks, page_width)

    # 展平栏流（栏间左→右，栏内上→下）作为正文顺序
    body_ordered = [b for col in columns for b in col]

    if not full_blocks:
        return body_ordered

    # 合并通栏块：通栏块（标题/脚注）按"栏区间"插入，而非单个正文块。
    # 关键洞察：多栏的 y 区间通常重叠（左右栏同处一个垂直范围），
    # 因此不能用"逐块比较 y1 <= fb.y0"——右栏的块会被误判为在脚注上方。
    # 正确语义：通栏块应位于"所有 y 区间整体在其上方的栏"之后、
    # "任何与其 y 重叠的栏"之前。
    if not body_ordered:
        return full_blocks

    def _full_block_position(fb: TextBlock) -> int:
        """返回通栏块应插入的正文流索引（相对原始 body_ordered）。

        判定：通栏块位于所有"栏最大 y1 <= fb.y0"（整栏在其上方）的栏之后。
        对每栏取该栏最后一个正文块在 body_ordered 中的索引 +1，取最大值。
        栏与 fb 的 y 重叠（栏 y1 > fb.y0）→ 该栏不纳入，确保脚注不会
        插入到与其 y 重叠的栏的内容之间。
        """
        insert_at = 0
        body_index = {id(b): i for i, b in enumerate(body_ordered)}
        for col in columns:
            if not col:
                continue
            col_y1 = max(b.y1 for b in col)
            # 整栏在通栏块上方：栏的最大 y1 <= fb.y0
            if col_y1 <= fb.y0:
                last_idx = max(body_index[id(b)] for b in col)
                insert_at = max(insert_at, last_idx + 1)
        return insert_at

    # 计算每个通栏块的插入位置，一次性合并（不能逐个 insert 会偏移索引）。
    insertions: list[tuple[int, TextBlock]] = [
        (_full_block_position(fb), fb) for fb in full_blocks
    ]
    from collections import defaultdict

    at_position: dict[int, list[TextBlock]] = defaultdict(list)
    for pos, fb in insertions:
        at_position[pos].append(fb)

    merged: list[TextBlock] = []
    for i, body in enumerate(body_ordered):
        if i in at_position:
            merged.extend(at_position[i])
        merged.append(body)
    if len(body_ordered) in at_position:
        merged.extend(at_position[len(body_ordered)])
    return merged


def extract_page_text_ordered(
    page_dict: dict[str, Any],
    page_width: float,
) -> str:
    """从 pymupdf ``get_text("dict")`` 结果生成按阅读顺序排列的纯文本。

    这是给调用方的一站式接口：dict 数据 → 版式排序 → 拼接文本。
    任何异常都返回空字符串，由调用方降级处理。

    Args:
        page_dict: ``page.get_text("dict")`` 的返回值。
        page_width: 页面宽度（pt），通常取 ``page.rect.width``。

    Returns:
        按阅读顺序拼接的文本（block 间以换行分隔）。无文本或异常返回空串。
    """
    try:
        raw_blocks = page_dict.get("blocks", [])
        blocks = [
            tb for tb in (TextBlock.from_pymupdf(b) for b in raw_blocks)
            if tb is not None and tb.text.strip()
        ]
        if not blocks:
            return ""
        ordered = sort_reading_order(blocks, page_width)
        return "\n".join(b.text for b in ordered)
    except Exception as exc:  # 防御：版式分析绝不能影响主解析流程
        log.warning("pdf.layout_analyze_failed", error=str(exc))
        return ""
