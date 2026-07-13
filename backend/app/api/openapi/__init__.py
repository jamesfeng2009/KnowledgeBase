"""L13 开放接口层 — 面向外部系统的 API。

与 v1 内部 API 的区别：
- 认证方式：API Key（X-API-Key header）而非 JWT
- 权限范围：受限于 API Key 的 scopes 字段
- 速率限制：独立的限流策略
- 版本独立：不受 v1 内部 API 变更影响

包含六类开放能力：
1. OpenAPI — 知识库 CRUD 查询
2. 企业连接器 — 对接 OA/ERP/HR 系统
3. LLM API 适配 — 统一 LLM 接口给第三方
4. 多媒体 API — 文档解析/图片识别
5. Webhook 事件 — 知识变更通知
6. MCP 工具协议 — 暴露 MCP 工具给外部 AI Agent
"""
from fastapi import APIRouter

from app.api.openapi.v1.knowledge import router as knowledge_router
from app.api.openapi.v1.connectors import router as connectors_router
from app.api.openapi.v1.llm import router as llm_router
from app.api.openapi.v1.multimedia import router as multimedia_router
from app.api.openapi.v1.webhooks import router as webhooks_router
from app.api.openapi.v1.mcp import router as mcp_router

openapi_router = APIRouter(prefix="/openapi", tags=["开放接口"])

openapi_router.include_router(knowledge_router)
openapi_router.include_router(connectors_router)
openapi_router.include_router(llm_router)
openapi_router.include_router(multimedia_router)
openapi_router.include_router(webhooks_router)
openapi_router.include_router(mcp_router)

__all__ = ["openapi_router"]
