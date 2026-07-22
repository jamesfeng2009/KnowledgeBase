"""LLM 适配层开放 API — 统一 LLM 接口给第三方系统。

将内部 LLM Provider（SaaS Anthropic / 私有 vLLM）的能力
以标准化接口暴露给外部系统，屏蔽底层部署差异。

权限说明：
- 需要 scope: ``llm:chat``（对话）/ ``llm:embed``（向量化）；
- 认证方式为 API Key（X-API-Key header）。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.config import get_settings
from app.llm.base import Message
from app.llm.factory import get_embedder, get_llm_provider, list_llm_providers
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger
from app.utils.sse import format_sse_event

logger = get_logger(__name__)

router = APIRouter(prefix="/llm", tags=["开放接口-LLM 适配"])

settings = get_settings()


# ======================================================================
# 请求 Schema
# ======================================================================


class ChatMessage(BaseModel):
    """对话消息。"""

    role: str = Field(..., description="角色: system/user/assistant")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    """LLM 对话请求（非流式）。"""

    messages: list[ChatMessage] = Field(..., min_length=1, description="消息列表")
    max_tokens: int = Field(default=4096, ge=1, le=8192, description="最大生成 token 数")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="采样温度")


class EmbeddingRequest(BaseModel):
    """文本向量化请求。"""

    texts: list[str] = Field(..., min_length=1, description="待向量化的文本列表")


# ======================================================================
# 端点
# ======================================================================


@router.post("/chat", response_model=ApiResponse[dict])
async def chat(
    body: ChatRequest,
    api_key_info: dict = Depends(require_scope("llm:chat")),
) -> ApiResponse[dict]:
    """LLM 对话（非流式）。

    接收消息列表，返回完整回复文本。底层 Provider 由 DEPLOY_MODE 决定。
    """
    provider = get_llm_provider()
    messages: list[Message] = [
        {"role": msg.role, "content": msg.content} for msg in body.messages
    ]

    parts: list[str] = []
    try:
        async for chunk in provider.chat(
            messages,
            stream=False,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
        ):
            if isinstance(chunk, str):
                parts.append(chunk)
    except Exception as exc:
        logger.error("openapi.llm.chat_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM 调用失败: {exc}",
        ) from exc

    return ApiResponse(
        code=0,
        data={
            "content": "".join(parts),
            "model": settings.VLLM_MODEL if settings.is_private else "claude",
            "deploy_mode": settings.DEPLOY_MODE,
        },
        message="success",
    )


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    api_key_info: dict = Depends(require_scope("llm:chat")),
) -> StreamingResponse:
    """LLM 对话（SSE 流式）。

    逐 token 以 SSE 事件流返回，事件格式::

        data: {"content": "token 片段", "done": false}

    流结束发送::

        data: {"content": "", "done": true}
    """
    provider = get_llm_provider()
    messages: list[Message] = [
        {"role": msg.role, "content": msg.content} for msg in body.messages
    ]

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for chunk in provider.chat(
                messages,
                stream=True,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
            ):
                if isinstance(chunk, str):
                    yield format_sse_event(
                        json.dumps(
                            {"content": chunk, "done": False}, ensure_ascii=False
                        )
                    )
            yield format_sse_event(
                json.dumps({"content": "", "done": True}, ensure_ascii=False)
            )
        except Exception as exc:
            logger.error("openapi.llm.stream_error", error=str(exc))
            yield format_sse_event(
                json.dumps(
                    {"content": "", "done": True, "error": str(exc)},
                    ensure_ascii=False,
                )
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/embeddings", response_model=ApiResponse[dict])
async def embeddings(
    body: EmbeddingRequest,
    api_key_info: dict = Depends(require_scope("llm:embed")),
) -> ApiResponse[dict]:
    """文本向量化。

    返回与入参等长的向量列表，维度由当前 Embedder 决定
    （SaaS: 3072 维 / 私有部署: 1024 维）。
    """
    embedder = get_embedder()
    try:
        vectors = await embedder.embed(body.texts)
    except Exception as exc:
        logger.error("openapi.llm.embed_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"向量化失败: {exc}",
        ) from exc

    return ApiResponse(
        code=0,
        data={
            "count": len(vectors),
            "dim": embedder.dim,
            "model": getattr(embedder, "model", "unknown"),
            "embeddings": vectors,
        },
        message="success",
    )


@router.get("/models", response_model=ApiResponse[dict])
async def list_models(
    api_key_info: dict = Depends(require_scope("llm:chat")),
) -> ApiResponse[dict]:
    """可用模型列表。

    返回当前部署模式可用的 LLM 与 Embedding 模型信息。
    """
    providers = list_llm_providers()

    return ApiResponse(
        code=0,
        data={
            "deploy_mode": settings.DEPLOY_MODE,
            "llm_model": settings.VLLM_MODEL if settings.is_private else "claude-sonnet",
            "embedding_dim": 1024 if settings.is_private else 3072,
            "available_providers": providers,
        },
        message="success",
    )
