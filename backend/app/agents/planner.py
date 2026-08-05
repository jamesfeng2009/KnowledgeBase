"""
显式计划管理器 — P1-9：plan 步骤清单 + 偏离检测 + 仅对剩余步骤重规划。

现状问题：
    原 Agent Loop 是"动态 think 循环 + 硬上限"，每轮 LLM 自由决策下一步，
    没有全局计划视图。当观察结果偏离预期（检索为空、工具结果重复）时，
    Agent 只能在原地兜圈直到触发 max_iterations。

核心机制：
    1. **plan 状态清单**：循环开始前由 LLM 生成 2-4 步计划
       （retrieve / tool_call / generate），每步带 pending/done 状态；
    2. **计划感知决策**：think 的动态上下文注入"已完成 / 剩余步骤"，
       LLM 决策时能看到全局进度而非只看最近观察；
    3. **偏离检测**：观察结果异常（检索为空 / 工具结果重复指针）时，
       LLM 判定偏离度（0-1），>= 阈值触发重规划；
    4. **仅重规划剩余**：重规划保留已完成步骤，只重新生成剩余步骤，
       避免"已完成工作被推翻"的开销与不连贯。

成本控制（与全局限流策略一致）：
    - 偏离检测是额外 LLM 调用，仅在启发式触发器命中时执行
      （检索为空 / dedup 指针命中 / 同一决策重复），不做每轮全量检测；
    - 每次会话最多重规划 max_replans 次，防止震荡；
    - 所有 LLM 调用失败时优雅降级为"无计划模式"（退回原动态循环）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

#: 合法步骤动作（与 Agent Loop 路由信号一致）
STEP_ACTIONS = ("retrieve", "tool_call", "generate")

#: 步骤状态
STEP_PENDING = "pending"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"

#: 偏离度阈值 — >= 此值触发重规划
DEVIATION_THRESHOLD = 0.6

#: 默认最大重规划次数（每会话）
DEFAULT_MAX_REPLANS = 2

#: 初始计划生成 prompt
_PLAN_PROMPT = """你是任务规划专家。为用户问题制定一个 2-4 步的执行计划。

可用动作：
- retrieve：检索知识库文档
- tool_call：调用业务工具（查工单/查审批/查日程等）
- generate：生成最终答案（必须是最后一步）

用户问题：{query}

以 JSON 数组输出，每步含 action 和 description：
```json
[
  {{"action": "retrieve", "description": "检索报销政策文档"}},
  {{"action": "generate", "description": "整理政策要点生成答案"}}
]
```

只输出 JSON，不要其他内容。"""

#: 偏离度判定 prompt
_DEVIATION_PROMPT = """你是执行监控专家。判断最新观察结果是否偏离了剩余计划。

用户问题：{query}
剩余计划步骤：{remaining}
最新观察结果：{observation}

偏离判定标准：
- 0.0-0.3：观察正常，计划可继续（如检索到相关文档、工具返回预期数据）
- 0.4-0.6：观察部分偏离，但剩余步骤仍可能完成（如文档相关性一般）
- 0.7-1.0：观察严重偏离，剩余步骤大概率无法完成（如检索为空、工具重复返回相同结果）

以 JSON 输出：
```json
{{"deviation": 0.8, "reason": "检索结果为空，原计划依赖文档内容"}}
```

只输出 JSON，不要其他内容。"""

#: 剩余步骤重规划 prompt
_REPLAN_PROMPT = """你是任务规划专家。当前执行遇到偏离，需要重新规划剩余步骤。

用户问题：{query}
已完成步骤（保留不变）：{done}
原剩余步骤（需要替换）：{remaining}
最新观察结果：{observation}

请重新制定 1-3 步剩余计划（最后一步必须是 generate）：
```json
[
  {{"action": "tool_call", "description": "改用工具查询实时数据"}},
  {{"action": "generate", "description": "基于工具结果生成答案"}}
]
```

只输出 JSON，不要其他内容。"""


class PlanManager:
    """显式计划管理器 — plan 状态清单的生命周期管理。

    无状态工具类：所有方法操作传入的 plan 列表，不保存会话状态。
    plan 本身存储在 AgentState["plan_steps"] 中随循环流转。

    Args:
        llm: LLM Provider（计划生成 / 偏离判定 / 重规划共用）。
        max_replans: 每会话最大重规划次数。
    """

    def __init__(self, llm: Any, max_replans: int = DEFAULT_MAX_REPLANS) -> None:
        self._llm = llm
        self.max_replans = max_replans

    # ------------------------------------------------------------------
    # 计划生成
    # ------------------------------------------------------------------

    async def build_initial_plan(self, query: str) -> list[dict[str, Any]]:
        """生成初始计划步骤清单。

        LLM 生成失败时降级为默认两步计划（retrieve → generate），
        保证计划机制始终可用且零阻塞。

        Args:
            query: 用户问题。

        Returns:
            步骤清单 [{step_id, action, description, status}]。
        """
        try:
            text = await self._chat(_PLAN_PROMPT.format(query=query))
            steps = self._parse_steps(text)
            if steps:
                log.info("planner.initial_plan", steps=len(steps))
                return steps
        except Exception as exc:
            log.warning("planner.initial_plan_error", error=str(exc))

        # 降级：默认两步计划
        return self._fallback_plan()

    @staticmethod
    def _fallback_plan() -> list[dict[str, Any]]:
        """默认两步计划 — LLM 不可用时的降级。"""
        return [
            {
                "step_id": 1,
                "action": "retrieve",
                "description": "检索知识库文档",
                "status": STEP_PENDING,
            },
            {
                "step_id": 2,
                "action": "generate",
                "description": "基于检索结果生成答案",
                "status": STEP_PENDING,
            },
        ]

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------

    @staticmethod
    def mark_action_done(
        plan: list[dict[str, Any]], action: str
    ) -> dict[str, Any] | None:
        """将第一个 pending 且 action 匹配的步骤标记为 done。

        Args:
            plan: 步骤清单（原地修改）。
            action: 刚执行完成的动作。

        Returns:
            被标记的步骤；无匹配时返回 None。
        """
        for step in plan:
            if step.get("status") == STEP_PENDING and step.get("action") == action:
                step["status"] = STEP_DONE
                return step
        return None

    @staticmethod
    def pending_steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """返回剩余（pending）步骤。"""
        return [s for s in plan if s.get("status") == STEP_PENDING]

    @staticmethod
    def done_steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """返回已完成步骤。"""
        return [s for s in plan if s.get("status") == STEP_DONE]

    @staticmethod
    def format_plan_brief(plan: list[dict[str, Any]]) -> str:
        """格式化为 think 动态上下文注入的简短计划视图。

        示例: "已完成[1.检索文档]；剩余[2.生成答案]"
        """
        done = [f"{s['step_id']}.{s['description']}" for s in plan if s.get("status") == STEP_DONE]
        pending = [f"{s['step_id']}.{s['description']}" for s in plan if s.get("status") == STEP_PENDING]
        parts: list[str] = []
        if done:
            parts.append("已完成[" + "；".join(done) + "]")
        if pending:
            parts.append("剩余[" + "；".join(pending) + "]")
        return "，".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # 偏离检测与重规划
    # ------------------------------------------------------------------

    async def assess_deviation(
        self,
        query: str,
        plan: list[dict[str, Any]],
        observation: str,
    ) -> float:
        """LLM 判定最新观察结果相对剩余计划的偏离度。

        仅在启发式触发器命中时由引擎调用（成本控制）。
        LLM 失败时返回 0.0（不触发重规划，保守策略）。

        Args:
            query: 用户问题。
            plan: 当前步骤清单。
            observation: 最新观察结果摘要。

        Returns:
            偏离度 0.0-1.0。
        """
        remaining = self.format_plan_brief(
            [s for s in plan if s.get("status") == STEP_PENDING]
        ) or "无"
        try:
            text = await self._chat(
                _DEVIATION_PROMPT.format(
                    query=query,
                    remaining=remaining,
                    observation=observation[:500],
                )
            )
            data = self._extract_json(text)
            if data:
                deviation = float(data.get("deviation", 0.0))
                deviation = max(0.0, min(1.0, deviation))
                log.info(
                    "planner.deviation_assessed",
                    deviation=deviation,
                    reason=str(data.get("reason", ""))[:100],
                )
                return deviation
        except Exception as exc:
            log.warning("planner.deviation_error", error=str(exc))
        return 0.0

    async def replan_remaining(
        self,
        query: str,
        plan: list[dict[str, Any]],
        observation: str,
    ) -> list[dict[str, Any]]:
        """仅对剩余步骤重规划 — 保留已完成步骤，替换 pending 步骤。

        LLM 失败时返回原计划（不改动）。

        Args:
            query: 用户问题。
            plan: 当前步骤清单。
            observation: 触发重规划的观察结果。

        Returns:
            新步骤清单（done 保留 + 新生成的 pending）。
        """
        done = self.done_steps(plan)
        done_brief = "；".join(f"{s['step_id']}.{s['description']}" for s in done) or "无"
        remaining_brief = "；".join(
            f"{s['step_id']}.{s['description']}" for s in self.pending_steps(plan)
        ) or "无"

        try:
            text = await self._chat(
                _REPLAN_PROMPT.format(
                    query=query,
                    done=done_brief,
                    remaining=remaining_brief,
                    observation=observation[:500],
                )
            )
            new_pending = self._parse_steps(text, start_id=len(done) + 1)
            if new_pending:
                # 强制最后一步为 generate（收敛保证）
                if new_pending[-1].get("action") != "generate":
                    new_pending.append({
                        "step_id": len(done) + len(new_pending) + 1,
                        "action": "generate",
                        "description": "生成最终答案",
                        "status": STEP_PENDING,
                    })
                log.info(
                    "planner.replanned",
                    kept_done=len(done),
                    new_pending=len(new_pending),
                )
                return done + new_pending
        except Exception as exc:
            log.warning("planner.replan_error", error=str(exc))
        return plan

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _chat(self, prompt: str) -> str:
        """调用 LLM 并拼接完整响应文本。"""
        from app.llm.base import Message

        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请按要求输出。"},
        ]
        text = ""
        async for chunk in self._llm.chat(messages, stream=False):
            if isinstance(chunk, str):
                text += chunk
        return text

    @staticmethod
    def _parse_steps(
        text: str, start_id: int = 1
    ) -> list[dict[str, Any]]:
        """从 LLM 响应解析步骤清单，过滤非法动作。"""
        data = PlanManager._extract_json(text)
        if not isinstance(data, list):
            return []
        steps: list[dict[str, Any]] = []
        for item in data:
            action = str(item.get("action", "")).strip()
            if action not in STEP_ACTIONS:
                continue
            steps.append({
                "step_id": start_id + len(steps),
                "action": action,
                "description": str(item.get("description", action))[:100],
                "status": STEP_PENDING,
            })
        return steps

    @staticmethod
    def _extract_json(text: str) -> Any:
        """从文本中提取 JSON（对象或数组），支持 markdown 代码块包裹。"""
        text = text.strip()
        candidates = [text]
        if "```" in text:
            for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
                candidates.insert(0, match.group(1).strip())
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
        # 兜底：提取第一个 [ 到最后一个 ] / 第一个 { 到最后一个 }
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            first = text.find(open_ch)
            last = text.rfind(close_ch)
            if first != -1 and last > first:
                try:
                    return json.loads(text[first : last + 1])
                except (json.JSONDecodeError, ValueError):
                    continue
        return None


#: crew 子任务类型 → plan 动作映射（P1-9 crew 对齐）
TASK_TYPE_TO_ACTION: dict[str, str] = {
    "qa": "retrieve",
    "workflow": "tool_call",
    "action": "tool_call",
}


def map_task_type_to_action(task_type: str) -> str:
    """将 crew 子任务类型映射为 plan 步骤动作。"""
    return TASK_TYPE_TO_ACTION.get(task_type, "retrieve")
