"""
对话历史滚动摘要 — ConversationSummaryBuffer 策略 + 模型可读摘要（P0-2）。

当对话历史超过 token 阈值时，将旧消息压缩为滚动摘要，
保留近期消息原文。结构：

    [摘要: "用户询问了北京天气和限号政策..."] + [最近4条原文]

P0-2 改造：摘要"给模型看、不是给人看"。相比普通摘要（保留主题/已确认事实/
用户偏好），模型可读摘要强制保留决策关键信息并删除冗余：
    强制保留（逐字保留具体值，不概括）：
      - 约束与禁止：不能 / 不要 / 禁止 / 必须 / 务必 / 不得超过 / 不允许
      - 否定与转折：不是A是B / 但 / 除非 / 仅当
      - 数字与度量：金额、数量、百分比、截止日期、时长、版本号、配置参数值
      - 时间与来源：具体时间点 / 日期 / 编号 / 文档或来源名称
      - 关键证据：已被确认的事实、引用的数据、给出的案例
    必删项：
      - 礼貌铺垫（"你好""谢谢""请帮忙"）
      - 口语填充（"嗯""那个""我觉得可能"）
      - 重复解释、寒暄、无信息量客套
    输出格式：每行一条事实（key: value），值完整保留数字与约束词 —
      显著降 token 且不丢决策关键信息，对知识库场景尤为适用。

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

    # P0-2: 模型可读摘要 prompt — 强制保留约束/否定/数字/来源/证据，
    # 删除礼貌铺垫/口语填充/重复解释，输出高信息密度的事实清单。
    _SUMMARIZE_PROMPT: str = (
        "你是上下文压缩引擎。把对话历史压缩为高信息密度的模型可读摘要"
        "（给后续回答的模型看，不是给人看）。\n\n"
        "必须强制保留（逐字保留具体值，不要概括）：\n"
        "1. 约束与禁止：不能/不要/禁止/必须/务必/不得超过/不允许/一定要\n"
        "2. 否定与转折：不是A而是B、但、除非、仅当\n"
        "3. 数字与度量：金额、数量、百分比、截止日期、时长、版本号、配置参数值\n"
        "4. 时间与来源：具体时间点/日期、编号、文档或来源名称\n"
        "5. 关键证据：已被确认的事实、引用的数据、给出的案例\n\n"
        "必须删除：\n"
        "- 礼貌铺垫（你好/谢谢/请帮忙）\n"
        "- 口语填充（嗯/那个/我觉得可能）\n"
        "- 重复解释、寒暄、无信息量的客套\n\n"
        "输出格式（每行一条事实，key: value，值完整保留数字与约束词，"
        "不超过200字）：\n"
        "事实: ...\n"
        "约束: ...\n"
        "决策: ...\n"
        "偏好: ...\n\n"
        "对话历史：\n{history}\n\n"
        "高密度摘要："
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
                    f"{m.get('role', 'user')}: {(m.get('content') or '')[:200]}"
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
            # 降级：用旧摘要 + 最近 N 条旧消息（保留最新上下文，
            # 而非首条 —— 首条是最旧消息，保留它会静默丢失近期事实）。
            if old_messages:
                fallback_lines = [
                    f"{m.get('role', 'user')}: {(m.get('content') or '')[:100]}"
                    for m in old_messages[-3:]
                ]
                parts = [p for p in (existing_summary, *fallback_lines) if p]
                summary = "\n".join(parts)

        return summary, recent_messages

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 返回文本。"""
        messages: list[Message] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        async for chunk in self._llm.chat(messages, stream=True, max_tokens=300):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks).strip()
