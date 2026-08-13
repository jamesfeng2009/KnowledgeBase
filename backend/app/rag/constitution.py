"""EKB Agent 宪法加载器 — 将核心约束从独立文件读入，实现"人机共治"。

对应 P1-2「宪法独立成文件」：
- 核心约束（决策大脑 system prompt + 尾部约束提醒）不再硬编码在 engine.py，
  而是放在独立 ``CONSTITUTION.md`` 中，人类可直接编辑，机器启动时加载；
- 前缀稳定：宪法内容稳定、不随轮次变化，命中 Anthropic KV Cache；
- 单一事实来源：engine 与各 Agent 都从本模块读取，避免多份拷贝漂移。

文件位置：与本模块同目录的 ``CONSTITUTION.md``。
"""

from __future__ import annotations

from pathlib import Path

# 宪法文件路径 — 与本模块同目录
_CONSTITUTION_FILE: Path = Path(__file__).resolve().parent / "CONSTITUTION.md"

# 节标题标记（markdown 二级标题）
_SECTION_SYSTEM: str = "## 决策大脑"
_SECTION_CONSTRAINT: str = "## 必须遵守"

# 内置默认宪法 — 仅当 CONSTITUTION.md 缺失或损坏时使用，保证系统不因文件缺失而崩溃。
_DEFAULT_CONSTITUTION: str = (
    "# EKB Agent 宪法（内置默认）\n"
    "\n"
    "## 决策大脑\n"
    "你是企业知识库助手的决策大脑。分析用户问题和已有信息，决定下一步：\n"
    '- 回复 "retrieve"：需要检索知识库补充信息；\n'
    '- 回复 "tool_call"：需要调用企业系统工具（如查 OA/ERP/IT 工单）；\n'
    '- 回复 "generate"：已有足够信息，可以生成最终答案。\n'
    "\n"
    "只回复上述三个关键词之一，不要附加解释。\n"
    "\n"
    "## 必须遵守\n"
    "【必须遵守】\n"
    "- 仅检索已发布（published）状态的文档，不得检索草稿或待审核文档；\n"
    "- 禁止越权访问：不得读取超出当前用户权限（classification 级别）的文档；\n"
    "- 不得虚构或编造未在检索上下文与工具结果中出现的事实与引用；\n"
    "- 检索与工具调用必须限定在当前租户（tenant）范围内，防止跨租户信息泄漏。\n"
)


def _read_constitution() -> str:
    """读取宪法文件全文；文件缺失时回退到内置默认值，保证系统可用。"""
    try:
        return _CONSTITUTION_FILE.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT_CONSTITUTION


def _extract_section(text: str, header: str) -> str:
    """按 markdown 二级标题提取节内容（不含标题行），找不到返回空串。"""
    lines = text.splitlines()
    out: list[str] = []
    capture = False
    for line in lines:
        if line.strip() == header:
            capture = True
            continue
        if capture and line.strip().startswith("## "):
            break
        if capture:
            out.append(line)
    return "\n".join(out).strip()


def get_system_prompt() -> str:
    """返回决策大脑 system prompt（宪法「决策大脑」节）。"""
    text = _read_constitution()
    section = _extract_section(text, _SECTION_SYSTEM)
    if not section:
        section = _extract_section(_DEFAULT_CONSTITUTION, _SECTION_SYSTEM)
    return section


def get_constraint_reminder() -> str:
    """返回尾部约束提醒（宪法「必须遵守」节）。"""
    text = _read_constitution()
    section = _extract_section(text, _SECTION_CONSTRAINT)
    if not section:
        section = _extract_section(_DEFAULT_CONSTITUTION, _SECTION_CONSTRAINT)
    return section


def get_constitution_path() -> str:
    """返回宪法文件绝对路径（供运维 / 文档引用）。"""
    return str(_CONSTITUTION_FILE)
