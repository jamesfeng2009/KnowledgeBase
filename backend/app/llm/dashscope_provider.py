"""
DashScope LLM Provider — 单一职责：通过 OpenAI 兼容 API 调用阿里云通义千问。

DashScope（百炼平台）提供 OpenAI 兼容接口，因此复用 ``VLLMProvider`` 的全部
chat / tool_use / stream 逻辑，仅覆盖 ``__init__`` 指向 DashScope endpoint。

覆盖 SaaS·国内（saas_dashscope）场景：
    - 通义千问 Qwen 系列（qwen-turbo / qwen-plus / qwen-max / qwen-7b-chat 等）
    - 国内直连，无需代理
    - Qwen-7B 无限制免费，qwen-turbo/qwen-plus 有新用户免费额度

遵循开闭原则：新增模型只需在 settings.DASHSCOPE_LLM_MODEL 配置，无需改本文件。
遵循单一职责：本模块只负责 DashScope 连接初始化，chat 逻辑继承自 VLLMProvider。

使用方式::

    # .env 中配置
    DEPLOY_MODE=saas_dashscope
    DASHSCOPE_API_KEY=sk-xxx
    DASHSCOPE_LLM_MODEL=qwen-turbo

    # 业务代码无感知
    from app.llm.factory import get_llm_provider
    llm = get_llm_provider()
    async for chunk in llm.chat(messages):
        print(chunk)
"""

from __future__ import annotations

from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.vllm_provider import VLLMProvider

settings = get_settings()


class DashScopeProvider(VLLMProvider):
    """阿里云通义千问 Provider — 通过 DashScope OpenAI 兼容接口调用。

    继承 ``VLLMProvider`` 的全部 chat / tool_use / stream 逻辑，
    仅覆盖 ``__init__`` 指向 DashScope endpoint 和 API Key。

    DashScope 兼容 OpenAI API 格式：
        - base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
        - 模型名直接使用通义千问模型 ID（qwen-turbo / qwen-plus / qwen-max）
        - 支持 function calling（tools 参数）
        - 支持流式输出（stream=True）
    """

    def __init__(self, model: str | None = None) -> None:
        """初始化 DashScope 异步客户端。

        Args:
            model: 默认模型 ID（如 qwen-turbo / qwen-plus / qwen-max）；
                   为 None 时回退到 settings.DASHSCOPE_LLM_MODEL。
        """
        self.client = AsyncOpenAI(
            base_url=settings.DASHSCOPE_BASE_URL,
            api_key=settings.DASHSCOPE_API_KEY,
        )
        self.default_model = model or settings.DASHSCOPE_LLM_MODEL
