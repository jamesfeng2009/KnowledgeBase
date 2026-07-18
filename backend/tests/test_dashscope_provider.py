"""DashScope Provider 测试 — 验证通义千问 LLM + Embedding 集成。

覆盖：
- DashScopeProvider：继承 VLLMProvider、初始化、模型解析
- DashScopeEmbedder：初始化、维度、embed 调用
- factory：saas_dashscope 模式注册和路由
- config：DASHSCOPE_* 配置项默认值
- is_saas：saas_dashscope 识别为 SaaS 模式
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# Config 配置项测试
# ======================================================================


class TestDashScopeConfig:
    """DashScope 配置项测试。"""

    def test_dashscope_api_key_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "DASHSCOPE_API_KEY")
        assert settings.DASHSCOPE_API_KEY == ""

    def test_dashscope_base_url_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert "dashscope.aliyuncs.com" in settings.DASHSCOPE_BASE_URL
        assert "compatible-mode" in settings.DASHSCOPE_BASE_URL

    def test_dashscope_llm_model_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.DASHSCOPE_LLM_MODEL == "qwen-turbo"

    def test_dashscope_embed_model_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.DASHSCOPE_EMBED_MODEL == "text-embedding-v3"

    def test_dashscope_embed_dim_default(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.DASHSCOPE_EMBED_DIM == 1024

    def test_deploy_mode_includes_saas_dashscope(self) -> None:
        """DEPLOY_MODE Literal 包含 saas_dashscope。"""
        from app.config import Settings
        from typing import get_type_hints

        hints = get_type_hints(Settings)
        deploy_mode_type = str(hints["DEPLOY_MODE"])
        assert "saas_dashscope" in deploy_mode_type

    def test_is_saas_includes_dashscope(self) -> None:
        """saas_dashscope 识别为 SaaS 模式。"""
        from app.config import Settings

        settings = Settings(DEPLOY_MODE="saas_dashscope")
        assert settings.is_saas is True

    def test_is_saas_dashscope_not_private(self) -> None:
        """saas_dashscope 不是私有部署模式。"""
        from app.config import Settings

        settings = Settings(DEPLOY_MODE="saas_dashscope")
        assert settings.is_private is False


# ======================================================================
# DashScopeProvider 测试
# ======================================================================


class TestDashScopeProvider:
    """DashScopeProvider LLM Provider 测试。"""

    def test_inherits_vllm_provider(self) -> None:
        """DashScopeProvider 继承 VLLMProvider。"""
        from app.llm.dashscope_provider import DashScopeProvider
        from app.llm.vllm_provider import VLLMProvider

        assert issubclass(DashScopeProvider, VLLMProvider)

    def test_init_with_default_model(self) -> None:
        """初始化使用默认模型（DASHSCOPE_LLM_MODEL）。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test-key"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider()
            assert provider.default_model == "qwen-turbo"

    def test_init_with_custom_model(self) -> None:
        """初始化支持自定义模型。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test-key"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider(model="qwen-plus")
            assert provider.default_model == "qwen-plus"

    def test_client_uses_dashscope_endpoint(self) -> None:
        """客户端使用 DashScope base_url。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test-key"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider()
            # AsyncOpenAI 内部存储 base_url
            assert "dashscope" in str(provider.client.base_url)

    def test_client_uses_dashscope_api_key(self) -> None:
        """客户端使用 DashScope API Key。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-my-secret-key"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider()
            assert provider.client.api_key == "sk-my-secret-key"

    @pytest.mark.asyncio
    async def test_chat_non_stream(self) -> None:
        """非流式 chat 返回文本。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider()

            # Mock OpenAI 响应
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "你好！我是通义千问。"
            mock_resp.choices[0].message.tool_calls = None

            provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

            messages = [{"role": "user", "content": "你好"}]
            results = []
            async for chunk in provider.chat(messages, stream=False):
                results.append(chunk)

            assert "通义千问" in results[0]

    @pytest.mark.asyncio
    async def test_chat_with_tool_use(self) -> None:
        """chat 支持 function calling（tool_use）。"""
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

            from app.llm.dashscope_provider import DashScopeProvider

            provider = DashScopeProvider()

            # Mock 带 tool_calls 的响应
            mock_tc = MagicMock()
            mock_tc.id = "call_001"
            mock_tc.function.name = "knowledge_search"
            mock_tc.function.arguments = '{"query": "Python"}'

            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = None
            mock_resp.choices[0].message.tool_calls = [mock_tc]

            provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

            tools = [{
                "name": "knowledge_search",
                "description": "搜索知识库",
                "parameters": {"type": "object", "properties": {}},
            }]
            messages = [{"role": "user", "content": "搜索 Python"}]

            results = []
            async for chunk in provider.chat(messages, tools=tools, stream=False):
                results.append(chunk)

            # 应该 yield 一个 ToolUse dict
            tool_use = [r for r in results if isinstance(r, dict)]
            assert len(tool_use) == 1
            assert tool_use[0]["type"] == "tool_use"
            assert tool_use[0]["name"] == "knowledge_search"
            assert tool_use[0]["input"]["query"] == "Python"


# ======================================================================
# DashScopeEmbedder 测试
# ======================================================================


class TestDashScopeEmbedder:
    """DashScopeEmbedder 向量化测试。"""

    def test_dim_default_1024(self) -> None:
        """默认维度 1024（text-embedding-v3）。"""
        with patch("app.llm.embedder.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_EMBED_MODEL = "text-embedding-v3"
            mock_settings.DASHSCOPE_EMBED_DIM = 1024

            from app.llm.embedder import DashScopeEmbedder

            embedder = DashScopeEmbedder()
            assert embedder.dim == 1024

    def test_model_default_v3(self) -> None:
        """默认模型 text-embedding-v3。"""
        with patch("app.llm.embedder.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_EMBED_MODEL = "text-embedding-v3"
            mock_settings.DASHSCOPE_EMBED_DIM = 1024

            from app.llm.embedder import DashScopeEmbedder

            embedder = DashScopeEmbedder()
            assert embedder.model == "text-embedding-v3"

    @pytest.mark.asyncio
    async def test_embed_returns_vectors(self) -> None:
        """embed 返回向量列表。"""
        with patch("app.llm.embedder.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_EMBED_MODEL = "text-embedding-v3"
            mock_settings.DASHSCOPE_EMBED_DIM = 1024

            from app.llm.embedder import DashScopeEmbedder

            embedder = DashScopeEmbedder()

            mock_resp = MagicMock()
            mock_resp.data = [
                MagicMock(embedding=[0.1] * 1024),
                MagicMock(embedding=[0.2] * 1024),
            ]
            embedder.client.embeddings.create = AsyncMock(return_value=mock_resp)

            result = await embedder.embed(["你好", "世界"])
            assert len(result) == 2
            assert len(result[0]) == 1024
            assert len(result[1]) == 1024

    @pytest.mark.asyncio
    async def test_embed_empty_input(self) -> None:
        """空输入返回空列表。"""
        with patch("app.llm.embedder.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_EMBED_MODEL = "text-embedding-v3"
            mock_settings.DASHSCOPE_EMBED_DIM = 1024

            from app.llm.embedder import DashScopeEmbedder

            embedder = DashScopeEmbedder()
            result = await embedder.embed([])
            assert result == []


# ======================================================================
# Factory 工厂注册测试
# ======================================================================


class TestFactoryDashScope:
    """Provider 工厂 saas_dashscope 注册测试。"""

    def test_dashscope_provider_registered(self) -> None:
        """saas_dashscope 已注册到 LLM 工厂。"""
        from app.llm.factory import _llm_provider_registry

        assert "saas_dashscope" in _llm_provider_registry

    def test_dashscope_embedder_registered(self) -> None:
        """saas_dashscope 已注册到 Embedder 工厂。"""
        from app.llm.embedder import _embedder_registry

        assert "saas_dashscope" in _embedder_registry

    def test_list_llm_providers_includes_dashscope(self) -> None:
        """list_llm_providers 包含 saas_dashscope。"""
        from app.llm.factory import list_llm_providers

        providers = list_llm_providers()
        assert "saas_dashscope" in providers

    def test_get_llm_provider_dashscope(self) -> None:
        """get_llm_provider 在 saas_dashscope 模式返回 DashScopeProvider。"""
        with patch("app.llm.factory.settings") as mock_settings:
            mock_settings.DEPLOY_MODE = "saas_dashscope"
            with patch("app.llm.dashscope_provider.settings") as mock_ds_settings:
                mock_ds_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
                mock_ds_settings.DASHSCOPE_API_KEY = "sk-test"
                mock_ds_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"

                from app.llm.dashscope_provider import DashScopeProvider
                from app.llm.factory import get_llm_provider

                # 清除 lru_cache
                get_llm_provider.cache_clear()
                provider = get_llm_provider()
                assert isinstance(provider, DashScopeProvider)
                get_llm_provider.cache_clear()

    def test_get_embedder_dashscope(self) -> None:
        """get_embedder 在 saas_dashscope 模式返回 DashScopeEmbedder。"""
        with patch("app.llm.embedder.settings") as mock_settings:
            mock_settings.DEPLOY_MODE = "saas_dashscope"
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_EMBED_MODEL = "text-embedding-v3"
            mock_settings.DASHSCOPE_EMBED_DIM = 1024

            from app.llm.embedder import DashScopeEmbedder, get_embedder

            get_embedder.cache_clear()
            embedder = get_embedder()
            assert isinstance(embedder, DashScopeEmbedder)
            get_embedder.cache_clear()


# ======================================================================
# 向后兼容性测试
# ======================================================================


class TestBackwardCompatibility:
    """验证新增 saas_dashscope 不影响既有模式。"""

    def test_saas_mode_still_works(self) -> None:
        """saas 模式仍返回 AnthropicProvider。"""
        with patch("app.llm.factory.settings") as mock_settings:
            mock_settings.DEPLOY_MODE = "saas"

            from app.llm.anthropic_provider import AnthropicProvider
            from app.llm.factory import get_llm_provider

            get_llm_provider.cache_clear()
            provider = get_llm_provider()
            assert isinstance(provider, AnthropicProvider)
            get_llm_provider.cache_clear()

    def test_private_modes_still_works(self) -> None:
        """private_overseas/private_domestic 仍返回 VLLMProvider。"""
        from app.llm.factory import _llm_provider_registry

        assert "private_overseas" in _llm_provider_registry
        assert "private_domestic" in _llm_provider_registry

    def test_all_four_modes_registered(self) -> None:
        """四种部署模式全部已注册。"""
        from app.llm.factory import _llm_provider_registry

        assert len(_llm_provider_registry) == 4
        assert set(_llm_provider_registry.keys()) == {
            "saas", "saas_dashscope", "private_overseas", "private_domestic"
        }
