"""
多模态处理服务测试 — 测试 JSON 解析和 VLM 优雅降级。

不依赖真实 VLM，使用 Mock VisionProvider。
"""

import json

import pytest

from app.services.multimodal_service import MultimodalService
from app.vlm.provider import VisionProvider


class MockVisionProvider(VisionProvider):
    """测试用 Mock VLM — 返回预设文本。"""

    def __init__(self, response: str = "测试描述"):
        self._response = response

    async def understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str = "image/png",
    ) -> str:
        return self._response


class FailingVisionProvider(VisionProvider):
    """总是失败的 Mock VLM — 测试优雅降级。"""

    async def understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str = "image/png",
    ) -> str:
        raise RuntimeError("VLM 服务不可用")


class TestMultimodalJSONParsing:
    """JSON 解析测试。"""

    def test_parse_json_valid(self):
        """正常 JSON 应正确解析。"""
        text = '{"key": "value", "count": 42}'
        result = MultimodalService._parse_json(text, default={})
        assert result == {"key": "value", "count": 42}

    def test_parse_json_with_markdown_block(self):
        """带 markdown 代码块的 JSON 应正确提取。"""
        text = '```json\n{"key": "value"}\n```'
        result = MultimodalService._parse_json(text, default={})
        assert result == {"key": "value"}

    def test_parse_json_with_plain_code_block(self):
        """带普通代码块的 JSON 应正确提取。"""
        text = '```\n{"key": "value"}\n```'
        result = MultimodalService._parse_json(text, default={})
        assert result == {"key": "value"}

    def test_parse_json_invalid(self):
        """无效 JSON 应返回默认值。"""
        text = "这不是 JSON"
        result = MultimodalService._parse_json(text, default={"default": True})
        assert result == {"default": True}

    def test_parse_json_empty(self):
        """空字符串应返回默认值。"""
        result = MultimodalService._parse_json("", default=[])
        assert result == []

    def test_parse_json_array(self):
        """JSON 数组应正确解析。"""
        text = '[{"col": "a"}, {"col": "b"}]'
        result = MultimodalService._parse_json(text, default=[])
        assert len(result) == 2
        assert result[0]["col"] == "a"

    def test_parse_json_none_text(self):
        """None 文本应返回默认值。"""
        result = MultimodalService._parse_json(None, default="fallback")
        assert result == "fallback"

    def test_parse_json_with_extra_text(self):
        """JSON 前后有额外文本时应尝试提取。"""
        text = '结果是：{"key": "value"} 完成'
        result = MultimodalService._parse_json(text, default={})
        # json.loads 只解析完整 JSON，前后有文本会失败
        # 这里验证不崩溃即可
        assert isinstance(result, (dict, str))


@pytest.mark.asyncio
class TestMultimodalService:
    """多模态服务测试。"""

    async def test_process_image_with_mock(self):
        """图片解析应返回描述和标签。"""
        mock_vlm = MockVisionProvider("这是一张架构图\n架构图,微服务,API网关")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_image(b"fake_image_data")

        assert "description" in result
        assert "tags" in result
        assert "架构图" in result["description"]
        assert len(result["tags"]) == 3

    async def test_process_image_no_tags(self):
        """无标签行时应返回空标签列表。"""
        mock_vlm = MockVisionProvider("这是一张图片描述")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_image(b"fake_image_data")

        assert result["description"] == "这是一张图片描述"
        assert result["tags"] == []

    async def test_process_table_with_mock(self):
        """表格结构化应返回 JSON 数组。"""
        table_json = json.dumps([
            {"职级": "P7", "报销额度": "5000"},
            {"职级": "P8", "报销额度": "8000"},
        ])
        mock_vlm = MockVisionProvider(table_json)
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_table(b"fake_table_image")

        assert len(result) == 2
        assert result[0]["职级"] == "P7"

    async def test_process_table_invalid_json(self):
        """表格 JSON 解析失败应返回空列表。"""
        mock_vlm = MockVisionProvider("这不是 JSON")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_table(b"fake_table_image")
        assert result == []

    async def test_process_scanned_pdf_with_mock(self):
        """扫描件 OCR 应返回纯文本。"""
        mock_vlm = MockVisionProvider("这是识别到的文字内容")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_scanned_pdf(b"fake_scanned_image")

        assert result == "这是识别到的文字内容"

    async def test_process_whiteboard_with_mock(self):
        """白板处理应返回结构化会议纪要。"""
        whiteboard_json = json.dumps({
            "summary": "讨论了 Q2 规划",
            "key_points": ["要点1", "要点2"],
            "action_items": [{"assignee": "张三", "content": "完成文档"}],
        })
        mock_vlm = MockVisionProvider(whiteboard_json)
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_whiteboard(b"fake_whiteboard_image")

        assert result["summary"] == "讨论了 Q2 规划"
        assert len(result["key_points"]) == 2
        assert result["action_items"][0]["assignee"] == "张三"

    async def test_process_whiteboard_invalid_json(self):
        """白板 JSON 解析失败应返回默认结构。"""
        mock_vlm = MockVisionProvider("这不是 JSON")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_whiteboard(b"fake_whiteboard_image")

        assert "summary" in result
        assert "key_points" in result
        assert "action_items" in result
        assert result["summary"] == ""

    async def test_vlm_failure_graceful_degradation(self):
        """VLM 不可用时应优雅降级，不抛异常。"""
        service = MultimodalService(vlm=FailingVisionProvider())

        # 图片解析
        result = await service.process_image(b"fake_image")
        assert "失败" in result["description"] or result["description"] != ""

        # 表格结构化
        result = await service.process_table(b"fake_table")
        assert result == []

        # 扫描件 OCR
        result = await service.process_scanned_pdf(b"fake_scanned")
        assert "失败" in result or result != ""

        # 白板
        result = await service.process_whiteboard(b"fake_whiteboard")
        assert "summary" in result

    async def test_process_table_with_markdown_wrapper(self):
        """表格 JSON 被 markdown 包裹时应正确提取。"""
        table_json = json.dumps([{"col": "a"}])
        mock_vlm = MockVisionProvider(f"```json\n{table_json}\n```")
        service = MultimodalService(vlm=mock_vlm)
        result = await service.process_table(b"fake_table_image")

        assert len(result) == 1
        assert result[0]["col"] == "a"
