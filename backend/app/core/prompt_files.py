"""文件化 Prompt 通用原语 — 技能文档外置 / 分节解析 / 渲染。

被两处消费：
- app/agents/prompt_loader.py（Agent system prompt 外置，P0）
- app/rag/generator.py（生成层基础指引，P1 技能进化循环的进化对象）

分节约定（用于可进化目标文件）::

    ## 指引
    <可进化指引行...>

    ## 红线（冻结区，禁止修改）
    <红线行...>

解析后红线区由代码单独持有、永不进入编辑器输入 — 进化循环在结构上
无法修改红线（受保护区守卫，对应 SkillOpt 纪律的 frozen section）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: 指引区标题（精确匹配）
GUIDANCE_HEADING = "## 指引"
#: 红线区标题前缀（含"红线"即视为冻结区标题）
REDLINE_HEADING_PREFIX = "## 红线"


@lru_cache(maxsize=64)
def _read_text(directory: str, name: str) -> str | None:
    """读取文件文本；缺失或为空时返回 None（调用方回退内置默认）。"""
    try:
        text = (Path(directory) / name).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def load_prompt_file(directory: Path, name: str, default: str = "") -> str:
    """读取 prompt 文件全文（strip 后）；缺失/为空时返回 default。

    空文件视为缺失 — 意外清空文件不会抹掉 prompt，只会回退默认值。
    """
    text = _read_text(str(directory), name)
    return text if text is not None else default


def _trim_blank(lines: list[str]) -> list[str]:
    """去掉列表首尾的空行（保留中间空行与行序）。"""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def split_guidance_sections(text: str) -> tuple[list[str], list[str]]:
    """按分节约定拆分为（指引区行列表, 红线区行列表）。

    - 无任何标题时：全部行归入指引区（兼容 P0 纯文本文件）；
    - 标题行本身不进入任何区；
    - 各区首尾空行被去除。
    """
    guidance: list[str] = []
    redline: list[str] = []
    current = guidance
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == GUIDANCE_HEADING:
            current = guidance
            continue
        if stripped.startswith(REDLINE_HEADING_PREFIX) and "红线" in stripped:
            current = redline
            continue
        current.append(raw.rstrip())
    return _trim_blank(guidance), _trim_blank(redline)


def compose_guidance_prompt(
    guidance_lines: list[str], redline_lines: list[str]
) -> str:
    """把（指引区, 红线区）拼回最终注入 LLM 的基础指引文本。

    直接以单换行连接（与历史硬编码三段式逐字一致，无段间空行）。
    """
    return "\n".join([*guidance_lines, *redline_lines]).strip()


def render_guidance_file(
    guidance_lines: list[str], redline_lines: list[str]
) -> str:
    """把（指引区, 红线区）渲染为完整文件文本（带分节标题）。

    进化循环用「候选指引区 + 原红线区」渲染候选文件 — 红线区永远来自
    原文件，编辑器产出的只有指引区。
    """
    parts: list[str] = []
    if guidance_lines:
        parts.append(GUIDANCE_HEADING)
        parts.extend(guidance_lines)
    if redline_lines:
        if parts:
            parts.append("")
        parts.append(f"{REDLINE_HEADING_PREFIX}（冻结区，禁止修改）")
        parts.extend(redline_lines)
    return "\n".join(parts).strip() + "\n"
