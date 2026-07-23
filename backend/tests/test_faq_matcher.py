"""
FAQ 快捷匹配器单元测试 — app/rag/faq_matcher.py。

覆盖：
    - FAQMatchResult 数据结构
    - _extract_answer 辅助函数（中英文 FAQ 格式）
    - FAQMatcher.match — 命中 / 未命中 / 阈值不足 / 降级
    - OpenSearch 不可用降级
    - 空查询跳过
    - kb_ids 过滤
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.rag.faq_matcher import FAQMatcher, FAQMatchResult, _extract_answer


# ======================================================================
# FAQMatchResult
# ======================================================================

class TestFAQMatchResult:
    """FAQMatchResult 数据结构测试。"""

    def test_no_match(self):
        result = FAQMatchResult(matched=False)
        d = result.to_dict()
        assert d["matched"] is False
        assert d["answer"] == ""
        assert d["score"] == 0.0

    def test_match(self):
        result = FAQMatchResult(
            matched=True,
            answer="报销需要填写报销单",
            score=18.5,
            chunk_id="c1",
            doc_id="d1",
        )
        d = result.to_dict()
        assert d["matched"] is True
        assert d["answer"] == "报销需要填写报销单"
        assert d["score"] == 18.5
        assert d["chunk_id"] == "c1"
        assert d["doc_id"] == "d1"


# ======================================================================
# _extract_answer
# ======================================================================

class TestExtractAnswer:
    """_extract_answer 辅助函数测试。"""

    def test_chinese_format(self):
        """中文 FAQ 格式：问：X\n\n答：Y"""
        content = "问：报销流程是什么？\n\n答：填写报销单并提交给财务部门。"
        assert _extract_answer(content) == "填写报销单并提交给财务部门。"

    def test_chinese_format_no_newline(self):
        """中文 FAQ 格式：问：X\n答：Y"""
        content = "问：报销流程\n答：填写报销单"
        assert _extract_answer(content) == "填写报销单"

    def test_english_format(self):
        """英文 FAQ 格式：Q: X\nA: Y"""
        content = "Q: How to apply?\nA: Fill the form."
        assert _extract_answer(content) == "Fill the form."

    def test_english_format_answer_keyword(self):
        """英文 FAQ 格式：Q: X\nAnswer: Y"""
        content = "Q: How to apply?\nAnswer: Fill the form."
        assert _extract_answer(content) == "Fill the form."

    def test_no_format_return_original(self):
        """格式不匹配 → 返回原文。"""
        content = "这是一段普通文本，没有问答格式。"
        assert _extract_answer(content) == "这是一段普通文本，没有问答格式。"

    def test_empty_content(self):
        assert _extract_answer("") == ""


# ======================================================================
# FAQMatcher.match
# ======================================================================

def _make_mock_response(
    hits: list[dict] | None = None,
    status_code: int = 200,
) -> MagicMock:
    """构造模拟 httpx.Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"hits": {"hits": hits or []}}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestFAQMatcherMatch:
    """FAQMatcher.match 方法测试。"""

    @pytest.mark.asyncio
    async def test_faq_hit(self):
        """FAQ 精准命中 — score > threshold。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([
            {
                "_score": 22.5,
                "_source": {
                    "doc_id": "d1",
                    "chunk_id": "c1",
                    "content": "问：报销流程是什么？\n\n答：填写报销单并提交给财务。",
                    "title_path": "财务制度 > 报销",
                },
            }
        ]))
        matcher = FAQMatcher(http_client=mock_http, score_threshold=15.0)

        result = await matcher.match("报销流程是什么？")

        assert result.matched is True
        assert result.score == 22.5
        assert "填写报销单" in result.answer
        assert result.chunk_id == "c1"
        assert result.doc_id == "d1"

    @pytest.mark.asyncio
    async def test_faq_below_threshold(self):
        """FAQ score 低于阈值 — 不命中。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([
            {
                "_score": 10.0,
                "_source": {
                    "doc_id": "d1",
                    "chunk_id": "c1",
                    "content": "问：其他\n\n答：其他答案",
                },
            }
        ]))
        matcher = FAQMatcher(http_client=mock_http, score_threshold=15.0)

        result = await matcher.match("模糊查询")

        assert result.matched is False

    @pytest.mark.asyncio
    async def test_no_hits(self):
        """无 FAQ chunk 命中 — 返回未匹配。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([]))
        matcher = FAQMatcher(http_client=mock_http)

        result = await matcher.match("冷门查询")

        assert result.matched is False

    @pytest.mark.asyncio
    async def test_empty_query(self):
        """空查询 — 直接返回未匹配，不调用 OpenSearch。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock()
        matcher = FAQMatcher(http_client=mock_http)

        result = await matcher.match("")

        assert result.matched is False
        mock_http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_query(self):
        """纯空白查询 — 跳过。"""
        matcher = FAQMatcher(http_client=MagicMock())
        result = await matcher.match("   ")
        assert result.matched is False

    @pytest.mark.asyncio
    async def test_opensearch_unavailable_degrade(self):
        """OpenSearch 不可用 → 降级返回未匹配。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(side_effect=ConnectionError("OS unavailable"))
        matcher = FAQMatcher(http_client=mock_http)

        result = await matcher.match("查询")

        assert result.matched is False

    @pytest.mark.asyncio
    async def test_http_error_degrade(self):
        """HTTP 4xx/5xx → 降级。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response(status_code=500))
        matcher = FAQMatcher(http_client=mock_http)

        result = await matcher.match("查询")

        assert result.matched is False

    @pytest.mark.asyncio
    async def test_kb_ids_filter_in_query(self):
        """kb_ids 过滤 — 查询 payload 包含 kb_id filter。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([]))
        matcher = FAQMatcher(http_client=mock_http)

        await matcher.match("查询", kb_ids=["kb1", "kb2"])

        call_args = mock_http.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        # 验证 filter 中包含 kb_id terms
        filters = payload["query"]["bool"]["filter"]
        kb_filter = [f for f in filters if "terms" in f and "kb_id" in f["terms"]]
        assert len(kb_filter) == 1
        assert kb_filter[0]["terms"]["kb_id"] == ["kb1", "kb2"]

    @pytest.mark.asyncio
    async def test_content_type_filter_in_query(self):
        """查询 payload 包含 content_type=faq filter。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([]))
        matcher = FAQMatcher(http_client=mock_http)

        await matcher.match("查询")

        call_args = mock_http.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        filters = payload["query"]["bool"]["filter"]
        ct_filter = [f for f in filters if "term" in f and "content_type" in f["term"]]
        assert len(ct_filter) == 1
        assert ct_filter[0]["term"]["content_type"] == "faq"

    @pytest.mark.asyncio
    async def test_extract_answer_from_english_faq(self):
        """命中英文 FAQ chunk — 正确提取答案。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([
            {
                "_score": 20.0,
                "_source": {
                    "doc_id": "d1",
                    "chunk_id": "c1",
                    "content": "Q: How to reset password?\nA: Click reset link in email.",
                },
            }
        ]))
        matcher = FAQMatcher(http_client=mock_http, score_threshold=15.0)

        result = await matcher.match("How to reset password?")

        assert result.matched is True
        assert "Click reset link" in result.answer

    @pytest.mark.asyncio
    async def test_malformed_content_returns_original(self):
        """FAQ chunk 内容格式不匹配 → 返回原文。"""
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=_make_mock_response([
            {
                "_score": 20.0,
                "_source": {
                    "doc_id": "d1",
                    "chunk_id": "c1",
                    "content": "这是一段没有问答格式的文本内容。",
                },
            }
        ]))
        matcher = FAQMatcher(http_client=mock_http, score_threshold=15.0)

        result = await matcher.match("查询")

        assert result.matched is True
        assert "没有问答格式" in result.answer

    @pytest.mark.asyncio
    async def test_retry_after_opensearch_recovery(self):
        """OpenSearch 恢复后自动重试探测。"""
        mock_http = MagicMock()
        # 第一次失败，第二次成功
        mock_http.post = AsyncMock(
            side_effect=[
                ConnectionError("OS unavailable"),
                _make_mock_response([
                    {
                        "_score": 20.0,
                        "_source": {
                            "doc_id": "d1",
                            "chunk_id": "c1",
                            "content": "问：测试\n\n答：结果",
                        },
                    }
                ]),
            ]
        )
        matcher = FAQMatcher(http_client=mock_http, score_threshold=15.0)

        # 第一次 — 降级
        result1 = await matcher.match("测试")
        assert result1.matched is False

        # 手动重置重试时间，模拟等待后恢复
        matcher._retry_at = 0.0

        # 第二次 — 恢复
        result2 = await matcher.match("测试")
        assert result2.matched is True
