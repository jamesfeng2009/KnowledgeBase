"""批次三测试 — Provider 注册表 saas_dashscope 补全 + 飞书适配器修复。

覆盖修复点：
- reranker / vlm / asr 三处注册表补全 saas_dashscope（原实现缺注册，
  DEPLOY_MODE=saas_dashscope 时 get_* 抛 ValueError）；
- DashScopeReranker：gte-rerank 原生 HTTP API 请求构造与响应解析；
- DashScopeVisionProvider：指向 DashScope OpenAI 兼容端点；
- saas_dashscope ASR：复用自托管 ASR 服务（DashScope 录音文件转写
  需公网 URL，不适用本地文件）；
- 飞书 _extract_text：真实 API 结构 text_run.content（旧实现误读
  elem["content"]，真实环境提取全文为空）、mention_doc 标题提取、
  平铺 content 防御性回退。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ======================================================================
# 注册表完整性
# ======================================================================


class TestSaasDashscopeRegistry:
    """三处 Provider 注册表均覆盖 saas_dashscope。"""

    def test_reranker_registry_has_saas_dashscope(self) -> None:
        from app.rag.reranker import _reranker_registry

        assert "saas_dashscope" in _reranker_registry

    def test_vision_registry_has_saas_dashscope(self) -> None:
        from app.vlm.provider import _vision_registry

        assert "saas_dashscope" in _vision_registry

    def test_asr_registry_has_saas_dashscope(self) -> None:
        from app.asr.provider import _asr_registry

        assert "saas_dashscope" in _asr_registry

    def test_get_reranker_saas_dashscope(self) -> None:
        """saas_dashscope 模式 get_reranker 返回 DashScopeReranker（不再 ValueError）。"""
        with patch("app.rag.reranker.settings") as mock_settings:
            mock_settings.DEPLOY_MODE = "saas_dashscope"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_RERANK_MODEL = "gte-rerank-v2"

            from app.rag.reranker import DashScopeReranker, get_reranker

            get_reranker.cache_clear()
            reranker = get_reranker()
            assert isinstance(reranker, DashScopeReranker)
            get_reranker.cache_clear()

    def test_get_vision_provider_saas_dashscope(self) -> None:
        """saas_dashscope 模式 get_vision_provider 返回 DashScopeVisionProvider。"""
        with patch("app.vlm.provider.settings") as mock_settings:
            mock_settings.DEPLOY_MODE = "saas_dashscope"
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_VLM_MODEL = "qwen-vl-max"

            from app.vlm.provider import DashScopeVisionProvider, get_vision_provider

            get_vision_provider.cache_clear()
            provider = get_vision_provider()
            assert isinstance(provider, DashScopeVisionProvider)
            assert provider.model == "qwen-vl-max"
            get_vision_provider.cache_clear()

    def test_get_asr_provider_saas_dashscope(self) -> None:
        """saas_dashscope 模式 get_asr_provider 返回 WhisperASRProvider（自托管服务）。"""
        with patch("app.asr.provider.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.DEPLOY_MODE = "saas_dashscope"
            mock_get_settings.return_value = mock_settings

            from app.asr.provider import (
                WhisperASRProvider,
                get_asr_provider,
                reset_asr_cache,
            )

            reset_asr_cache()
            provider = get_asr_provider()
            assert isinstance(provider, WhisperASRProvider)
            reset_asr_cache()

    def test_other_modes_unaffected(self) -> None:
        """既有注册项不受新增影响（回归保护）。"""
        from app.asr.provider import _asr_registry
        from app.rag.reranker import _reranker_registry
        from app.vlm.provider import _vision_registry

        for registry in (_reranker_registry, _vision_registry, _asr_registry):
            assert "saas" in registry
            assert "private_overseas" in registry
            assert "private_domestic" in registry


# ======================================================================
# DashScopeReranker 请求/响应
# ======================================================================


class TestDashScopeReranker:
    """gte-rerank 原生 HTTP API 的请求构造与响应解析。"""

    def _make_reranker(self) -> Any:
        with patch("app.rag.reranker.settings") as mock_settings:
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_RERANK_MODEL = "gte-rerank-v2"
            from app.rag.reranker import DashScopeReranker

            return DashScopeReranker()

    @pytest.mark.asyncio
    async def test_rerank_request_payload(self) -> None:
        """请求体符合 DashScope gte-rerank API 契约。"""
        reranker = self._make_reranker()

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.42},
                ]
            }
        }
        reranker.client = AsyncMock()
        reranker.client.post = AsyncMock(return_value=resp)

        results = await reranker.rerank(
            query="报销流程",
            documents=["文档A", "文档B"],
            top_k=2,
        )

        # 验证请求
        call = reranker.client.post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        assert "dashscope.aliyuncs.com" in url
        assert "rerank" in url
        payload = call.kwargs["json"]
        assert payload["model"] == "gte-rerank-v2"
        assert payload["input"]["query"] == "报销流程"
        assert payload["input"]["documents"] == ["文档A", "文档B"]
        assert payload["parameters"]["top_n"] == 2
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test"

        # 验证响应解析 — 按 relevance_score 降序
        assert len(results) == 2
        assert results[0]["index"] == 1
        assert results[0]["score"] == 0.95
        assert results[0]["content"] == "文档B"
        assert results[1]["index"] == 0
        assert results[1]["content"] == "文档A"

    @pytest.mark.asyncio
    async def test_rerank_empty_documents(self) -> None:
        """空文档列表直接返回空，不发请求。"""
        reranker = self._make_reranker()
        reranker.client = AsyncMock()

        results = await reranker.rerank(query="q", documents=[], top_k=5)

        assert results == []
        reranker.client.post.assert_not_called()

    def test_parse_response_malformed(self) -> None:
        """异常响应结构解析为空列表（不抛异常）。"""
        from app.rag.reranker import DashScopeReranker

        assert DashScopeReranker._parse_response({}, ["a"], 5) == []
        assert DashScopeReranker._parse_response({"output": None}, ["a"], 5) == []
        assert DashScopeReranker._parse_response("not a dict", ["a"], 5) == []


# ======================================================================
# DashScopeVisionProvider
# ======================================================================


class TestDashScopeVisionProvider:
    """qwen-vl 指向 DashScope OpenAI 兼容端点。"""

    def test_init_points_to_dashscope(self) -> None:
        with patch("app.vlm.provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_API_KEY = "sk-vlm"
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_VLM_MODEL = "qwen-vl-max"

            from app.vlm.provider import DashScopeVisionProvider

            provider = DashScopeVisionProvider()
            assert provider.model == "qwen-vl-max"
            # AsyncOpenAI client 指向 DashScope endpoint
            assert "dashscope.aliyuncs.com" in str(provider.client.base_url)

    def test_model_override(self) -> None:
        with patch("app.vlm.provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_API_KEY = "sk-vlm"
            mock_settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            mock_settings.DASHSCOPE_VLM_MODEL = "qwen-vl-max"

            from app.vlm.provider import DashScopeVisionProvider

            provider = DashScopeVisionProvider(model="qwen-vl-plus")
            assert provider.model == "qwen-vl-plus"


# ======================================================================
# 飞书 _extract_text 真实结构
# ======================================================================


class TestFeishuExtractText:
    """飞书真实 API 结构 text_run.content 提取（修复回归保护）。"""

    def _adapter(self) -> Any:
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        return FeishuAdapter()

    def test_text_run_structure(self) -> None:
        """真实飞书结构 {"text_run": {"content": ...}} 正确提取。"""
        adapter = self._adapter()
        block = {
            "block_type": 2,
            "text": {
                "elements": [
                    {"text_run": {"content": "第一段", "text_element_style": {}}},
                    {"text_run": {"content": "第二段", "text_element_style": {}}},
                ]
            },
        }

        assert adapter._extract_text(block) == "第一段第二段"

    def test_heading_text_run(self) -> None:
        """标题块 text_run 结构提取。"""
        adapter = self._adapter()
        block = {
            "block_type": 3,
            "heading1": {"elements": [{"text_run": {"content": "架构设计"}}]},
        }

        assert adapter._extract_text(block) == "架构设计"

    def test_mention_doc_extracts_title(self) -> None:
        """mention_doc 元素提取文档标题（保留语义）。"""
        adapter = self._adapter()
        block = {
            "block_type": 2,
            "text": {
                "elements": [
                    {"text_run": {"content": "详见"}},
                    {"mention_doc": {"title": "报销制度", "token": "doccnX"}},
                ]
            },
        }

        assert adapter._extract_text(block) == "详见报销制度"

    def test_flat_content_fallback(self) -> None:
        """平铺 content 字段防御性回退（兼容旧结构）。"""
        adapter = self._adapter()
        block = {
            "block_type": 2,
            "text": {"elements": [{"content": "旧结构文本"}]},
        }

        assert adapter._extract_text(block) == "旧结构文本"

    def test_empty_elements_returns_empty(self) -> None:
        """空 elements / 非 dict 元素不崩溃。"""
        adapter = self._adapter()

        assert adapter._extract_text({"block_type": 2, "text": {"elements": []}}) == ""
        assert adapter._extract_text(
            {"block_type": 2, "text": {"elements": ["not-a-dict", None]}}
        ) == ""
        assert adapter._extract_text({"block_type": 2}) == ""

    def test_blocks_to_markdown_real_structure(self) -> None:
        """端到端：真实 API 结构的块列表转 Markdown 非空（修复前为空）。"""
        adapter = self._adapter()
        blocks = [
            {"block_id": "page1", "block_type": 1, "children": ["b1", "b2"]},
            {
                "block_id": "b1",
                "block_type": 3,
                "heading1": {"elements": [{"text_run": {"content": "制度总览"}}]},
            },
            {
                "block_id": "b2",
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": "本制度适用于全体员工。"}}]},
            },
        ]

        result = adapter._blocks_to_markdown(blocks, "员工制度")

        assert "# 制度总览" in result
        assert "本制度适用于全体员工。" in result


# ======================================================================
# DashScope 新配置项
# ======================================================================


class TestDashScopeNewConfig:
    """新增配置项默认值。"""

    def test_rerank_model_default(self) -> None:
        from app.config import Settings

        settings = Settings(_env_file=None)
        assert settings.DASHSCOPE_RERANK_MODEL == "gte-rerank-v2"

    def test_vlm_model_default(self) -> None:
        from app.config import Settings

        settings = Settings(_env_file=None)
        assert settings.DASHSCOPE_VLM_MODEL == "qwen-vl-max"
