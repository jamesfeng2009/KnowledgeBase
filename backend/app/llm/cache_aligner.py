"""
Cache Aligner — 检测 system prompt 中的易变内容，防止 KV Cache 前缀失效。

借鉴 Headroom 项目的 CacheAligner 设计，检测 UUID、ISO8601 时间戳、JWT、
十六进制哈希等易变内容。这些内容每次调用都不同，会导致 Anthropic
Prompt Cache 的前缀匹配失败，使缓存失效。

使用方式::

    from app.llm.cache_aligner import check_cache_alignment

    warnings = check_cache_alignment(system_prompt)
    for w in warnings:
        log.warning("cache_aligner.volatile", warning=w)

遵循单一职责：本模块只负责检测和报告，不修改原始内容。
"""

from __future__ import annotations

import re

# 易变内容模式列表 — 每次调用值不同的内容，会破坏 KV Cache 前缀稳定性。
# 格式：(编译后的正则, 人类可读名称)
_VOLATILE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "UUID",
    ),
    (
        re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),
        "ISO8601 timestamp",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ"),
        "JWT token",
    ),
    (
        re.compile(r"\b[0-9a-f]{40,64}\b"),
        "hex hash",
    ),
]


def check_cache_alignment(text: str) -> list[str]:
    """检测文本中的易变内容，返回警告列表。

    如果文本包含 UUID、时间戳、JWT、哈希等每次调用都不同的内容，
    会破坏 Anthropic Prompt Cache 的前缀匹配，导致缓存失效。

    Args:
        text: 待检测的文本（通常是 system prompt）。

    Returns:
        警告字符串列表，空列表表示未检测到易变内容。
    """
    warnings: list[str] = []
    for pattern, name in _VOLATILE_PATTERNS:
        if pattern.search(text):
            warnings.append(
                f"System prompt contains {name}, "
                "which will break KV cache prefix stability"
            )
    return warnings
