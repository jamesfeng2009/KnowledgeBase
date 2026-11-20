"""B10 验收闸门 — private_finetuned 模式必须注册 embedder。

审计证据：embedder.py 注册表只有 saas / saas_dashscope /
private_overseas / private_domestic 四种模式；DEPLOY_MODE=private_finetuned
时 get_embedder() 直接抛 ValueError，Mem0 检索/写入退化为纯关键词。

决策（用户已批准）：private_finetuned 的"微调"只针对 LLM（vLLM 多 LoRA），
embedding 与其他私有模式一致走 TEI/BGE-M3 —— 注册 TEI 工厂即可，
memory_embedding_dim 的 TEI 兜底随之从"暂按"变为正式契约。

红基线（修复前）：get_embedder() 抛 ValueError "不支持的 DEPLOY_MODE"。
"""

from __future__ import annotations

import pytest

from app.llm import embedder as embedder_mod
from app.llm.embedder import TEIEmbedder, get_embedder, memory_embedding_dim


@pytest.fixture(autouse=True)
def _strip_malformed_no_proxy(monkeypatch):
    """本机 NO_PROXY 含裸 [::1]，httpx trust_env 解析直接抛 ValueError
    （与 52 条基线失败同根因）。闸门剥离该变量，与测量环境解耦。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture
def finetuned_mode(monkeypatch):
    """切到 private_finetuned 并清空 lru_cache，退出时恢复缓存态。"""
    monkeypatch.setattr(embedder_mod.settings, "DEPLOY_MODE", "private_finetuned")
    get_embedder.cache_clear()
    yield
    get_embedder.cache_clear()


class TestB10FinetunedEmbedder:
    async def test_get_embedder_returns_tei(self, finetuned_mode):
        """private_finetuned 必须能拿到 embedder（TEI/BGE-M3），不得抛 ValueError。"""
        emb = get_embedder()
        assert isinstance(emb, TEIEmbedder)
        assert emb.dim == TEIEmbedder.dim == 1024

    async def test_memory_dim_matches_registered_embedder(self, finetuned_mode):
        """列宽函数与注册 embedder 输出维度一致（B2 契约在第五种模式下成立）。"""
        assert memory_embedding_dim(embedder_mod.settings) == get_embedder().dim

    async def test_other_modes_unchanged(self, monkeypatch):
        """正向证人：既有四种模式的注册不被破坏。"""
        expected = {
            "saas": embedder_mod.OpenAIEmbedder,
            "saas_dashscope": embedder_mod.DashScopeEmbedder,
            "private_overseas": TEIEmbedder,
            "private_domestic": TEIEmbedder,
        }
        get_embedder.cache_clear()
        try:
            for mode, cls in expected.items():
                monkeypatch.setattr(embedder_mod.settings, "DEPLOY_MODE", mode)
                get_embedder.cache_clear()
                assert isinstance(get_embedder(), cls), mode
        finally:
            get_embedder.cache_clear()
