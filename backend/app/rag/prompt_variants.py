"""
生成层 prompt 变体注册表 — 单一职责：按变体名提供「指引区」内容。

为什么变体只带指引区、不带红线区：
    A/B 实验要能回答「换这段指引是否更好」，就必须保证**唯一变量是指引**。
    红线区（禁止编造等安全约束）由 generate_base.md 提供、结构上不进变体，
    否则实验组可能悄悄放宽安全约束 —— 那是评测指标看不出来、
    线上才会爆的那类回归。红线继承与 app/evolution/editor 的做法一致
    （编辑器永远拿不到红线区）。

文件约定（app/rag/prompts/variants/<name>.md）::

    ## 指引
    你是企业知识库助手……（该变体的指引行）

    ## 红线（可选，写了也一律忽略）

查找规则：
    - 变体文件缺失 / 指引区为空 → 返回 None，调用方回退线上默认指引
      （实验配置写错名字不会把线上 prompt 变成空串）；
    - 变体名做白名单式清洗（仅字母数字下划线连字符），防目录穿越。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.prompt_files import load_prompt_file, split_guidance_sections
from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = ["variant_guidance", "variant_exists"]

#: 变体文件目录（与 generate_base.md 同级）
_VARIANTS_DIR = Path(__file__).resolve().parent / "prompts" / "variants"

#: 合法变体名 — 防止实验配置里的 "../secrets" 变成文件路径
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _sanitize(name: str) -> str | None:
    """变体名清洗 — 非法返回 None。"""
    cleaned = (name or "").strip()
    if not cleaned or not _SAFE_NAME_RE.match(cleaned):
        return None
    return cleaned


def variant_guidance(name: str) -> list[str] | None:
    """读取变体的指引区行列表；不存在或无指引内容时返回 None。"""
    safe = _sanitize(name)
    if safe is None:
        log.warning("prompt_variant.name_rejected", variant=name)
        return None
    text = load_prompt_file(_VARIANTS_DIR, f"{safe}.md", "")
    if not text:
        log.warning("prompt_variant.missing_file", variant=safe)
        return None
    guidance, _redline = split_guidance_sections(text)
    if not guidance:
        log.warning("prompt_variant.empty_guidance", variant=safe)
        return None
    # 红线区刻意丢弃：见模块 docstring
    return guidance


def variant_exists(name: str) -> bool:
    """变体是否可用（供配置校验与实验注册期自检）。"""
    return variant_guidance(name) is not None
