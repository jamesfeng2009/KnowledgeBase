"""API / services 层中等问题修复的回归测试。

覆盖：
1. documents.py 上传大小闸门 — Content-Length 预检 + 分块流式计数（内存 DoS 防护）；
2. documents.py multipart 会话 — redis.asyncio（不阻塞事件循环）；
3. /health/providers 信息泄漏防护 — 非 admin 仅返回健康状态摘要，error 细节仅 admin 可见；
4. /chat/stream 连接池治理 — 准备阶段完成后释放 DB 连接，流式期间不持有；
5. admin 密级口径统一 + chat SSE 权限异常 → SSE error 事件。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.api.v1.documents import (
    _check_content_length,
    _delete_multipart_session,
    _load_multipart_session,
    _read_upload_bounded,
)
from app.services.chat_service import ChatService, PreparedChat
from app.services.health_check import (
    ProviderHealth,
    is_request_admin,
    sanitize_providers,
)
from app.utils.sse import SSEEvent, SSEEventType
from fastapi import HTTPException


# ======================================================================
# 工具
# ======================================================================


def _make_user(role: str = "editor", clearance: str = "internal"):
    return SimpleNamespace(
        id=uuid4(),
        email="u@ekb.com",
        name="用户",
        role=role,
        clearance_level=clearance,
        dept_id=None,
        is_active=True,
    )


class _FakeUploadFile:
    """模拟 UploadFile — 按 read(size) 顺序返回分块，末尾返回空字节。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _make_request(headers: dict[str, str] | None = None):
    return SimpleNamespace(headers=headers or {})


@pytest_asyncio.fixture
async def auth_client(mock_user):
    """带认证与 DB 覆盖的客户端（与 tests/test_api_endpoints.py 模式一致）。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app

    async def override_user():
        return mock_user

    mock_db = AsyncMock()

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    # 每个测试使用唯一 X-API-Key — 限流中间件按 X-API-Key 分桶计数，
    # 避免与全量套件中其他测试共享 ip:testclient 桶导致偶发 429。
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": f"test-{uuid4()}"},
    ) as client:
        client._ekb_db = mock_db  # type: ignore[attr-defined]
        client._ekb_user = mock_user  # type: ignore[attr-defined]
        yield client

    app.dependency_overrides.clear()


# ======================================================================
# 1. 上传大小闸门 — Content-Length 预检 + 分块流式计数
# ======================================================================


class TestUploadSizeGates:
    """内存 DoS 防护：大小校验必须先于全量读取。"""

    def test_content_length_over_limit_rejected_before_read(self) -> None:
        """Content-Length 声明超限 → 直接 413，不读取任何字节。"""
        request = _make_request({"content-length": str(60 * 1024 * 1024)})
        with pytest.raises(HTTPException) as exc_info:
            _check_content_length(request)
        assert exc_info.value.status_code == 413
        assert "超过上限" in exc_info.value.detail

    def test_content_length_within_limit_passes(self) -> None:
        """Content-Length 未超限（含 multipart 开销余量）→ 放行。"""
        request = _make_request({"content-length": str(50 * 1024 * 1024)})
        _check_content_length(request)  # 不抛异常

    def test_content_length_missing_or_invalid_passes(self) -> None:
        """缺失 / 非数字 Content-Length → 跳过预检（由流式计数兜底）。"""
        _check_content_length(_make_request({}))
        _check_content_length(_make_request({"content-length": "abc"}))

    @pytest.mark.asyncio
    async def test_read_upload_bounded_small_file(self) -> None:
        """小文件：分块读取后返回完整内容。"""
        fake = _FakeUploadFile([b"hello ", b"world"])
        content = await _read_upload_bounded(fake)
        assert content == b"hello world"
        assert fake.read_calls == 3  # 两块 + 末尾空读

    @pytest.mark.asyncio
    async def test_read_upload_bounded_oversized_aborts_early(self) -> None:
        """超限：流式读取途中即 413 中止，不继续读后续分块（不先全量读入内存）。"""
        one_mb = b"x" * (1024 * 1024)
        fake = _FakeUploadFile([one_mb, one_mb, one_mb])
        with patch("app.api.v1.documents.get_settings") as mock_settings:
            mock_settings.return_value = SimpleNamespace(MAX_UPLOAD_SIZE_MB=1)
            with pytest.raises(HTTPException) as exc_info:
                await _read_upload_bounded(fake)
        assert exc_info.value.status_code == 413
        # 读到第 2 块即超限中止，第 3 块未被读入
        assert fake.read_calls == 2

    @pytest.mark.asyncio
    async def test_upload_endpoint_oversized_content_length_413(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """端点集成：声明超大 Content-Length → 413，且业务 service 未被调用。"""
        # 使用 admin 角色跳过权限查询，确保只验证 Content-Length 预检逻辑
        auth_client._ekb_user.role = "admin"  # type: ignore[attr-defined]
        with patch(
            "app.api.v1.documents.KnowledgeService"
        ) as mock_service_cls:
            resp = await auth_client.post(
                "/api/v1/documents/upload",
                params={"kb_id": str(uuid4()), "title": "大文件"},
                files={"file": ("big.md", b"tiny body")},
                headers={"content-length": str(60 * 1024 * 1024)},
            )
        assert resp.status_code == 413
        # 大小校验先于任何文件读取 / 业务调用
        mock_service_cls.return_value.upload_document.assert_not_called()


# ======================================================================
# 2. multipart 会话 — redis.asyncio（不阻塞事件循环）
# ======================================================================


class TestMultipartAsyncRedis:
    """async 上下文必须使用 redis.asyncio 客户端。"""

    @pytest.mark.asyncio
    async def test_load_multipart_session_uses_async_redis(self) -> None:
        """_load_multipart_session 通过 redis.asyncio 读取并关闭客户端。"""
        payload = {"user_id": str(uuid4()), "object_name": "kb/a"}
        client = AsyncMock()
        client.get = AsyncMock(return_value=json.dumps(payload))
        client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=client) as mock_from_url:
            session = await _load_multipart_session("up-1")

        mock_from_url.assert_called_once()
        client.get.assert_awaited_once_with("ekb:multipart:up-1")
        client.aclose.assert_awaited_once()
        assert session == payload

    @pytest.mark.asyncio
    async def test_load_multipart_session_missing_returns_none(self) -> None:
        """会话不存在 → None。"""
        client = AsyncMock()
        client.get = AsyncMock(return_value=None)
        client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=client):
            session = await _load_multipart_session("up-x")

        assert session is None
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_multipart_session_uses_async_redis(self) -> None:
        """_delete_multipart_session 通过 redis.asyncio 删除并关闭客户端。"""
        client = AsyncMock()
        client.delete = AsyncMock(return_value=1)
        client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=client):
            await _delete_multipart_session("up-2")

        client.delete.assert_awaited_once_with("ekb:multipart:up-2")
        client.aclose.assert_awaited_once()

    def test_documents_module_has_no_sync_redis(self) -> None:
        """源码级守护：documents 模块不再使用同步 redis 客户端。"""
        import inspect

        import app.api.v1.documents as docs_module

        src = inspect.getsource(docs_module)
        # 所有 from_url 调用均来自 redis.asyncio（aioredis）
        assert src.count("from_url(") == src.count("aioredis.from_url(")
        assert "import redis\n" not in src


# ======================================================================
# 3. /health/providers 信息泄漏防护
# ======================================================================


def _provider_with_sensitive_error() -> ProviderHealth:
    return ProviderHealth(
        name="llm_vllm",
        type="llm",
        healthy=False,
        latency_ms=12.0,
        error="connect http://internal-llm.local:8000 failed, key=sk-frag123",
        circuit_state="open",
        last_check="2026-07-22T00:00:00+00:00",
    )


class TestSanitizeProviders:
    """非 admin 仅返回健康状态摘要；error 细节仅 admin 可见。"""

    def test_sanitize_strips_error_for_non_admin(self) -> None:
        providers = {"llm_vllm": _provider_with_sensitive_error()}
        views = sanitize_providers(providers, include_details=False)
        assert "error" not in views["llm_vllm"]
        assert views["llm_vllm"]["healthy"] is False
        assert views["llm_vllm"]["name"] == "llm_vllm"

    def test_sanitize_keeps_error_for_admin(self) -> None:
        providers = {"llm_vllm": _provider_with_sensitive_error()}
        views = sanitize_providers(providers, include_details=True)
        assert views["llm_vllm"]["error"] is not None


class TestIsRequestAdmin:
    """可选 Bearer 认证解析 — 失败一律按非 admin（安全默认）。"""

    @pytest.mark.asyncio
    async def test_no_authorization_header(self) -> None:
        assert await is_request_admin(_make_request()) is False

    @pytest.mark.asyncio
    async def test_non_bearer_scheme(self) -> None:
        request = _make_request({"authorization": "Basic abc"})
        assert await is_request_admin(request) is False

    @pytest.mark.asyncio
    async def test_invalid_token_falls_back_to_non_admin(self) -> None:
        request = _make_request({"authorization": "Bearer bad-token"})
        with patch("app.services.auth_service.AuthService") as mock_auth_cls:
            mock_auth_cls.return_value.get_current_user = AsyncMock(
                side_effect=ValueError("无效凭证")
            )
            assert await is_request_admin(request) is False

    @pytest.mark.asyncio
    async def test_db_unavailable_falls_back_to_non_admin(self) -> None:
        request = _make_request({"authorization": "Bearer token"})
        with patch(
            "app.database.async_session_factory",
            side_effect=RuntimeError("db down"),
        ):
            assert await is_request_admin(request) is False

    @pytest.mark.asyncio
    async def test_admin_user_returns_true(self) -> None:
        request = _make_request({"authorization": "Bearer admin-token"})
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_cm.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("app.database.async_session_factory", return_value=session_cm),
            patch("app.services.auth_service.AuthService") as mock_auth_cls,
        ):
            mock_auth_cls.return_value.get_current_user = AsyncMock(
                return_value=SimpleNamespace(role="admin", is_active=True)
            )
            assert await is_request_admin(request) is True

    @pytest.mark.asyncio
    async def test_non_admin_user_returns_false(self) -> None:
        request = _make_request({"authorization": "Bearer viewer-token"})
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_cm.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("app.database.async_session_factory", return_value=session_cm),
            patch("app.services.auth_service.AuthService") as mock_auth_cls,
        ):
            mock_auth_cls.return_value.get_current_user = AsyncMock(
                return_value=SimpleNamespace(role="viewer", is_active=True)
            )
            assert await is_request_admin(request) is False


class TestHealthProvidersEndpointGate:
    """端点级：未认证/非 admin 看不到 error 细节，admin 可见。"""

    @pytest.mark.asyncio
    async def test_unauthenticated_gets_summary_without_error(self) -> None:
        from app.main import app

        cached = {"llm_vllm": _provider_with_sensitive_error()}
        with patch(
            "app.services.health_check.HealthCheckService.load_from_redis",
            new=AsyncMock(return_value=cached),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/health/providers")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["source"] == "cache"
        assert data["healthy_count"] == 0
        provider = data["providers"]["llm_vllm"]
        assert provider["healthy"] is False
        assert "error" not in provider  # 敏感细节已剥离

    @pytest.mark.asyncio
    async def test_admin_sees_error_details(self) -> None:
        from app.main import app

        cached = {"llm_vllm": _provider_with_sensitive_error()}
        with (
            patch(
                "app.services.health_check.HealthCheckService.load_from_redis",
                new=AsyncMock(return_value=cached),
            ),
            patch(
                "app.services.health_check.is_request_admin",
                new=AsyncMock(return_value=True),
            ),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/health/providers",
                    headers={"authorization": "Bearer admin-token"},
                )

        assert resp.status_code == 200
        provider = resp.json()["data"]["providers"]["llm_vllm"]
        assert "sk-frag123" in provider["error"]


# ======================================================================
# 4+5. ChatService 拆分（连接池治理）+ SSE 权限异常 → error 事件
# ======================================================================


def _make_chat_service(user=None, tenant_id=None) -> ChatService:
    """绕过 __init__（避免真实 LLM provider），注入全 mock 依赖。"""
    service = object.__new__(ChatService)
    service.db = AsyncMock()
    service.user = user or _make_user()
    service._tenant_id = tenant_id
    service.conv_repo = AsyncMock()
    service.msg_repo = AsyncMock()
    service.memory = AsyncMock()
    service.llm = AsyncMock()
    return service


def _make_prepared(user_id, **overrides) -> PreparedChat:
    defaults = {
        "query": "测试问题",
        "conversation_id": uuid4(),
        "agent_type": "qa",
        "tenant_id": None,
        "memory_context": "ctx",
        "resolved_model_id": "",
        "default_model_id": "",
    }
    defaults.update(overrides)
    return PreparedChat(**defaults)


class TestChatServicePrepare:
    """准备阶段：流式开始前完成 DB 读写；权限异常在此阶段抛出。"""

    @pytest.mark.asyncio
    async def test_prepare_chat_permission_denied_before_stream(self) -> None:
        """访问他人对话 → prepare_chat 抛 PermissionError（流开始前）。"""
        user = _make_user()
        service = _make_chat_service(user=user)
        foreign_conversation = SimpleNamespace(id=uuid4(), user_id=uuid4())
        service.conv_repo.get_by_id = AsyncMock(return_value=foreign_conversation)

        with pytest.raises(PermissionError, match="无权访问该对话"):
            await service.prepare_chat(
                query="问题",
                conversation_id=foreign_conversation.id,
                agent_type="qa",
            )

        # 权限失败时不应写入任何消息
        service.msg_repo.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_chat_creates_conversation_and_persists_message(
        self,
    ) -> None:
        """新对话：创建会话 + 持久化用户消息 + 返回 PreparedChat（不触发引擎）。"""
        user = _make_user()
        service = _make_chat_service(user=user)
        conversation = SimpleNamespace(id=uuid4(), user_id=user.id)
        service.conv_repo.create = AsyncMock(return_value=conversation)
        service.memory.build_context = AsyncMock(return_value={})
        service._build_engine_memory_context = AsyncMock(return_value="mem-ctx")

        with (
            patch(
                "app.services.model_selection_service.ModelSelectionService"
            ) as mock_mss,
            patch("app.llm.model_config.get_default_model") as mock_gdm,
        ):
            mock_mss.return_value.resolve_model = AsyncMock(return_value="gpt-4o")
            mock_gdm.return_value = {"id": "gpt-4o"}
            prepared = await service.prepare_chat(
                query="你好", conversation_id=None, agent_type="qa"
            )

        service.conv_repo.create.assert_awaited_once()
        service.msg_repo.create_message.assert_awaited_once()
        args = service.msg_repo.create_message.await_args.args
        assert args[0] == conversation.id
        assert args[1] == "user"
        assert args[2] == "你好"
        assert prepared.conversation_id == conversation.id
        assert prepared.memory_context == "mem-ctx"
        assert prepared.resolved_model_id == "gpt-4o"


class _FakeEngine:
    """模拟 RAG 引擎 — answer() 为 async 生成器。"""

    def __init__(self, chunks=None, exc: Exception | None = None) -> None:
        self._chunks = chunks or []
        self._exc = exc

    async def answer(self, **kwargs):
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc


class TestChatServiceStream:
    """流式阶段：不持有 DB 连接；权限异常 → SSE error 事件；结束后短事务持久化。"""

    @pytest.mark.asyncio
    async def test_stream_chat_permission_error_becomes_sse_error_event(
        self,
    ) -> None:
        """流中权限异常 → 转为 SSE error 事件（前端可收到友好错误）。"""
        user = _make_user()
        service = _make_chat_service(user=user)
        prepared = _make_prepared(user.id)
        engine = _FakeEngine(exc=PermissionError("无权访问密级文档"))

        with patch(
            "app.services.chat_service.get_rag_engine", return_value=engine
        ):
            events = [e async for e in service.stream_chat(prepared)]

        error_events = [
            e
            for e in events
            if isinstance(e, SSEEvent) and e.event == SSEEventType.ERROR
        ]
        assert len(error_events) == 1
        assert error_events[0].data["type"] == "error"
        assert "无权访问密级文档" in error_events[0].data["message"]
        # 权限失败时不持久化 assistant 消息
        service.msg_repo.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_chat_persists_and_releases_after_stream(self) -> None:
        """正常流式：先 meta → token 透传 → 结束后一次性 commit + close 释放连接。"""
        user = _make_user()
        service = _make_chat_service(user=user)
        prepared = _make_prepared(user.id)
        engine = _FakeEngine(
            chunks=["你", "好", SSEEvent(data={}, event=SSEEventType.DONE)]
        )

        with patch(
            "app.services.chat_service.get_rag_engine", return_value=engine
        ):
            events = [e async for e in service.stream_chat(prepared)]

        # meta 事件先行
        first = events[0]
        assert isinstance(first, SSEEvent)
        assert first.event == SSEEventType.META
        assert first.data["conversation_id"] == str(prepared.conversation_id)
        # token 透传
        assert "你" in events and "好" in events

        # 结束后持久化 assistant 消息
        service.msg_repo.create_message.assert_awaited_once()
        call = service.msg_repo.create_message.await_args
        assert call.args[0] == prepared.conversation_id
        assert call.args[1] == "assistant"
        assert call.args[2] == "你好"

        # 记忆保存（best-effort）
        service.memory.save_session.assert_awaited_once()

        # 短事务结束即提交并释放连接
        service.db.commit.assert_awaited_once()
        service.db.close.assert_awaited_once()


class TestChatStreamEndpoint:
    """端点级：权限拒绝 → SSE error 事件；流式开始前释放 DB 连接。"""

    @pytest.mark.asyncio
    async def test_permission_denied_returns_sse_error_event(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """POST /chat/stream 权限异常 → 200 SSE 流内含 error 事件（非 403/断流）。"""

        class _DeniedService:
            async def prepare_chat(self, **kwargs):
                raise PermissionError("无权访问该对话")

        with patch(
            "app.api.v1.chat.ChatService", return_value=_DeniedService()
        ):
            resp = await auth_client.post(
                "/api/v1/chat/stream",
                json={"query": "问题", "conversation_id": str(uuid4())},
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "event: error" in resp.text
        assert "无权访问该对话" in resp.text

    @pytest.mark.asyncio
    async def test_db_session_released_before_streaming(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """准备阶段完成后即 commit + close（连接归还池），随后才开始流式。"""
        release_state = {"committed": False, "closed": False}

        class _TrackingService:
            async def prepare_chat(self, **kwargs):
                return SimpleNamespace(conversation_id=uuid4())

            async def stream_chat(self, prepared, db=None):
                # 流式开始时连接必须已释放
                assert release_state["committed"] and release_state["closed"]
                yield "ok"
                yield SSEEvent(data={}, event=SSEEventType.DONE)

        mock_db = auth_client._ekb_db  # type: ignore[attr-defined]

        async def _commit():
            release_state["committed"] = True

        async def _close():
            release_state["closed"] = True

        mock_db.commit = AsyncMock(side_effect=_commit)
        mock_db.close = AsyncMock(side_effect=_close)

        with patch(
            "app.api.v1.chat.ChatService", return_value=_TrackingService()
        ):
            resp = await auth_client.post(
                "/api/v1/chat/stream",
                json={"query": "问题"},
            )

        assert resp.status_code == 200
        assert release_state["committed"]
        assert release_state["closed"]


# ======================================================================
# 5. admin 密级口径统一（service 层回归 — 详见 tests/test_services.py 更新）
# ======================================================================


class TestAdminClassificationUnified:
    """admin 放行所有密级 — check_function / allowed_classifications /
    filter_documents 三处口径一致。"""

    def test_allowed_classifications_admin_all(self) -> None:
        from app.services.permission_service import (
            _CLEARANCE_ORDER,
            PermissionService,
        )

        user = _make_user(role="admin", clearance="internal")
        service = PermissionService(db=AsyncMock(), user=user)
        assert service.allowed_classifications() == list(_CLEARANCE_ORDER.keys())

    @pytest.mark.asyncio
    async def test_filter_documents_admin_unrestricted(self) -> None:
        from app.services.permission_service import PermissionService

        user = _make_user(role="admin", clearance="internal")
        service = PermissionService(db=AsyncMock(), user=user)
        docs = [
            SimpleNamespace(classification="public", kb_id=uuid4()),
            SimpleNamespace(classification="secret", kb_id=uuid4()),
        ]
        assert await service.filter_documents(docs) == docs
        # admin 路径不触 DB（纯放行）
        service.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_function_admin_all_kbs(self) -> None:
        from app.services.permission_service import PermissionService

        user = _make_user(role="admin", clearance="internal")
        service = PermissionService(db=AsyncMock(), user=user)
        assert await service.check_function(uuid4()) is True
