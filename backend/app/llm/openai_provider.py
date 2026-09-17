"""
OpenAI Provider — 单一职责：通过 OpenAI 兼容 API 接入任意第三方厂商 LLM。

复用 ``VLLMProvider`` 的全部 chat / tool_use / stream 逻辑（OpenAI 兼容协议），
仅覆盖 ``__init__`` 指向厂商自定义 endpoint 与鉴权。

一接入 20+ 厂商（P1 模型厂商扩展）：凡提供 OpenAI 兼容
``/v1/chat/completions`` 的服务均可通过配置接入，无需新增代码：
    - OpenAI（gpt-4o / gpt-4o-mini …）
    - DeepSeek（deepseek-chat / deepseek-reasoner）
    - Moonshot Kimi（moonshot-v1-8k …）
    - 智谱 GLM（glm-4 …）
    - Groq / Together / Fireworks（开源模型托管）
    - Ollama / LM Studio（本地 OpenAI 兼容网关）
    - 腾讯混元 / 百度文心（OpenAI 兼容网关模式）

使用方式::

    # .env 中配置
    OPENAI_BASE_URL=https://api.deepseek.com/v1
    OPENAI_API_KEY=sk-xxx
    OPENAI_LLM_MODEL=deepseek-chat

    # 业务代码无感知
    from app.llm.factory import get_llm_provider_by_model  # provider_type=openai
"""

from __future__ import annotations

from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.vllm_provider import VLLMProvider
from app.utils.circuit_breaker import get_circuit_breaker

settings = get_settings()


class OpenAIProvider(VLLMProvider):
    """通用 OpenAI 兼容厂商 Provider — 通过 base_url 路由到任意厂商。

    继承 ``VLLMProvider`` 的 chat / tool_use / stream 全量逻辑，
    仅覆盖 ``__init__`` 指向厂商 endpoint / API Key / 默认模型。
    """

    _circuit_breaker_name: str = "openai"

    def __init__(self, model: str | None = None) -> None:
        """初始化 OpenAI 兼容异步客户端。

        Args:
            model: 默认模型 ID（如 deepseek-chat / gpt-4o-mini / moonshot-v1-8k）；
                   为 None 时回退到 settings.OPENAI_LLM_MODEL。
        """
        self.client = AsyncOpenAI(
            base_url=settings.OPENAI_BASE_URL,
            api_key=settings.OPENAI_API_KEY,
        )
        self.default_model = model or settings.OPENAI_LLM_MODEL
        self._cb = get_circuit_breaker(self._circuit_breaker_name)
