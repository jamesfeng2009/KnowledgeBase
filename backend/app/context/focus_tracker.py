"""
对话焦点追踪器 — 从对话历史中提取当前焦点（主题、实体、意图）。

策略：
    1. 规则优先：借助 P2 EntityRegistry 识别实体，零 Token
    2. LLM 兜底：规则未命中时，1 次轻量 LLM 调用（max_tokens=80）
    3. 焦点继承：最新查询无明显主题切换时，继承上一轮焦点

遵循单一职责：本模块只负责焦点提取，不做指代消解。
遵循优雅降级：EntityRegistry 不可用时跳过规则提取，LLM 不可用时返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ConversationFocus:
    """对话焦点 — 描述当前对话的主题和实体。

    Attributes:
        topic: 当前话题，如 "限号政策"。
        entity: 当前讨论的主体实体，如 "北京"。
        intent: 用户意图（查询/操作/对比等）。
        turn_index: 焦点确立的轮次（0 表示无法确定）。
        confidence: 置信度 [0.0, 1.0]。
    """

    topic: str
    entity: str
    intent: str = "查询"
    turn_index: int = 0
    confidence: float = 0.5

    def to_context_str(self) -> str:
        """渲染为 LLM prompt 片段。"""
        return f"当前对话焦点：主题={self.topic}，实体={self.entity}，意图={self.intent}"

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供 SSE 事件序列化）。"""
        return {
            "topic": self.topic,
            "entity": self.entity,
            "intent": self.intent,
            "turn_index": self.turn_index,
            "confidence": self.confidence,
        }


class TopicTracker:
    """对话焦点追踪器 — 从历史消息中提取当前焦点。

    使用方式::

        tracker = TopicTracker(llm)
        focus = await tracker.extract_focus(history_dicts)
        # focus.topic = "限号政策", focus.entity = "北京"

    策略优先级：规则提取（零 Token）→ LLM 提取（1 次轻量调用）→ 继承上次焦点。

    P4-C 增强：焦点历史栈（保留最近 N 个焦点），支持多轮跨指代回溯。
    """

    _FOCUS_PROMPT: str = (
        "分析以下对话历史，提取当前对话焦点。\n"
        "输出格式：topic|entity|intent（用|分隔，不要换行）\n"
        "示例：限号政策|北京|查询\n\n"
        "对话历史（最近3轮）：\n{history}\n\n"
        "当前焦点："
    )

    _HISTORY_PREVIEW_CHARS: int = 150
    _FOCUS_STACK_SIZE: int = 5

    def __init__(self, llm: LLMProvider | None = None) -> None:
        """初始化焦点追踪器。

        Args:
            llm: LLM Provider，为 None 时只走规则提取。
        """
        self._llm = llm
        self._focus_stack: list[ConversationFocus] = []

    @property
    def _last_focus(self) -> ConversationFocus | None:
        """兼容属性 — 返回栈顶焦点。"""
        return self._focus_stack[-1] if self._focus_stack else None

    @_last_focus.setter
    def _last_focus(self, value: ConversationFocus | None) -> None:
        """兼容 setter — None 时清空栈，非 None 时压入栈。"""
        if value is None:
            self._focus_stack.clear()
        else:
            self._push_focus(value)

    def _push_focus(self, focus: ConversationFocus) -> None:
        """压入新焦点，保持栈大小。"""
        self._focus_stack.append(focus)
        if len(self._focus_stack) > self._FOCUS_STACK_SIZE:
            self._focus_stack.pop(0)

    def reset_focus(self) -> None:
        """清空焦点栈 — 漂移检测触发时调用。"""
        self._focus_stack.clear()

    def get_focus_history(self, n: int = 3) -> list[ConversationFocus]:
        """获取最近 N 个焦点 — 供指代消解回溯。

        Args:
            n: 返回的焦点数量。

        Returns:
            最近 N 个焦点的列表（按时间顺序，最后一个是最新焦点）。
        """
        if not self._focus_stack:
            return []
        return self._focus_stack[-n:]

    async def extract_focus(
        self,
        history: list[dict[str, str]],
    ) -> ConversationFocus | None:
        """从对话历史中提取当前焦点。

        Args:
            history: 对话历史列表 [{"role": "user/assistant", "content": "..."}]

        Returns:
            ConversationFocus | None: 当前焦点，无法确定时返回 None
        """
        if not history:
            return self._last_focus

        # 单轮对话无法确定焦点
        if len(history) < 2:
            return self._last_focus

        # 1. 规则优先：从最近 user 消息提取
        recent_user_msgs = [m for m in history[-6:] if m.get("role") == "user"]
        if recent_user_msgs:
            focus = self._rule_extract(recent_user_msgs[-1].get("content", ""))
            if focus:
                self._last_focus = focus
                return focus

        # 2. LLM 兜底
        if self._llm:
            focus = await self._llm_extract(history[-6:])
            if focus:
                self._last_focus = focus
                return focus

        # 3. 继承上一轮焦点
        return self._last_focus

    def _rule_extract(self, query: str) -> ConversationFocus | None:
        """规则提取焦点 — 零 Token。

        借助 P2 EntityRegistry 识别查询中的实体，推断意图。
        """
        if not query or not query.strip():
            return None

        try:
            from app.ontology.entity_registry import EntityRegistry

            _, entity_names = EntityRegistry.expand_query(query)
            if entity_names:
                entity_def = EntityRegistry.resolve_entity(entity_names[0])
                if entity_def:
                    # 推断 intent
                    intent = "查询"
                    if any(kw in query for kw in ["对比", "比较", "区别"]):
                        intent = "对比"
                    elif any(kw in query for kw in ["创建", "上传", "提交", "新建"]):
                        intent = "操作"

                    return ConversationFocus(
                        topic=entity_def.display_name,
                        entity=entity_def.display_name,
                        intent=intent,
                        turn_index=0,
                        confidence=0.8,
                    )
        except Exception as exc:
            log.debug("topic_tracker.rule_extract_failed", error=str(exc))

        # 回退：从查询中提取关键词作为 topic
        # 检测常见话题关键词
        topic_keywords = {
            "天气": ("天气", "城市"),
            "限号": ("限号政策", "城市"),
            "限行": ("限号政策", "城市"),
            "报销": ("报销流程", "员工"),
            "请假": ("请假流程", "员工"),
            "合同": ("合同管理", "企业"),
            "采购": ("采购流程", "企业"),
        }
        for keyword, (topic, entity) in topic_keywords.items():
            if keyword in query:
                intent = "查询"
                if any(kw in query for kw in ["对比", "比较", "区别"]):
                    intent = "对比"
                elif any(kw in query for kw in ["创建", "上传", "提交", "新建"]):
                    intent = "操作"
                return ConversationFocus(
                    topic=topic,
                    entity=entity,
                    intent=intent,
                    turn_index=0,
                    confidence=0.7,
                )

        return None

    async def _llm_extract(self, recent: list[dict[str, str]]) -> ConversationFocus | None:
        """LLM 提取焦点 — 1 次轻量调用。"""
        history_text = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')[:self._HISTORY_PREVIEW_CHARS]}"
            for m in recent
        )
        prompt = self._FOCUS_PROMPT.format(history=history_text)
        try:
            text = await self._call_llm(prompt)
            parts = text.strip().split("|")
            if len(parts) >= 3:
                topic = parts[0].strip()
                entity = parts[1].strip()
                intent = parts[2].strip()
                if topic and entity:
                    return ConversationFocus(
                        topic=topic,
                        entity=entity,
                        intent=intent or "查询",
                        turn_index=0,
                        confidence=0.7,
                    )
        except Exception as exc:
            log.warning("topic_tracker.llm_extract_failed", error=str(exc))
        return None

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=80):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    def reset(self) -> None:
        """重置焦点状态 — 供测试使用。"""
        self._last_focus = None
