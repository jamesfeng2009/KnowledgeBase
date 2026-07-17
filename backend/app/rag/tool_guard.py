"""
MCP 工具调用守卫 — 单一职责：在工具执行前拦截危险操作。

借鉴 DECO 数仓 Agent 的 DangerousToolGuard 设计，针对企业知识库 RAG 场景简化：
    - 配置驱动的危险工具清单（YAML / 环境变量）；
    - 写操作类工具需要用户确认后才执行；
    - 只读工具（knowledge_search / document_get / query_oa_approval）直接放行；
    - 危险工具拦截后返回结构化错误，不阻断 Agent Loop 主流程。

遵循依赖倒置：守卫通过构造注入 AgenticRAGEngine，可替换为 Mock。
遵循开闭原则：新增危险工具只需在配置中添加条目，不改代码。

设计原则（来自 DECO 文章）：
    "prompt 是软约束，不是安全边界。任何不可逆操作都必须有代码级强制确认。"
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


class GuardAction(Enum):
    """守卫决策动作。"""

    ALLOW = "allow"        # 放行 — 安全工具或已确认
    BLOCK = "block"        # 阻断 — 危险工具未确认
    CONFIRM = "confirm"    # 需要用户确认


# ------------------------------------------------------------------
# 默认危险工具配置
# ------------------------------------------------------------------

# 写操作类工具 — 需要 HITL 确认
_DEFAULT_DANGEROUS_TOOLS: dict[str, dict[str, Any]] = {
    "document_create": {
        "reason": "创建文档会写入知识库",
        "irreversible": False,
    },
    "create_it_ticket": {
        "reason": "提交 IT 工单会触发外部流程",
        "irreversible": True,
    },
}

# 已知的只读工具 — 直接放行
_SAFE_TOOLS: set[str] = {
    "knowledge_search",
    "document_get",
    "query_oa_approval",
}


class GuardResult:
    """守卫检查结果。"""

    def __init__(
        self,
        action: GuardAction,
        tool_name: str = "",
        reason: str = "",
        irreversible: bool = False,
    ) -> None:
        self.action = action
        self.tool_name = tool_name
        self.reason = reason
        self.irreversible = irreversible

    @property
    def allowed(self) -> bool:
        """是否放行。"""
        return self.action == GuardAction.ALLOW

    @property
    def blocked(self) -> bool:
        """是否阻断。"""
        return self.action == GuardAction.BLOCK

    @property
    def needs_confirmation(self) -> bool:
        """是否需要用户确认。"""
        return self.action == GuardAction.CONFIRM

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "action": self.action.value,
            "tool_name": self.tool_name,
            "reason": self.reason,
            "irreversible": self.irreversible,
        }


class DangerousToolGuard:
    """危险工具守卫 — 配置驱动的 beforeTool 拦截器。

    在 Agent 调用 MCP 工具前检查工具是否危险：
        - 安全工具（只读）→ 直接放行；
        - 危险工具（写操作）→ 需要用户确认；
        - 未知工具 → 默认放行（保持兼容），记录警告。

    使用方式::

        guard = DangerousToolGuard()
        result = guard.check("document_create", {"title": "test"})
        if result.needs_confirmation:
            # 前端弹框确认，用户同意后调用 guard.confirm("document_create")
            ...
        if guard.is_confirmed("document_create"):
            # 放行执行
            ...
    """

    def __init__(
        self,
        dangerous_tools: dict[str, dict[str, Any]] | None = None,
        safe_tools: set[str] | None = None,
    ) -> None:
        """初始化守卫。

        Args:
            dangerous_tools: 危险工具配置，key 为工具名，value 含 reason / irreversible。
            safe_tools: 已知安全工具集合（只读），直接放行。
        """
        self._dangerous: dict[str, dict[str, Any]] = (
            dangerous_tools if dangerous_tools is not None else _DEFAULT_DANGEROUS_TOOLS
        )
        self._safe: set[str] = safe_tools if safe_tools is not None else _SAFE_TOOLS
        # 已确认的工具集合（同一会话内有效）
        self._confirmed: set[str] = set()

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> GuardResult:
        """检查工具调用是否需要拦截。

        Args:
            tool_name: 工具名称。
            tool_input: 工具入参（预留用于参数级检查，当前未使用）。

        Returns:
            GuardResult — ALLOW / CONFIRM / BLOCK。
        """
        # 安全工具直接放行
        if tool_name in self._safe:
            return GuardResult(
                action=GuardAction.ALLOW,
                tool_name=tool_name,
                reason="只读工具",
            )

        # 已确认的工具放行
        if tool_name in self._confirmed:
            return GuardResult(
                action=GuardAction.ALLOW,
                tool_name=tool_name,
                reason="用户已确认",
            )

        # 危险工具 — 需要确认
        if tool_name in self._dangerous:
            config = self._dangerous[tool_name]
            reason = config.get("reason", "危险操作")
            irreversible = config.get("irreversible", False)
            log.warning(
                "tool_guard.dangerous_tool_blocked",
                tool=tool_name,
                reason=reason,
                irreversible=irreversible,
            )
            return GuardResult(
                action=GuardAction.CONFIRM,
                tool_name=tool_name,
                reason=reason,
                irreversible=irreversible,
            )

        # 未知工具 — 默认放行，记录警告
        log.warning(
            "tool_guard.unknown_tool",
            tool=tool_name,
            hint="未在安全/危险清单中，默认放行",
        )
        return GuardResult(
            action=GuardAction.ALLOW,
            tool_name=tool_name,
            reason="未知工具，默认放行",
        )

    def confirm(self, tool_name: str) -> None:
        """标记工具为已确认（用户同意执行）。

        Args:
            tool_name: 工具名称。
        """
        self._confirmed.add(tool_name)
        log.info(
            "tool_guard.confirmed",
            tool=tool_name,
        )

    def is_confirmed(self, tool_name: str) -> bool:
        """检查工具是否已确认。

        Args:
            tool_name: 工具名称。

        Returns:
            True 表示已确认。
        """
        return tool_name in self._confirmed

    def revoke(self, tool_name: str) -> None:
        """撤销工具确认（用户取消或会话结束）。

        Args:
            tool_name: 工具名称。
        """
        self._confirmed.discard(tool_name)

    def reset(self) -> None:
        """重置所有确认状态 — 新会话时调用。"""
        self._confirmed.clear()

    def get_dangerous_tools(self) -> dict[str, dict[str, Any]]:
        """返回危险工具配置（用于前端展示确认清单）。"""
        return dict(self._dangerous)

    def block_result(
        self,
        tool_name: str,
        reason: str,
    ) -> GuardResult:
        """生成阻断结果 — 用于未确认时阻断工具调用。"""
        return GuardResult(
            action=GuardAction.BLOCK,
            tool_name=tool_name,
            reason=reason,
            irreversible=self._dangerous.get(tool_name, {}).get("irreversible", False),
        )
