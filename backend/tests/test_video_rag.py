"""视频 RAG 测试 — 验证 ASR Provider、视频处理器、视频分块、document_tasks 集成。

覆盖：
- TranscribeSegment：数据类、时间戳格式化
- ASRProvider：抽象类、注册表、工厂
- OpenAIASRProvider / WhisperASRProvider：优雅降级
- VideoProcessor：extract_audio / extract_keyframes（Mock ffmpeg）
- SemanticChunker.chunk_video_transcript：时间窗口合并、关键帧对齐、空输入
- document_tasks：_parse_video、_chunk_video_document、_extract_keyframe_descriptions
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

if "opensearchpy" not in sys.modules:
    sys.modules["opensearchpy"] = MagicMock()

if "pymilvus" not in sys.modules:
    sys.modules["pymilvus"] = MagicMock()


# ======================================================================
# TranscribeSegment 测试
# ======================================================================


class TestTranscribeSegment:
    """TranscribeSegment 数据类测试。"""

    def test_to_dict(self) -> None:
        """to_dict 返回正确字典。"""
        from app.asr.provider import TranscribeSegment

        seg = TranscribeSegment(start=10.5, end=25.3, text="测试文本")
        d = seg.to_dict()
        assert d["start"] == 10.5
        assert d["end"] == 25.3
        assert d["text"] == "测试文本"

    def test_timestamp_label(self) -> None:
        """timestamp_label 格式为 MM:SS。"""
        from app.asr.provider import TranscribeSegment

        assert TranscribeSegment(0, 10, "a").timestamp_label == "00:00"
        assert TranscribeSegment(65, 80, "b").timestamp_label == "01:05"
        assert TranscribeSegment(125, 140, "c").timestamp_label == "02:05"


# ======================================================================
# ASR Provider 测试
# ======================================================================


class TestASRProvider:
    """ASR Provider 抽象层测试。"""

    def test_cannot_instantiate_abstract(self) -> None:
        """ASRProvider 是抽象类。"""
        from app.asr.provider import ASRProvider

        with pytest.raises(TypeError):
            ASRProvider()  # type: ignore[abstract]

    def test_registry_has_saas_and_private(self) -> None:
        """注册表包含 SaaS 和私有部署。"""
        from app.asr.provider import _asr_registry

        assert "saas" in _asr_registry
        assert "private_overseas" in _asr_registry
        assert "private_domestic" in _asr_registry

    def test_factory_returns_correct_provider(self) -> None:
        """工厂按 DEPLOY_MODE 返回正确实现。"""
        from app.asr.provider import (
            OpenAIASRProvider,
            WhisperASRProvider,
            get_asr_provider,
            reset_asr_cache,
        )

        reset_asr_cache()

        with patch("app.asr.provider.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(DEPLOY_MODE="saas")
            provider = get_asr_provider()
            assert isinstance(provider, OpenAIASRProvider)

        reset_asr_cache()

        with patch("app.asr.provider.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(DEPLOY_MODE="private_domestic")
            provider = get_asr_provider()
            assert isinstance(provider, WhisperASRProvider)

        reset_asr_cache()

    def test_factory_raises_on_invalid_mode(self) -> None:
        """不支持的 DEPLOY_MODE 抛出 ValueError。"""
        from app.asr.provider import get_asr_provider, reset_asr_cache

        reset_asr_cache()

        with patch("app.asr.provider.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(DEPLOY_MODE="invalid")
            with pytest.raises(ValueError, match="不支持的 DEPLOY_MODE"):
                get_asr_provider()

        reset_asr_cache()


class TestOpenAIASRProvider:
    """OpenAI ASR Provider 测试。"""

    @pytest.mark.asyncio
    async def test_transcribe_returns_segments(self) -> None:
        """成功转写返回片段列表。"""
        from app.asr.provider import OpenAIASRProvider

        provider = OpenAIASRProvider()

        mock_response = MagicMock()
        mock_response.segments = [
            {"start": 0.0, "end": 5.0, "text": "你好"},
            {"start": 5.0, "end": 10.0, "text": "世界"},
        ]

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

        with patch("builtins.open", MagicMock()), \
             patch("app.asr.provider.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(OPENAI_API_KEY="test")
            with patch("openai.AsyncOpenAI", return_value=mock_client):
                segments = await provider.transcribe("/fake/audio.wav", language="zh")

        assert len(segments) == 2
        assert segments[0].text == "你好"
        assert segments[1].text == "世界"

    @pytest.mark.asyncio
    async def test_transcribe_returns_empty_on_error(self) -> None:
        """转写失败返回空列表。"""
        from app.asr.provider import OpenAIASRProvider

        provider = OpenAIASRProvider()

        with patch("builtins.open", side_effect=FileNotFoundError):
            segments = await provider.transcribe("/nonexistent.wav")

        assert segments == []


class TestWhisperASRProvider:
    """Whisper ASR Provider（私有部署）测试。"""

    @pytest.mark.asyncio
    async def test_transcribe_returns_segments(self) -> None:
        """成功转写返回片段列表。"""
        from app.asr.provider import WhisperASRProvider

        provider = WhisperASRProvider()

        mock_response_data = {
            "segments": [
                {"start": 0.0, "end": 3.0, "text": "测试"},
                {"start": 3.0, "end": 6.0, "text": "转写"},
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("builtins.open", MagicMock()), \
             patch("app.asr.provider.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(ASR_HOST="asr", ASR_PORT="8005")
            with patch("httpx.AsyncClient", return_value=mock_client):
                segments = await provider.transcribe("/fake/audio.wav")

        assert len(segments) == 2
        assert segments[0].text == "测试"

    @pytest.mark.asyncio
    async def test_transcribe_returns_empty_on_error(self) -> None:
        """转写失败返回空列表。"""
        from app.asr.provider import WhisperASRProvider

        provider = WhisperASRProvider()

        with patch("builtins.open", side_effect=FileNotFoundError):
            segments = await provider.transcribe("/nonexistent.wav")

        assert segments == []


# ======================================================================
# VideoProcessor 测试
# ======================================================================


class TestVideoProcessor:
    """视频处理器测试。"""

    @pytest.mark.asyncio
    async def test_extract_audio_no_file(self) -> None:
        """文件不存在时返回空字符串。"""
        from app.video.processor import VideoProcessor

        processor = VideoProcessor()
        result = await processor.extract_audio("/nonexistent.mp4")
        assert result == ""

    @pytest.mark.asyncio
    async def test_extract_audio_empty_path(self) -> None:
        """空路径返回空字符串。"""
        from app.video.processor import VideoProcessor

        processor = VideoProcessor()
        result = await processor.extract_audio("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_extract_keyframes_no_file(self) -> None:
        """文件不存在时返回空列表。"""
        from app.video.processor import VideoProcessor

        processor = VideoProcessor()
        result = await processor.extract_keyframes("/nonexistent.mp4")
        assert result == []

    @pytest.mark.asyncio
    async def test_extract_keyframes_disabled(self) -> None:
        """配置禁用时返回空列表。"""
        from app.video.processor import VideoProcessor

        processor = VideoProcessor()
        with patch("app.video.processor.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VIDEO_KEYFRAME_ENABLED=False)
            result = await processor.extract_keyframes("/fake.mp4")
        assert result == []

    def test_keyframe_timestamp_label(self) -> None:
        """KeyFrame.timestamp_label 格式正确。"""
        from app.video.processor import KeyFrame

        kf = KeyFrame(timestamp=65.0, image_path="/tmp/frame.png")
        assert kf.timestamp_label == "01:05"


# ======================================================================
# SemanticChunker.chunk_video_transcript 测试
# ======================================================================


class TestChunkVideoTranscript:
    """视频转写分块测试。"""

    def test_empty_segments_returns_empty(self) -> None:
        """空转写片段返回空列表。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        chunks = chunker.chunk_video_transcript([])
        assert chunks == []

    def test_single_segment_returns_one_chunk(self) -> None:
        """单个片段返回一个 chunk。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [{"start": 0.0, "end": 10.0, "text": "这是一段测试文本。"}]
        chunks = chunker.chunk_video_transcript(segments)

        assert len(chunks) == 1
        assert "测试文本" in chunks[0].content
        assert chunks[0].content_type == "video"
        assert chunks[0].chunk_strategy == "video_semantic"
        assert "00:00" in chunks[0].title_path

    def test_multiple_segments_within_window(self) -> None:
        """窗口内的多个片段合并为一个 chunk。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [
            {"start": 0.0, "end": 30.0, "text": "第一段。"},
            {"start": 30.0, "end": 60.0, "text": "第二段。"},
            {"start": 60.0, "end": 90.0, "text": "第三段。"},
        ]
        chunks = chunker.chunk_video_transcript(segments)

        assert len(chunks) == 1
        assert "第一段" in chunks[0].content
        assert "第二段" in chunks[0].content
        assert "第三段" in chunks[0].content

    def test_segments_across_windows(self) -> None:
        """跨窗口的片段被分为多个 chunk。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [
            {"start": 0.0, "end": 60.0, "text": "窗口一内容。"},
            {"start": 60.0, "end": 120.0, "text": "窗口一尾部。"},
            {"start": 120.0, "end": 180.0, "text": "窗口二内容。"},
        ]
        chunks = chunker.chunk_video_transcript(segments)

        assert len(chunks) >= 2

    def test_keyframe_descriptions_appended(self) -> None:
        """关键帧 VLM 描述被追加到 chunk。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [
            {"start": 0.0, "end": 30.0, "text": "讲解架构。"},
            {"start": 30.0, "end": 60.0, "text": "展示图表。"},
        ]
        keyframes = [
            {"timestamp": 15.0, "description": "幻灯片显示三层架构图"},
            {"timestamp": 45.0, "description": "展示数据流向图"},
        ]
        chunks = chunker.chunk_video_transcript(segments, keyframes)

        assert len(chunks) == 1
        assert "三层架构图" in chunks[0].content
        assert "数据流向图" in chunks[0].content
        assert "视觉描述" in chunks[0].content

    def test_empty_text_segments_skipped(self) -> None:
        """空文本片段被跳过。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [
            {"start": 0.0, "end": 5.0, "text": ""},
            {"start": 5.0, "end": 10.0, "text": "有效文本。"},
            {"start": 10.0, "end": 15.0, "text": "   "},
        ]
        chunks = chunker.chunk_video_transcript(segments)

        assert len(chunks) == 1
        assert "有效文本" in chunks[0].content

    def test_title_path_contains_timestamp_range(self) -> None:
        """title_path 包含时间范围。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [{"start": 0.0, "end": 60.0, "text": "内容。"}]
        chunks = chunker.chunk_video_transcript(segments)

        assert len(chunks) == 1
        assert "-" in chunks[0].title_path
        assert "00:00" in chunks[0].title_path

    def test_chunk_has_correct_content_type_and_strategy(self) -> None:
        """chunk 的 content_type 和 chunk_strategy 正确。"""
        from app.rag.chunker import SemanticChunker

        chunker = SemanticChunker()
        segments = [{"start": 0.0, "end": 10.0, "text": "测试。"}]
        chunks = chunker.chunk_video_transcript(segments)

        assert chunks[0].content_type == "video"
        assert chunks[0].chunk_strategy == "video_semantic"


# ======================================================================
# document_tasks 视频处理集成测试
# ======================================================================


class TestDocumentTasksVideo:
    """document_tasks 视频处理集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_video_no_file_path(self) -> None:
        """无 file_path 时返回 content_text。"""
        from tasks.document_tasks import _parse_video

        mock_doc = MagicMock()
        mock_doc.file_path = None
        mock_doc.content_text = "已有内容"
        mock_doc.id = None

        result = await _parse_video(mock_doc)
        assert result == "已有内容"

    @pytest.mark.asyncio
    async def test_parse_video_asr_success(self) -> None:
        """ASR 转写成功返回文本。"""
        from tasks.document_tasks import _parse_video
        from app.asr.provider import TranscribeSegment

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/video.mp4"
        mock_doc.content_text = ""
        mock_doc.id = "test-id"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="/tmp/audio.wav")

        mock_asr = MagicMock()
        mock_asr.transcribe = AsyncMock(
            return_value=[
                TranscribeSegment(start=0, end=5, text="你好"),
                TranscribeSegment(start=5, end=10, text="世界"),
            ]
        )

        with patch("app.video.get_video_processor", return_value=mock_processor), \
             patch("app.asr.get_asr_provider", return_value=mock_asr), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"):
            result = await _parse_video(mock_doc)

        assert "你好" in result
        assert "世界" in result

    @pytest.mark.asyncio
    async def test_parse_video_asr_failure_returns_content_text(self) -> None:
        """ASR 失败时返回 content_text。"""
        from tasks.document_tasks import _parse_video

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/video.mp4"
        mock_doc.content_text = "降级内容"
        mock_doc.id = "test-id"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="")

        with patch("app.video.get_video_processor", return_value=mock_processor):
            result = await _parse_video(mock_doc)

        assert result == "降级内容"

    @pytest.mark.asyncio
    async def test_chunk_video_document_fallback_to_text(self) -> None:
        """ASR 不可用时降级为普通文本分块。"""
        from tasks.document_tasks import _chunk_video_document

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/video.mp4"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="")

        with patch("app.video.get_video_processor", return_value=mock_processor):
            chunks = await _chunk_video_document(mock_doc, "这是一段足够长的测试文本内容，用于验证分块功能。" * 5)

        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_chunk_video_document_with_segments(self) -> None:
        """有 ASR 片段时走视频分块。"""
        from tasks.document_tasks import _chunk_video_document
        from app.asr.provider import TranscribeSegment

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/video.mp4"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="/tmp/audio.wav")

        mock_asr = MagicMock()
        mock_asr.transcribe = AsyncMock(
            return_value=[
                TranscribeSegment(start=0, end=30, text="第一段内容。"),
                TranscribeSegment(start=30, end=60, text="第二段内容。"),
            ]
        )

        with patch("app.video.get_video_processor", return_value=mock_processor), \
             patch("app.asr.get_asr_provider", return_value=mock_asr), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch("tasks.document_tasks._extract_keyframe_descriptions", new_callable=AsyncMock, return_value=[]):
            chunks = await _chunk_video_document(mock_doc, "transcript text")

        assert len(chunks) >= 1
        assert chunks[0].content_type == "video"
        assert chunks[0].chunk_strategy == "video_semantic"

    @pytest.mark.asyncio
    async def test_extract_keyframe_descriptions_no_path(self) -> None:
        """空路径返回空列表。"""
        from tasks.document_tasks import _extract_keyframe_descriptions

        result = await _extract_keyframe_descriptions("")
        assert result == []

    @pytest.mark.asyncio
    async def test_extract_keyframe_descriptions_success(self) -> None:
        """成功提取关键帧描述。"""
        import tempfile
        import os
        from tasks.document_tasks import _extract_keyframe_descriptions
        from app.video.processor import KeyFrame

        # 创建真实的临时图片文件 — Phase 4 修复后代码会读取文件 bytes
        tmpdir = tempfile.mkdtemp()
        frame1_path = os.path.join(tmpdir, "frame1.png")
        frame2_path = os.path.join(tmpdir, "frame2.png")
        with open(frame1_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with open(frame2_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        try:
            mock_processor = MagicMock()
            mock_processor.extract_keyframes = AsyncMock(
                return_value=[
                    KeyFrame(timestamp=10.0, image_path=frame1_path),
                    KeyFrame(timestamp=30.0, image_path=frame2_path),
                ]
            )

            mock_vlm = MagicMock()
            mock_vlm.understand = AsyncMock(
                side_effect=["架构图描述", "流程图描述"]
            )

            with patch("app.video.get_video_processor", return_value=mock_processor), \
                 patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
                result = await _extract_keyframe_descriptions("/fake/video.mp4")

            assert len(result) == 2
            assert result[0]["timestamp"] == 10.0
            assert result[0]["description"] == "架构图描述"
            assert result[1]["timestamp"] == 30.0
            assert result[1]["description"] == "流程图描述"
            # 验证 VLM 接收到的是 bytes（Phase 4 修复验证）
            for call in mock_vlm.understand.call_args_list:
                assert isinstance(call.kwargs.get("image", call.args[0] if call.args else None), bytes)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_extract_keyframe_descriptions_vlm_failure(self) -> None:
        """VLM 失败时跳过该帧。"""
        import tempfile
        import os
        from tasks.document_tasks import _extract_keyframe_descriptions
        from app.video.processor import KeyFrame

        # 创建真实的临时图片文件
        tmpdir = tempfile.mkdtemp()
        frame1_path = os.path.join(tmpdir, "frame1.png")
        frame2_path = os.path.join(tmpdir, "frame2.png")
        with open(frame1_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with open(frame2_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        try:
            mock_processor = MagicMock()
            mock_processor.extract_keyframes = AsyncMock(
                return_value=[
                    KeyFrame(timestamp=10.0, image_path=frame1_path),
                    KeyFrame(timestamp=30.0, image_path=frame2_path),
                ]
            )

            mock_vlm = MagicMock()
            mock_vlm.understand = AsyncMock(
                side_effect=[Exception("VLM error"), "成功描述"]
            )

            with patch("app.video.get_video_processor", return_value=mock_processor), \
                 patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
                result = await _extract_keyframe_descriptions("/fake/video.mp4")

            assert len(result) == 1
            assert result[0]["description"] == "成功描述"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ======================================================================
# config.py 配置项测试
# ======================================================================


class TestVideoConfig:
    """视频处理配置项测试。"""

    def test_asr_config_exists(self) -> None:
        """ASR 配置项存在。"""
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "ASR_HOST")
        assert hasattr(settings, "ASR_PORT")
        assert hasattr(settings, "ASR_MODEL")

    def test_video_config_exists(self) -> None:
        """视频处理配置项存在。"""
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "VIDEO_KEYFRAME_ENABLED")
        assert hasattr(settings, "VIDEO_KEYFRAME_SCENE_THRESHOLD")
        assert hasattr(settings, "VIDEO_KEYFRAME_MAX_COUNT")

    def test_video_config_defaults(self) -> None:
        """视频处理配置默认值正确。"""
        from app.config import Settings

        settings = Settings()
        assert settings.VIDEO_KEYFRAME_ENABLED is True
        assert settings.VIDEO_KEYFRAME_SCENE_THRESHOLD == 0.3
        assert settings.VIDEO_KEYFRAME_MAX_COUNT == 100


# ======================================================================
# finalize_video_task 修复回归测试
# ======================================================================
# 原实现：调用 _chunk_video_document(doc, text, segments, keyframe_descriptions)
# （真实签名为 2 参 → TypeError）与不存在的 _embed_and_index（→ ImportError），
# 视频文档 finalize 必失败。修复后：直接复用 chord 产出的 segments/keyframes
# 调 SemanticChunker.chunk_video_transcript 分块（空则 _chunk_document 兜底），
# 向量化与索引走 _generate_embeddings + _build_indexes 真实签名。


class _CeleryTaskStub:
    """celery_app.task 装饰器桩 — 保留原函数使其可直接调用。"""

    @staticmethod
    def task(*_args, **_kwargs):
        def _decorator(fn):
            return fn

        return _decorator


def _load_video_tasks_module():
    """以受控方式加载真实的 tasks/video_tasks.py（celery_app 打桩）。

    套件中 celery_app 可能被注入 MagicMock；此处用桩 celery_app exec
    源码加载独立模块对象，拿到可调用的真实 finalize_video_task。
    """
    import types as _types
    from pathlib import Path

    stub_celery_app = _types.ModuleType("celery_app")
    stub_celery_app.celery_app = _CeleryTaskStub()

    path = Path(__file__).resolve().parent.parent / "tasks" / "video_tasks.py"
    source = path.read_text(encoding="utf-8")
    module = _types.ModuleType("ekb_test_video_tasks_finalize")
    module.__file__ = str(path)

    saved = sys.modules.get("celery_app")
    sys.modules["celery_app"] = stub_celery_app
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        if saved is not None:
            sys.modules["celery_app"] = saved
        else:
            sys.modules.pop("celery_app", None)
    return module


class TestFinalizeVideoTask:
    """finalize_video_task 对齐 document_tasks 真实函数签名（Bug 修复回归）。"""

    @pytest.fixture()
    def harness(self, monkeypatch):
        import types as _types
        from contextlib import asynccontextmanager

        module = _load_video_tasks_module()
        calls: dict[str, Any] = {
            "progress": [],
            "chunk": None,
            "fallback_chunk": None,
            "embed": None,
            "indexes": None,
        }

        # --- document_tasks 依赖打桩 ---
        import tasks.document_tasks as doc_tasks

        monkeypatch.setattr(
            doc_tasks,
            "_update_parse_progress",
            lambda *a, **k: calls["progress"].append((a, k)),
        )

        chunk_objects = [MagicMock(content="分块内容一"), MagicMock(content="分块内容二")]

        class _FakeChunker:
            def chunk_video_transcript(self, segments, keyframe_descriptions):
                calls["chunk"] = (segments, keyframe_descriptions)
                return list(chunk_objects)

        monkeypatch.setattr("app.rag.chunker.SemanticChunker", _FakeChunker)

        def _fake_chunk_document(text, doc_type="md", **_kwargs):
            calls["fallback_chunk"] = (text, doc_type)
            return [MagicMock(content="兜底分块")]

        monkeypatch.setattr(doc_tasks, "_chunk_document", _fake_chunk_document)

        async def _fake_generate_embeddings(chunks):
            calls["embed"] = chunks
            return [[0.1, 0.2], [0.3, 0.4]][: len(chunks)]

        async def _fake_build_indexes(doc_id, c_objects, chunks, embeddings):
            calls["indexes"] = (doc_id, c_objects, chunks, embeddings)

        monkeypatch.setattr(doc_tasks, "_generate_embeddings", _fake_generate_embeddings)
        monkeypatch.setattr(doc_tasks, "_build_indexes", _fake_build_indexes)

        # --- DB 打桩：task_db_session + DocumentRepository ---
        fake_db = AsyncMock()

        @asynccontextmanager
        async def _fake_session():
            yield fake_db

        monkeypatch.setattr("app.database.task_db_session", _fake_session)

        fake_doc = MagicMock()
        fake_doc.tenant_id = None
        fake_doc.file_path = "local:///tmp/fake.mp4"

        class _FakeRepo:
            def __init__(self, db, tenant_id=None):
                pass

            async def get_by_id(self, doc_uuid):
                return fake_doc

        monkeypatch.setattr(
            "app.repositories.knowledge_repository.DocumentRepository", _FakeRepo
        )

        # --- intelligence 链式触发打桩 ---
        intel_module = _types.ModuleType("tasks.intelligence_tasks")
        intel_module.process_intelligence = MagicMock()
        monkeypatch.setitem(sys.modules, "tasks.intelligence_tasks", intel_module)

        # --- redis 打桩（清理缓存分支快速失败，不影响主流程） ---
        monkeypatch.setattr(
            "redis.from_url", MagicMock(side_effect=Exception("no redis"))
        )

        return module, calls, chunk_objects

    def test_finalize_uses_real_signatures(self, harness):
        """分块/向量化/索引全部按真实签名调用，不再 TypeError/ImportError。"""
        module, calls, chunk_objects = harness
        segments = [{"start": 0.0, "end": 5.0, "text": "你好"}]
        keyframes = [{"timestamp": 1.0, "description": "画面"}]
        doc_id = "12345678-1234-5678-1234-567812345678"
        asr_result = {"doc_id": doc_id, "segments": segments, "text": "你好"}
        kf_result = {"doc_id": doc_id, "keyframes": keyframes}

        module.finalize_video_task([asr_result, kf_result], doc_id, "")

        # 分块：直接复用 chord 产出的 segments/keyframes
        assert calls["chunk"] == (segments, keyframes)
        # 不再触发兜底文本分块
        assert calls["fallback_chunk"] is None
        # 向量化：_generate_embeddings 收到 chunk 文本列表
        assert calls["embed"] == ["分块内容一", "分块内容二"]
        # 索引：_build_indexes(doc_id, chunk_objects, chunks, embeddings)
        idx = calls["indexes"]
        assert idx is not None
        assert idx[0] == doc_id
        assert idx[1] == chunk_objects
        assert idx[2] == ["分块内容一", "分块内容二"]
        assert idx[3] == [[0.1, 0.2], [0.3, 0.4]]
        # 完成进度
        stages = [a[1] for a, _k in calls["progress"] if len(a) > 1]
        assert "done" in stages

    def test_finalize_empty_chunks_fallback_to_text(self, harness, monkeypatch):
        """语义分块无结果时降级 _chunk_document(text, "txt") 兜底。"""
        module, calls, _chunk_objects = harness

        class _EmptyChunker:
            def chunk_video_transcript(self, segments, keyframe_descriptions):
                return []

        monkeypatch.setattr("app.rag.chunker.SemanticChunker", _EmptyChunker)

        doc_id = "12345678-1234-5678-1234-567812345678"
        asr_result = {"doc_id": doc_id, "segments": [{"start": 0, "end": 1, "text": "嗯"}], "text": "嗯"}
        module.finalize_video_task([asr_result], doc_id, "")

        assert calls["fallback_chunk"] == ("嗯", "txt")
        # 兜底分块照常走向量化 + 索引
        assert calls["embed"] == ["兜底分块"]
        assert calls["indexes"] is not None

    def test_finalize_no_segments_marks_failed(self, harness):
        """无 ASR 结果时标记失败并提前返回（不进入分块/索引）。"""
        module, calls, _chunk_objects = harness

        module.finalize_video_task([], "doc-empty", "")

        stages = [a[1] for a, _k in calls["progress"] if len(a) > 1]
        assert "failed" in stages
        assert calls["chunk"] is None
        assert calls["indexes"] is None
