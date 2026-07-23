"""
偏好偏移检测器 — 检测用户回答风格偏好的变化。

纯规则检测，零 LLM Token，在 prepare_chat 中同步执行。

检测场景：
    - 简洁偏好："简单点" / "太长了" / "简洁"
    - 详细偏好："详细点" / "展开说" / "具体"
    - 语言偏好："用英文" / "用中文"
    - 代码偏好："不要代码" / "给代码"

设计要点：
    - 纯关键词匹配，无 LLM 调用，无延迟
    - 检测结果注入 system prompt，调整回答风格
    - 优雅降级：无需降级（纯规则，零外部依赖）

遵循单一职责：本模块只负责偏好检测，不做风格调整。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class PreferenceDriftResult:
    """偏好偏移检测结果。

    Attributes:
        has_preference_change: 是否检测到偏好变化。
        preference_type: 偏好类型 "concise" / "detailed" / "language" / "code"。
        new_value: 新偏好值 "concise" / "detailed" / "en" / "zh" / "no_code" / "with_code"。
        detected_from: 检测来源 "rule"。
    """

    has_preference_change: bool
    preference_type: str = ""
    new_value: str = ""
    detected_from: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "has_preference_change": self.has_preference_change,
            "preference_type": self.preference_type,
            "new_value": self.new_value,
            "detected_from": self.detected_from,
        }


class PreferenceDriftDetector:
    """偏好偏移检测器 — 纯规则，零 LLM Token。

    在 prepare_chat 中同步执行，检测结果注入 system prompt。

    使用方式::

        detector = PreferenceDriftDetector()
        result = detector.detect("回答简单点，不要代码")
        if result.has_preference_change:
            # 调整 system prompt 风格指令
            ...
    """

    #: 偏好关键词规则 — key 为 new_value，value 为关键词列表
    _PREFERENCE_RULES: dict[str, list[str]] = {
        "concise": ["简单点", "太长了", "简洁", "简短", "精简", "少说"],
        "detailed": ["详细点", "展开", "具体", "详尽", "多说", "详细说明"],
        "en": ["用英文", "in english", "answer in english", "用英语"],
        "zh": ["用中文", "用汉语", "answer in chinese"],
        "no_code": ["不要代码", "别给代码", "不用代码"],
        "with_code": ["给代码", "要代码", "带代码", "show code"],
    }

    #: new_value → preference_type 映射
    _PREFERENCE_TYPE_MAP: dict[str, str] = {
        "concise": "concise",
        "detailed": "detailed",
        "en": "language",
        "zh": "language",
        "no_code": "code",
        "with_code": "code",
    }

    def detect(
        self,
        query: str,
        current_preferences: dict[str, str] | None = None,
    ) -> PreferenceDriftResult:
        """纯规则检测 — 扫描查询中的偏好关键词。

        Args:
            query: 当前用户查询。
            current_preferences: 当前已有偏好（可选，用于避免重复检测）。

        Returns:
            PreferenceDriftResult: 偏好检测结果。
        """
        if not query or not query.strip():
            return PreferenceDriftResult(has_preference_change=False)

        query_lower = query.lower()

        for new_value, keywords in self._PREFERENCE_RULES.items():
            for kw in keywords:
                if kw in query_lower:
                    pref_type = self._PREFERENCE_TYPE_MAP.get(new_value, new_value)

                    # 如果已有相同偏好，不算变化
                    if current_preferences and current_preferences.get(pref_type) == new_value:
                        continue

                    log.info(
                        "preference_drift.detected",
                        preference_type=pref_type,
                        new_value=new_value,
                        keyword=kw,
                    )
                    return PreferenceDriftResult(
                        has_preference_change=True,
                        preference_type=pref_type,
                        new_value=new_value,
                        detected_from="rule",
                    )

        return PreferenceDriftResult(has_preference_change=False)

    def get_system_prompt_modifier(self, result: PreferenceDriftResult) -> str:
        """根据检测结果生成 system prompt 补充指令。

        Args:
            result: 偏好检测结果。

        Returns:
            补充指令文本，无偏好变化时返回空字符串。
        """
        if not result.has_preference_change:
            return ""

        modifiers: dict[str, str] = {
            "concise": "请用简洁的方式回答，避免冗长。",
            "detailed": "请详细展开回答，提供充分的信息。",
            "en": "Please answer in English.",
            "zh": "请用中文回答。",
            "no_code": "回答中不要包含代码示例。",
            "with_code": "请在回答中提供代码示例。",
        }
        return modifiers.get(result.new_value, "")
