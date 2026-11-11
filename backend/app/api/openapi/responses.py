"""开放接口的响应语义助手 — 统一表达「能力尚未实现」。

背景（P2）：connectors / multimedia 里的预留端点此前返回

    {"code": 0, "data": {...占位空值...}, "message": "success"}

集成方看到 ``code=0`` 会当成「查询成功，但当前没有数据」，于是按「空结果」
写死下游逻辑（重试、缓存空值、告警阈值全部基于错误前提）。真实语义是
「这个能力根本还没做」，两者必须可区分。

统一约定：
    HTTP 501 + ``code = 501`` + ``data = null`` + 明确的 message。
真实对接实现后，把 ``not_implemented(...)`` 换成正常返回即可，集成方的
判断分支不用改。
"""

from __future__ import annotations

from fastapi import Response, status

from app.schemas.common import ApiResponse

#: 「未实现」业务码 —— 与 HTTP 501 对齐，便于集成方统一判断
NOT_IMPLEMENTED_CODE: int = status.HTTP_501_NOT_IMPLEMENTED


def not_implemented(
    response: Response,
    capability: str,
    *,
    hint: str = "",
) -> ApiResponse[dict]:
    """把预留/未实现的能力显式表达为 501。

    Args:
        response: FastAPI 注入的 ``Response``，用于把状态码改成 501。
        capability: 能力名称，如 "OA 连接器查询"。
        hint: 补充说明（例如需要先对接什么系统）。

    Returns:
        ``ApiResponse(code=501, data=None, message=...)``
    """
    response.status_code = NOT_IMPLEMENTED_CODE
    message = f"{capability} 尚未实现"
    if hint:
        message = f"{message}：{hint}"
    return ApiResponse(code=NOT_IMPLEMENTED_CODE, data=None, message=message)
