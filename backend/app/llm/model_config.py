"""P2 模型配置加载器 — 单一职责：从 models.json 加载模型定义。

遵循单一职责：本模块只负责读取和缓存 models.json 配置，不创建 Provider 实例。
遵循开闭原则：新增模型只需在 models.json 中添加条目，无需修改代码。

模型配置结构（models.json）::
    {
      "models": [
        {
          "id": "claude-sonnet-4.6",           # 唯一标识（前端引用）
          "display_name": "Claude Sonnet 4.6",  # 前端显示名
          "provider_type": "anthropic",          # Provider 类型
          "deploy_mode": "saas",                 # 部署模式
          "model_id": "claude-sonnet-4-6-...",   # 实际模型 ID
          "max_tokens": 200000,                  # 上下文窗口
          "max_output_tokens": 8192,             # 最大输出
          "description": "...",                  # 描述
          "tier": "premium",                     # 层级（premium/standard/lite）
          "enabled": true,                       # 是否启用
          "is_default": true,                    # 是否为该 deploy_mode 的默认模型
          "supports_streaming": true,            # 是否支持流式
          "supports_tool_use": true,             # 是否支持工具调用
          "supports_vision": true                # 是否支持视觉
        }
      ]
    }

P2-1: 创建 models.json 配置文件（Git 管理）
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# models.json 文件路径 — 相对于 backend/ 根目录
_MODELS_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "models.json"


class ModelConfig(TypedDict, total=False):
    """单个模型的配置定义。"""

    id: str
    display_name: str
    provider_type: str          # anthropic / dashscope / vllm
    deploy_mode: str            # saas / saas_dashscope / private_overseas / private_domestic
    model_id: str               # Provider 实际使用的模型 ID
    max_tokens: int             # 上下文窗口
    max_output_tokens: int      # 最大输出 token
    description: str
    tier: str                   # premium / standard / lite
    enabled: bool
    is_default: bool            # 该 deploy_mode 的默认模型
    supports_streaming: bool
    supports_tool_use: bool
    supports_vision: bool


@lru_cache
def _load_models_raw() -> list[ModelConfig]:
    """加载 models.json 原始配置（带缓存）。"""
    try:
        with open(_MODELS_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        models = data.get("models", [])
        log.info("model_config.loaded", count=len(models), path=str(_MODELS_JSON_PATH))
        return models
    except FileNotFoundError:
        log.warning("model_config.file_not_found", path=str(_MODELS_JSON_PATH))
        return []
    except json.JSONDecodeError as exc:
        log.error("model_config.parse_error", error=str(exc))
        return []


def get_available_models() -> list[ModelConfig]:
    """获取当前部署模式下所有启用的模型列表。

    Returns:
        当前 DEPLOY_MODE 下 enabled=True 的模型列表。
    """
    settings = get_settings()
    deploy_mode = settings.DEPLOY_MODE
    all_models = _load_models_raw()
    return [
        m for m in all_models
        if m.get("deploy_mode") == deploy_mode and m.get("enabled", True)
    ]


def get_default_model() -> ModelConfig | None:
    """获取当前部署模式的默认模型。

    优先返回 is_default=True 的模型；
    若无标记，返回第一个可用模型。

    Returns:
        默认模型配置，无可用模型时返回 None。
    """
    models = get_available_models()
    if not models:
        return None
    for m in models:
        if m.get("is_default", False):
            return m
    # 无默认标记 — 返回第一个
    return models[0]


def get_model_by_id(model_id: str) -> ModelConfig | None:
    """按 ID 查找模型配置。

    Args:
        model_id: 模型唯一标识（如 "claude-sonnet-4.6"）。

    Returns:
        模型配置，不存在时返回 None。
    """
    all_models = _load_models_raw()
    for m in all_models:
        if m.get("id") == model_id:
            return m
    return None


def get_model_config_for_user(model_id: str | None) -> ModelConfig | None:
    """获取用户指定模型的有效配置（含权限校验）。

    P2 两级优先级：session > system default。
    本函数处理 session 级选择：如果 model_id 有效且属于当前 deploy_mode，返回该模型；
    否则返回 None（由调用方回退到系统默认）。

    Args:
        model_id: 用户选择的模型 ID，None 表示使用默认。

    Returns:
        匹配的模型配置；model_id 无效或不在当前 deploy_mode 下返回 None。
    """
    if model_id is None:
        return None

    settings = get_settings()
    deploy_mode = settings.DEPLOY_MODE

    model = get_model_by_id(model_id)
    if model is None:
        log.warning("model_config.not_found", model_id=model_id)
        return None

    if model.get("deploy_mode") != deploy_mode:
        log.warning(
            "model_config.deploy_mode_mismatch",
            model_id=model_id,
            model_deploy_mode=model.get("deploy_mode"),
            current_deploy_mode=deploy_mode,
        )
        return None

    if not model.get("enabled", True):
        log.warning("model_config.disabled", model_id=model_id)
        return None

    return model


def reload_models_cache() -> None:
    """清除配置缓存 — 运维修改 models.json 后调用。

    正常运行时 models.json 不变（Git 管理），此函数仅供测试和管理界面使用。
    """
    _load_models_cache_clear()


def _load_models_cache_clear() -> None:
    """清除 lru_cache。"""
    _load_models_raw.cache_clear()
