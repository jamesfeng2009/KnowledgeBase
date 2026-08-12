"""AES-GCM 凭证加解密单测 — 覆盖 app.utils.crypto.encrypt_secret / decrypt_secret。

测试目标：
    1. 加解密往返（round-trip）— 正常文本、空串、Unicode、大文本
    2. nonce 随机化 — 同一明文两次加密产生不同密文
    3. 篡改检测 — AES-GCM 认证标签保证密文被改后解密失败
    4. 格式校验 — blob 过短抛 ValueError
    5. 密钥隔离 — 不同 SECRET_KEY 派生的密钥无法解密对方密文
"""
from __future__ import annotations

import os

import pytest

from app.utils.crypto import decrypt_secret, encrypt_secret


# ------------------------------------------------------------------
# 往返测试
# ------------------------------------------------------------------

class TestRoundTrip:
    """加解密往返 — 相同明文经 encrypt→decrypt 应还原原值。"""

    @pytest.mark.parametrize(
        "plaintext",
        [
            "simple ascii",
            "",
            "中文凭证内容",
            '{"app_id": "cli_xxx", "app_secret": "secret!”"}',
            "emoji 🔐🚀 and special <>& chars",
        ],
        ids=["ascii", "empty", "chinese", "json", "emoji-special"],
    )
    def test_roundtrip(self, plaintext: str) -> None:
        blob = encrypt_secret(plaintext)
        assert decrypt_secret(blob) == plaintext

    def test_large_payload(self) -> None:
        """大文本加解密 — 模拟完整 JSON 凭证（10KB）。"""
        plaintext = "x" * 10_000
        blob = encrypt_secret(plaintext)
        assert decrypt_secret(blob) == plaintext

    def test_blob_format(self) -> None:
        """blob 结构 = nonce(12) + ciphertext + tag(16)。"""
        blob = encrypt_secret("hello")
        # nonce 12B + tag 16B = 28B 最小（密文 0 字节也行，这里 hello 5 字节）
        assert len(blob) >= 28 + len("hello")


# ------------------------------------------------------------------
# nonce 随机化
# ------------------------------------------------------------------

class TestNonceRandomization:
    """同一明文每次加密结果不同（nonce 随机），但都能解密。"""

    def test_distinct_ciphertexts(self) -> None:
        plaintext = "same-secret"
        blob1 = encrypt_secret(plaintext)
        blob2 = encrypt_secret(plaintext)
        # 密文不同（nonce 不同）
        assert blob1 != blob2
        # 但都能正确解密
        assert decrypt_secret(blob1) == plaintext
        assert decrypt_secret(blob2) == plaintext

    def test_nonce_prefix_differs(self) -> None:
        """blob 前 12 字节为 nonce，两次应不同。"""
        blob1 = encrypt_secret("x")
        blob2 = encrypt_secret("x")
        assert blob1[:12] != blob2[:12]


# ------------------------------------------------------------------
# 篡改检测
# ------------------------------------------------------------------

class TestTamperDetection:
    """AES-GCM 认证标签 — 密文被改后解密抛异常。"""

    def test_modified_ciphertext_raises(self) -> None:
        blob = bytearray(encrypt_secret("secret-value"))
        # 翻转中间一字节（破坏 ciphertext）
        blob[20] ^= 0xFF
        with pytest.raises(Exception):
            decrypt_secret(bytes(blob))

    def test_modified_nonce_raises(self) -> None:
        blob = bytearray(encrypt_secret("secret-value"))
        # 翻转 nonce 首字节
        blob[0] ^= 0xFF
        with pytest.raises(Exception):
            decrypt_secret(bytes(blob))

    def test_truncated_blob_raises(self) -> None:
        """截断到 12 字节（只剩 nonce，无 tag）→ ValueError。"""
        blob = encrypt_secret("x")[:12]
        with pytest.raises(ValueError, match="too short"):
            decrypt_secret(blob)

    def test_empty_blob_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decrypt_secret(b"")

    def test_too_short_blob_raises(self) -> None:
        """blob 不足 28 字节（nonce+tag 最小长度）→ ValueError。"""
        with pytest.raises(ValueError, match="too short"):
            decrypt_secret(b"\x00" * 27)


# ------------------------------------------------------------------
# 密钥隔离
# ------------------------------------------------------------------

class TestKeyIsolation:
    """不同 SECRET_KEY 派生的 AES 密钥不同，无法互解。"""

    @staticmethod
    def _clear_settings_cache() -> None:
        from app.config import get_settings

        get_settings.cache_clear()

    def test_wrong_key_cannot_decrypt(self, monkeypatch) -> None:
        """用 KEY_A 加密，切换到 KEY_B 后解密应失败。"""
        # 1. KEY_A 加密
        monkeypatch.setenv("SECRET_KEY", "key-A-for-test")
        self._clear_settings_cache()  # 让新 env 生效
        blob = encrypt_secret("payload")

        # 2. 切换到 KEY_B
        monkeypatch.setenv("SECRET_KEY", "key-B-different")
        self._clear_settings_cache()

        # 3. 用 KEY_B 解密 KEY_A 的密文 — 应失败
        with pytest.raises(Exception):
            decrypt_secret(blob)

    def test_same_key_roundtrip_across_instances(self, monkeypatch) -> None:
        """同一 KEY 两次派生密钥应一致，可互解。"""
        monkeypatch.setenv("SECRET_KEY", "stable-key")
        self._clear_settings_cache()
        blob = encrypt_secret("payload")

        # 重新派生（cache 清除模拟新进程）
        self._clear_settings_cache()
        assert decrypt_secret(blob) == "payload"
