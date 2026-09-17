"""i18n 包 — 多语言资源与翻译服务（zh/en/ja/ko）。"""

from app.i18n.service import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    all_translations,
    get_supported_locales,
    load_locale,
    parse_accept_language,
    t,
)

__all__ = [
    "SUPPORTED_LOCALES",
    "DEFAULT_LOCALE",
    "load_locale",
    "get_supported_locales",
    "parse_accept_language",
    "t",
    "all_translations",
]
