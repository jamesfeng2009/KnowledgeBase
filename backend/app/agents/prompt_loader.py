"""Agent Prompt 文件加载器 — 单一职责：把 Agent 的 system prompt 外置为可版本管理的 markdown 文件。

设计（P0 · prompt 外置重构）：
- 每个 Agent 类型对应 prompts/{agent_type}.md，文件内容即 system prompt 全文；
- 文件缺失 / 为空时回退到内置默认（与历史硬编码逐字一致，零回归保证）；
- lru_cache 缓存（底层 app.core.prompt_files），进程内只读一次磁盘；
- 文件纳入 git 管理 — 人工 review diff 后合入，git 即审计链与回滚手段。

注意：本模块只做「读文件 + 回退」，不做分节解析；分节（指引/红线）仅
用于 P1 技能进化的目标文件（app/rag/prompts/generate_base.md），
由 app.core.prompt_files.split_guidance_sections 处理。
"""

from __future__ import annotations

from pathlib import Path

from app.core.prompt_files import load_prompt_file

#: Agent prompt 文件目录
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 内置默认 — 与外置文件缺失时的回退值；修改 prompts/*.md 时须同步此处。
_DEFAULTS: dict[str, str] = {
    "qa": (
        "你是一个企业知识库问答助手。请基于提供的知识库检索结果，"
        "准确、简洁地回答用户问题。\n"
        "要求：\n"
        "1. 优先引用知识库中的文档内容；\n"
        "2. 若检索结果不足以回答，请如实说明信息不足；\n"
        "3. 回答使用中文，格式清晰。"
    ),
    "workflow": (
        "你是一个企业工作流执行助手。请理解用户的业务需求，"
        "引导用户完成对应的业务流程。\n"
        "要求：\n"
        "1. 识别用户意图所属的业务流程类型（如报销、请假、采购等）；\n"
        "2. 明确告知用户需要提供的信息和操作步骤；\n"
        "3. 需要查询外部系统状态时，告知用户正在查询；\n"
        "4. 回答使用中文，步骤清晰，格式规范。"
    ),
    "action": (
        "你是一个行动执行助手。请将用户的指令转化为具体可执行的步骤，"
        "并协助用户完成操作。\n"
        "要求：\n"
        "1. 分析用户指令，识别需要执行的操作类型；\n"
        "2. 将复杂指令拆解为清晰的步骤列表；\n"
        "3. 能通过工具自动完成的操作，直接执行并返回结果；\n"
        "4. 需要用户手动操作的，给出明确的操作指引；\n"
        "5. 回答使用中文，步骤明确，格式规范。"
    ),
}


def load_agent_prompt(agent_type: str) -> str:
    """加载指定 Agent 类型的 system prompt。

    Args:
        agent_type: Agent 类型标识（"qa" / "workflow" / "action"）。

    Returns:
        文件内容；文件缺失或为空时返回内置默认（未知类型返回空串）。
    """
    return load_prompt_file(
        PROMPTS_DIR, f"{agent_type}.md", _DEFAULTS.get(agent_type, "")
    )
