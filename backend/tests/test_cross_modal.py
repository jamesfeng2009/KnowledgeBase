"""
P2-Step3: 跨模态向量检索测试 — MultimodalEmbedder + CrossModalService。

覆盖：
    - JinaCLIPEmbedder 文本/图片向量化
    - CrossModalService embed_and_store_images
    - 降级逻辑（未启用、无 API Key、Embedder 不可用）
    - 空输入处理
    - 向量维度一致性
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.cross_modal_service import CrossModalService


# ------------------------------------------------------------------
# JinaCLIPEmbedder 测试
# ------------------------------------------------------------------


class TestJinaCLIPEmbedder:
    """JinaCLIP v2 跨模态 Embedder 测试。"""

    def test_dim_matches_config(self) -> None:
        """维度应从配置获取，默认 1024。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        assert embedder.dim == 1024

    def test_model_name(self) -> None:
        """模型名应为 jina-clip-v2。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        assert "clip" in embedder.model.lower()

    @pytest.mark.asyncio
    async def test_embed_empty_texts_returns_empty(self) -> None:
        """空文本列表应返回空列表。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        result = await embedder.embed([])
        assert result == []
        await embedder.close()

    @pytest.mark.asyncio
    async def test_embed_images_empty_returns_empty(self) -> None:
        """空图片列表应返回空列表。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        result = await embedder.embed_images([])
        assert result == []
        await embedder.close()

    @pytest.mark.asyncio
    async def test_embed_text_success(self) -> None:
        """文本向量化成功 — mock API 返回。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        # Mock HTTP response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"embedding": [0.1] * 1024},
                {"embedding": [0.2] * 1024},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        embedder._http = AsyncMock()
        embedder._http.post = AsyncMock(return_value=mock_response)

        result = await embedder.embed(["hello", "world"])
        assert len(result) == 2
        assert len(result[0]) == 1024
        assert result[0][0] == 0.1
        await embedder.close()

    @pytest.mark.asyncio
    async def test_embed_images_success(self) -> None:
        """图片向量化成功 — mock API 返回。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"embedding": [0.3] * 1024},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        embedder._http = AsyncMock()
        embedder._http.post = AsyncMock(return_value=mock_response)

        result = await embedder.embed_images([b"fake-image-bytes"])
        assert len(result) == 1
        assert len(result[0]) == 1024
        assert result[0][0] == 0.3
        await embedder.close()

    @pytest.mark.asyncio
    async def test_embed_images_and_text_same_dimension(self) -> None:
        """文本和图片向量维度一致 — 同一嵌入空间。"""
        from app.llm.multimodal_embedder import JinaCLIPEmbedder

        embedder = JinaCLIPEmbedder()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1] * 1024}]
        }
        mock_response.raise_for_status = MagicMock()
        embedder._http = AsyncMock()
        embedder._http.post = AsyncMock(return_value=mock_response)

        text_vec = await embedder.embed(["test"])
        image_vec = await embedder.embed_images([b"image"])

        assert len(text_vec[0]) == len(image_vec[0])
        await embedder.close()


# ------------------------------------------------------------------
# get_multimodal_embedder 测试
# ------------------------------------------------------------------


class TestGetMultimodalEmbedder:
    """get_multimodal_embedder 工厂函数测试。"""

    def test_returns_none_when_disabled(self) -> None:
        """CROSS_MODAL_ENABLED=False 时返回 None。"""
        from app.llm.multimodal_embedder import get_multimodal_embedder

        get_multimodal_embedder.cache_clear()
        with patch("app.llm.multimodal_embedder.settings") as mock_settings:
            mock_settings.CROSS_MODAL_ENABLED = False
            result = get_multimodal_embedder()
            assert result is None

    def test_returns_none_when_no_api_key(self) -> None:
        """CROSS_MODAL_ENABLED=True 但无 API Key 时返回 None。"""
        from app.llm.multimodal_embedder import get_multimodal_embedder

        get_multimodal_embedder.cache_clear()
        with patch("app.llm.multimodal_embedder.settings") as mock_settings:
            mock_settings.CROSS_MODAL_ENABLED = True
            mock_settings.JINA_API_KEY = ""
            result = get_multimodal_embedder()
            assert result is None

    def test_returns_embedder_when_enabled(self) -> None:
        """CROSS_MODAL_ENABLED=True 且有 API Key 时返回 Embedder。"""
        from app.llm.multimodal_embedder import get_multimodal_embedder

        get_multimodal_embedder.cache_clear()
        with patch("app.llm.multimodal_embedder.settings") as mock_settings:
            mock_settings.CROSS_MODAL_ENABLED = True
            mock_settings.JINA_API_KEY = "test-key"
            mock_settings.DEPLOY_MODE = "saas"
            mock_settings.JINA_CLIP_MODEL = "jina-clip-v2"
            mock_settings.JINA_CLIP_DIM = 1024
            result = get_multimodal_embedder()
            assert result is not None
            assert hasattr(result, "embed_images")


# ------------------------------------------------------------------
# CrossModalService 测试
# ------------------------------------------------------------------


class TestCrossModalService:
    """CrossModalService 跨模态服务测试。"""

    def test_is_enabled_false_by_default(self) -> None:
        """默认未启用跨模态检索。"""
        with patch("app.services.cross_modal_service.settings") as mock_settings:
            mock_settings.CROSS_MODAL_ENABLED = False
            mock_settings.JINA_API_KEY = ""
            service = CrossModalService()
            assert service.is_enabled() is False

    def test_is_enabled_true_when_configured(self) -> None:
        """配置正确时启用跨模态检索。"""
        with patch("app.services.cross_modal_service.settings") as mock_settings:
            mock_settings.CROSS_MODAL_ENABLED = True
            mock_settings.JINA_API_KEY = "test-key"
            service = CrossModalService()
            assert service.is_enabled() is True

    @pytest.mark.asyncio
    async def test_embed_and_store_empty_images(self) -> None:
        """空图片列表返回 0。"""
        service = CrossModalService()
        count = await service.embed_and_store_images("doc-1", "kb-1", [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_embed_and_store_embedder_unavailable(self) -> None:
        """Embedder 不可用时返回 0，不抛异常。"""
        service = CrossModalService()
        service._mm_embedder = None
        with patch(
            "app.services.cross_modal_service.CrossModalService._get_mm_embedder",
            return_value=None,
        ):
            count = await service.embed_and_store_images(
                "doc-1", "kb-1", [(b"img", "desc")]
            )
            assert count == 0

    @pytest.mark.asyncio
    async def test_embed_and_store_success(self) -> None:
        """成功向量化并入库图片。"""
        service = CrossModalService()

        # Mock embedder
        mock_embedder = AsyncMock()
        mock_embedder.embed_images = AsyncMock(
            return_value=[[0.1] * 1024, [0.2] * 1024]
        )
        service._mm_embedder = mock_embedder

        # Mock vector store
        mock_store = AsyncMock()
        mock_store.upsert = AsyncMock(return_value=2)
        service._vector_store = mock_store

        images = [
            (b"img1", "流程图描述"),
            (b"img2", "架构图描述"),
        ]
        count = await service.embed_and_store_images("doc-1", "kb-1", images)

        assert count == 2
        mock_embedder.embed_images.assert_called_once()
        mock_store.upsert.assert_called_once()

        # 验证传入 upsert 的 Chunk 有正确的 content_type
        call_args = mock_store.upsert.call_args
        chunks = call_args[0][1]
        assert len(chunks) == 2
        assert all(c.content_type == "image" for c in chunks)
        assert chunks[0].content == "流程图描述"
        assert chunks[1].content == "架构图描述"

    @pytest.mark.asyncio
    async def test_embed_and_store_embed_failure(self) -> None:
        """向量化失败时返回 0，不抛异常。"""
        service = CrossModalService()

        mock_embedder = AsyncMock()
        mock_embedder.embed_images = AsyncMock(side_effect=Exception("API error"))
        service._mm_embedder = mock_embedder

        count = await service.embed_and_store_images(
            "doc-1", "kb-1", [(b"img", "desc")]
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_embed_and_store_no_description(self) -> None:
        """无 VLM 描述时使用默认占位文本。"""
        service = CrossModalService()

        mock_embedder = AsyncMock()
        mock_embedder.embed_images = AsyncMock(return_value=[[0.1] * 1024])
        service._mm_embedder = mock_embedder

        mock_store = AsyncMock()
        mock_store.upsert = AsyncMock(return_value=1)
        service._vector_store = mock_store

        count = await service.embed_and_store_images("doc-1", "kb-1", [(b"img", "")])
        assert count == 1

        call_args = mock_store.upsert.call_args
        chunks = call_args[0][1]
        assert chunks[0].content == "[图片内容]"
