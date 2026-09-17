"""企业微信集成单测 — P1 IM 集成（connector + bot 服务 + 回调验签）。

测试策略：
    - WeComConnector：mock httpx 验证未配置降级 / 搜索 / 连接测试；
    - WeComBotService：mock httpx 验证推送成功/失败，纯函数验签算法；
    - 回调验签：用标准企微算法构造正反例。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.registry import connector_registry
from app.connectors.wecom import WeComConnector
from app.services.wecom_bot_service import WeComBotService


class TestWeComConnector:
    def test_registered_in_registry(self) -> None:
        conn = connector_registry.get("wecom")
        assert conn is not None
        assert isinstance(conn, WeComConnector)

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_disabled(self) -> None:
        from types import SimpleNamespace

        with patch("app.connectors.wecom.settings", SimpleNamespace(CONNECTOR_WECOM_ENABLED=False)):
            conn = WeComConnector()
            assert await conn.search("张三") == []

    @pytest.mark.asyncio
    async def test_search_maps_users(self) -> None:
        from types import SimpleNamespace

        with patch(
            "app.connectors.wecom.settings",
            SimpleNamespace(
                CONNECTOR_WECOM_ENABLED=True, WECOM_CORP_ID="corp1", WECOM_SECRET="secret"
            ),
        ):
            conn = WeComConnector()

        async def fake_get_token():
            return "TOKEN"

        async def fake_search(token, keyword, top_k):
            return [
                {"userid": "u1", "name": "张三", "department_label": "技术部", "position": "工程师"}
            ]

        conn._get_access_token = fake_get_token
        conn._search_users = fake_search

        results = await conn.search("张三")
        assert len(results) == 1
        assert results[0].source == "wecom"
        assert results[0].title == "张三"
        assert results[0].metadata == {"userid": "u1"}

    @pytest.mark.asyncio
    async def test_get_access_token(self) -> None:
        from types import SimpleNamespace

        with patch(
            "app.connectors.wecom.settings",
            SimpleNamespace(
                CONNECTOR_WECOM_ENABLED=True, WECOM_CORP_ID="corp1", WECOM_SECRET="secret"
            ),
        ):
            conn = WeComConnector()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "access_token": "abc123"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("app.connectors.wecom.httpx.AsyncClient", return_value=mock_client):
            token = await conn._get_access_token()

        assert token == "abc123"
        call_kwargs = mock_client.get.call_args
        assert call_kwargs.kwargs["params"]["corpid"] == "corp1"


class TestWeComBotService:
    def test_signature_algorithm(self) -> None:
        """标准企微验签算法：sha1(排序拼接)。"""
        import hashlib

        token, timestamp, nonce, echostr = "TESTTOKEN", "1409659813", "1372623149", "hello"
        raw = "".join(sorted([token, timestamp, nonce, echostr]))
        expect = hashlib.sha1(raw.encode()).hexdigest()
        assert WeComBotService.verify_signature(
            token, timestamp, nonce, echostr, expect
        ) is True
        assert WeComBotService.verify_signature(
            token, timestamp, nonce, echostr, "0" * 40
        ) is False

    @pytest.mark.asyncio
    async def test_send_markdown_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("app.services.wecom_bot_service.httpx.AsyncClient", return_value=mock_client):
            ok = await WeComBotService(webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send").send_markdown("# 标题")

        assert ok is True
        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert sent_payload["msgtype"] == "markdown"

    @pytest.mark.asyncio
    async def test_send_markdown_no_webhook(self) -> None:
        assert await WeComBotService(webhook="").send_markdown("x") is False

    @pytest.mark.asyncio
    async def test_send_markdown_failure(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 93000, "errmsg": "invalid webhook"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("app.services.wecom_bot_service.httpx.AsyncClient", return_value=mock_client):
            ok = await WeComBotService(webhook="https://x/send").send_markdown("x")

        assert ok is False

    @pytest.mark.asyncio
    async def test_send_markdown_network_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("timeout")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("app.services.wecom_bot_service.httpx.AsyncClient", return_value=mock_client):
            ok = await WeComBotService(webhook="https://x/send").send_markdown("x")

        assert ok is False

    def test_verify_callback_instance(self) -> None:
        service = WeComBotService(token="TKN")
        import hashlib

        parts = sorted(["TKN", "1", "2", "echo"])
        sig = hashlib.sha1("".join(parts).encode()).hexdigest()
        assert service.verify_callback("1", "2", "echo", sig) is True


class TestWeComAPI:
    def test_router_registered(self) -> None:
        from app.api.v1.im import router

        paths = {r.path for r in router.routes}
        assert "/im/wecom/callback" in paths
        assert "/im/wecom/status" in paths
        assert "/im/wecom/bot/markdown" in paths
