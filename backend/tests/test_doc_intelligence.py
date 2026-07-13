"""
文档智能处理测试 — 测试 JSON 解析、日期解析等纯逻辑。

不依赖 LLM / PostgreSQL 外部服务。
"""

import pytest

from app.services.doc_intelligence_service import DocIntelligenceService


class TestJsonParsing:
    """JSON 解析测试。"""

    def test_parse_valid_json(self):
        text = '[{"question": "什么是微服务", "answer": "一种架构模式"}]'
        result = DocIntelligenceService._parse_json(text, default=[])
        assert len(result) == 1
        assert result[0]["question"] == "什么是微服务"

    def test_parse_json_with_codeblock(self):
        text = '```json\n[{"question": "Q1", "answer": "A1"}]\n```'
        result = DocIntelligenceService._parse_json(text, default=[])
        assert len(result) == 1

    def test_parse_invalid_json(self):
        text = "not json at all"
        result = DocIntelligenceService._parse_json(text, default=[])
        assert result == []

    def test_parse_empty_string(self):
        result = DocIntelligenceService._parse_json("", default=[])
        assert result == []


class TestDateParsing:
    """日期解析测试。"""

    def test_valid_date(self):
        from datetime import date

        result = DocIntelligenceService._parse_date("2026-07-06")
        assert result == date(2026, 7, 6)

    def test_none_input(self):
        assert DocIntelligenceService._parse_date(None) is None

    def test_empty_string(self):
        assert DocIntelligenceService._parse_date("") is None

    def test_invalid_format(self):
        assert DocIntelligenceService._parse_date("2026/07/06") is None

    def test_invalid_date(self):
        assert DocIntelligenceService._parse_date("2026-13-45") is None


class TestUuidParsing:
    """UUID 解析测试。"""

    def test_valid_uuid(self):
        import uuid

        test_uuid = "550e8400-e29b-41d4-a716-446655440000"
        result = DocIntelligenceService._parse_uuid(test_uuid)
        assert isinstance(result, uuid.UUID)

    def test_invalid_uuid_raises(self):
        with pytest.raises(Exception):
            DocIntelligenceService._parse_uuid("not-a-uuid")
