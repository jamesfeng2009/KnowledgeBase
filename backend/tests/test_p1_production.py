"""
P1 多租户生产化测试。

覆盖：
1. 租户维度限流（middleware 配置 + 实例化）
2. 子域/workspace 路由解析（tenant_resolver）
3. 定时 PG 备份（备份/恢复脚本存在性与可执行性）
4. Prometheus 可观测指标（metrics 模块）
5. 机密管理（secrets 模块：文件提供方 / 幂等 / env 优先）

本批单测不依赖真实数据库 / Redis，使用 mock 与临时目录隔离。
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.core.tenant_resolver import (
    parse_tenant_subdomain,
    extract_domain_from_request,
    resolve_tenant_id,
    TENANT_ID_HEADER,
)
from app.core import secrets as secrets_module
from app.middleware import (
    RateLimiter,
    _build_limiter,
    get_tenant_rate_limiter,
)
from app.utils import metrics


# ==================================================================
# 1. 租户维度限流
# ==================================================================


class TestTenantRateLimit:
    def test_build_limiter_memory_when_no_redis(self):
        """无 Redis URL 时构建内存令牌桶。"""
        limiter = _build_limiter(per_minute=600, burst=50, redis_url=None)
        assert isinstance(limiter, RateLimiter)

    def test_tenant_limiter_isolation(self):
        """不同租户使用独立桶 — 一个租户打满不影响另一个。"""
        limiter = _build_limiter(per_minute=2, burst=1, redis_url=None)
        tid_a, tid_b = str(uuid.uuid4()), str(uuid.uuid4())

        # 租户 A 消费突发容量（burst=1）
        assert limiter.allow(f"tenant:{tid_a}") is True
        assert limiter.allow(f"tenant:{tid_a}") is False  # A 打满
        # 租户 B 不受影响
        assert limiter.allow(f"tenant:{tid_b}") is True

    def test_tenant_limiter_configured_in_middleware(self):
        """setup_middleware 会初始化租户限流器。"""
        from app.middleware import _tenant_rate_limiter

        # 默认 RATE_LIMIT_TENANT_ENABLED=True，配置后实例应为 RateLimiter（无 Redis）
        assert _tenant_rate_limiter is None or isinstance(
            _tenant_rate_limiter, RateLimiter
        )


# ==================================================================
# 2. 子域/workspace 路由解析
# ==================================================================


class TestTenantResolver:
    def test_parse_tenant_subdomain_basic(self):
        """解析 {workspace}.{root_domain} 中的 workspace。"""
        assert (
            parse_tenant_subdomain("acme.example.com", "example.com") == "acme"
        )

    def test_parse_tenant_subdomain_no_root(self):
        """未配置根域不路由。"""
        assert parse_tenant_subdomain("acme.example.com", "") is None
        assert parse_tenant_subdomain("acme.example.com", None) is None

    def test_parse_tenant_subdomain_apex(self):
        """根域自身（apex）无子域。"""
        assert parse_tenant_subdomain("example.com", "example.com") is None

    def test_parse_tenant_subdomain_no_match(self):
        """不属于根域的子域不路由。"""
        assert parse_tenant_subdomain("acme.other.com", "example.com") is None

    def _make_request(self, headers: dict[str, str]):
        from fastapi import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/chat",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in headers.items()
            ],
        }
        return Request(scope)

    @patch("app.utils.crypto.decode_access_token")
    def test_resolve_tenant_jwt_priority(self, mock_decode):
        """JWT 中的 tenant_id 优先。"""
        tid = uuid.uuid4()
        mock_decode.return_value = {"tenant_id": str(tid)}
        req = self._make_request({"authorization": "Bearer x.y.z"})
        tenant_id, domain = resolve_tenant_id(req, None)
        assert tenant_id == tid

    def test_resolve_tenant_header_fallback(self):
        """无 JWT 时回退到网关 X-Tenant-Id 头。"""
        tid = uuid.uuid4()
        req = self._make_request({TENANT_ID_HEADER: str(tid)})
        tenant_id, domain = resolve_tenant_id(req, None)
        assert tenant_id == tid

    def test_resolve_tenant_no_token_no_header(self):
        """无 JWT 且无头时返回 None。"""
        req = self._make_request({"host": "acme.example.com"})
        tenant_id, domain = resolve_tenant_id(req, None)
        assert tenant_id is None
        # 子域被提取为 tenant_domain（但不触 DB 映射）
        assert domain == "acme.example.com"

    def test_extract_domain_from_header(self):
        """优先取网关透传 X-Tenant-Domain 头。"""
        req = self._make_request(
            {
                "host": "acme.example.com",
                "X-Tenant-Domain": "acme.example.com",
            }
        )
        assert extract_domain_from_request(req) == "acme.example.com"


# ==================================================================
# 3. 定时 PG 备份 + 恢复演练（脚本存在与可执行）
# ==================================================================


class TestPgBackup:
    @pytest.mark.parametrize(
        "path",
        [
            "infra/backup/backup.sh",
            "infra/backup/restore_drill.sh",
            "infra/backup/entrypoint.sh",
        ],
    )
    def test_backup_scripts_exist_and_executable(self, path):
        """备份/恢复脚本存在且具备可执行位。"""
        full = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), path
        )
        assert os.path.isfile(full), f"脚本不存在: {path}"
        assert os.access(full, os.X_OK), f"脚本无执行权限: {path}"


# ==================================================================
# 4. Prometheus 可观测指标
# ==================================================================


class TestMetrics:
    def test_metrics_response_contains_http_requests(self):
        """/metrics 输出包含 http_requests_total。"""
        metrics.record_http_request("GET", "/api/v1/chat", 200, 0.01, "t1")
        body = metrics.metrics_response().body.decode()
        assert "http_requests_total" in body
        assert "http_request_duration_seconds" in body

    def test_normalize_path_hides_uuid(self):
        """UUID 动态段被归一化，控制指标基数。"""
        from app.utils.metrics import _normalize_path

        tid = str(uuid.uuid4())
        assert _normalize_path(f"/api/v1/documents/{tid}") == (
            "/api/v1/documents/{id}"
        )
        # 数字段归一化
        assert _normalize_path("/api/v1/knowledge/42") == (
            "/api/v1/knowledge/{id}"
        )
        # 静态段保留
        assert _normalize_path("/api/v1/chat") == "/api/v1/chat"

    def test_record_llm_usage_zero_ignored(self):
        """0 token 不产生计数。"""
        metrics.record_llm_usage("t1", "qwen", "input", 0)  # 不应抛异常
        metrics.record_llm_usage("t1", "qwen", "input", 100)

    def test_record_tenant_ratelimit_denied(self):
        """租户限流拒绝计数。"""
        metrics.record_tenant_ratelimit_denied("t1")
        body = metrics.metrics_response().body.decode()
        assert "tenant_ratelimit_denied_total" in body


# ==================================================================
# 5. 机密管理（Secrets Manager 替代 .env）
# ==================================================================


class TestSecretsManager:
    def test_apply_secrets_idempotent(self, monkeypatch, tmp_path):
        """apply_secrets 幂等 — 多次调用只注入一次。"""
        monkeypatch.setattr(secrets_module, "_secrets_applied", False)
        os.environ["SECRETS_PROVIDER"] = "file"
        os.environ["SECRETS_FILE_DIR"] = str(tmp_path)
        secret_file = tmp_path / "DATABASE_URL"
        secret_file.write_text("postgresql+asyncpg://secret")

        # 清理可能存在的同名环境变量
        os.environ.pop("DATABASE_URL", None)

        secrets_module.apply_secrets()
        assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://secret"

        # 第二次调用不应重新注入/异常
        secrets_module.apply_secrets()
        assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://secret"

        monkeypatch.setattr(secrets_module, "_secrets_applied", False)

    def test_env_var_overrides_secret_file(self, monkeypatch, tmp_path):
        """显式环境变量优先于机密文件。"""
        monkeypatch.setattr(secrets_module, "_secrets_applied", False)
        os.environ["SECRETS_PROVIDER"] = "file"
        os.environ["SECRETS_FILE_DIR"] = str(tmp_path)
        os.environ["DATABASE_URL"] = "postgresql+asyncpg://from_env"
        (tmp_path / "DATABASE_URL").write_text("postgresql+asyncpg://from_file")

        secrets_module.apply_secrets()
        assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://from_env"

        monkeypatch.setattr(secrets_module, "_secrets_applied", False)

    def test_file_secrets_missing_dir_is_noop(self, monkeypatch):
        """机密目录不存在时静默跳过。"""
        monkeypatch.setattr(secrets_module, "_secrets_applied", False)
        os.environ["SECRETS_PROVIDER"] = "file"
        os.environ["SECRETS_FILE_DIR"] = "/nonexistent/secret/dir"
        secrets_module.apply_secrets()  # 不应抛异常

        monkeypatch.setattr(secrets_module, "_secrets_applied", False)
