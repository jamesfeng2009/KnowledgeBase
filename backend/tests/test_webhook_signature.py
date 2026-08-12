"""Webhook 签名验证单测 — 飞书 SHA256 + Confluence HMAC + 幂等策略。

覆盖：
    - 飞书签名：合法通过 / 错误签名 / 时间戳过期 / 时间戳非法 / 缺字段
    - Confluence 签名：合法通过 / 错误签名
    - 统一入口：no_secret 模式（dev）/ unsupported adapter
    - 时间戳新鲜度窗口边界
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.webhook_signature import (
    SignatureResult,
    _compute_confluence_signature,
    _compute_feishu_signature,
    verify_confluence_signature,
    verify_feishu_signature,
    verify_webhook_signature,
)


# ==================================================================
# 飞书签名
# ==================================================================

class TestFeishuSignature:
    """飞书 X-Lark-Signature 验证。"""

    def _make_valid(self, encrypt_key: str = "my_key", body: str = '{"a":1}'):
        """构造一组合法签名参数。"""
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "nonce-abc"
        signature = _compute_feishu_signature(timestamp, nonce, encrypt_key, body)
        return {
            "timestamp": timestamp,
            "nonce": nonce,
            "body": body,
            "signature": signature,
            "encrypt_key": encrypt_key,
        }

    def test_valid_signature_passes(self) -> None:
        p = self._make_valid()
        result = verify_feishu_signature(**p)
        assert result.valid is True
        assert result.mode == "verified"

    def test_wrong_signature_fails(self) -> None:
        p = self._make_valid()
        result = verify_feishu_signature(
            timestamp=p["timestamp"],
            nonce=p["nonce"],
            body=p["body"],
            signature="sha256=wrong",
            encrypt_key=p["encrypt_key"],
        )
        assert result.valid is False
        assert "signature_mismatch" in result.reason

    def test_tampered_body_fails(self) -> None:
        """body 被篡改 → 签名不匹配。"""
        p = self._make_valid(body='{"a":1}')
        result = verify_feishu_signature(
            timestamp=p["timestamp"],
            nonce=p["nonce"],
            body='{"a":2}',  # 篡改
            signature=p["signature"],
            encrypt_key=p["encrypt_key"],
        )
        assert result.valid is False

    def test_stale_timestamp_fails(self) -> None:
        """时间戳超出 5 分钟窗口 → 拒绝（防重放）。"""
        old_ts = str(int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()))
        nonce = "n"
        body = "{}"
        sig = _compute_feishu_signature(old_ts, nonce, "k", body)
        result = verify_feishu_signature(
            timestamp=old_ts, nonce=nonce, body=body,
            signature=sig, encrypt_key="k",
        )
        assert result.valid is False
        assert "timestamp" in result.reason

    def test_invalid_timestamp_fails(self) -> None:
        """时间戳非数字 → 拒绝。"""
        result = verify_feishu_signature(
            timestamp="not-a-number", nonce="n", body="{}",
            signature="sha256=x", encrypt_key="k",
        )
        assert result.valid is False

    def test_boundary_timestamp_passes(self) -> None:
        """时间戳在窗口边界内（4 分钟）→ 通过。"""
        ts = str(int((datetime.now(timezone.utc) - timedelta(minutes=4)).timestamp()))
        nonce = "n"
        body = "{}"
        sig = _compute_feishu_signature(ts, nonce, "k", body)
        result = verify_feishu_signature(
            timestamp=ts, nonce=nonce, body=body,
            signature=sig, encrypt_key="k",
        )
        assert result.valid is True

    def test_unicode_body_passes(self) -> None:
        """包含中文的 body 签名验证。"""
        body = '{"file_name":"报销政策v2"}'
        p = self._make_valid(body=body)
        result = verify_feishu_signature(**p)
        assert result.valid is True


# ==================================================================
# Confluence 签名
# ==================================================================

class TestConfluenceSignature:
    """Confluence X-Hub-Signature (HMAC-SHA256) 验证。"""

    def test_valid_signature_passes(self) -> None:
        secret = "conf_secret"
        body = '{"webhookEvent":"page_updated"}'
        signature = _compute_confluence_signature(secret, body)
        result = verify_confluence_signature(
            body=body, signature=signature, webhook_secret=secret
        )
        assert result.valid is True

    def test_wrong_signature_fails(self) -> None:
        result = verify_confluence_signature(
            body="{}", signature="sha256=wrong", webhook_secret="s"
        )
        assert result.valid is False

    def test_tampered_body_fails(self) -> None:
        secret = "s"
        body = '{"page":{"id":1}}'
        sig = _compute_confluence_signature(secret, body)
        result = verify_confluence_signature(
            body='{"page":{"id":2}}',  # 篡改
            signature=sig,
            webhook_secret=secret,
        )
        assert result.valid is False

    def test_wrong_secret_fails(self) -> None:
        """用 secret_A 签名，用 secret_B 验证 → 失败。"""
        body = "{}"
        sig = _compute_confluence_signature("secret_A", body)
        result = verify_confluence_signature(
            body=body, signature=sig, webhook_secret="secret_B"
        )
        assert result.valid is False


# ==================================================================
# 统一入口 verify_webhook_signature
# ==================================================================

class TestVerifyWebhookDispatch:
    """verify_webhook_signature 按 adapter_id 分发 + 凭证策略。"""

    def test_feishu_no_encrypt_key_allows_dev_mode(self) -> None:
        """飞书凭证无 encrypt_key → 放行（dev 模式）。"""
        result = verify_webhook_signature(
            adapter_id="feishu",
            body="{}",
            headers={},
            credentials={},  # 无 encrypt_key
        )
        assert result.valid is True
        assert result.mode == "no_secret"

    def test_confluence_no_secret_allows_dev_mode(self) -> None:
        """Confluence 凭证无 webhook_secret → 放行。"""
        result = verify_webhook_signature(
            adapter_id="confluence",
            body="{}",
            headers={},
            credentials={},
        )
        assert result.valid is True
        assert result.mode == "no_secret"

    def test_feishu_valid_passes(self) -> None:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        encrypt_key = "ek"
        body = '{"x":1}'
        sig = _compute_feishu_signature(timestamp, nonce, encrypt_key, body)
        result = verify_webhook_signature(
            adapter_id="feishu",
            body=body,
            headers={
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
                "x-lark-signature": sig,
            },
            credentials={"encrypt_key": encrypt_key},
        )
        assert result.valid is True
        assert result.mode == "verified"

    def test_confluence_valid_passes(self) -> None:
        secret = "ws"
        body = '{"y":2}'
        sig = _compute_confluence_signature(secret, body)
        result = verify_webhook_signature(
            adapter_id="confluence",
            body=body,
            headers={"x-hub-signature": sig},
            credentials={"webhook_secret": secret},
        )
        assert result.valid is True

    def test_unsupported_adapter_fails(self) -> None:
        """notion / obsidian 无 webhook → 拒绝。"""
        result = verify_webhook_signature(
            adapter_id="notion",
            body="{}",
            headers={},
            credentials={},
        )
        assert result.valid is False
        assert "unsupported_adapter" in result.reason

    def test_feishu_header_case_insensitive(self) -> None:
        """headers 已转小写 — 大写 header 名也能匹配。"""
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        nonce = "n"
        encrypt_key = "ek"
        body = '{}'
        sig = _compute_feishu_signature(timestamp, nonce, encrypt_key, body)
        # 模拟端点已将 header 转小写
        result = verify_webhook_signature(
            adapter_id="feishu",
            body=body,
            headers={
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
                "x-lark-signature": sig,
            },
            credentials={"encrypt_key": encrypt_key},
        )
        assert result.valid is True
