"""Webhook 签名验证 — P1 Webhook 主动同步的安全防线。

两类平台签名机制：
    - 飞书: X-Lark-Signature = SHA256(timestamp + nonce + encrypt_key + body)
      + 时间戳新鲜度校验（防重放）
    - Confluence: X-Hub-Signature = HMAC-SHA256(webhook_secret, body)

凭证来源：ExternalCredential.credentials 加密 dict 中的
    - 飞书: ``encrypt_key`` 字段
    - Confluence: ``webhook_secret`` 字段

策略：
    - 凭证中配置了 secret → 必须验证签名，失败则拒绝
    - 凭证中未配置 secret → 放行并记录 warning（dev 模式可用，
      生产环境应强制配置）

遵循单一职责：仅做签名计算与比对，不涉及事件解析或 DB 访问。
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from app.utils.logger import get_logger

log = get_logger(__name__)

# 飞书时间戳新鲜度窗口（秒）— 超过此值视为重放攻击
_FEISHU_TIMESTAMP_TOLERANCE_SECONDS: int = 300  # 5 分钟


@dataclass
class SignatureResult:
    """签名验证结果。

    Attributes:
        valid: 签名是否通过。
        reason: 失败原因（valid=False 时）。
        mode: 验证模式："verified"（已验证）/ "no_secret"（未配置 secret，放行）。
    """

    valid: bool
    reason: str = ""
    mode: str = "verified"


# ==================================================================
# 飞书签名验证
# ==================================================================

def verify_feishu_signature(
    *,
    timestamp: str,
    nonce: str,
    body: str,
    signature: str,
    encrypt_key: str,
) -> SignatureResult:
    """验证飞书 webhook 签名（X-Lark-Signature）。

    飞书签名算法::

        signature = "sha256=" + SHA256(timestamp + nonce + encrypt_key + body)

    Args:
        timestamp: X-Lark-Request-Timestamp header 值（秒级时间戳字符串）。
        nonce: X-Lark-Request-Nonce header 值。
        body: 原始请求体字符串（未解析的 JSON 文本）。
        signature: X-Lark-Signature header 值（形如 ``sha256=xxxx``）。
        encrypt_key: 飞书应用配置的 Encrypt Key（存储在凭证中）。

    Returns:
        SignatureResult — valid=True 表示通过。
    """
    # 1. 时间戳新鲜度校验（防重放）
    if not _is_timestamp_fresh(timestamp):
        return SignatureResult(
            valid=False,
            reason=f"timestamp_outdated or invalid: {timestamp}",
        )

    # 2. 计算签名
    expected = _compute_feishu_signature(timestamp, nonce, encrypt_key, body)

    # 3. 比对（hmac.compare_digest 防时序攻击）
    if not hmac.compare_digest(expected, signature):
        return SignatureResult(
            valid=False,
            reason="signature_mismatch",
        )

    return SignatureResult(valid=True, mode="verified")


def _compute_feishu_signature(
    timestamp: str, nonce: str, encrypt_key: str, body: str
) -> str:
    """计算飞书签名：sha256= + SHA256(timestamp + nonce + encrypt_key + body)。"""
    raw = f"{timestamp}{nonce}{encrypt_key}{body}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256={digest}"


def _is_timestamp_fresh(timestamp: str) -> bool:
    """校验时间戳是否在容忍窗口内（防重放攻击）。"""
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    return abs(now - ts) <= _FEISHU_TIMESTAMP_TOLERANCE_SECONDS


# ==================================================================
# Confluence 签名验证
# ==================================================================

def verify_confluence_signature(
    *,
    body: str,
    signature: str,
    webhook_secret: str,
) -> SignatureResult:
    """验证 Confluence webhook 签名（X-Hub-Signature）。

    Confluence Cloud webhook secret 启用后，请求头携带::

        X-Hub-Signature: sha256=<HMAC-SHA256(secret, body) 的十六进制>

    Args:
        body: 原始请求体字符串。
        signature: X-Hub-Signature header 值（形如 ``sha256=xxxx``）。
        webhook_secret: Confluence webhook 配置的 secret。

    Returns:
        SignatureResult — valid=True 表示通过。
    """
    expected = _compute_confluence_signature(webhook_secret, body)
    if not hmac.compare_digest(expected, signature):
        return SignatureResult(valid=False, reason="signature_mismatch")
    return SignatureResult(valid=True, mode="verified")


def _compute_confluence_signature(secret: str, body: str) -> str:
    """计算 Confluence 签名：sha256= + HMAC-SHA256(secret, body)。"""
    digest = hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


# ==================================================================
# 统一入口 — 按 adapter_id 分发 + 凭证策略
# ==================================================================

def verify_webhook_signature(
    *,
    adapter_id: str,
    body: str,
    headers: dict[str, str],
    credentials: dict[str, str],
) -> SignatureResult:
    """按适配器类型分发签名验证。

    策略：
        - 凭证中配置了对应 secret → 必须验证签名
        - 凭证中未配置 secret → 放行并 warning（dev 模式）
        - 飞书 header 不区分大小写，统一转小写查找

    Args:
        adapter_id: 适配器 ID（feishu / confluence）。
        body: 原始请求体字符串。
        headers: 请求头（key 已转小写）。
        credentials: 解密后的凭证 dict。

    Returns:
        SignatureResult — valid=False 时端点应返回 401。
    """
    if adapter_id == "feishu":
        encrypt_key = credentials.get("encrypt_key", "")
        if not encrypt_key:
            log.warning(
                "webhook.no_encrypt_key",
                adapter_id=adapter_id,
                msg="未配置 encrypt_key，跳过签名验证（dev 模式）",
            )
            return SignatureResult(valid=True, mode="no_secret")
        return verify_feishu_signature(
            timestamp=headers.get("x-lark-request-timestamp", ""),
            nonce=headers.get("x-lark-request-nonce", ""),
            body=body,
            signature=headers.get("x-lark-signature", ""),
            encrypt_key=encrypt_key,
        )

    if adapter_id == "confluence":
        secret = credentials.get("webhook_secret", "")
        if not secret:
            log.warning(
                "webhook.no_webhook_secret",
                adapter_id=adapter_id,
                msg="未配置 webhook_secret，跳过签名验证（dev 模式）",
            )
            return SignatureResult(valid=True, mode="no_secret")
        return verify_confluence_signature(
            body=body,
            signature=headers.get("x-hub-signature", ""),
            webhook_secret=secret,
        )

    # 其他适配器（notion/obsidian）— 无 webhook 机制
    log.warning("webhook.unsupported_adapter", adapter_id=adapter_id)
    return SignatureResult(valid=False, reason=f"unsupported_adapter: {adapter_id}")
