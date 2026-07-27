"""
ReviewerAgent — 高风险操作的角色对抗审查 Agent。

设计原理（借鉴 Q8 多 Agent 对抗设计）：
    "写代码"和"审查代码"需要的视角是对抗性的，合并在一个 Agent 里
    容易自己检查不出自己的问题。对于高风险操作（如 create_it_ticket、
    document_create），由独立的 ReviewerAgent 从安全/合规角度审查，
    而非 ActionAgent 自我审查。

审查维度：
    1. 权限合规：用户是否有执行该操作的权限
    2. 参数合理性：操作参数是否合理（如金额是否异常、目标系统是否正确）
    3. 不可逆性影响：操作是否可撤销，影响范围评估
    4. 上下文一致性：操作是否与用户原始意图一致

遵循单一职责：本模块只负责审查决策，不执行任何实际操作。
遵循优雅降级：LLM 不可用时默认放行（不阻断业务流程）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ReviewResult:
    """审查结果。

    Attributes:
        approved: 是否批准操作。
        reason: 批准/拒绝原因。
        risk_level: 风险等级 "low" / "medium" / "high"。
        concerns: 审查中发现的风险点列表。
    """

    approved: bool
    reason: str = ""
    risk_level: str = "low"
    concerns: list[str] = None

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "approved": self.approved,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "concerns": self.concerns or [],
        }


class ReviewerAgent:
    """高风险操作审查 Agent — 从安全/合规角度审查操作方案。

    使用方式::

        reviewer = ReviewerAgent(llm)
        result = await reviewer.review(
            tool_name="create_it_ticket",
            tool_args={"title": "服务器扩容", "priority": "high"},
            user_query="帮我创建一个高优先级工单",
            context={},
        )
        if not result.approved:
            # 阻断操作
            ...

    设计要点：
        - 独立于 ActionAgent，不共享执行逻辑
        - 审查视角与执行视角对立（对抗性设计）
        - LLM 不可用时默认放行（不阻断业务）
        - 审查维度：权限/参数/不可逆性/上下文一致性
    """

    #: 审查 prompt 模板
    _REVIEW_PROMPT: str = (
        "你是企业知识库的安全审查专家。你的职责是审查 AI 助手即将执行的操作是否安全合规。\n\n"
        "审查维度：\n"
        "1. 权限合规：操作是否在用户权限范围内\n"
        "2. 参数合理性：操作参数是否合理（金额异常、目标系统正确性）\n"
        "3. 不可逆性影响：操作是否可撤销，影响范围\n"
        "4. 上下文一致性：操作是否与用户原始意图一致\n\n"
        "请以 JSON 格式输出审查结果：\n"
        '{{"approved": true/false, "reason": "批准/拒绝原因", '
        '"risk_level": "low/medium/high", "concerns": ["风险点1", "风险点2"]}}\n\n'
        "即将执行的操作：{tool_name}\n"
        "操作参数：{tool_args}\n"
        "用户原始请求：{user_query}\n"
        "操作上下文：{context}\n\n"
        "审查结果："
    )

    #: 需要审查的高风险工具
    _HIGH_RISK_TOOLS: set[str] = {
        "create_it_ticket",
        "document_create",
        "document_delete",
        "system_config_change",
    }

    def __init__(self, llm: LLMProvider | None = None) -> None:
        """初始化审查 Agent。

        Args:
            llm: LLM Provider，为 None 时跳过 LLM 审查（默认放行）。
        """
        self._llm = llm

    def needs_review(self, tool_name: str) -> bool:
        """判断工具是否需要审查。

        Args:
            tool_name: 工具名称。

        Returns:
            True 如果该工具是高风险工具，需要审查。
        """
        return tool_name in self._HIGH_RISK_TOOLS

    async def review(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        user_query: str,
        context: dict[str, Any] | None = None,
    ) -> ReviewResult:
        """审查一个高风险操作。

        Args:
            tool_name: 即将执行的工具名称。
            tool_args: 工具参数。
            user_query: 用户原始请求（防传话游戏，审查原始意图）。
            context: 操作上下文（如用户权限、租户信息等）。

        Returns:
            ReviewResult: 审查结果。
        """
        # 非高风险工具直接放行
        if not self.needs_review(tool_name):
            return ReviewResult(
                approved=True,
                reason="非高风险工具，无需审查",
                risk_level="low",
            )

        # LLM 不可用时默认放行（不阻断业务）
        if self._llm is None:
            log.warning(
                "reviewer.llm_unavailable_default_approve",
                tool_name=tool_name,
            )
            return ReviewResult(
                approved=True,
                reason="审查 Agent LLM 不可用，默认放行",
                risk_level="medium",
            )

        prompt = self._REVIEW_PROMPT.format(
            tool_name=tool_name,
            tool_args=str(tool_args),
            user_query=user_query[:500],
            context=str(context or {}),
        )

        try:
            result = await self._call_llm_json(prompt)
            approved = result.get("approved", True)
            risk_level = result.get("risk_level", "medium")
            concerns = result.get("concerns", [])
            reason = result.get("reason", "")

            log.info(
                "reviewer.review_complete",
                tool_name=tool_name,
                approved=approved,
                risk_level=risk_level,
                concerns_count=len(concerns),
            )

            if not approved:
                log.warning(
                    "reviewer.operation_rejected",
                    tool_name=tool_name,
                    reason=reason,
                    concerns=concerns,
                )

            return ReviewResult(
                approved=approved,
                reason=reason,
                risk_level=risk_level,
                concerns=concerns,
            )
        except Exception as exc:
            log.warning("reviewer.review_failed", error=str(exc), tool_name=tool_name)
            # 审查失败时默认放行（不阻断业务）
            return ReviewResult(
                approved=True,
                reason=f"审查异常，默认放行：{exc}",
                risk_level="medium",
            )

    async def _call_llm_json(self, prompt: str) -> dict[str, Any]:
        """调用 LLM 并解析 JSON 响应。"""
        import json

        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=200):
            if isinstance(chunk, str):
                chunks.append(chunk)
        text = "".join(chunks).strip()

        # 清理 markdown 代码块包裹
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start : end + 1])
            raise
