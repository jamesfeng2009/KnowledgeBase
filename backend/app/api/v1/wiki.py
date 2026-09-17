"""
Wiki 路由 — 自动生成互链知识库的 HTTP 请求/响应转换（P0-1）。

权限模型：
    - 读（list / page 详情 / versions / graph）：要求对知识库有读权限；
    - 写（generate / update / rollback）：要求有写权限。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.wiki import WikiPageUpdate
from app.services.permission_service import PermissionService
from app.services.wiki_service import WikiService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["Wiki 知识库"])


def _wiki_service(db: AsyncSession, request: Request, user: User) -> WikiService:
    return WikiService(db, user, tenant_id=getattr(request.state, "tenant_id", None))


def _perm(db: AsyncSession, request: Request, user: User) -> PermissionService:
    return PermissionService(db, user, tenant_id=getattr(request.state, "tenant_id", None))


def _page_dict(page, links=None) -> dict:  # noqa: ANN001
    data = {
        "id": str(page.id),
        "kb_id": str(page.kb_id),
        "source_doc_id": str(page.source_doc_id) if page.source_doc_id else None,
        "title": page.title,
        "content_md": page.content_md,
        "status": page.status,
        "author_id": str(page.author_id),
        "created_at": page.created_at.isoformat() if page.created_at else None,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }
    if links is not None:
        data["outbound_links"] = [
            {"to_page_id": str(l.to_page_id), "text": l.link_text} for l in links
        ]
    return data


def _version_dict(version) -> dict:  # noqa: ANN001
    return {
        "id": str(version.id),
        "version_seq": version.version_seq,
        "title": version.title,
        "content_md": version.content_md,
        "summary": version.summary,
        "author_id": str(version.author_id),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


@router.post("/knowledge/{kb_id}/wiki/generate", response_model=ApiResponse[dict])
async def generate_wiki(
    kb_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """为知识库全部文档生成/更新 Wiki 页面并重建互链。"""
    perm = _perm(db, request, user)
    if not await perm.check_write(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    service = _wiki_service(db, request, user)
    try:
        result = await service.generate(kb_id, author_id=user.id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    except Exception as exc:  # noqa: BLE001 — LLM 链路异常统一降级为 502
        log.error("wiki.generate_error", kb_id=str(kb_id), error=str(exc))
        return ApiResponse(code=502, data=None, message=f"生成失败: {exc}")
    await db.commit()
    return ApiResponse(code=0, data=result, message="success")


@router.get("/knowledge/{kb_id}/wiki", response_model=ApiResponse[dict])
async def list_wiki_pages(
    kb_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """列出知识库 Wiki 页面（不含正文，标题升序）。"""
    perm = _perm(db, request, user)
    if not await perm.check_function(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    service = _wiki_service(db, request, user)
    pages = await service.list_pages(kb_id)
    items = [
        {k: v for k, v in _page_dict(p).items() if k not in ("content_md", "outbound_links")}
        for p in pages
    ]
    return ApiResponse(code=0, data={"items": items, "total": len(items)}, message="success")


@router.get("/knowledge/{kb_id}/wiki/graph", response_model=ApiResponse[dict])
async def get_wiki_graph(
    kb_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """返回知识库 Wiki 图谱（节点 + 互链边）。"""
    perm = _perm(db, request, user)
    if not await perm.check_function(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    service = _wiki_service(db, request, user)
    data = await service.graph(kb_id)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/wiki/{page_id}", response_model=ApiResponse[dict])
async def get_wiki_page(
    page_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """返回 Wiki 页面详情（含出链）。"""
    service = _wiki_service(db, request, user)
    page = await service.get_page(page_id)
    if page is None:
        return ApiResponse(code=404, data=None, message="页面不存在")
    perm = _perm(db, request, user)
    if not await perm.check_function(page.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    links = await service.get_outbound_links(page_id)
    return ApiResponse(code=0, data=_page_dict(page, links), message="success")


@router.put("/wiki/{page_id}", response_model=ApiResponse[dict])
async def update_wiki_page(
    page_id: UUID,
    body: WikiPageUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """编辑 Wiki 页面 — 快照 + 更新 + 重建链接。"""
    service = _wiki_service(db, request, user)
    page = await service.get_page(page_id)
    if page is None:
        return ApiResponse(code=404, data=None, message="页面不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(page.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    try:
        updated = await service.update_page(
            page_id, title=body.title, content_md=body.content_md,
            author_id=user.id, summary=body.summary,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=_page_dict(updated), message="success")


@router.get("/wiki/{page_id}/versions", response_model=ApiResponse[dict])
async def list_wiki_versions(
    page_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """列出 Wiki 页面版本历史（版本号降序）。"""
    service = _wiki_service(db, request, user)
    page = await service.get_page(page_id)
    if page is None:
        return ApiResponse(code=404, data=None, message="页面不存在")
    perm = _perm(db, request, user)
    if not await perm.check_function(page.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    versions = await service.list_versions(page_id)
    return ApiResponse(
        code=0,
        data={"items": [_version_dict(v) for v in versions], "total": len(versions)},
        message="success",
    )


@router.post(
    "/wiki/{page_id}/versions/{version_id}/rollback", response_model=ApiResponse[dict]
)
async def rollback_wiki_page(
    page_id: UUID,
    version_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """回滚 Wiki 页面到指定版本 — 快照当前 + 写回 + 重建链接。"""
    service = _wiki_service(db, request, user)
    page = await service.get_page(page_id)
    if page is None:
        return ApiResponse(code=404, data=None, message="页面不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(page.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    try:
        updated = await service.rollback(page_id, version_id, author_id=user.id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=_page_dict(updated), message="success")
