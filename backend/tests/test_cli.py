"""EKB CLI 客户端单测 — P2 CLI。

测试策略：
    - mock httpx.Client 验证认证头、请求路径、响应解包与错误包装；
    - 命令能力（search / kb / wiki / queues / upload）逐一覆盖；
    - CLI 参数解析冒烟。
"""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.cli import EkbClient, EkbClientError
from app.cli.client import EkbClient as EkbClientCls


def _make_client(mock_http: MagicMock) -> EkbClient:
    return EkbClient(
        base_url="http://localhost:8000",
        api_key="sk-test",
        client=mock_http,
    )


def _ok_response(data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"code": 0, "data": data, "message": "success"}
    resp.text = ""
    return resp


class TestAuth:
    def test_headers_include_credentials(self) -> None:
        """认证头在 httpx.Client 构造时注入。"""
        with patch("app.cli.client.httpx.Client") as mock_client_cls:
            EkbClientCls(base_url="http://localhost:8000", api_key="sk-test")
            headers = mock_client_cls.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer sk-test"
            assert headers["X-API-Key"] == "sk-test"


class TestCommands:
    def test_list_kb(self) -> None:
        http = MagicMock()
        http.request.return_value = _ok_response([{"id": "1", "name": "产品"}])
        client = _make_client(http)
        assert client.list_knowledge_bases() == [{"id": "1", "name": "产品"}]
        assert http.request.call_args.args[1] == "/api/v1/knowledge"

    def test_search_sends_params(self) -> None:
        http = MagicMock()
        http.request.return_value = _ok_response([{"content": "x"}])
        client = _make_client(http)
        client.search("退款", top_k=3, kb_id="kb1")
        kwargs = http.request.call_args.kwargs
        assert kwargs["params"] == {"query": "退款", "top_k": 3, "kb_id": "kb1"}

    def test_upload_document_multipart(self) -> None:
        http = MagicMock()
        http.post.return_value = _ok_response({"doc_id": "d1"})
        client = _make_client(http)
        with patch("pathlib.Path.exists", return_value=True), patch(
            "pathlib.Path.read_bytes", return_value=b"%PDF"
        ):
            result = client.upload_document("kb1", "/tmp/a.pdf", "标题")
        assert result == {"doc_id": "d1"}
        call = http.post.call_args
        assert call.args[0] == "/api/v1/documents/upload"
        assert call.kwargs["data"] == {"kb_id": "kb1", "title": "标题"}
        assert "file" in call.kwargs["files"]

    def test_upload_missing_file(self) -> None:
        http = MagicMock()
        client = _make_client(http)
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(EkbClientError):
                client.upload_document("kb1", "/tmp/nope.pdf")

    def test_wiki_generate(self) -> None:
        http = MagicMock()
        http.request.return_value = _ok_response({"created": 2})
        client = _make_client(http)
        result = client.wiki_generate("kb1")
        assert result == {"created": 2}
        assert http.request.call_args.args[1] == "/api/v1/knowledge/kb1/wiki/generate"

    def test_queue_status(self) -> None:
        http = MagicMock()
        http.request.return_value = _ok_response({"queues": []})
        client = _make_client(http)
        assert client.queue_status() == {"queues": []}


class TestErrors:
    def test_http_error_wrapped(self) -> None:
        http = MagicMock()
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        http.request.return_value = resp
        client = _make_client(http)
        with pytest.raises(EkbClientError, match="401"):
            client.list_knowledge_bases()

    def test_business_error_wrapped(self) -> None:
        http = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": 1001, "message": "知识库不存在"}
        http.request.return_value = resp
        client = _make_client(http)
        with pytest.raises(EkbClientError, match="1001"):
            client.create_knowledge_base("x")


class TestCLIEntry:
    def test_parser_builds(self) -> None:
        from scripts.ekb_cli import build_parser

        parser = build_parser()
        assert parser is not None

    def test_main_missing_credentials(self) -> None:
        from scripts import ekb_cli

        with patch.object(ekb_cli, "sys") as mock_sys:
            mock_sys.stderr = io.StringIO()
            mock_sys.exit = MagicMock(side_effect=SystemExit(2))
            with patch.dict("os.environ", {}, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    ekb_cli.main(["kb", "list"])
        assert exc_info.value.code == 2
        mock_sys.exit.assert_called_once_with(2)
