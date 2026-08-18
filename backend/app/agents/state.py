"""Agent Loop 共享状态 — AgentState 的唯一权威定义。

此前 agents/base.py 与 rag/engine.py 各定义一份 AgentState 且字段漂移：
Agent 类型层（QA/Action/Workflow）拿不到 scratchpad / plan_steps 等富字段。
本模块收敛为单一定义，两处均从此导入（原位置保留再导出，既有引用零改动）。

设计约定：
- AgentState 是扁平 TypedDict（total=False），作为循环各阶段间的通信总线：
  think / action / reflect 不直接互相调用，全部通过读写同一个 state 协作；
- 模块保持零业务依赖（仅 app.llm.base 的 Message，其为纯类型叶子模块），
  避免引擎层与 Agent 层的循环导入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from app.llm.base import Message

# 权限过滤器类型 — 接收候选文档列表，返回过滤后的列表。
# 由调用方注入（通常封装 PermissionService.filter_documents 对 dict 的适配）。
PermissionFilter = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


class AgentState(TypedDict, total=False):
    """Agent Loop 状态 — 在循环各节点间传递。

    Attributes:
        query: 用户原始问题。
        user_id: 当前用户 ID。
        session_id: 会话 ID（用于记忆与缓存隔离，兼作 LangGraph thread_id）。
        messages: 累积的消息列表（system / user / assistant / tool）。
        retrieved_docs: 检索并重排后的文档列表。
        tool_results: MCP 工具调用结果列表。
        answer: 生成的最终答案。
        iteration: 当前迭代轮次。
        max_iterations: 最大迭代次数（防无限循环）。
        kb_ids: 可选，限定检索的知识库范围。
        memory_context: 记忆引擎提供的上下文。
    """

    query: str
    # P2-B: 查询重写后的查询文本（用于检索），None 表示未重写
    rewritten_query: str | None
    user_id: str
    session_id: str
    messages: list[Message]
    retrieved_docs: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    answer: str
    iteration: int
    max_iterations: int
    kb_ids: list[str] | None
    memory_context: str
    # 多租户预留 — 当前不实施隔离逻辑，仅预留字段供未来扩展。
    tenant_id: str | None
    # P3-E: Scratchpad 草稿本 — 累积每轮推理笔记，压缩时优先保留
    scratchpad: str
    # P4-E: 对话焦点传入引擎（来自 TopicTracker / DriftDetector）
    conversation_focus: dict[str, Any] | None
    # P4-E: 漂移检测结果（来自 DriftDetector）
    drift_info: dict[str, Any] | None
    # 请求级 ABAC 权限过滤器（callable，携带当前用户上下文）— 优先于
    # 引擎构造级 permission_filter；仅纯 Python answer() 路径使用。
    permission_filter: PermissionFilter | None
    # P0 wiki 层级：检索层级过滤 — series_id/path_prefix/parent_id/depth/
    # version_of。透传到 retriever.search，由 filter_builder 转为后端 filter。
    # None 表示不做层级过滤（向后兼容旧调用）。
    filters: dict[str, Any] | None
    # --- LangGraph 专用字段（纯 Python 路径不使用）---
    # think 节点产出的路由信号：retrieve / tool_call / generate。
    _decision: str
    # generate 节点产出的逐 token 片段，供 answer_with_graph 流式回放。
    _stream_tokens: list[str]
    # P1-9: 显式计划状态清单 — [{step_id, action, description, status}]
    # status: pending / done / skipped（app.agents.planner 常量）
    plan_steps: list[dict[str, Any]]
    # P1-9: 本会话已发生的重规划次数（上限由 PlanManager.max_replans 控制）
    replan_count: int
    # P1: 约束注入通道输出（ConstraintChannel.fetch）— 确定性红线条款，
    # 由 _retrieve 与检索并行获取，generate 透传给 generator 红线段。
    constraint_context: list[dict[str, Any]]
    # P1: 注入约束的机器可执行定义（rule_id/severity/normalized/triggers），
    # 供 L2 post_verify / L3 tool_gate 消费（Phase 3 接入）。
    injected_constraints: list[dict[str, Any]]
