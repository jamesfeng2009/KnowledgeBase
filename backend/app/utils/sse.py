"""
SSE 流式响应封装 — 单一职责：将异步生成器输出转为 SSE 协议文本。

遵循单一职责：仅处理 SSE 协议格式与 StreamingResponse 封装，不关心业务数据。
遵循开闭原则：新增事件类型只需构造 SSEEvent，无需修改本模块。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from fastapi.responses import StreamingResponse


@dataclass
class SSEEvent:
    """SSE 事件 — 封装 data / event / id 三个字段。

    data 可为 str / dict / list：dict / list 会用 json.dumps 序列化。
    """

    data: Any
    event: Optional[str] = None
    id: Optional[str] = None

    def to_text(self) -> str:
        """渲染为符合 SSE 协议的文本块。"""
        if isinstance(self.data, (dict, list)):
            payload = json.dumps(self.data, ensure_ascii=False)
        else:
            payload = str(self.data)
        return format_sse_event(payload, event=self.event, id=self.id)


def format_sse_event(data: str, event: str = None, id: str = None) -> str:
    """将字段格式化为 SSE 协议文本块（以两个换行结尾）。

    - data 按行拆分，每行加 ``data: `` 前缀（符合 SSE 规范）。
    - event / id 仅在非 None 时输出对应字段。
    """
    lines: list[str] = []
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    data_lines = data.splitlines() or [""]
    for line in data_lines:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


async def _to_sse_stream(generator: AsyncGenerator) -> AsyncGenerator[str, None]:
    """将异步生成器转为 SSE 文本流。

    生成器可产出以下类型，均会被正确序列化（dict / list 用 json.dumps）：

    - SSEEvent：直接渲染其 data / event / id；
    - str：作为 data；
    - dict / list：json.dumps 后作为 data；
    - 其他类型：包装为 ``{"type": "data", "data": ...}`` 后序列化。

    流结束自动发送一个 ``event=done`` 终止事件。
    """
    async for chunk in generator:
        if isinstance(chunk, SSEEvent):
            yield chunk.to_text()
        elif isinstance(chunk, str):
            yield format_sse_event(chunk)
        elif isinstance(chunk, (dict, list)):
            yield format_sse_event(json.dumps(chunk, ensure_ascii=False))
        else:
            yield format_sse_event(
                json.dumps(
                    {"type": "data", "data": chunk},
                    ensure_ascii=False,
                    default=str,
                )
            )
    yield format_sse_event(json.dumps({"type": "done"}), event="done")


def sse_response(generator: AsyncGenerator) -> StreamingResponse:
    """将异步生成器包装为 SSE StreamingResponse。

    content_type 为 ``text/event-stream``，并设置禁用缓冲的响应头，
    以兼容 APISIX / Nginx 透传场景。
    """
    return StreamingResponse(
        _to_sse_stream(generator),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
