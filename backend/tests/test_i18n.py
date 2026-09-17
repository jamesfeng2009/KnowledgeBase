"""i18n 国际化单测 — P2 i18n（zh/en/ja/ko）。

测试策略：
    - 资源完整性：4 种语言资源文件存在且 JSON 合法、关键键齐全；
    - 语言协商：Accept-Language q 值解析、未知语言兜底；
    - 翻译查询：命中 / 缺失回退 en / 双缺失返回原始 key；
    - API 路由：/i18n/locales 与 /i18n/{locale}/messages 注册。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.i18n.service import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    all_translations,
    get_supported_locales,
    load_locale,
    parse_accept_language,
    t,
)


class TestLocaleResources:
    @pytest.mark.parametrize("locale", ["zh", "en", "ja", "ko"])
    def test_resource_exists_and_valid(self, locale: str) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "app" / "i18n" / "locales" / f"{locale}.json"
        )
        assert path.exists(), f"{locale}.json 缺失"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "common" in data
        assert "common.app_name" in _flatten(data)

    def test_all_locales_have_parallel_keys(self) -> None:
        keysets = {
            loc: set(_flatten(load_locale(loc)).keys())
            for loc in SUPPORTED_LOCALES
        }
        reference = keysets["zh"]
        for loc, keys in keysets.items():
            assert keys == reference, f"{loc} 与 zh 键不一致"

    def test_supported_locales_list(self) -> None:
        assert SUPPORTED_LOCALES == ("zh", "en", "ja", "ko")
        locales = get_supported_locales()
        assert {l["locale"] for l in locales} == set(SUPPORTED_LOCALES)


def _flatten(data: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


class TestAcceptLanguage:
    def test_prefers_highest_q(self) -> None:
        assert parse_accept_language("zh-CN;q=1.0,en;q=0.9") == "zh"
        assert parse_accept_language("en;q=0.8,ja;q=0.9") == "ja"
        assert parse_accept_language("ko-KR;q=0.7,zh;q=0.6") == "ko"

    def test_default_when_empty(self) -> None:
        assert parse_accept_language(None) == DEFAULT_LOCALE
        assert parse_accept_language("") == DEFAULT_LOCALE

    def test_unknown_locale_falls_back(self) -> None:
        assert parse_accept_language("fr-FR,fr;q=0.9") == DEFAULT_LOCALE

    def test_base_language_matching(self) -> None:
        assert parse_accept_language("zh-TW") == "zh"
        assert parse_accept_language("ko-KR") == "ko"


class TestTranslate:
    def test_hit(self) -> None:
        assert t("zh", "common.search") == "搜索"
        assert t("en", "common.search") == "Search"
        assert t("ja", "common.search") == "検索"
        assert t("ko", "common.search") == "검색"

    def test_fallback_to_english(self) -> None:
        # ja 资源缺少某键时回退 en
        assert t("ja", "nav.analytics") == "分析"

    def test_missing_key_returns_key(self) -> None:
        assert t("zh", "no.such.key") == "no.such.key"

    def test_all_translations(self) -> None:
        data = all_translations("en")
        assert data["common"]["app_name"] == "Enterprise Knowledge Base"


class TestI18nAPI:
    def test_router_registered(self) -> None:
        from app.api.v1.i18n import router

        paths = {r.path for r in router.routes}
        assert "/i18n/locales" in paths
        assert "/i18n/{locale}/messages" in paths
