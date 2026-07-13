"""
LDAP 同步服务 — 从 LDAP 目录同步用户和组织架构。

遵循单一职责：LDAPSyncService 只负责 LDAP 数据同步，
不涉及认证或权限判断。

遵循开闭原则：sync 方法内部判断 LDAP 是否配置，
未配置时静默返回空结果，不影响调用方业务流程。
当 LDAP 配置就绪后，方法自动启用同步逻辑，无需修改调用方代码。

依赖说明：
- 当 ``LDAP_URL`` 配置为空时，所有 sync 方法直接返回空列表，不报错。
- 当 ``LDAP_URL`` 配置后，需要安装 ``ldap3`` 库（``pip install ldap3``）。
- 如果 LDAP 连接失败，方法捕获异常并返回空列表，同时记录错误日志。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)


class LDAPSyncService:
    """LDAP 同步服务 — 从 LDAP 目录同步用户与组织架构。

    当 LDAP 未配置时，所有 sync 方法静默返回空列表，
    不抛出异常，保证调用方无需额外处理兼容逻辑。

    使用方式::

        async def sync_endpoint(db: AsyncSession = Depends(get_db_session)):
            ldap = LDAPSyncService(db)
            users = await ldap.sync_users()
            depts = await ldap.sync_org_tree()
            # 即使 LDAP 未配置，users/depts 也是空列表，不会报错
    """

    def __init__(self, db: AsyncSession) -> None:
        """初始化 LDAP 同步服务。

        Args:
            db: 异步数据库会话，用于写入同步后的数据。
        """
        self.db = db
        self._settings = get_settings()

    def _is_ldap_configured(self) -> bool:
        """检查是否配置了 LDAP。

        判断标准：``LDAP_URL`` 非空。

        Returns:
            True 表示已配置 LDAP，False 表示未配置。
        """
        return bool(self._settings.LDAP_URL)

    async def sync_users(self) -> list[dict[str, Any]]:
        """从 LDAP 同步用户。

        如果 LDAP 未配置，直接返回空列表，不报错。
        如果已配置，连接 LDAP 目录服务，查询 ``person`` 对象，
        提取邮箱、姓名、部门、上级等属性。

        Returns:
            同步的用户列表，每项为包含 dn / name / email /
            department / manager 的 dict。未配置或失败时返回空列表。
        """
        if not self._is_ldap_configured():
            logger.debug("LDAP 未配置，跳过用户同步")
            return []

        try:
            from ldap3 import ALL, Connection, Server
        except ImportError:
            logger.warning("ldap3 库未安装，无法执行 LDAP 用户同步")
            return []

        server = Server(self._settings.LDAP_URL, get_info=ALL)
        synced: list[dict[str, Any]] = []

        try:
            with Connection(
                server,
                user=self._settings.LDAP_BIND_DN,
                password=self._settings.LDAP_BIND_PASSWORD,
                auto_bind=True,
            ) as conn:
                conn.search(
                    search_base=self._settings.LDAP_BASE_DN,
                    search_filter="(objectClass=person)",
                    attributes=["cn", "mail", "department", "manager"],
                )

                for entry in conn.entries:
                    synced.append(
                        {
                            "dn": str(entry.dn),
                            "name": str(entry.cn) if entry.cn else "",
                            "email": str(entry.mail) if entry.mail else "",
                            "department": (
                                str(entry.department) if entry.department else ""
                            ),
                            "manager": (
                                str(entry.manager) if entry.manager else ""
                            ),
                        }
                    )
        except Exception:
            logger.exception("LDAP 用户同步失败")
            return []

        logger.info("LDAP 用户同步完成，共 %d 条记录", len(synced))
        return synced

    async def sync_org_tree(self) -> list[dict[str, Any]]:
        """同步组织架构（部门树）。

        如果 LDAP 未配置，直接返回空列表，不报错。
        如果已配置，连接 LDAP 目录服务，查询 ``organizationalUnit``
        对象，提取部门名称、描述、DN 等属性。

        Returns:
            同步的部门列表，每项为包含 dn / name / description
            的 dict。未配置或失败时返回空列表。
        """
        if not self._is_ldap_configured():
            logger.debug("LDAP 未配置，跳过组织架构同步")
            return []

        try:
            from ldap3 import ALL, Connection, Server
        except ImportError:
            logger.warning("ldap3 库未安装，无法执行 LDAP 组织架构同步")
            return []

        server = Server(self._settings.LDAP_URL, get_info=ALL)
        synced: list[dict[str, Any]] = []

        try:
            with Connection(
                server,
                user=self._settings.LDAP_BIND_DN,
                password=self._settings.LDAP_BIND_PASSWORD,
                auto_bind=True,
            ) as conn:
                conn.search(
                    search_base=self._settings.LDAP_BASE_DN,
                    search_filter="(objectClass=organizationalUnit)",
                    attributes=["ou", "description"],
                )

                for entry in conn.entries:
                    synced.append(
                        {
                            "dn": str(entry.dn),
                            "name": str(entry.ou) if entry.ou else "",
                            "description": (
                                str(entry.description) if entry.description else ""
                            ),
                        }
                    )
        except Exception:
            logger.exception("LDAP 组织架构同步失败")
            return []

        logger.info("LDAP 组织架构同步完成，共 %d 条记录", len(synced))
        return synced
