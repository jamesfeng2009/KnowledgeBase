"""D7 解析成本开关租户级覆盖 — 全局 env 降为缺省值。

复用 modules 的租户配置模式：覆盖项存 Tenant.settings JSONB 的
``parse_config`` 键（TenantService.get_parse_config / update_parse_config
读写），管线入口按 doc.tenant_id 加载后以 contextvar 下发，解析器
消费点改读 ``get_parse_settings()`` — 有覆盖取覆盖，无覆盖回落全局
Settings，未配置租户行为与全局默认完全一致。

白名单刻意收窄到「解析成本开关」四组（VLM 描述 / OCR / MinerU /
表格模式）；DOCLING_ENABLED 等路由开关与 MinIO 上传开关不在其列。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.config import get_settings

# 可覆盖的布尔开关（VLM 描述组 / OCR 组 / MinerU 组）
_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "PDF_IMAGE_EXTRACTION_ENABLED",
        "DOCX_IMAGE_EXTRACTION_ENABLED",
        "PPTX_IMAGE_EXTRACTION_ENABLED",
        "DOCLING_VLM_IMAGE_ENHANCE",
        "PDF_SCAN_OCR_ENABLED",
        "DOCLING_DO_OCR",
        "MINERU_ENABLED",
    }
)
# 表格模式组 — 仅接受 FAST / ACCURATE（大小写不敏感，规范化为大写）
_TABLE_MODE_KEY = "DOCLING_TABLE_MODE"
_TABLE_MODES = frozenset({"FAST", "ACCURATE"})

OVERRIDABLE_PARSE_KEYS: frozenset[str] = _BOOL_KEYS | {_TABLE_MODE_KEY}

_parse_overrides: ContextVar[dict] = ContextVar("parse_overrides", default={})


def validate_parse_config(raw: Any) -> dict:
    """白名单＋类型校验 — 管理端点写入边界。

    Raises:
        ValueError: 非 dict、白名单外键、类型/取值非法。
    """
    if not isinstance(raw, dict):
        raise ValueError("parse_config 必须是对象")
    out: dict = {}
    for key, value in raw.items():
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值")
            out[key] = value
        elif key == _TABLE_MODE_KEY:
            mode = str(value).upper()
            if mode not in _TABLE_MODES:
                raise ValueError(f"{_TABLE_MODE_KEY} 仅接受 FAST/ACCURATE")
            out[key] = mode
        else:
            raise ValueError(f"不支持的解析配置键: {key}")
    return out


@contextmanager
def parse_overrides(overrides: dict | None):
    """在上下文内应用租户覆盖 — 退出必还原，杜绝跨租户泄漏。"""
    token = _parse_overrides.set(overrides or {})
    try:
        yield
    finally:
        _parse_overrides.reset(token)


class _SettingsOverlay:
    """覆盖优先、其余回落全局 Settings 的只读代理。"""

    def __init__(self, base: Any, overrides: dict) -> None:
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def get_parse_settings() -> Any:
    """解析器消费点统一入口 — 无覆盖时返回全局 Settings 本体。"""
    overrides = _parse_overrides.get()
    base = get_settings()
    if not overrides:
        return base
    return _SettingsOverlay(base, overrides)
