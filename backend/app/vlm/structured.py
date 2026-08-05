"""
VLM 结构化输出约束与校验层 — 单一职责：prompt 路由 + JSON schema 解析 + 数值范围校验。

对应"约束 VLM 幻觉"场景（P0-5）：
    1. prompt 路由：按图片类型（图纸/手写批注/数据图表等）使用专用 prompt，
       统一要求 JSON 结构化输出（状态枚举判定代替自由生成）；
    2. schema 校验：status 必须是 ok/unclear/not_applicable 枚举，
       非法 JSON 或非法枚举 → low_confidence；
    3. 数值校验：对 numbers 字段做范围规则校验（置信度 0-1、百分比 0-100、
       图纸尺寸 > 0），越界 → low_confidence + issue 说明，不直接入库。

遵循优雅降级：任何解析/校验异常都不会抛出，结果标记 low_confidence。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

# 支持的图片类型 — prompt 路由键
ImageType = Literal[
    "general",        # 通用图片
    "drawing",        # 图纸（工程图/设计图）
    "handwriting",    # 手写批注
    "chart",          # 数据图表
    "table",          # 表格
    "scanned_text",   # 扫描文字
    "whiteboard",     # 白板
]

SUPPORTED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"general", "drawing", "handwriting", "chart", "table", "scanned_text", "whiteboard"}
)

# 状态枚举 — 结构化输出的判定字段，非法值视为低置信度
_VALID_STATUS: frozenset[str] = frozenset({"ok", "unclear", "not_applicable"})

_COMMON_SCHEMA = (
    '{"status": "ok|unclear|not_applicable", "description": "一句话描述", '
    '"tags": ["标签1", "标签2"], '
    '"numbers": [{"label": "数值含义", "value": 数字, "unit": "单位", "confidence": 0.0-1.0}]}'
)

# prompt 路由表 — 按图片类型定制关注点，统一 JSON 输出约束
_PROMPT_ROUTES: dict[str, str] = {
    "general": (
        "请描述这张图片的内容，重点关注图表、数据和关键信息。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "drawing": (
        "这是一张图纸。请识别其中的尺寸标注、公差、材料和技术要求，"
        "数值必须取自图中文字，禁止估算或推测。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "handwriting": (
        "这是一张手写批注。请转写手写文字内容，无法辨认的部分在 description 中"
        "注明[无法辨认]，禁止编造。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "chart": (
        "这是一张数据图表。请识别图表类型、坐标轴含义和关键数值，"
        "百分比数值必须在 0-100 范围内，数值必须取自图中标注，禁止推测。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "table": (
        "这是一张表格图片。请识别行列结构和单元格内容，数值原样转录。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "scanned_text": (
        "这是扫描文字图片。请按原文转写所有文字，不要额外解释。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
    "whiteboard": (
        "这是白板照片。请识别讨论要点、结论和待办事项。"
        f"严格输出 JSON：{_COMMON_SCHEMA}。只输出 JSON。"
    ),
}


@dataclass
class StructuredImageResult:
    """VLM 结构化理解结果。

    Attributes:
        status: 状态枚举（ok / unclear / not_applicable / parse_error）。
        description: 图片内容描述。
        tags: 关键词标签。
        numbers: 提取的数值列表。
        low_confidence: 校验未通过（越界数值/非法状态/解析失败）时为 True，
            调用方应标记待人工复核而非直接入库。
        issues: 校验问题描述列表（可观测，供日志与复核）。
    """

    status: str = "unclear"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    numbers: list[dict[str, Any]] = field(default_factory=list)
    low_confidence: bool = False
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "description": self.description,
            "tags": self.tags,
            "numbers": self.numbers,
            "low_confidence": self.low_confidence,
            "issues": self.issues,
        }


def build_structured_prompt(image_type: str = "general") -> str:
    """按图片类型路由到专用 prompt。

    未知类型回退 general（优雅降级）。
    """
    return _PROMPT_ROUTES.get(image_type, _PROMPT_ROUTES["general"])


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 VLM 输出中提取 JSON 对象（容忍 markdown 代码块包裹）。"""
    if not text or not text.strip():
        return None
    candidate = text.strip()
    if "```" in candidate:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
        if match:
            candidate = match.group(1)
    else:
        # 截取第一个 { 到最后一个 }，容忍前后解释性文字
        start = candidate.find("{")
        end = candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_number(
    item: Any, image_type: str, issues: list[str]
) -> dict[str, Any] | None:
    """校验单个数值项，返回净化后的数值项；越界时记录 issue。"""
    if not isinstance(item, dict):
        issues.append("numbers 项非对象，已丢弃")
        return None
    value = item.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        issues.append(f"数值项缺失合法 value: {item!r}")
        return None

    cleaned = {
        "label": str(item.get("label") or ""),
        "value": value,
        "unit": str(item.get("unit") or ""),
    }

    # 通用规则：confidence ∈ [0, 1]
    confidence = item.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            issues.append(f"confidence 越界 [0,1]: {confidence!r}")
        else:
            cleaned["confidence"] = float(confidence)

    # 类型规则：数据图表百分比 ∈ [0, 100]
    unit = cleaned["unit"]
    if image_type == "chart" and unit in {"%", "％", "percent", "percentage"}:
        if not 0.0 <= float(value) <= 100.0:
            issues.append(f"百分比越界 [0,100]: {value}{unit}")

    # 类型规则：图纸尺寸必须为正且不超过 1e6（mm 量级）
    if image_type == "drawing":
        if float(value) <= 0:
            issues.append(f"图纸数值非正数: {value}{unit}")
        elif abs(float(value)) > 1e6:
            issues.append(f"图纸数值超量程: {value}{unit}")

    return cleaned


def parse_structured(text: str, image_type: str = "general") -> StructuredImageResult:
    """解析并校验 VLM 结构化输出。

    Args:
        text: VLM 原始输出文本（预期为 JSON，容忍 markdown 包裹）。
        image_type: 图片类型（决定数值范围规则）。

    Returns:
        StructuredImageResult — 任何异常都归一为 low_confidence=True。
    """
    result = StructuredImageResult()
    data = _extract_json(text)
    if data is None:
        # 非法 JSON：保留原始文本截断作为描述，但标记低置信度，
        # 防止幻觉内容直接入库
        result.status = "parse_error"
        result.description = (text or "").strip()[:500]
        result.low_confidence = True
        result.issues.append("输出非合法 JSON")
        return result

    # 状态枚举校验
    status = data.get("status")
    if status not in _VALID_STATUS:
        result.issues.append(f"非法 status 枚举: {status!r}")
        result.low_confidence = True
        result.status = "unclear"
    else:
        result.status = status

    result.description = str(data.get("description") or "").strip()
    tags = data.get("tags")
    if isinstance(tags, list):
        result.tags = [str(t).strip() for t in tags if str(t).strip()]

    # 数值字段范围校验
    numbers = data.get("numbers")
    if isinstance(numbers, list):
        for item in numbers:
            cleaned = _validate_number(item, image_type, result.issues)
            if cleaned is not None:
                result.numbers.append(cleaned)

    if result.issues:
        result.low_confidence = True
    # 模型自报不确定 → 低置信度
    if result.status in {"unclear", "not_applicable"}:
        result.low_confidence = True
    return result


__all__ = [
    "ImageType",
    "SUPPORTED_IMAGE_TYPES",
    "StructuredImageResult",
    "build_structured_prompt",
    "parse_structured",
]
