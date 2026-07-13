"""
加密工具 — 单一职责：密码哈希与 JWT 令牌处理。

遵循单一职责：仅提供 hash_password / verify_password /
create_access_token / decode_access_token 四个函数，不包含其他逻辑。
遵循依赖倒置：SECRET_KEY / ALGORITHM / 过期时间由 app.config.Settings 提供。
"""

from __future__ import annotations

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
