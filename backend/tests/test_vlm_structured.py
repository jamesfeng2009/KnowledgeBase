"""
P0-5 VLM 结构化输出约束 + 校验层 + prompt 路由 单元测试。

覆盖：
    - build_structured_prompt：按图片类型路由、未知类型回退
    - parse_structured：合法 JSON、markdown 包裹、非法 JSON、状态枚举、
      数值范围校验（百分比越界 / 图纸负尺寸 / confidence 越界）
    - 核心验证场景：含越界数值的图纸/图表输出被标记 low_confidence，
      而非作为正常结果直接入库
    - VisionProvider.understand_structured / MultimodalService.process_image_typed
"""

import json

import pytest

from app.services.multimodal_service import MultimodalService
from app.vlm.provider import VisionProvider
from app.vlm.structured import (
    SUPPORTED_IMAGE_TYPES,
    build_structured_prompt,
    parse_structured,
)


def _make_json(**kwargs) -> str:
    base = {"status": "ok", "description": "测试描述", "tags": ["图"], "numbers": []}
    base.update(kwargs)
    return json.dumps(base, ensure_ascii=False)


# ============================================================
# build_structured_prompt — prompt 路由
# ============================================================

class TestBuildStructuredPrompt:
    """按图片类型路由 prompt。"""

    def test_all_supported_types_have_prompt(self):
        for image_type in SUPPORTED_IMAGE_TYPES:
            prompt = build_structured_prompt(image_type)
            assert prompt
            assert "JSON" in prompt  # 统一结构化输出约束

    def test_type_specific_prompts_differ(self):
        """图纸 / 手写批注 / 数据图表应路由到不同 prompt。"""
        drawing = build_structured_prompt("drawing")
        handwriting = build_structured_prompt("handwriting")
        chart = build_structured_prompt("chart")
        assert "图纸" in drawing
        assert "手写" in handwriting
        assert "图表" in chart
        assert len({drawing, handwriting, chart}) == 3

    def test_unknown_type_falls_back_to_general(self):
        assert build_structured_prompt("unknown") == build_structured_prompt("general")


# ============================================================
# parse_structured — schema 校验 + 数值范围校验
# ============================================================

class TestParseStructured:
    """结构化输出解析与校验。"""

    def test_valid_output_not_low_confidence(self):
        result = parse_structured(
            _make_json(
                numbers=[{"label": "占比", "value": 85.0, "unit": "%", "confidence": 0.9}]
            ),
            image_type="chart",
        )
        assert result.status == "ok"
        assert result.low_confidence is False
        assert result.issues == []
        assert result.numbers[0]["value"] == 85.0

    def test_markdown_wrapped_json(self):
        text = f"```json\n{_make_json()}\n```"
        result = parse_structured(text)
        assert result.status == "ok"
        assert result.low_confidence is False

    def test_json_with_surrounding_text(self):
        text = f"这是分析结果：{_make_json()} 以上。"
        result = parse_structured(text)
        assert result.status == "ok"

    def test_invalid_json_marked_low_confidence(self):
        """自由文本（非 JSON）→ parse_error + low_confidence，不直接入库。"""
        result = parse_structured("这张图好像是个表格，大概有 100% 的增长")
        assert result.status == "parse_error"
        assert result.low_confidence is True
        assert "输出非合法 JSON" in result.issues

    def test_invalid_status_enum(self):
        result = parse_structured(_make_json(status="maybe"))
        assert result.status == "unclear"
        assert result.low_confidence is True
        assert any("status" in i for i in result.issues)

    def test_unclear_status_low_confidence(self):
        """模型自报不确定 → 低置信度。"""
        result = parse_structured(_make_json(status="unclear"))
        assert result.low_confidence is True

    def test_chart_percentage_out_of_range(self):
        """核心验证场景：图表数值 250% 越界 → low_confidence + issue。"""
        result = parse_structured(
            _make_json(
                numbers=[{"label": "增长率", "value": 250.0, "unit": "%", "confidence": 0.9}]
            ),
            image_type="chart",
        )
        assert result.low_confidence is True
        assert any("百分比越界" in i for i in result.issues)

    def test_chart_percentage_boundary_ok(self):
        for v in (0.0, 100.0):
            result = parse_structured(
                _make_json(numbers=[{"label": "x", "value": v, "unit": "%"}]),
                image_type="chart",
            )
            assert result.low_confidence is False, f"value={v} 应在界内"

    def test_drawing_negative_dimension(self):
        """图纸负尺寸 → low_confidence。"""
        result = parse_structured(
            _make_json(numbers=[{"label": "轴径", "value": -5.0, "unit": "mm"}]),
            image_type="drawing",
        )
        assert result.low_confidence is True
        assert any("非正数" in i for i in result.issues)

    def test_drawing_huge_dimension_out_of_range(self):
        result = parse_structured(
            _make_json(numbers=[{"label": "总长", "value": 9e9, "unit": "mm"}]),
            image_type="drawing",
        )
        assert result.low_confidence is True
        assert any("超量程" in i for i in result.issues)

    def test_confidence_out_of_range(self):
        result = parse_structured(
            _make_json(numbers=[{"label": "x", "value": 10, "confidence": 1.5}]),
        )
        assert result.low_confidence is True
        assert any("confidence 越界" in i for i in result.issues)

    def test_number_item_missing_value_dropped(self):
        result = parse_structured(
            _make_json(numbers=[{"label": "无数值"}]),
        )
        assert result.numbers == []
        assert result.low_confidence is True
        assert any("value" in i for i in result.issues)

    def test_percentage_rule_only_for_chart(self):
        """百分比规则仅 chart 类型生效（general 类型不误伤）。"""
        result = parse_structured(
            _make_json(numbers=[{"label": "页码", "value": 250, "unit": "%"}]),
            image_type="general",
        )
        assert result.low_confidence is False


# ============================================================
# VisionProvider.understand_structured
# ============================================================

class _MockStructuredVLM(VisionProvider):
    def __init__(self, response: str):
        self._response = response
        self.last_prompt: str | None = None

    async def understand(self, image: bytes, prompt: str, mime_type: str = "image/png") -> str:
        self.last_prompt = prompt
        return self._response


class TestUnderstandStructured:
    """Provider 基类结构化模式（子类零改动生效）。"""

    @pytest.mark.asyncio
    async def test_returns_structured_dict(self):
        vlm = _MockStructuredVLM(_make_json(description="架构图"))
        result = await vlm.understand_structured(b"img", image_type="chart")
        assert result["status"] == "ok"
        assert result["description"] == "架构图"
        assert result["low_confidence"] is False
        # prompt 已按 chart 类型路由
        assert "图表" in (vlm.last_prompt or "")

    @pytest.mark.asyncio
    async def test_hallucinated_output_flagged(self):
        """幻觉数值（250%）→ low_confidence=True，调用方可拦截入库。"""
        vlm = _MockStructuredVLM(
            _make_json(numbers=[{"label": "增长", "value": 250, "unit": "%"}])
        )
        result = await vlm.understand_structured(b"img", image_type="chart")
        assert result["low_confidence"] is True
        assert result["issues"]


# ============================================================
# MultimodalService.process_image_typed
# ============================================================

class _FailingVLM(VisionProvider):
    async def understand(self, image: bytes, prompt: str, mime_type: str = "image/png") -> str:
        raise RuntimeError("VLM 不可用")


class TestProcessImageTyped:
    @pytest.mark.asyncio
    async def test_valid_result(self):
        service = MultimodalService(vlm=_MockStructuredVLM(_make_json()))
        result = await service.process_image_typed(b"img", image_type="drawing")
        assert result["status"] == "ok"
        assert result["low_confidence"] is False

    @pytest.mark.asyncio
    async def test_vlm_failure_graceful_degrade(self):
        """VLM 异常 → error 占位 + low_confidence，不阻塞流程。"""
        service = MultimodalService(vlm=_FailingVLM())
        result = await service.process_image_typed(b"img", image_type="chart")
        assert result["status"] == "error"
        assert result["low_confidence"] is True
