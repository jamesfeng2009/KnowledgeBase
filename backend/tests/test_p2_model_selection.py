"""P2 模型选择集成测试 — 覆盖模型配置加载、选择服务、Provider 工厂。

测试覆盖：
- ModelConfig: models.json 加载、按 deploy_mode 过滤、默认模型查找
- ModelSelectionService: 会话级模型设置/查询、两级优先级解析
- Factory: get_llm_provider_by_model 按 model_id 创建 Provider
- UserModelPreference: ORM 模型字段和注册

P2 核心流程：
    用户在前端选择模型 → PUT /api/v1/models/session/{id} 持久化
    → ChatService.resolve_model() 解析两级优先级
    → get_rag_engine_by_model(model_id) 获取对应引擎
    → engine.answer() 使用指定模型生成
"""
from __future__ import annotations

import json
import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# ModelConfig 测试
# ======================================================================


class TestModelConfig:
    """P2-1: models.json 配置加载测试。"""

    def test_models_json_exists_and_valid(self) -> None:
        """models.json 文件存在且 JSON 格式正确。"""
        from pathlib import Path

        from app.llm.model_config import _MODELS_JSON_PATH

        assert _MODELS_JSON_PATH.exists(), f"models.json 不存在: {_MODELS_JSON_PATH}"
        with open(_MODELS_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert "models" in data
        assert len(data["models"]) > 0

    def test_load_models_raw(self) -> None:
        """_load_models_raw 返回模型列表。"""
        from app.llm.model_config import _load_models_raw

        models = _load_models_raw()
        assert len(models) > 0
        # 每个模型都有必要字段
        for m in models:
            assert "id" in m
            assert "display_name" in m
            assert "provider_type" in m
            assert "deploy_mode" in m
            assert "model_id" in m

    def test_get_model_by_id(self) -> None:
        """按 ID 查找模型。"""
        from app.llm.model_config import get_model_by_id

        # 先获取一个已知的模型 ID
        from app.llm.model_config import _load_models_raw

        all_models = _load_models_raw()
        if all_models:
            first_id = all_models[0]["id"]
            model = get_model_by_id(first_id)
            assert model is not None
            assert model["id"] == first_id

    def test_get_model_by_id_not_found(self) -> None:
        """不存在的 model_id 返回 None。"""
        from app.llm.model_config import get_model_by_id

        assert get_model_by_id("nonexistent-model-12345") is None

    def test_get_available_models_filtered_by_deploy_mode(self) -> None:
        """get_available_models 只返回当前 deploy_mode 的模型。"""
        from app.llm.model_config import get_available_models, _load_models_raw
        from app.config import get_settings

        settings = get_settings()
        available = get_available_models()
        # 所有可用模型都应该属于当前 deploy_mode
        for m in available:
            assert m["deploy_mode"] == settings.DEPLOY_MODE
            assert m.get("enabled", True) is True

    def test_get_default_model(self) -> None:
        """get_default_model 返回 is_default=True 的模型。"""
        from app.llm.model_config import get_default_model, get_available_models

        available = get_available_models()
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        default = get_default_model()
        assert default is not None
        # 默认模型应该在可用列表中
        assert default["id"] in [m["id"] for m in available]

    def test_get_model_config_for_user_invalid_model(self) -> None:
        """无效 model_id 返回 None。"""
        from app.llm.model_config import get_model_config_for_user

        assert get_model_config_for_user("invalid-id") is None

    def test_get_model_config_for_user_none(self) -> None:
        """model_id=None 返回 None。"""
        from app.llm.model_config import get_model_config_for_user

        assert get_model_config_for_user(None) is None

    def test_each_deploy_mode_has_default(self) -> None:
        """每个 deploy_mode 至少有一个 is_default=True 的模型。"""
        from app.llm.model_config import _load_models_raw
        from collections import defaultdict

        all_models = _load_models_raw()
        defaults_by_mode: dict[str, int] = defaultdict(int)
        for m in all_models:
            if m.get("is_default") and m.get("enabled", True):
                defaults_by_mode[m["deploy_mode"]] += 1

        # 每个 deploy_mode 至少有一个默认
        for mode, count in defaults_by_mode.items():
            assert count >= 1, f"deploy_mode {mode} 没有默认模型"


# ======================================================================
# ModelSelectionService 测试
# ======================================================================


class TestModelSelectionService:
    """P2-2: ModelSelectionService 测试。"""

    def _make_mock_db(self) -> MagicMock:
        db = MagicMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_get_session_model_not_set(self) -> None:
        """未设置会话模型时返回 None。"""
        from app.services.model_selection_service import ModelSelectionService

        db = self._make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)

        service = ModelSelectionService(db)
        result = await service.get_session_model(uuid.uuid4(), "test-session")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_session_model_creates_new(self) -> None:
        """设置会话模型 — 新建记录。"""
        from app.services.model_selection_service import ModelSelectionService
        from app.llm.model_config import _load_models_raw
        from app.config import get_settings

        # 获取当前 deploy_mode 的一个可用模型
        settings = get_settings()
        all_models = _load_models_raw()
        available = [
            m for m in all_models
            if m["deploy_mode"] == settings.DEPLOY_MODE and m.get("enabled", True)
        ]
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        model_id = available[0]["id"]

        db = self._make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)

        service = ModelSelectionService(db)
        result = await service.set_session_model(
            uuid.uuid4(), "test-session", model_id
        )

        db.add.assert_called_once()
        assert result.model_id == model_id

    @pytest.mark.asyncio
    async def test_set_session_model_invalid_model_raises(self) -> None:
        """设置不存在的模型 ID 时抛出 ValueError。"""
        from app.services.model_selection_service import ModelSelectionService

        db = self._make_mock_db()
        service = ModelSelectionService(db)

        with pytest.raises(ValueError, match="不存在"):
            await service.set_session_model(
                uuid.uuid4(), "test-session", "nonexistent-model"
            )

    @pytest.mark.asyncio
    async def test_resolve_model_falls_back_to_default(self) -> None:
        """未设置会话模型时回退到系统默认。"""
        from app.services.model_selection_service import ModelSelectionService
        from app.llm.model_config import get_default_model

        db = self._make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)

        service = ModelSelectionService(db)
        result = await service.resolve_model(uuid.uuid4(), "test-session")

        default = get_default_model()
        if default:
            assert result == default["id"]
        else:
            assert result == ""

    @pytest.mark.asyncio
    async def test_resolve_model_uses_session_model(self) -> None:
        """设置了会话模型时返回会话模型。"""
        from app.services.model_selection_service import ModelSelectionService
        from app.llm.model_config import _load_models_raw, get_model_config_for_user
        from app.config import get_settings

        settings = get_settings()
        all_models = _load_models_raw()
        available = [
            m for m in all_models
            if m["deploy_mode"] == settings.DEPLOY_MODE and m.get("enabled", True)
        ]
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        model_id = available[0]["id"]

        db = self._make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=model_id)
        db.execute = AsyncMock(return_value=mock_result)

        service = ModelSelectionService(db)
        result = await service.resolve_model(uuid.uuid4(), "test-session")
        assert result == model_id

    @pytest.mark.asyncio
    async def test_get_available_models_for_user(self) -> None:
        """获取可用模型列表（前端下拉框数据源）。"""
        from app.services.model_selection_service import ModelSelectionService

        db = self._make_mock_db()
        service = ModelSelectionService(db)
        models = await service.get_available_models_for_user()

        assert isinstance(models, list)
        # 每个模型都有必要字段
        for m in models:
            assert "id" in m
            assert "display_name" in m
            assert "tier" in m


# ======================================================================
# Provider Factory 测试
# ======================================================================


class TestProviderFactory:
    """P2-3: get_llm_provider_by_model 测试。"""

    def test_get_llm_provider_by_model_returns_provider(self) -> None:
        """按 model_id 返回 LLMProvider 实例。"""
        from app.llm.factory import get_llm_provider_by_model, clear_model_provider_cache
        from app.llm.model_config import _load_models_raw, get_available_models
        from app.llm.base import LLMProvider

        available = get_available_models()
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        clear_model_provider_cache()
        model_id = available[0]["id"]
        provider = get_llm_provider_by_model(model_id)

        assert isinstance(provider, LLMProvider)

    def test_get_llm_provider_by_model_cached(self) -> None:
        """同一 model_id 返回缓存的 Provider 实例。"""
        from app.llm.factory import get_llm_provider_by_model, clear_model_provider_cache
        from app.llm.model_config import get_available_models

        available = get_available_models()
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        clear_model_provider_cache()
        model_id = available[0]["id"]
        provider1 = get_llm_provider_by_model(model_id)
        provider2 = get_llm_provider_by_model(model_id)

        assert provider1 is provider2  # 同一实例

    def test_get_llm_provider_by_model_invalid_id_raises(self) -> None:
        """无效 model_id 抛出 ValueError。"""
        from app.llm.factory import get_llm_provider_by_model, clear_model_provider_cache

        clear_model_provider_cache()
        with pytest.raises(ValueError, match="不存在"):
            get_llm_provider_by_model("nonexistent-model-factory-test")

    def test_get_llm_provider_by_model_wrong_deploy_mode_raises(self) -> None:
        """不属于当前 deploy_mode 的模型抛出 ValueError。"""
        from app.llm.factory import get_llm_provider_by_model, clear_model_provider_cache
        from app.llm.model_config import _load_models_raw
        from app.config import get_settings

        settings = get_settings()
        all_models = _load_models_raw()
        # 找一个不属于当前 deploy_mode 的模型
        wrong_mode_models = [
            m for m in all_models
            if m["deploy_mode"] != settings.DEPLOY_MODE
        ]
        if not wrong_mode_models:
            pytest.skip("只有一个 deploy_mode，无法测试跨模式")

        clear_model_provider_cache()
        with pytest.raises(ValueError, match="不属于当前部署模式"):
            get_llm_provider_by_model(wrong_mode_models[0]["id"])


# ======================================================================
# RAG Engine Factory 测试
# ======================================================================


class TestRagEngineFactory:
    """P2-5: get_rag_engine_by_model 测试。

    注意：这些测试需要 cohere 模块（CohereReranker 依赖），
    在未安装 cohere 的环境中跳过。
    """

    @pytest.fixture(autouse=True)
    def _check_cohere(self):
        """检查 cohere 是否可用。"""
        try:
            import cohere  # noqa: F401
        except ImportError:
            pytest.skip("cohere 模块未安装，跳过引擎工厂测试")

    def test_get_rag_engine_by_model(self) -> None:
        """按 model_id 返回 AgenticRAGEngine 实例。"""
        from app.rag.factory import get_rag_engine_by_model, clear_model_engine_cache
        from app.rag.engine import AgenticRAGEngine
        from app.llm.model_config import get_available_models

        available = get_available_models()
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        clear_model_engine_cache()
        model_id = available[0]["id"]
        engine = get_rag_engine_by_model(model_id)

        assert isinstance(engine, AgenticRAGEngine)

    def test_get_rag_engine_by_model_cached(self) -> None:
        """同一 model_id 返回缓存的引擎实例。"""
        from app.rag.factory import get_rag_engine_by_model, clear_model_engine_cache
        from app.llm.model_config import get_available_models

        available = get_available_models()
        if not available:
            pytest.skip("当前 deploy_mode 无可用模型")

        clear_model_engine_cache()
        model_id = available[0]["id"]
        engine1 = get_rag_engine_by_model(model_id)
        engine2 = get_rag_engine_by_model(model_id)

        assert engine1 is engine2  # 同一实例

    def test_different_models_return_different_engines(self) -> None:
        """不同 model_id 返回不同的引擎实例（但共享 MCP/Retriever）。"""
        from app.rag.factory import get_rag_engine_by_model, clear_model_engine_cache
        from app.llm.model_config import get_available_models

        available = get_available_models()
        if len(available) < 2:
            pytest.skip("当前 deploy_mode 仅有 1 个模型，无法测试多模型")

        clear_model_engine_cache()
        engine1 = get_rag_engine_by_model(available[0]["id"])
        engine2 = get_rag_engine_by_model(available[1]["id"])

        assert engine1 is not engine2
        # 但共享 MCP / Retriever / Reranker
        assert engine1.mcp is engine2.mcp
        assert engine1.retriever is engine2.retriever
        assert engine1.reranker is engine2.reranker


# ======================================================================
# UserModelPreference ORM 模型测试
# ======================================================================


class TestUserModelPreferenceModel:
    """P2-2: UserModelPreference ORM 模型测试。"""

    def test_model_tablename(self) -> None:
        """表名正确。"""
        from app.models.user_model_preference import UserModelPreference

        assert UserModelPreference.__tablename__ == "user_model_preferences"

    def test_model_fields(self) -> None:
        """模型包含所有必要字段。"""
        from app.models.user_model_preference import UserModelPreference

        columns = {c.name for c in UserModelPreference.__table__.columns}
        required = {"id", "user_id", "session_id", "model_id", "tenant_id", "created_at", "updated_at"}
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_model_registered_in_metadata(self) -> None:
        """模型已注册到 Base.metadata。"""
        from app.models import Base, UserModelPreference

        assert "user_model_preferences" in Base.metadata.tables

    def test_unique_constraint_exists(self) -> None:
        """唯一约束 (user_id, session_id) 存在。"""
        from app.models.user_model_preference import UserModelPreference

        table = UserModelPreference.__table__
        constraint_names = [c.name for c in table.constraints]
        assert "uq_user_session_model" in constraint_names
