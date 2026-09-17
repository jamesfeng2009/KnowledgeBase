"""
i18n 服务 — 单一职责：多语言资源加载、语言协商与翻译查询。

支持语言：zh / en / ja / ko（P2 i18n）。
翻译键格式：点分路径（如 "common.search" → {"common": {"search": "搜索"}}）。

设计要点：
    - 资源文件打包在 app/i18n/locales/{locale}.json，启动时惰性加载并缓存；
    - Accept-Language 解析：按 q 值降序匹配支持的语言（zh → en 兜底）；
    - t(locale, key) 查询缺失时逐级回退：ja → en → 返回原始 key；
    - 不依赖第三方 gettext，JSON 资源便于前端直接消费（API 提供原始资源）。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# 支持的语言（顺序即优先级）
SUPPORTED_LOCALES: tuple[str, ...] = ("zh", "en", "ja", "ko")
DEFAULT_LOCALE: str = "zh"

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# Accept-Language 的 q 值解析：zh-CN,zh;q=0.9,en;q=0.8
_ACCEPT_RE = re.compile(
    r"([a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{1,8})?)\s*(?:;\s*q\s*=\s*([0-9.]+))?"
)


@lru_cache(maxsize=16)
def load_locale(locale: str) -> dict[str, Any]:
    """加载指定语言的翻译资源（不存在时返回空 dict）。"""
    path = _LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_supported_locales() -> list[dict[str, str]]:
    """返回支持的语言列表（含本地名称）。"""
    names = {"zh": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어"}
    return [
        {"locale": loc, "name": names.get(loc, loc)}
        for loc in SUPPORTED_LOCALES
    ]


def parse_accept_language(header: str | None) -> str:
    """解析 Accept-Language 头，返回最匹配的支持语言（兜底 DEFAULT_LOCALE）。"""
    if not header:
        return DEFAULT_LOCALE
    entries: list[tuple[float, str]] = []
    for match in _ACCEPT_RE.finditer(header):
        tag = match.group(1).lower()
        q = float(match.group(2)) if match.group(2) else 1.0
        base = tag.split("-")[0]
        if base in SUPPORTED_LOCALES:
            entries.append((q, base))
    if not entries:
        return DEFAULT_LOCALE
    entries.sort(key=lambda x: x[0], reverse=True)
    return entries[0][1]


def _lookup(data: dict[str, Any], key: str) -> str | None:
    """按点分路径查找翻译。"""
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def t(locale: str, key: str) -> str:
    """翻译查询 — 缺失时回退 en，再缺失返回原始 key。"""
    for cand in (locale, "en"):
        if cand not in SUPPORTED_LOCALES:
            continue
        value = _lookup(load_locale(cand), key)
        if value is not None:
            return value
    return key


def all_translations(locale: str) -> dict[str, Any]:
    """返回完整翻译资源（前端 i18n 加载用）。"""
    return load_locale(locale)
