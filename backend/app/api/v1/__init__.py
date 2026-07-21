"""
API v1 路由聚合 — 单一职责：将所有业务子 router 挂载到统一的 api_router。

遵循单一职责：本模块仅做路由聚合，不包含任何业务逻辑或端点定义。
遵循开闭原则：新增业务模块只需 import 并 include_router，不修改既有代码。
"""

from fastapi import APIRouter

from app.api.v1.agents import router as agents_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.apikeys import router as apikeys_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.comments import router as comments_router
from app.api.v1.connectors import router as connectors_router
from app.api.v1.documents import router as documents_router
from app.api.v1.experts import router as experts_router
from app.api.v1.feedback import router as feedback_router
from app.api.v1.graph import router as graph_router
from app.api.v1.intelligence import router as intelligence_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.models import router as models_router
from app.api.v1.multimodal import router as multimodal_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.qa import router as qa_router
from app.api.v1.reports import router as reports_router
from app.api.v1.search import router as search_router
from app.api.v1.settings import router as settings_router
from app.api.v1.stats import router as stats_router
from app.api.v1.tenants import router as tenants_router
from app.api.v1.users import router as users_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth_router)
api_router.include_router(knowledge_router)
api_router.include_router(documents_router)
api_router.include_router(chat_router)
api_router.include_router(qa_router)
api_router.include_router(comments_router)
api_router.include_router(feedback_router)
api_router.include_router(graph_router)
api_router.include_router(intelligence_router)
api_router.include_router(analytics_router)
api_router.include_router(experts_router)
api_router.include_router(notifications_router)
api_router.include_router(audit_router)
api_router.include_router(search_router)
api_router.include_router(connectors_router)
api_router.include_router(multimodal_router)
api_router.include_router(agents_router)
api_router.include_router(users_router)
api_router.include_router(tenants_router)
api_router.include_router(apikeys_router)
api_router.include_router(settings_router)
api_router.include_router(reports_router)
api_router.include_router(stats_router)
api_router.include_router(approvals_router)
api_router.include_router(models_router)
