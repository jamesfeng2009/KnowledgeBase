"""APISIX 边缘网关配置安全测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


class TestApisixConfig:
    """验证 APISIX standalone 配置启用 admin key 且无错误插件字段。"""

    @pytest.fixture
    def apisix_config(self) -> dict:
        config_path = (
            Path(__file__).resolve().parent.parent.parent
            / "infra"
            / "apisix"
            / "apisix.yml"
        )
        assert config_path.exists(), "apisix.yml 应存在"
        return yaml.safe_load(config_path.read_text())

    def test_admin_key_required_enabled(self, apisix_config: dict) -> None:
        assert apisix_config["deployment"]["admin"]["admin_key_required"] is True

    def test_admin_key_configured(self, apisix_config: dict) -> None:
        keys = apisix_config["deployment"]["admin"]["admin_key"]
        assert len(keys) >= 1
        assert keys[0]["role"] == "admin"
        assert "${APISIX_ADMIN_KEY" in keys[0]["key"]
        assert "change-me" in keys[0]["key"] or "APISIX_ADMIN_KEY" in keys[0]["key"]

    def test_limit_req_has_no_header_set(self, apisix_config: dict) -> None:
        """limit-req 插件不存在 header.set 字段，防止配置被拒绝。"""
        routes = apisix_config.get("routes", [])
        for route in routes:
            limit_req = route.get("plugins", {}).get("limit-req")
            if limit_req is not None:
                assert "header" not in limit_req, (
                    f"route {route.get('id')} 的 limit-req 包含无效 header 字段"
                )
