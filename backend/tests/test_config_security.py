"""配置安全校验测试 — 防止默认密钥/明文凭证流入生产环境。"""
from __future__ import annotations

import os

import pytest

from app.config import Settings


class TestSecretKeyValidation:
    """生产环境 SECRET_KEY 校验。"""

    def test_production_with_default_secret_key_raises(self, monkeypatch):
        """DEBUG=False 且使用默认 SECRET_KEY 时必须抛 ValueError。"""
        monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
        monkeypatch.setenv("DEBUG", "false")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb")
        with pytest.raises(ValueError, match="生产环境.*默认 SECRET_KEY"):
            Settings()

    def test_development_with_default_secret_key_allowed(self, monkeypatch):
        """DEBUG=True 时允许使用默认 SECRET_KEY（本地开发）。"""
        monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
        monkeypatch.setenv("DEBUG", "true")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb")
        # 不应抛异常
        settings = Settings()
        assert settings.SECRET_KEY == "change-me-in-production"
        assert settings.DEBUG is True

    def test_production_with_random_secret_key_allowed(self, monkeypatch):
        """DEBUG=False 且使用随机 SECRET_KEY 时正常启动。"""
        random_key = "a" * 64
        monkeypatch.setenv("SECRET_KEY", random_key)
        monkeypatch.setenv("DEBUG", "false")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb")
        settings = Settings()
        assert settings.SECRET_KEY == random_key
        assert settings.DEBUG is False


class TestEnvFileNoRealSecrets:
    """.env 文件不应包含真实密钥。"""

    def test_backend_env_has_no_dashscope_key(self):
        """backend/.env 中 DASHSCOPE_API_KEY 必须被注释或为空/占位符。"""
        backend_env = (
            __file__.replace("/tests/test_config_security.py", "/.env")
        )
        if not os.path.exists(backend_env):
            pytest.skip("backend/.env 不存在")
        content = open(backend_env).read()
        for line in content.splitlines():
            if line.strip().startswith("DASHSCOPE_API_KEY="):
                value = line.split("=", 1)[1].strip()
                assert value in ("", "sk-xxx"), f"backend/.env 包含真实 DASHSCOPE_API_KEY: {line}"

    def test_frontend_env_has_no_autodl_token(self):
        """frontend/.env 中不应包含 AUTODL_API_TOKEN。"""
        frontend_env = (
            __file__.replace("/backend/tests/test_config_security.py", "/frontend/.env")
        )
        if not os.path.exists(frontend_env):
            pytest.skip("frontend/.env 不存在")
        content = open(frontend_env).read()
        assert "AUTODL_API_TOKEN=" not in content, "frontend/.env 仍包含 AUTODL_API_TOKEN"
