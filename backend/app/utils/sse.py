"""
SSE 流式响应封装 — 单一职责：将异步生成器输出转为 SSE 协议文本。

遵循单一职责：仅处理 SSE 协议格式与 StreamingResponse 封装，不关心业务数据。
遵循开闭原则：新增事件类型只需构造 SSEEvent，无需修改本模块。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from fastapi.responses import StreamingResponse

from app.utils.logger import get_logger

log = get_logger(__name__)


class SSEEventType:
    """SSE 事件类型统一常量 — 所有业务代码引用此类，禁止硬编码字符串。

    事件流时序示例（一次含工具调用的对话）::

        event: meta             data: {"conversation_id":"...","agent_type":"qa"}
        event: thinking         data: {"content":"正在分析问题..."}
        event: retrieve_start   data: {"query":"重写后的查询"}
        event: retrieve_end     data: {"doc_count":3,"sources":[...]}
        event: tool_call_start  data: {"tool_name":"query_oa_approval","tool_use_id":"tu_001","arguments":{...}}
        event: tool_call_end    data: {"tool_use_id":"tu_001","result":"...","duration_ms":320,"status":"success"}
        data: 根据               ← token（默认事件，无 event 字段）
        event: sources          data: {"sources":[...]}
        event: quality          data: {"score":0.85,"low_confidence":false}
        event: done             data: {"message_id":"...","token_count":156,"model_used":"claude-sonnet-4"}
    """

    # 会话元数据
    META = "meta"
    # 意图识别结果（P1 IntentRouter）
    INTENT = "intent"
    # 上下文工程结果（P3 焦点追踪 + 指代消解）
    CONTEXT_RESOLVED = "context_resolved"
    # P4-A: 话题漂移检测
    DRIFT_DETECTED = "drift_detected"
    # P4-B: 矛盾检测
    CONTRADICTION_DETECTED = "contradiction_detected"
    # P4-D: 检索匹配检测
    RETRIEVAL_MISMATCH = "retrieval_mismatch"
    # P4-F: 偏好偏移检测
    PREFERENCE_CHANGED = "preference_changed"
    # P4-G: 重复提问检测
    REPETITION_DETECTED = "repetition_detected"
    # 思考过程（Agent Loop think 阶段）
    THINKING = "thinking"
    # 查询重写（P2-B）
    QUERY_REWRITE = "query_rewrite"
    # 检索过程
    RETRIEVE_START = "retrieve_start"
    RETRIEVE_END = "retrieve_end"
    # 工具调用
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    # 生成
    TOKEN = "token"
    SOURCES = "sources"
    # 审批（P1 预留）
    APPROVAL_REQUIRED = "approval_required"
    # 结束
    QUALITY = "quality"
    DONE = "done"
    ERROR = "error"


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


def format_sse_event(
    data: str, event: Optional[str] = None, id: Optional[str] = None
) -> str:
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


# 心跳间隔（秒）— 模块级常量，便于测试调整
_HEARTBEAT_INTERVAL: float = 30.0


async def _to_sse_stream(generator: AsyncGenerator) -> AsyncGenerator[str, None]:
    """将异步生成器转为 SSE 文本流。

    生成器可产出以下类型，均会被正确序列化（dict / list 用 json.dumps）：

    - SSEEvent：直接渲染其 data / event / id；
    - str：作为 data；
    - dict / list：json.dumps 后作为 data；
    - 其他类型：包装为 ``{"type": "data", "data": ...}`` 后序列化。

    若生成器已 yield ``event=done`` 的 SSEEvent，则流末尾不再自动追加
    重复的 done 事件；否则自动发送一个 ``event=done`` 终止事件作为安全兜底。

    优雅关闭支持：
    - 30 秒心跳保活（SSE 注释 ``: heartbeat\\n\\n``），防止代理超时断连；
      心跳通过 ``asyncio.wait`` 实现 — 超时不取消 pending 的 ``__anext__``
      任务，发送心跳后继续 await 同一任务，LLM 长时间静默不会杀死整个流；
    - 客户端断连时捕获 CancelledError，优雅退出不抛异常。
    """
    done_yielded = False
    heartbeat_interval = _HEARTBEAT_INTERVAL

    # 挂起的 __anext__ 任务 — 跨心跳复用，超时绝不取消。
    anext_task: asyncio.Task | None = None

    try:
        while True:
            if anext_task is None:
                anext_task = asyncio.ensure_future(generator.__anext__())

            try:
                done, _pending = await asyncio.wait(
                    {anext_task}, timeout=heartbeat_interval
                )
            except asyncio.CancelledError:
                # 客户端断连或服务关闭 — 优雅退出
                log.debug("sse.stream_cancelled")
                break

            if not done:
                # 心跳保活 — SSE 注释行，浏览器忽略但不超时；
                # 继续等待同一个 anext 任务，不取消、不重建。
                yield ": heartbeat\n\n"
                continue

            task, anext_task = anext_task, None
            try:
                chunk = task.result()
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                # 底层生成器被取消 — 优雅退出
                log.debug("sse.stream_cancelled")
                break

            if isinstance(chunk, SSEEvent):
                yield chunk.to_text()
                if chunk.event == SSEEventType.DONE:
                    done_yielded = True
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
    finally:
        # 清理仍挂起的 anext 任务，避免泄漏（客户端断连 / 流被提前关闭）
        if anext_task is not None:
            anext_task.cancel()

    if not done_yielded:
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
