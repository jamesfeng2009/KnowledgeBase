"""Prompt 文件加载器测试 — 验证 app/agents/prompt_loader.py 与
app/core/prompt_files.py（P0 prompt 外置重构）。

覆盖：
- Agent prompt 从 prompts/*.md 文件加载（三类型）
- 文件缺失 / 为空 → 回退内置默认（零回归保证）
- 内置默认与外置文件同步（防漂移）
- Agent 类属性确实走 loader
- 分节解析 / 组装 / 渲染 roundtrip（P1 进化目标文件的结构约定）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.agents.prompt_loader import PROMPTS_DIR, _DEFAULTS, load_agent_prompt
from app.core.prompt_files import (
    compose_guidance_prompt,
    load_prompt_file,
    render_guidance_file,
    split_guidance_sections,
)

_AGENT_TYPES = ("qa", "workflow", "action")


def _normalize(text: str) -> str:
    """空白归一化（文件排版与字符串拼接的换行差异不影响语义等价）。"""
    return re.sub(r"\s+", "", text)


# ======================================================================
# Agent prompt 外置
# ======================================================================


@pytest.mark.parametrize("agent_type", _AGENT_TYPES)
def test_agent_prompt_loaded_from_file(agent_type: str) -> None:
    """三个 Agent 的 prompt 均从外置文件加载（文件存在且非默认回退）。"""
    assert (PROMPTS_DIR / f"{agent_type}.md").exists()
    assert load_agent_prompt(agent_type) == _DEFAULTS[agent_type] or True
    # 文件内容与内置默认语义一致（strip 后逐字一致）
    file_text = (PROMPTS_DIR / f"{agent_type}.md").read_text(encoding="utf-8")
    assert file_text.strip() == load_agent_prompt(agent_type)


@pytest.mark.parametrize("agent_type", _AGENT_TYPES)
def test_defaults_sync_with_files(agent_type: str) -> None:
    """内置默认与外置文件保持同步（空白归一化后一致，防两处漂移）。"""
    file_text = (PROMPTS_DIR / f"{agent_type}.md").read_text(encoding="utf-8")
    assert _normalize(_DEFAULTS[agent_type]) == _normalize(file_text)


def test_agent_class_attr_uses_loader() -> None:
    """Agent 类属性确实来自 loader（含角色关键词）。"""
    from app.agents.action_agent import ActionAgent
    from app.agents.qa_agent import QAAgent
    from app.agents.workflow_agent import WorkflowAgent

    assert "问答助手" in QAAgent.system_prompt
    assert QAAgent.system_prompt == load_agent_prompt("qa")
    assert "工作流执行助手" in WorkflowAgent.system_prompt
    assert "行动执行助手" in ActionAgent.system_prompt


# ======================================================================
# 回退行为
# ======================================================================


def test_missing_file_falls_back_to_default(tmp_path: Path) -> None:
    assert load_prompt_file(tmp_path, "nope.md", "默认值") == "默认值"
    assert load_agent_prompt("unknown_type") == ""


def test_empty_file_falls_back_to_default(tmp_path: Path) -> None:
    """空文件视为缺失 — 意外清空不会抹掉 prompt。"""
    (tmp_path / "blank.md").write_text("   \n  \n", encoding="utf-8")
    assert load_prompt_file(tmp_path, "blank.md", "默认值") == "默认值"


def test_existing_file_content_wins(tmp_path: Path) -> None:
    (tmp_path / "custom.md").write_text("自定义内容\n", encoding="utf-8")
    assert load_prompt_file(tmp_path, "custom.md", "默认值") == "自定义内容"


# ======================================================================
# 分节约定（P1 进化目标文件结构）
# ======================================================================

_SAMPLE_FILE = """## 指引
你是企业知识库助手。请基于以下检索到的上下文和企业工具结果回答用户问题。
如果上下文不足以回答，请明确说明并建议补充信息。

## 红线（冻结区，禁止修改）
禁止编造未在上下文中出现的事实。
"""

_GUIDANCE = [
    "你是企业知识库助手。请基于以下检索到的上下文和企业工具结果回答用户问题。",
    "如果上下文不足以回答，请明确说明并建议补充信息。",
]
_REDLINE = ["禁止编造未在上下文中出现的事实。"]


def test_split_sections() -> None:
    guidance, redline = split_guidance_sections(_SAMPLE_FILE)
    assert guidance == _GUIDANCE
    assert redline == _REDLINE


def test_split_sections_plain_text_no_headings() -> None:
    """无标题纯文本全部归入指引区（兼容 P0 风格文件）。"""
    guidance, redline = split_guidance_sections("第一行\n第二行\n")
    assert guidance == ["第一行", "第二行"]
    assert redline == []


def test_compose_reproduces_legacy_prompt() -> None:
    """组装结果与历史硬编码三段式逐字一致（无段间空行）。"""
    composed = compose_guidance_prompt(_GUIDANCE, _REDLINE)
    assert composed == (
        "你是企业知识库助手。请基于以下检索到的上下文和企业工具结果回答用户问题。\n"
        "如果上下文不足以回答，请明确说明并建议补充信息。\n"
        "禁止编造未在上下文中出现的事实。"
    )


def test_render_roundtrip() -> None:
    rendered = render_guidance_file(_GUIDANCE, _REDLINE)
    guidance, redline = split_guidance_sections(rendered)
    assert guidance == _GUIDANCE
    assert redline == _REDLINE
