"""
加密工具 — 密码哈希、JWT 令牌、凭证对称加密。

三类职责：
1. 密码哈希：hash_password / verify_password（bcrypt）
2. JWT 令牌：create_access_token / decode_access_token
3. 凭证加密：encrypt_secret / decrypt_secret（AES-GCM，用于 external_credentials 表）

遵循依赖倒置：SECRET_KEY / ALGORITHM / 过期时间由 app.config.Settings 提供。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.config import get_settings

# passlib CryptContext：统一使用 bcrypt，旧算法自动标记 deprecated
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """使用 passlib bcrypt 对明文密码进行哈希。"""
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希值是否匹配。"""
    return _pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    """生成 JWT access token。

    将 data 编码进 payload，并写入 ``exp``（过期时间取
    ``ACCESS_TOKEN_EXPIRE_MINUTES``），最后使用 SECRET_KEY 与 ALGORITHM 签名。
    """
    settings = get_settings()
    to_encode: dict[str, Any] = dict(data)
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并校验 JWT，返回 payload dict。

    校验失败（签名错误 / 过期等）将抛出 ``jose.JWTError``，由调用方捕获处理。
    """
    settings = get_settings()
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


# ====================================================================
# 凭证对称加密（AES-GCM）
# ====================================================================
# 用于 external_credentials 表 — 加密存储外部平台凭证
# （飞书 app_id/app_secret、Confluence api_token、Notion integration_token 等）。
#
# 密钥派生：SECRET_KEY → PBKDF2-HMAC-SHA256(100k iterations) → 32B AES-256 密钥
# 每次加密生成新的 12B nonce，密文格式：nonce(12B) || ciphertext+tag
# 同一明文每次加密结果不同（nonce 随机），但都能用同一密钥解密。

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # noqa: E402

_PBKDF2_ITERATIONS: int = 100_000
# 固定盐值（SECRET_KEY 本身已是机密，盐值仅防同类密钥的彩虹表）
_PBKDF2_SALT: bytes = b"ekb-external-creds-v1"


def _derive_aes_key() -> bytes:
    """从 SECRET_KEY 派生 32 字节 AES-256 密钥（PBKDF2-HMAC-SHA256）。"""
    settings = get_settings()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_PBKDF2_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(settings.SECRET_KEY.encode("utf-8"))


def encrypt_secret(plaintext: str) -> bytes:
    """AES-GCM 加密字符串。

    返回 ``nonce(12B) + ciphertext+tag`` 的二进制 blob，用于凭证持久化。
    同一明文每次加密结果不同（nonce 随机），但都能用同一密钥解密。

    Args:
        plaintext: 明文字符串（如 credentials JSON）。

    Returns:
        二进制 blob（nonce + ciphertext + tag）。
    """
    aesgcm = AESGCM(_derive_aes_key())
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_secret(blob: bytes) -> str:
    """AES-GCM 解密。

    Args:
        blob: encrypt_secret 返回的二进制 blob。

    Returns:
        原始明文字符串。

    Raises:
        ValueError: blob 格式错误或解密失败（被篡改/密钥不匹配）。
    """
    # nonce(12) + tag(16) = 28，密文至少 0 字节 → blob 至少 28 字节
    if len(blob) < 28:
        raise ValueError("invalid ciphertext blob: too short")
    aesgcm = AESGCM(_derive_aes_key())
    nonce = blob[:12]
    ciphertext = blob[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
