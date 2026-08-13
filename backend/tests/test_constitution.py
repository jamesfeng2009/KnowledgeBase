"""宪法独立成文件测试 — 验证 CONSTITUTION.md 加载与 engine/base 集成。

覆盖：
- constitution 加载器：get_system_prompt / get_constraint_reminder / get_constitution_path
- 节提取：markdown 二级标题解析、缺失节回退默认
- engine 集成：_THINK_SYSTEM_STABLE / _CONSTRAINT_REMINDER 由宪法派生
- base 集成：BaseAgent.system_prompt 默认取自宪法
"""
from __future__ import annotations

import pytest

from app.rag import constitution
from app.rag.constitution import (
    get_constraint_reminder,
    get_constitution_path,
    get_system_prompt,
)


class TestConstitutionLoader:
    """宪法加载器基础能力。"""

    def test_constitution_file_exists(self):
        """宪法文件应存在且可读。"""
        path = get_constitution_path()
        assert path.endswith("CONSTITUTION.md")
        text = constitution._read_constitution()
        assert "决策大脑" in text
        assert "必须遵守" in text

    def test_get_system_prompt_has_decision_keywords(self):
        """决策大脑节应包含三个决策关键词。"""
        prompt = get_system_prompt()
        assert "retrieve" in prompt
        assert "tool_call" in prompt
        assert "generate" in prompt

    def test_get_constraint_reminder_has_core_rules(self):
        """必须遵守节应包含核心约束。"""
        reminder = get_constraint_reminder()
        assert "必须遵守" in reminder
        assert "仅检索已发布" in reminder
        assert "禁止越权访问" in reminder
        assert "不得虚构" in reminder
        assert "租户" in reminder

    def test_extract_section_missing_falls_back(self, monkeypatch):
        """缺失节时应回退到内置默认值。"""
        monkeypatch.setattr(constitution, "_read_constitution", lambda: "## 决策大脑\n内容")
        # 必须遵守节缺失 → 回退默认
        reminder = get_constraint_reminder()
        assert "必须遵守" in reminder


class TestEngineConstitutionIntegration:
    """engine 常量由宪法派生。"""

    def test_think_system_stable_from_constitution(self):
        from app.rag.engine import _THINK_SYSTEM_STABLE

        assert _THINK_SYSTEM_STABLE == get_system_prompt()
        assert "retrieve" in _THINK_SYSTEM_STABLE

    def test_constraint_reminder_from_constitution(self):
        from app.rag.engine import _CONSTRAINT_REMINDER

        assert _CONSTRAINT_REMINDER == get_constraint_reminder()
        assert "必须遵守" in _CONSTRAINT_REMINDER


class TestBaseAgentConstitutionIntegration:
    """BaseAgent 默认 system_prompt 取自宪法。"""

    def test_base_system_prompt_from_constitution(self):
        from app.agents.base import BaseAgent

        assert BaseAgent.system_prompt == get_system_prompt()
