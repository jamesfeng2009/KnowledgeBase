"""
连接器抽象基类 — 定义统一接口：search / test_connection / get_permissions。

所有外部系统连接器（OA/ERP/CRM/邮件）继承此基类，
实现各自的 search 方法返回统一格式 ExternalSearchResult。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExternalSearchResult:
    """外部系统搜索结果统一格式。

    所有连接器的 search 方法返回此格式列表，
    由 SearchService 统一合并和去重。
    """

    source: str
    """来源系统标识：oa / erp / crm / mail"""

    source_label: str
    """显示名称：OA 审批 / ERP 记录"""

    title: str
    """结果标题"""

    snippet: str
    """摘要片段"""

    url: str = ""
    """原始链接（点击跳转到外部系统）"""

    metadata: dict[str, Any] = field(default_factory=dict)
    """附加元数据（作者/时间/类型等）"""

    score: float = 0.0
    """相关度分数（0-1）"""


class BaseConnector(ABC):
    """连接器抽象基类 — 所有外部系统连接器实现此接口。

    子类需实现：
        - search(keyword, top_k) → list[ExternalSearchResult]
        - test_connection() → bool

    可选覆盖：
        - get_permissions(user_id) → list[str]
    """

    #: 连接器标识（如 "oa" / "erp"）
    connector_id: str = "base"

    #: 显示名称
    display_name: str = "基础连接器"

    #: 是否已启用
    is_active: bool = False

    @abstractmethod
    async def search(self, keyword: str, top_k: int = 5) -> list[ExternalSearchResult]:
        """搜索外部系统，返回统一格式结果。

        Args:
            keyword: 搜索关键词。
            top_k: 返回结果数量上限。

        Returns:
            ExternalSearchResult 列表。
        """
        ...

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试连接是否正常。

        Returns:
            连接是否成功。
        """
        ...

    async def get_permissions(self, user_id: str) -> list[str]:
        """获取用户在该系统中的权限范围（权限联邦用）。

        返回用户可访问的资源 ID 列表，用于搜索结果过滤。
        默认实现返回空列表（不过滤），子类按需覆盖。

        Args:
            user_id: 用户 ID 字符串。

        Returns:
            可访问的资源 ID 列表，空列表表示不过滤。
        """
        return []


class ConnectorNotImplementedError(NotImplementedError):
    """连接器已启用（配了 api_url/key）但适配器还没对接真实 API。

    为什么单独定义一个异常：此前 ``_call_*_api`` 直接 ``return []``，
    调用方拿到空列表后无法区分两种情况：

        - 系统级「没配 / 没启用」→ 空列表是正确的降级（不该告警）
        - 「配好了但适配器是空壳」→ 这是配置错误，应当显式告警

    混成同一返回值会让「启用了连接器却永远搜不到东西」长期无人察觉。
    适配器对接真实 API 后删掉 raise 即可，调用方无需改动。
    """
