"""
认证服务 — 注册、登录、令牌解析、密码修改。

遵循单一职责：AuthService 只处理认证相关业务逻辑，
密码哈希和 JWT 签发/解析委托给 ``app.utils.crypto`` 工具函数，
用户数据访问委托给 ``UserRepository``。

遵循依赖倒置：通过 UserRepository 接口访问用户数据，
不直接操作 SQLAlchemy Session。

遵循开闭原则：新增认证方式（如 OAuth、LDAP 认证）只需扩展
AuthService 或添加新的认证 Service，无需修改现有 crypto 工具。
"""

from __future__ import annotations

from uuid import UUID

from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenResponse, UserResponse
from app.utils.crypto import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class AuthService:
    """认证服务 — 处理用户注册、登录、令牌解析与密码修改。

    使用方式::

        async def login_endpoint(db: AsyncSession = Depends(get_db_session)):
            auth = AuthService(db)
            token = await auth.login(email, password)
            return token

    所有方法均通过注入的 ``AsyncSession`` 操作数据库，
    事务提交由 ``get_db_session`` 依赖统一处理。
    """

    def __init__(self, db: AsyncSession) -> None:
        """初始化认证服务。

        Args:
            db: 异步数据库会话，由依赖注入 ``get_db_session`` 提供。
        """
        self.db = db
        self.user_repo = UserRepository(db)

    async def register(self, email: str, password: str, name: str) -> User:
        """注册新用户。

        新用户默认角色为 ``viewer``，默认密级为 ``internal``，
        默认状态为激活。密码经 bcrypt 哈希后存储，不保存明文。

        Args:
            email: 用户邮箱（唯一）。
            password: 明文密码（由服务端哈希存储）。
            name: 用户姓名。

        Returns:
            新创建的 User 对象。

        Raises:
            ValueError: 邮箱已被注册。
        """
        existing = await self.user_repo.get_by_email(email)
        if existing is not None:
            raise ValueError(f"邮箱 {email} 已被注册")

        hashed = hash_password(password)
        user = await self.user_repo.create(
            email=email,
            hashed_password=hashed,
            name=name,
            role="viewer",
            clearance_level="internal",
            is_active=True,
        )
        return user

    async def login(self, email: str, password: str) -> TokenResponse:
        """用户登录，返回 JWT 令牌。

        校验流程：
        1. 根据邮箱查询用户；
        2. 检查用户是否存在且已激活；
        3. 校验密码哈希；
        4. 签发 JWT 并返回 TokenResponse。

        安全说明：邮箱不存在和密码错误返回相同的模糊错误信息，
        避免泄露邮箱是否已注册。

        Args:
            email: 用户邮箱。
            password: 明文密码。

        Returns:
            包含 ``access_token`` 和用户信息的 TokenResponse。

        Raises:
            ValueError: 邮箱不存在 / 账号禁用 / 密码错误。
        """
        user = await self.user_repo.get_by_email(email)
        if user is None:
            raise ValueError("邮箱或密码错误")

        if not user.is_active:
            raise ValueError("账号已被禁用")

        if not verify_password(password, user.hashed_password):
            raise ValueError("邮箱或密码错误")

        # JWT payload 中写入用户 ID（sub）和角色（role）
        token = create_access_token({"sub": str(user.id), "role": user.role})
        return TokenResponse(
            access_token=token,
            token_type="bearer",
            user=UserResponse.model_validate(user),
        )

    async def get_current_user(self, token: str) -> User:
        """从 JWT 令牌解析当前用户。

        校验流程：
        1. 解码 JWT，校验签名与过期时间；
        2. 从 payload 提取用户 ID（sub）；
        3. 查询用户是否存在且已激活。

        Args:
            token: JWT access token 字符串。

        Returns:
            当前登录的 User 对象。

        Raises:
            ValueError: 令牌无效 / 缺少用户信息 / 用户不存在 / 账号禁用。
        """
        try:
            payload = decode_access_token(token)
        except JWTError as exc:
            raise ValueError("无效的认证令牌") from exc

        user_id_str = payload.get("sub")
        if user_id_str is None:
            raise ValueError("令牌中缺少用户信息")

        try:
            user_id = UUID(user_id_str)
        except ValueError as exc:
            raise ValueError("令牌中用户 ID 格式错误") from exc

        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise ValueError("用户不存在")
        if not user.is_active:
            raise ValueError("账号已被禁用")

        return user

    async def change_password(
        self, user_id: UUID, old_password: str, new_password: str
    ) -> bool:
        """修改用户密码。

        校验流程：
        1. 查询用户是否存在；
        2. 校验旧密码；
        3. 哈希新密码并更新。

        Args:
            user_id: 用户 ID。
            old_password: 旧密码明文。
            new_password: 新密码明文。

        Returns:
            True 表示修改成功，False 表示旧密码错误。

        Raises:
            ValueError: 用户不存在。
        """
        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise ValueError("用户不存在")

        if not verify_password(old_password, user.hashed_password):
            return False

        new_hashed = hash_password(new_password)
        await self.user_repo.update(user_id, hashed_password=new_hashed)
        return True
