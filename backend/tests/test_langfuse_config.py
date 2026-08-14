"""LangFuse 自托管配置安全测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


class TestLangfuseConfig:
    """验证 LangFuse docker-compose 未硬编码 NEXTAUTH_SECRET / SALT，且关闭注册。"""

    @pytest.fixture
    def langfuse_compose(self) -> dict:
        compose_path = (
            Path(__file__).resolve().parent.parent.parent
            / "infra"
            / "langfuse"
            / "docker-compose.langfuse.yml"
        )
        assert compose_path.exists(), "LangFuse docker-compose 应存在"
        return yaml.safe_load(compose_path.read_text())

    def test_nextauth_secret_uses_env(self, langfuse_compose: dict) -> None:
        env = langfuse_compose["services"]["langfuse"]["environment"]
        assert "${LANGFUSE_NEXTAUTH_SECRET" in env["NEXTAUTH_SECRET"]
        assert "=" not in env["NEXTAUTH_SECRET"]

    def test_salt_uses_env(self, langfuse_compose: dict) -> None:
        env = langfuse_compose["services"]["langfuse"]["environment"]
        assert "${LANGFUSE_SALT" in env["SALT"]
        assert "=" not in env["SALT"]

    def test_signup_disabled(self, langfuse_compose: dict) -> None:
        env = langfuse_compose["services"]["langfuse"]["environment"]
        assert env["DISABLE_SIGNUP"] == "true"
