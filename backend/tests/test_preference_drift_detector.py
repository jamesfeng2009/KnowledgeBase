"""PreferenceDriftDetector 测试 — 偏好偏移检测。"""

import pytest

from app.context.preference_drift_detector import (
    PreferenceDriftDetector,
    PreferenceDriftResult,
)


class TestPreferenceDriftResult:
    """PreferenceDriftResult 序列化测试。"""

    def test_to_dict_with_change(self):
        """有偏好变化时序列化。"""
        result = PreferenceDriftResult(
            has_preference_change=True,
            preference_type="concise",
            new_value="concise",
            detected_from="rule",
        )
        d = result.to_dict()
        assert d["has_preference_change"] is True
        assert d["preference_type"] == "concise"
        assert d["new_value"] == "concise"
        assert d["detected_from"] == "rule"

    def test_to_dict_no_change(self):
        """无偏好变化时序列化。"""
        result = PreferenceDriftResult(has_preference_change=False)
        d = result.to_dict()
        assert d["has_preference_change"] is False
        assert d["preference_type"] == ""
        assert d["new_value"] == ""


class TestPreferenceDriftDetection:
    """偏好检测测试。"""

    @pytest.fixture()
    def detector(self):
        return PreferenceDriftDetector()

    def test_concise(self, detector):
        """检测"简单点" → concise。"""
        result = detector.detect("回答简单点")
        assert result.has_preference_change is True
        assert result.preference_type == "concise"
        assert result.new_value == "concise"
        assert result.detected_from == "rule"

    def test_detailed(self, detector):
        """检测"详细点" → detailed。"""
        result = detector.detect("能更详细点吗")
        assert result.has_preference_change is True
        assert result.preference_type == "detailed"
        assert result.new_value == "detailed"

    def test_language_en(self, detector):
        """检测"用英文" → language / en。"""
        result = detector.detect("用英文回答")
        assert result.has_preference_change is True
        assert result.preference_type == "language"
        assert result.new_value == "en"

    def test_language_zh(self, detector):
        """检测"用中文" → language / zh。"""
        result = detector.detect("请用中文回答")
        assert result.has_preference_change is True
        assert result.preference_type == "language"
        assert result.new_value == "zh"

    def test_no_code(self, detector):
        """检测"不要代码" → code / no_code。"""
        result = detector.detect("不要代码示例")
        assert result.has_preference_change is True
        assert result.preference_type == "code"
        assert result.new_value == "no_code"

    def test_with_code(self, detector):
        """检测"给代码" → code / with_code。"""
        result = detector.detect("给代码看看")
        assert result.has_preference_change is True
        assert result.preference_type == "code"
        assert result.new_value == "with_code"

    def test_no_preference(self, detector):
        """无偏好关键词 → no change。"""
        result = detector.detect("北京今天限号吗？")
        assert result.has_preference_change is False
        assert result.preference_type == ""
        assert result.new_value == ""

    def test_empty_query(self, detector):
        """空查询 → no change。"""
        result = detector.detect("")
        assert result.has_preference_change is False

    def test_case_insensitive_english(self, detector):
        """英文关键词大小写不敏感。"""
        result = detector.detect("Please answer IN ENGLISH")
        assert result.has_preference_change is True
        assert result.new_value == "en"

    def test_skip_existing_preference(self, detector):
        """已有相同偏好时不报变化。"""
        result = detector.detect(
            "简单点",
            current_preferences={"concise": "concise"},
        )
        assert result.has_preference_change is False

    def test_different_existing_preference(self, detector):
        """已有不同偏好时报变化。"""
        result = detector.detect(
            "详细点",
            current_preferences={"concise": "concise"},
        )
        assert result.has_preference_change is True
        assert result.new_value == "detailed"


class TestSystemPromptModifier:
    """system prompt 补充指令测试。"""

    @pytest.fixture()
    def detector(self):
        return PreferenceDriftDetector()

    def test_concise_modifier(self, detector):
        """concise 偏好生成补充指令。"""
        result = PreferenceDriftResult(
            has_preference_change=True,
            preference_type="concise",
            new_value="concise",
        )
        modifier = detector.get_system_prompt_modifier(result)
        assert "简洁" in modifier

    def test_no_change_modifier(self, detector):
        """无偏好变化时返回空字符串。"""
        result = PreferenceDriftResult(has_preference_change=False)
        modifier = detector.get_system_prompt_modifier(result)
        assert modifier == ""

    def test_en_modifier(self, detector):
        """en 偏好生成英文指令。"""
        result = PreferenceDriftResult(
            has_preference_change=True,
            preference_type="language",
            new_value="en",
        )
        modifier = detector.get_system_prompt_modifier(result)
        assert "English" in modifier
