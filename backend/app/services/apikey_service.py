"""
API 密钥服务 — 单一职责：API 密钥的生成、校验与管理。

遵循单一职责：ApiKeyService 只处理密钥业务逻辑
（生成、哈希、校验、停用），不感知 HTTP 层细节。

安全说明：
- 密钥生成使用 ``secrets.token_urlsafe(32)``，保证密码学安全随机性；
- 存储使用 SHA-256 哈希，明文密钥仅在创建时返回一次；
- 校验流程：提取前缀 → 查询记录 → 比对哈希 → 更新最后使用时间；
- 过期检查：校验时检查 expires_at 是否已过期。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.apikey import ApiKey
from app.repositories.apikey_repository import ApiKeyRepository

logger = logging.getLogger(__name__)

# 密钥前缀长度（明文前 N 位用于识别）
_KEY_PREFIX_LENGTH = 8


class ApiKeyService:
    """API 密钥服务 — 封装密钥的生成、校验与管理。

    使用方式::

        async def create_key_endpoint(db: AsyncSession = Depends(get_db_session), ...):
            service = ApiKeyService(db)
            api_key, plaintext = await service.create_key("my-key", ["read"])
            # plaintext 仅此一次返回给用户
    """

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        """初始化 API 密钥服务。

        Args:
            db: 异步数据库会话，事务由 get_db_session 统一管理。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self._tenant_id = tenant_id
        self.repo: ApiKeyRepository = ApiKeyRepository(db, tenant_id=tenant_id)

    # ------------------------------------------------------------------
    # 密钥生成与哈希
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_key() -> str:
        """生成随机 API 密钥明文。

        使用 ``secrets.token_urlsafe(32)`` 生成 43 字符的 URL 安全随机字符串，
        前缀加上 ``ekb_`` 以标识来源。

        Returns:
            密钥明文字符串。
        """
        return f"ekb_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _hash_key(key: str) -> str:
        """计算密钥的 SHA-256 哈希。

        Args:
            key: 密钥明文。

        Returns:
            64 字符的十六进制哈希字符串。
        """
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_prefix(key: str) -> str:
        """提取密钥前缀（明文前 N 位）。

        Args:
            key: 密钥明文。

        Returns:
            密钥前缀字符串。
        """
        return key[:_KEY_PREFIX_LENGTH]

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def create_key(
        self,
        name: str,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        """创建新的 API 密钥。

        生成流程：
        1. 生成随机明文密钥；
        2. 计算 SHA-256 哈希；
        3. 提取前缀；
        4. 持久化到数据库（仅存哈希与前缀）。

        Args:
            name: 密钥名称（用户可读标识）。
            scopes: 授权范围列表（如 ["read", "write"]）。
            expires_at: 过期时间（为空表示永不过期）。

        Returns:
            元组 (ApiKey 实例, 明文密钥)。
            明文密钥仅此一次返回，后续无法再次获取。
        """
        plaintext = self._generate_key()
        key_hash = self._hash_key(plaintext)
        key_prefix = self._extract_prefix(plaintext)

        api_key = await self.repo.create(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=scopes,
            expires_at=expires_at,
            is_active=True,
        )

        logger.info("API 密钥创建成功: name=%s, prefix=%s", name, key_prefix)
        return api_key, plaintext

    async def validate_key(self, key: str) -> ApiKey | None:
        """校验 API 密钥有效性。

        校验流程：
        1. 提取前缀；
        2. 通过前缀查询数据库记录；
        3. 比对完整哈希；
        4. 检查是否过期；
        5. 更新最后使用时间。

        Args:
            key: 待校验的密钥明文。

        Returns:
            有效的 ApiKey 实例，无效返回 None。
        """
        if not key or len(key) < _KEY_PREFIX_LENGTH:
            return None

        key_prefix = self._extract_prefix(key)
        api_key = await self.repo.get_by_prefix(key_prefix)
        if api_key is None:
            return None

        # 比对哈希
        expected_hash = self._hash_key(key)
        if not secrets.compare_digest(api_key.key_hash, expected_hash):
            return None

        # 检查过期
        if api_key.expires_at is not None:
            if api_key.expires_at < datetime.now(timezone.utc):
                logger.info("API 密钥已过期: prefix=%s", key_prefix)
                return None

        # 更新最后使用时间
        await self.repo.update_last_used(api_key.id)

        return api_key

    async def list_keys(self) -> list[ApiKey]:
        """查询所有 API 密钥（不含明文密钥）。

        Returns:
            ApiKey 列表（按创建时间倒序）。
        """
        return await self.repo.list_all()

    async def revoke_key(self, key_id: UUID) -> bool:
        """停用 API 密钥（软停用，不物理删除）。

        Args:
            key_id: 密钥 ID。

        Returns:
            True 表示停用成功，False 表示密钥不存在。
        """
        result = await self.repo.deactivate(key_id)
        if result:
            logger.info("API 密钥已停用: id=%s", key_id)
        return result
