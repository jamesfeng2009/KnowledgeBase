"""OpenAIProvider 单测 — P1 模型厂商扩展（OpenAI 兼容 20+ 厂商接入）。

测试策略：
    - 构造验证：mock AsyncOpenAI，断言 base_url / api_key / 默认模型来自配置；
    - 厂商路由：注册表中存在 openai 项，factory 按 provider_type 可创建；
    - chat 协议：继承 VLLMProvider，流式/非流式/tool_use 复用既有逻辑
      （此处仅验证构造与注册，深层协议由 vllm/dashscope 测试覆盖）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.openai_provider import OpenAIProvider


class TestOpenAIProviderInit:
    def test_init_uses_config(self) -> None:
        with patch(
            "app.llm.openai_provider.AsyncOpenAI", return_value=MagicMock()
        ) as mock_client, patch(
            "app.llm.openai_provider.settings"
        ) as mock_settings:
            mock_settings.OPENAI_BASE_URL = "https://api.deepseek.com/v1"
            mock_settings.OPENAI_API_KEY = "sk-test"
            mock_settings.OPENAI_LLM_MODEL = "deepseek-chat"

            provider = OpenAIProvider()

            mock_client.assert_called_once_with(
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test",
            )
            assert provider.default_model == "deepseek-chat"

    def test_init_with_explicit_model(self) -> None:
        with patch(
            "app.llm.openai_provider.AsyncOpenAI", return_value=MagicMock()
        ):
            provider = OpenAIProvider(model="moonshot-v1-8k")
            assert provider.default_model == "moonshot-v1-8k"

    def test_circuit_breaker_name(self) -> None:
        assert OpenAIProvider._circuit_breaker_name == "openai"

    def test_constructor_signature_matches_factory(self) -> None:
        """factory 的 _PROVIDER_CONSTRUCTORS 用 provider_type='openai' 创建。"""
        from app.llm.factory import _PROVIDER_CONSTRUCTORS

        assert "openai" in _PROVIDER_CONSTRUCTORS
        assert _PROVIDER_CONSTRUCTORS["openai"] is OpenAIProvider


class TestOpenAIRegistry:
    def test_registry_contains_openai(self) -> None:
        from app.llm.registry import get_llm_provider_entries

        names = {e.name for e in get_llm_provider_entries()}
        assert "openai" in names

    def test_openai_entry_metadata(self) -> None:
        from app.llm.registry import get_llm_provider_entries

        entry = next(e for e in get_llm_provider_entries() if e.name == "openai")
        assert entry.type == "llm"
        assert entry.breaker_name == "openai"
        # 工厂函数可调用并返回 OpenAIProvider 实例
        with patch(
            "app.llm.openai_provider.AsyncOpenAI", return_value=MagicMock()
        ):
            instance = entry.factory()
            assert isinstance(instance, OpenAIProvider)


class TestProviderByModelOpenAI:
    @pytest.mark.asyncio
    async def test_get_llm_provider_by_model_caches(self) -> None:
        """按 model_id 创建 openai provider 并缓存（复用既有工厂路径）。"""
        from app.llm import factory

        with patch(
            "app.llm.openai_provider.AsyncOpenAI", return_value=MagicMock()
        ) as mock_client, patch.object(
            factory, "get_model_by_id", return_value={
                "id": "deepseek-test",
                "model_id": "deepseek-chat",
                "provider_type": "openai",
                "deploy_mode": "saas",
                "enabled": True,
            }
        ), patch.object(
            factory.settings, "DEPLOY_MODE", "saas"
        ):
            factory.clear_model_provider_cache()
            provider = factory.get_llm_provider_by_model("deepseek-test")
            assert isinstance(provider, OpenAIProvider)
            assert provider.default_model == "deepseek-chat"
            # 二次获取命中缓存，不重复创建
            again = factory.get_llm_provider_by_model("deepseek-test")
            assert again is provider
            mock_client.assert_called_once()
            factory.clear_model_provider_cache()
