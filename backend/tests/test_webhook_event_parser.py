"""Webhook 事件解析单测 — 飞书/Confluence 事件格式解析。

覆盖：
    - 飞书 drive.file.updated_v1 → 提取 file_token
    - 飞书 challenge 检测
    - 飞书非关注事件 / 缺 file_token → None
    - Confluence page_updated → 提取 page.id
    - Confluence eventId 合成（缺 eventId 时）
    - Confluence 非关注事件 → None
    - 统一入口 parse_webhook_event 分发
"""
from __future__ import annotations

import pytest

from app.services.webhook_event_parser import (
    ParsedWebhookEvent,
    _synthesize_confluence_event_id,
    is_feishu_challenge,
    parse_confluence_event,
    parse_feishu_event,
    parse_webhook_event,
)


# ==================================================================
# 飞书事件解析
# ==================================================================

class TestFeishuParser:
    """飞书 event v2 解析。"""

    def test_drive_file_updated_parsed(self) -> None:
        """drive.file.updated_v1 → 提取 file_token。"""
        body = {
            "schema": "2.0",
            "header": {
                "event_id": "evt-001",
                "event_type": "drive.file.updated_v1",
                "tenant_key": "tenant-xxx",
            },
            "event": {
                "file_token": "doccnABCD1234",
                "file_type": "docx",
                "file_name": "报销政策v2",
            },
        }
        parsed = parse_feishu_event(body)
        assert parsed is not None
        assert parsed.adapter_id == "feishu"
        assert parsed.source_doc_id == "doccnABCD1234"
        assert parsed.event_id == "evt-001"
        assert parsed.event_type == "drive.file.updated_v1"
        assert parsed.tenant_key == "tenant-xxx"

    def test_drive_file_created_parsed(self) -> None:
        """drive.file.created_v1 → 同样触发同步。"""
        body = {
            "header": {"event_id": "e2", "event_type": "drive.file.created_v1"},
            "event": {"file_token": "doccnNEW"},
        }
        parsed = parse_feishu_event(body)
        assert parsed is not None
        assert parsed.source_doc_id == "doccnNEW"

    def test_missing_file_token_returns_none(self) -> None:
        body = {
            "header": {"event_id": "e3", "event_type": "drive.file.updated_v1"},
            "event": {"file_type": "docx"},  # 无 file_token
        }
        assert parse_feishu_event(body) is None

    def test_irrelevant_event_returns_none(self) -> None:
        """contact.user.updated_v3 非文档事件 → None。"""
        body = {
            "header": {"event_id": "e4", "event_type": "contact.user.updated_v3"},
            "event": {},
        }
        assert parse_feishu_event(body) is None

    def test_title_updated_event_parsed(self) -> None:
        """drive.file.title_updated_v1 也触发同步。"""
        body = {
            "header": {"event_id": "e5", "event_type": "drive.file.title_updated_v1"},
            "event": {"file_token": "doccnT"},
        }
        parsed = parse_feishu_event(body)
        assert parsed is not None
        assert parsed.source_doc_id == "doccnT"


class TestFeishuChallenge:
    """飞书 URL 验证 challenge 检测。"""

    def test_challenge_detected(self) -> None:
        body = {
            "challenge": "ajksdhfkjahdfkahdfkhakj",
            "token": "verification_token",
            "type": "url_verification",
        }
        challenge = is_feishu_challenge(body)
        assert challenge == "ajksdhfkjahdfkahdfkhakj"

    def test_non_challenge_returns_none(self) -> None:
        body = {"header": {"event_type": "drive.file.updated_v1"}, "event": {}}
        assert is_feishu_challenge(body) is None

    def test_challenge_missing_value(self) -> None:
        """type=url_verification 但无 challenge 值 → 返回空串。"""
        body = {"type": "url_verification"}
        assert is_feishu_challenge(body) == ""


# ==================================================================
# Confluence 事件解析
# ==================================================================

class TestConfluenceParser:
    """Confluence webhook 解析。"""

    def test_page_updated_parsed(self) -> None:
        body = {
            "webhookEvent": "page_updated",
            "eventId": "conf-evt-001",
            "page": {
                "id": 123456789,
                "title": "API 规范",
                "version": {"number": 5},
            },
        }
        parsed = parse_confluence_event(body)
        assert parsed is not None
        assert parsed.adapter_id == "confluence"
        assert parsed.source_doc_id == "123456789"  # int → str
        assert parsed.event_id == "conf-evt-001"
        assert parsed.event_type == "page_updated"

    def test_page_created_parsed(self) -> None:
        body = {
            "webhookEvent": "page_created",
            "eventId": "e2",
            "page": {"id": "98765"},
        }
        parsed = parse_confluence_event(body)
        assert parsed is not None
        assert parsed.source_doc_id == "98765"

    def test_missing_page_id_returns_none(self) -> None:
        body = {"webhookEvent": "page_updated", "page": {"title": "x"}}
        assert parse_confluence_event(body) is None

    def test_irrelevant_event_returns_none(self) -> None:
        """page_trashed 等非关注事件 → None。"""
        body = {"webhookEvent": "page_trashed", "page": {"id": 1}}
        assert parse_confluence_event(body) is None

    def test_event_id_synthesized_when_missing(self) -> None:
        """无 eventId 时用 event_type + page_id + timestamp 合成。"""
        body = {
            "webhookEvent": "page_updated",
            "timestamp": 1700000000,
            "page": {"id": 123},
        }
        parsed = parse_confluence_event(body)
        assert parsed is not None
        expected = _synthesize_confluence_event_id("page_updated", "123", 1700000000)
        assert parsed.event_id == expected

    def test_synthesized_id_stable(self) -> None:
        """同一事件多次合成产生相同 ID（幂等性保证）。"""
        id1 = _synthesize_confluence_event_id("page_updated", "123", 1700000000)
        id2 = _synthesize_confluence_event_id("page_updated", "123", 1700000000)
        assert id1 == id2

    def test_different_events_different_synthesized_ids(self) -> None:
        """不同事件合成不同 ID。"""
        id1 = _synthesize_confluence_event_id("page_updated", "123", 1700000000)
        id2 = _synthesize_confluence_event_id("page_updated", "124", 1700000000)
        assert id1 != id2

    def test_page_restored_parsed(self) -> None:
        """page_restored 也触发同步（恢复的页面可能内容变化）。"""
        body = {
            "webhookEvent": "page_restored",
            "eventId": "e3",
            "page": {"id": 555},
        }
        parsed = parse_confluence_event(body)
        assert parsed is not None
        assert parsed.source_doc_id == "555"


# ==================================================================
# 统一入口
# ==================================================================

class TestParseDispatch:
    """parse_webhook_event 按 adapter_id 分发。"""

    def test_feishu_dispatch(self) -> None:
        body = {
            "header": {"event_id": "e1", "event_type": "drive.file.updated_v1"},
            "event": {"file_token": "doccnX"},
        }
        parsed = parse_webhook_event("feishu", body)
        assert parsed is not None
        assert parsed.adapter_id == "feishu"
        assert parsed.source_doc_id == "doccnX"

    def test_confluence_dispatch(self) -> None:
        body = {"webhookEvent": "page_updated", "eventId": "e2", "page": {"id": 1}}
        parsed = parse_webhook_event("confluence", body)
        assert parsed is not None
        assert parsed.adapter_id == "confluence"

    def test_unsupported_adapter_returns_none(self) -> None:
        assert parse_webhook_event("notion", {}) is None
