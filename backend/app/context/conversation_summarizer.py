"""
对话历史滚动摘要 — ConversationSummaryBuffer 策略。

当对话历史超过 token 阈值时，将旧消息压缩为滚动摘要，
保留近期消息原文。结构：

    [摘要: "用户询问了北京天气和限号政策..."] + [最近4条原文]

设计要点：
    - T_max 触发压缩，T_retained 压缩后目标
    - 摘要增量更新（合并旧摘要 + 新旧消息）
    - LLM 压缩失败时回退为截断（保留最近 N 条）

遵循优雅降级：LLM 不可用时截断旧消息，保留最近 N 条原文。
"""

from __future__ import annotations

from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

log = get_logger(__name__)


class ConversationSummarizer:
    """对话历史滚动摘要器。

    使用方式::

        summarizer = ConversationSummarizer(llm)
        summary, recent = await summarizer.summarize_if_needed(history)
    """

    _SUMMARIZE_PROMPT: str = (
        "请将以下对话历史压缩为简洁的摘要（不超过200字）。\n"
        "保留关键信息：讨论的主题、已确认的事实、用户偏好。\n"
        "省略寒暄和重复内容。\n\n"
        "对话历史：\n{history}\n\n"
        "摘要："
    )

    def __init__(
        self,
        llm: LLMProvider | None = None,
        max_tokens: int = 600,
        retained_tokens: int = 200,
        summary_max_chars: int = 300,
    ) -> None:
        """初始化滚动摘要器。

        Args:
            llm: LLM Provider，为 None 时只做截断。
            max_tokens: 超过此值触发摘要。
            retained_tokens: 压缩后保留的近期消息 token。
            summary_max_chars: 摘要最大字符数。
        """
        self._llm = llm
        self._max_tokens = max_tokens
        self._retained_tokens = retained_tokens
        self._summary_max_chars = summary_max_chars

    async def summarize_if_needed(
        self,
        history: list[dict[str, str]],
        existing_summary: str = "",
    ) -> tuple[str, list[dict[str, str]]]:
        """如果历史超过阈值，压缩旧消息为摘要。

        Args:
            history: 完整对话历史。
            existing_summary: 已有的旧摘要（增量合并）。

        Returns:
            tuple[摘要文本, 保留的近期消息列表]:
            - 摘要文本：旧历史压缩后的摘要（可能为空）
            - 近期消息：保留原文的最近 N 条消息
        """
        if not history:
            return existing_summary, []

        # 估算总 token（粗估：字符数 / 3）
        total_chars = sum(len(m.get("content", "")) for m in history)
        total_tokens = total_chars // 3

        if total_tokens <= self._max_tokens:
            # 不需要压缩
            return existing_summary, list(history)

        # 分割：旧消息（待压缩）+ 近期消息（保留原文）
        retained_chars = self._retained_tokens * 3
        split_idx = len(history)
        chars_so_far = 0
        for i in range(len(history) - 1, -1, -1):
            chars_so_far += len(history[i].get("content", ""))
            if chars_so_far >= retained_chars:
                split_idx = i
                break

        old_messages = history[:split_idx]
        recent_messages = history[split_idx:]

        if not old_messages:
            return existing_summary, list(recent_messages)

        # 压缩旧消息
        summary = existing_summary
        try:
            if self._llm:
                # 合并旧摘要 + 新旧消息
                history_text = "\n".join(
                    f"{m.get('role', 'user')}: {m.get('content', '')[:200]}"
                    for m in old_messages
                )
                prompt_input = ""
                if existing_summary:
                    prompt_input = f"已有摘要：{existing_summary}\n\n新对话：\n{history_text}"
                else:
                    prompt_input = history_text

                prompt = self._SUMMARIZE_PROMPT.format(history=prompt_input)
                new_summary = await self._call_llm(prompt)
                if new_summary:
                    summary = new_summary[: self._summary_max_chars]
                    log.info(
                        "conversation_summarizer.compressed",
                        original_tokens=total_tokens,
                        summary_chars=len(summary),
                        retained_messages=len(recent_messages),
                    )
        except Exception as exc:
            log.warning("conversation_summarizer.llm_failed", error=str(exc))
            # 降级：用旧摘要 + 截断旧消息的第一条
            if old_messages:
                first_msg = old_messages[0]
                summary = f"{existing_summary}\n{first_msg.get('role', 'user')}: {first_msg.get('content', '')[:100]}"

        return summary, recent_messages

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=300):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks).strip()
