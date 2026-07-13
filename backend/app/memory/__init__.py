"""
记忆管理层 — 四级记忆架构。

遵循开闭原则：新增记忆源只需扩展记忆类型，不修改编排逻辑。
遵循单一职责：每个管理器只负责自己的记忆域。

四级记忆（从快到慢）：
  L1 短期窗口：当前对话最近 N 条消息（由 Conversation/Message 表承载）
  L2 Checkpoint 快照：LangGraph 会话状态持久化（PostgreSQL）
  L3 Mem0 长期偏好：跨会话用户偏好和事实
  L4 工作记忆上下文：当前任务相关的实体和关系

组件分工：
  Mem0Manager       — 当前事实（KV + Embedding），高频读写
  GraphitiManager   — 时序图谱（图 + 时间区间），追踪知识演化
  CheckpointManager — 会话状态持久化，支持中断恢复
  MemoryManager     — 四级编排器，协调所有记忆源
"""

from app.memory.checkpoint import CheckpointManager
from app.memory.graphiti_manager import GraphitiManager
from app.memory.mem0_manager import Mem0Manager
from app.memory.memory_manager import MemoryContext, MemoryManager

__all__ = [
    "Mem0Manager",
    "GraphitiManager",
    "CheckpointManager",
    "MemoryManager",
    "MemoryContext",
]
