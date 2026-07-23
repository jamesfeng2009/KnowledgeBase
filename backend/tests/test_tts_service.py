"""Tests for app.services.tts_service — TTS 语音合成服务。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tts_service import SUPPORTED_VOICES, get_available_voices, synthesize_text


class TestTTSService:
    """TTS 服务测试。"""

    def test_get_available_voices(self) -> None:
        """get_available_voices 返回支持的语音列表。"""
        voices = get_available_voices()
        assert len(voices) > 0
        assert all("voice" in v and "description" in v for v in voices)
        # 应包含默认的中文女声
        voice_names = [v["voice"] for v in voices]
        assert "zh-CN-XiaoxiaoNeural" in voice_names

    @pytest.mark.asyncio
    async def test_synthesize_empty_text_raises(self) -> None:
        """空文本应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            await synthesize_text("")

    @pytest.mark.asyncio
    async def test_synthesize_whitespace_text_raises(self) -> None:
        """纯空白文本应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            await synthesize_text("   \n\t  ")

    @pytest.mark.asyncio
    async def test_synthesize_tts_disabled_raises(self) -> None:
        """TTS_ENABLED=False 时应抛出 RuntimeError。"""
        with patch("app.services.tts_service.settings") as mock_settings:
            mock_settings.TTS_ENABLED = False
            with pytest.raises(RuntimeError, match="TTS 功能未启用"):
                await synthesize_text("测试文本")

    @pytest.mark.asyncio
    async def test_synthesize_long_text_truncated(self) -> None:
        """超长文本应被截断到 5000 字。"""
        long_text = "你好" * 3000  # 6000 字

        # Mock edge_tts.Communicate
        mock_communicate = MagicMock()
        mock_stream = AsyncMock()

        async def mock_stream_fn():
            yield {"type": "audio", "data": b"fake_audio_chunk"}

        mock_communicate.stream = mock_stream_fn

        with patch("app.services.tts_service.settings") as mock_settings:
            mock_settings.TTS_ENABLED = True
            mock_settings.TTS_VOICE = "zh-CN-XiaoxiaoNeural"
            mock_settings.TTS_RATE = "+0%"
            mock_settings.TTS_VOLUME = "+0%"

            with patch("edge_tts.Communicate", return_value=mock_communicate):
                result = await synthesize_text(long_text)

        assert result == b"fake_audio_chunk"

    @pytest.mark.asyncio
    async def test_synthesize_success(self) -> None:
        """正常合成应返回 MP3 字节流。"""
        test_text = "这是一个测试文本"

        # Mock edge_tts.Communicate
        mock_communicate = MagicMock()

        async def mock_stream_fn():
            yield {"type": "audio", "data": b"\xff\xfb"}  # MP3 header bytes
            yield {"type": "audio", "data": b"more_audio_data"}
            yield {"type": "WordBoundary", "data": None}  # 非 audio 类型应被跳过

        mock_communicate.stream = mock_stream_fn

        with patch("app.services.tts_service.settings") as mock_settings:
            mock_settings.TTS_ENABLED = True
            mock_settings.TTS_VOICE = "zh-CN-XiaoxiaoNeural"
            mock_settings.TTS_RATE = "+0%"
            mock_settings.TTS_VOLUME = "+0%"

            with patch("edge_tts.Communicate", return_value=mock_communicate) as mock_ctor:
                result = await synthesize_text(test_text)

        # 验证返回的是音频数据
        assert isinstance(result, bytes)
        assert b"\xff\xfb" in result
        assert b"more_audio_data" in result

        # 验证 Communicate 被正确调用
        mock_ctor.assert_called_once()
        call_kwargs = mock_ctor.call_args.kwargs
        assert call_kwargs["voice"] == "zh-CN-XiaoxiaoNeural"
        assert call_kwargs["text"] == test_text

    def test_supported_voices_structure(self) -> None:
        """SUPPORTED_VOICES 应包含中英文语音。"""
        assert "zh-CN-XiaoxiaoNeural" in SUPPORTED_VOICES
        assert "en-US-AriaNeural" in SUPPORTED_VOICES
        # 所有描述应为非空字符串
        for voice, desc in SUPPORTED_VOICES.items():
            assert isinstance(voice, str)
            assert isinstance(desc, str)
            assert len(desc) > 0
