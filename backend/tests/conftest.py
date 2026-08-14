"""测试公共夹具 — 确保环境变量在 app 模块导入前就位。

关键点：
    - SECRET_KEY 必须在导入 app.utils.crypto 前设置，否则 AES-GCM 密钥派生
      会因默认值不稳定而失败（项目要求 SECRET_KEY 必须显式配置）。
    - DATABASE_URL 指向 PostgreSQL（项目硬约束：禁止 SQLite，含测试环境）。
      本批单测全部通过 mock 隔离 DB，不会真实连接，但 env 仍需合法。
    - AUTO_MIGRATE=false 避免单测触发迁移。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

# 必须在 import app.* 之前设置
os.environ.setdefault("SECRET_KEY", "pytest-secret-key-do-not-use-in-prod")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb_test",
)
os.environ.setdefault("AUTO_MIGRATE", "false")
os.environ.setdefault("AUTO_CREATE_TABLES", "false")


@pytest.fixture
def mock_user():
    """认证用户替身 — 被 api_endpoints / api_service_security_fixes 等复用。"""
    return SimpleNamespace(
        id=uuid4(),
        email="test@ekb.local",
        name="测试用户",
        role="editor",
        clearance_level="internal",
        dept_id=None,
        is_active=True,
    )
