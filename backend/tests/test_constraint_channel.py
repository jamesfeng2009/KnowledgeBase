"""约束注入通道测试 — Phase 1（GAP-1 主体）。

覆盖：
    Router      T2 实体触发 / T4 高风险域默认注入 / OR 合并
    Channel     生效窗 / 权限链 fail-closed / observe 灰度 / enforce 注入 / 审计
    注入层      build_constraint_items 分槽（block mandatory / warn 预算截断）
    Generator   红线段渲染 / 约束先行配额挤占语义预算
    Engine      _safe_fetch_constraints 降级 / 总开关短路

mock 策略：async_session_factory 替换为可控 FakeSession 队列（按查询
顺序返回结果），EntityRegistry / 权限过滤器 / 审计均 mock — 不依赖
真实 PG / Neo4j / LLM。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Mock celery（测试环境未安装）— 与 test_retriever_filters.py 同款
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.config import get_settings
from app.rag.constraint_channel import (
    ACTION_EXPIRED,
    ACTION_FILTERED_PERM,
    ACTION_INJECTED,
    ACTION_SKIPPED_OBSERVE,
    ConstraintChannel,
    ConstraintRouter,
    DomainClassifier,
)
from app.rag.context_item import ContextItemBuilder
from app.rag.generator import Generator

KB_ID = uuid4()
DOC_ID = uuid4()


# ======================================================================
# Fake DB 基础设施
# ======================================================================


class _FakeScalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _FakeResult:
    def __init__(self, scalars: list | None = None, all_rows: list | None = None):
        self._scalars = scalars or []
        self._all = all_rows or []

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._scalars)

    def all(self) -> list:
        return self._all


class _FakeSession:
    """按队列顺序返回查询结果；支持审计写入（add_all/commit no-op）。"""

    def __init__(self, queue: list[_FakeResult]) -> None:
        self._queue = queue
        self.added: list = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, stmt: Any) -> _FakeResult:
        return self._queue.pop(0) if self._queue else _FakeResult()

    def add_all(self, records: list) -> None:
        self.added.extend(records)

    async def commit(self) -> None:
        pass


class _FakeSessionFactory:
    """替换 constraint_channel.async_session_factory — 队列跨 session 共享。"""

    def __init__(self, queue: list[_FakeResult]) -> None:
        self.queue = queue

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.queue)


def _rule(
    *,
    severity: str = "block",
    text: str = "单笔金额超过 5000 元的报销必须双人签批",
    entities: tuple[str, ...] = ("报销",),
    domains: tuple[str, ...] = (),
    intents: tuple[str, ...] = (),
    actions: tuple[str, ...] = ("inject",),
    kb_id: Any = KB_ID,
    effective_from: Any = None,
    effective_to: Any = None,
    rule_id: Any = None,
    doc_id: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=rule_id or uuid4(),
        kb_id=kb_id,
        document_id=doc_id or uuid4(),
        rule_text=text,
        normalized={"statement": text},
        severity=severity,
        actions=list(actions),
        trigger_entities=list(entities),
        trigger_domains=list(domains),
        trigger_intents=list(intents),
        effective_from=effective_from,
        effective_to=effective_to,
        classifier_confidence=0.9,
    )


@pytest.fixture
def settings_on(monkeypatch):
    """CONSTRAINT_ENABLED=True + enforce 模式（实例属性 monkeypatch）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "CONSTRAINT_ENABLED", True)
    monkeypatch.setattr(settings, "CONSTRAINT_INJECT_MODE", "enforce")
    return settings


@pytest.fixture
def settings_observe(monkeypatch):
    """observe 灰度模式 — 只审计不注入。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "CONSTRAINT_ENABLED", True)
    monkeypatch.setattr(settings, "CONSTRAINT_INJECT_MODE", "observe")
    return settings


@pytest.fixture
def audit_mock():
    with patch.object(ConstraintChannel, "_audit", new=AsyncMock()) as m:
        yield m


async def _allow_all(candidates: list[dict]) -> list[dict]:
    """放行全部 — 通道按 await perm_filter(...) 调用，须为协程函数。"""
    return candidates


# ======================================================================
# Router — T2 / T4 / OR 合并
# ======================================================================


class TestRouter:
    @pytest.mark.asyncio
    async def test_t4_filters_non_high_risk(self) -> None:
        kb_finance, kb_eng = uuid4(), uuid4()
        session = _FakeSession(
            [_FakeResult(all_rows=[(kb_finance, "finance"), (kb_eng, "engineering")])]
        )
        router = ConstraintRouter()
        hits = await router.high_risk_kb_ids([kb_finance, kb_eng], session)
        assert hits == [kb_finance]

    @pytest.mark.asyncio
    async def test_t2_gin_match_returns_rules(self) -> None:
        """T2 — trigger_entities overlap 匹配（GIN 查询结果透传）。"""
        matched = [_rule(entities=("报销",)), _rule(entities=("采购审批",))]
        session = _FakeSession([_FakeResult(scalars=matched)])
        router = ConstraintRouter()
        rules = await router.match_by_entities(
            entity_names=["报销"], kb_ids=[KB_ID], session=session
        )
        assert rules == matched

    @pytest.mark.asyncio
    async def test_t2_empty_entities_no_query(self) -> None:
        """实体抽不到 → T2 静默失效（T4 兜底路径不受影响）。"""
        session = _FakeSession([])  # 不应有任何查询
        router = ConstraintRouter()
        rules = await router.match_by_entities(
            entity_names=[], kb_ids=[KB_ID], session=session
        )
        assert rules == []
        assert session._queue == []


# ======================================================================
# Channel.fetch — 全链路行为
# ======================================================================


def _channel(cache: Any = None) -> ConstraintChannel:
    return ConstraintChannel(cache=cache)


def _fetch_kwargs(perm_filter: Any = _allow_all) -> dict[str, Any]:
    return dict(
        query="报销单怎么填",
        kb_ids=[str(KB_ID)],
        tenant_id=None,
        session_id="sess-1",
        user_id=str(uuid4()),
        perm_filter=perm_filter,
    )


class TestChannelFetch:
    @pytest.mark.asyncio
    async def test_disabled_short_circuit(self, monkeypatch) -> None:
        """总开关关闭 → 零查询零注入（一键回滚）。"""
        settings = get_settings()
        monkeypatch.setattr(settings, "CONSTRAINT_ENABLED", False)
        out = await _channel().fetch(**_fetch_kwargs())
        assert out == []

    @pytest.mark.asyncio
    async def test_empty_kb_short_circuit(self) -> None:
        out = await _channel().fetch(
            query="q", kb_ids=[], perm_filter=_allow_all
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_enforce_injects_with_triggers(
        self, settings_on, audit_mock
    ) -> None:
        """enforce — T2 命中注入，条目携带触发器证据。"""
        rule = _rule(severity="block")
        # 队列：1) T2 GIN 匹配 2) T4 kb.category 查询 3) T1 词汇表（空→T1 跳过）
        queue = [
            _FakeResult(scalars=[rule]),
            _FakeResult(all_rows=[]),  # 无高风险域 KB
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["source"] == "constraint"
        assert out[0]["rule_text"] == rule.rule_text
        assert out[0]["triggers"] == ["T2:entity"]
        # enforce 模式审计 action=injected
        assert audit_mock.await_count == 1
        assert audit_mock.call_args.args[0] == ACTION_INJECTED

    @pytest.mark.asyncio
    async def test_t4_or_merge(
        self, settings_on, audit_mock
    ) -> None:
        """T4 高风险域 — KB 全部 active 规则无条件进候选（OR 合并）。"""
        t4_rule = _rule(entities=("无关实体",))  # T2 不命中
        # 队列：1) T4 kb.category 判定（T2 实体为空直接短路，不发查询）
        #       2) T1 词汇表（空→T1 跳过） 3) T4 全量规则（cache=None 直查 PG）
        queue = [
            _FakeResult(all_rows=[(KB_ID, "finance")]),  # T4 判定命中
            _FakeResult(scalars=[]),  # T1 词汇表为空
            _FakeResult(scalars=[t4_rule]),  # T4 全量规则
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["triggers"] == ["T4:kb_domain"]

    @pytest.mark.asyncio
    async def test_effective_window_expired(
        self, settings_on, audit_mock
    ) -> None:
        """生效窗外 → 剔除 + expired 审计。"""
        from datetime import date, timedelta

        expired = _rule(effective_to=date.today() - timedelta(days=1))
        queue = [
            _FakeResult(scalars=[expired]),
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert out == []
        assert audit_mock.call_args.args[0] == ACTION_EXPIRED

    @pytest.mark.asyncio
    async def test_perm_filter_fail_closed_no_filter(
        self, settings_on, audit_mock
    ) -> None:
        """perm_filter=None → fail-closed 不注入（密级未知不放行）。"""
        rule = _rule()
        queue = [
            _FakeResult(scalars=[rule]),
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**{**_fetch_kwargs(), "perm_filter": None})

        assert out == []
        assert audit_mock.call_args.args[0] == ACTION_FILTERED_PERM

    @pytest.mark.asyncio
    async def test_perm_filter_blocks_rule(
        self, settings_on, audit_mock
    ) -> None:
        """权限链剔除 — 规则文档密级超限 / 非 published（Final Gate 复检）。"""
        allowed_rule, blocked_rule = _rule(), _rule()

        async def perm_filter(candidates: list[dict]) -> list[dict]:
            return [c for c in candidates if c["doc_id"] == str(allowed_rule.document_id)]

        queue = [
            _FakeResult(scalars=[allowed_rule, blocked_rule]),
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**{**_fetch_kwargs(), "perm_filter": perm_filter})

        assert len(out) == 1
        assert out[0]["rule_id"] == str(allowed_rule.id)
        # 两次审计：filtered_perm（被剔除）+ injected（放行）
        actions = [c.args[0] for c in audit_mock.call_args_list]
        assert ACTION_FILTERED_PERM in actions
        assert ACTION_INJECTED in actions

    @pytest.mark.asyncio
    async def test_observe_mode_audit_only(
        self, settings_observe, audit_mock
    ) -> None:
        """observe 灰度 — 路由照常、审计 skipped_observe、不注入。"""
        rule = _rule()
        queue = [
            _FakeResult(scalars=[rule]),
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert out == []
        assert audit_mock.call_args.args[0] == ACTION_SKIPPED_OBSERVE

    @pytest.mark.asyncio
    async def test_block_first_ordering(
        self, settings_on, audit_mock
    ) -> None:
        """输出排序 — block 先行（供预算分槽）。"""
        warn_rule = _rule(severity="warn")
        block_rule = _rule(severity="block")
        async def perm_filter(c: list[dict]) -> list[dict]:
            return c

        queue = [
            _FakeResult(scalars=[warn_rule, block_rule]),
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = _channel()
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**{**_fetch_kwargs(), "perm_filter": perm_filter})

        assert [c["severity"] for c in out] == ["block", "warn"]

    @pytest.mark.asyncio
    async def test_channel_error_returns_empty(self, settings_on) -> None:
        """通道内部异常 → 降级空列表（引擎侧另有 _safe 包装双保险）。"""
        channel = _channel()
        with patch.object(
            ConstraintRouter, "extract_entities", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError):
                # channel 自身不吞异常（由 engine._safe_fetch_constraints 降级）
                await channel.fetch(**_fetch_kwargs())


# ======================================================================
# 注入层 — build_constraint_items 分槽
# ======================================================================


class TestBuildConstraintItems:
    def test_block_mandatory_warn_not(self) -> None:
        items = ContextItemBuilder.build_constraint_items(
            [
                {"rule_text": "红线条款", "severity": "block"},
                {"rule_text": "提醒条款", "severity": "warn"},
            ]
        )
        by_sev = {i.meta["severity"]: i for i in items}
        assert by_sev["block"].mandatory is True
        assert by_sev["warn"].mandatory is False
        assert all(i.kind == "constraint" for i in items)
        assert all(i.priority == 120 for i in items)

    def test_soft_budget_truncation(self) -> None:
        """confirm/warn 超 CONSTRAINT_BUDGET_MAX_TOKENS 截断。"""
        # estimate_tokens = ceil(字符数/3.5)：700 字符 → 200 tokens/条
        long_text = "约" * 700
        items = ContextItemBuilder.build_constraint_items(
            [
                {"rule_text": long_text, "severity": "warn"},
                {"rule_text": long_text, "severity": "confirm"},
                {"rule_text": long_text, "severity": "warn"},
            ],
            budget_max_tokens=500,  # 容 2 条（400），第 3 条 600 > 500 截断
        )
        assert len(items) == 2

    def test_block_bypasses_budget(self) -> None:
        """block 不受预算约束 — 超量也全量保留。"""
        long_text = "红线" * 500
        items = ContextItemBuilder.build_constraint_items(
            [{"rule_text": long_text, "severity": "block"}] * 5,
            budget_max_tokens=10,
        )
        assert len(items) == 5

    def test_empty_and_blank(self) -> None:
        assert ContextItemBuilder.build_constraint_items(None) == []
        assert ContextItemBuilder.build_constraint_items(
            [{"rule_text": "", "severity": "block"}]
        ) == []


# ======================================================================
# Generator — 红线段渲染 + 约束先行配额
# ======================================================================


class _FakeLLM:
    async def chat(self, messages, stream=True, max_tokens=None):  # pragma: no cover
        yield ""

    async def chat_async(self, messages):  # pragma: no cover
        return ""


def _generator(budget: int = 2500) -> Generator:
    return Generator(llm=_FakeLLM(), context_budget=budget)


class TestGeneratorRedline:
    def test_redline_section_rendered_before_kb(self) -> None:
        """红线段位于知识库来源之前 + severity 标签正确。"""
        gen = _generator()
        prompt = gen._build_system_prompt(
            retrieved_docs=[{"content": "文档内容", "title": "t", "score": 0.9}],
            tool_results=[],
            memory_context="",
            constraint_context=[
                {"rule_text": "红线A", "severity": "block"},
                {"rule_text": "提醒B", "severity": "warn"},
            ],
        )
        assert "=== 强制约束（红线，必须遵守）===" in prompt
        assert "【红线·必须遵守】 红线A" in prompt
        assert "【提醒】 提醒B" in prompt
        assert prompt.index("=== 强制约束") < prompt.index("=== 知识库来源 ===")

    def test_no_constraint_section_when_empty(self) -> None:
        gen = _generator()
        prompt = gen._build_system_prompt([], [], "")
        assert "强制约束" not in prompt

    def test_constraint_preempts_document_budget(self) -> None:
        """约束先行配额 — 约束挤占语义预算，低相关文档被挤掉。"""
        gen = _generator(budget=300)
        # 无约束：文档正常注入
        docs = [
            {"content": f"文档{i}" + "x" * 200, "title": f"d{i}", "score": 0.9}
            for i in range(5)
        ]
        prompt_plain = gen._build_system_prompt(docs, [], "")
        # 有约束：剩余预算收缩，部分文档被淘汰，红线仍在
        constraint = [
            {"rule_text": "超长红线条款" * 60, "severity": "block"}  # ~360 tokens
        ]
        prompt_constrained = gen._build_system_prompt(
            docs, [], "", constraint_context=constraint
        )
        assert "【红线·必须遵守】" in prompt_constrained
        # 约束版本的知识库来源条目数 ≤ 无约束版本（预算被红线挤占）
        kb_plain = prompt_plain.count("[1]") + prompt_plain.count("[2]")
        kb_constrained = prompt_constrained.count("[1]") + prompt_constrained.count("[2]")
        assert kb_constrained <= kb_plain


# ======================================================================
# Engine — 安全包装与总开关
# ======================================================================


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_safe_fetch_degrades_on_error(self) -> None:
        """通道异常 → 空列表，不阻塞检索。"""
        from app.rag.engine import AgenticRAGEngine

        engine = AgenticRAGEngine.__new__(AgenticRAGEngine)
        engine._constraint_channel = SimpleNamespace(
            fetch=AsyncMock(side_effect=RuntimeError("db down"))
        )
        state = {"query": "q"}
        out = await engine._safe_fetch_constraints(state, ["kb"])
        assert out == []

    @pytest.mark.asyncio
    async def test_safe_fetch_passthrough(self) -> None:
        from app.rag.engine import AgenticRAGEngine

        expected = [{"source": "constraint", "rule_text": "r", "severity": "block"}]
        engine = AgenticRAGEngine.__new__(AgenticRAGEngine)
        engine._constraint_channel = SimpleNamespace(
            fetch=AsyncMock(return_value=expected)
        )
        engine.permission_filter = None
        state = {"query": "q", "session_id": "s", "user_id": "u"}
        out = await engine._safe_fetch_constraints(state, ["kb"])
        assert out == expected
        # fetch 收到权限过滤器（构造级 None 透传）
        kwargs = engine._constraint_channel.fetch.call_args.kwargs
        assert kwargs["perm_filter"] is None


# ======================================================================
# T1 域分类器 — Phase 3（五重触发唯一用 LLM 的一重）
# ======================================================================


def _llm_returning(payload: str):
    """构造 _generate 返回固定文本的分类器（mock LLM 边界）。"""
    classifier = DomainClassifier()
    return classifier, patch.object(
        classifier, "_generate", new=AsyncMock(return_value=payload)
    )


class TestDomainClassifierParse:
    """_parse — JSON 解析 / 词汇表过滤 / 置信度截断。"""

    def test_valid_json_filtered_to_vocab(self) -> None:
        domains, conf = DomainClassifier._parse(
            '{"domains": ["finance", "幻觉域"], "confidence": 0.85}',
            ["finance", "legal"],
        )
        # 幻觉标签（不在词汇表）过滤
        assert domains == ["finance"]
        assert conf == 0.85

    def test_duplicate_labels_deduped(self) -> None:
        domains, _ = DomainClassifier._parse(
            '{"domains": ["finance", "finance"], "confidence": 0.9}',
            ["finance"],
        )
        assert domains == ["finance"]

    def test_codeblock_wrapped(self) -> None:
        domains, conf = DomainClassifier._parse(
            '```json\n{"domains": ["legal"], "confidence": 0.8}\n```',
            ["finance", "legal"],
        )
        assert domains == ["legal"]
        assert conf == 0.8

    def test_confidence_clamped(self) -> None:
        _, conf = DomainClassifier._parse(
            '{"domains": [], "confidence": 1.7}', []
        )
        assert conf == 1.0

    def test_invalid_json_returns_empty(self) -> None:
        assert DomainClassifier._parse("不是 JSON", ["finance"]) == ([], 0.0)

    def test_non_dict_returns_empty(self) -> None:
        assert DomainClassifier._parse('["finance"]', ["finance"]) == ([], 0.0)

    def test_empty_response_returns_empty(self) -> None:
        assert DomainClassifier._parse("", ["finance"]) == ([], 0.0)


class TestDomainClassifierClassify:
    """classify — 置信度地板 / fail-open / 零成本守卫。"""

    @pytest.mark.asyncio
    async def test_above_floor_returns_domains(self) -> None:
        classifier, gen = _llm_returning(
            '{"domains": ["finance"], "confidence": 0.9}'
        )
        with gen:
            domains, conf = await classifier.classify(
                "报销单怎么填", ["finance", "legal"]
            )
        assert domains == ["finance"]
        assert conf == 0.9

    @pytest.mark.asyncio
    async def test_below_floor_no_conclusion(self, monkeypatch) -> None:
        """conf < FLOOR → 本路不出结论（[]），由 T4 兜底。"""
        settings = get_settings()
        monkeypatch.setattr(
            settings, "CONSTRAINT_DOMAIN_CONFIDENCE_FLOOR", 0.8
        )
        classifier, gen = _llm_returning(
            '{"domains": ["finance"], "confidence": 0.5}'
        )
        with gen:
            domains, _ = await classifier.classify("帮我写个周报", ["finance"])
        assert domains == []

    @pytest.mark.asyncio
    async def test_llm_error_fail_open(self) -> None:
        """LLM 异常 → 静默失效（([], 0.0)），不产生排除决策。"""
        classifier = DomainClassifier()
        with patch.object(
            classifier, "_generate", new=AsyncMock(side_effect=RuntimeError("llm down"))
        ):
            domains, conf = await classifier.classify("报销", ["finance"])
        assert domains == []
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_empty_vocabulary_zero_llm(self) -> None:
        """词汇表为空（无域标签规则）→ 零 LLM 成本直接返回。"""
        classifier = DomainClassifier()
        with patch.object(
            classifier, "_generate", new=AsyncMock()
        ) as gen:
            domains, _ = await classifier.classify("报销", [])
        assert domains == []
        gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_blank_query_zero_llm(self) -> None:
        classifier = DomainClassifier()
        with patch.object(
            classifier, "_generate", new=AsyncMock()
        ) as gen:
            domains, _ = await classifier.classify("   ", ["finance"])
        assert domains == []
        gen.assert_not_called()


class TestRouterT1:
    """Router — match_by_domains / distinct_domains。"""

    @pytest.mark.asyncio
    async def test_match_by_domains_overlap(self) -> None:
        """T1 — trigger_domains overlap 匹配（GIN 查询结果透传）。"""
        matched = [_rule(domains=("finance",)), _rule(domains=("legal",))]
        session = _FakeSession([_FakeResult(scalars=matched)])
        router = ConstraintRouter()
        rules = await router.match_by_domains(
            domains=["finance"], kb_ids=[KB_ID], session=session
        )
        assert rules == matched

    @pytest.mark.asyncio
    async def test_match_by_domains_empty_no_query(self) -> None:
        """T1 无结论（domains=[]）→ 零查询成本。"""
        session = _FakeSession([])  # 不应有任何查询
        router = ConstraintRouter()
        rules = await router.match_by_domains(
            domains=[], kb_ids=[KB_ID], session=session
        )
        assert rules == []

    @pytest.mark.asyncio
    async def test_distinct_domains_returns_vocabulary(self) -> None:
        """词汇表 — 范围内规则实际使用的域标签（去重）。"""
        session = _FakeSession([_FakeResult(scalars=["finance", "legal"])])
        router = ConstraintRouter()
        vocab = await router.distinct_domains([KB_ID], session)
        assert vocab == ["finance", "legal"]


class TestRouteT1Integration:
    """_route 集成 — T1 与 T2/T4 的 OR 合并与兜底。"""

    @pytest.mark.asyncio
    async def test_t1_hit_merges_with_t2(self, settings_on) -> None:
        """T1 域命中（口语化查询实体抽不到）→ 域标签规则进候选。

        设计 §6.1 思考题场景：查询口语化，T2 实体抽不到，
        T1 多标签仍可命中 — 五重互相独立。
        """
        domain_rule = _rule(entities=("无关实体",), domains=("finance",))
        # 队列（T2 实体空短路不发查询）：1) T4 判定（无高风险 KB → 不查全量）
        #       2) T1 词汇表 3) T1 域匹配
        queue = [
            _FakeResult(all_rows=[]),  # 无高风险域 KB
            _FakeResult(scalars=["finance"]),  # T1 词汇表
            _FakeResult(scalars=[domain_rule]),  # T1 域匹配
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ), patch.object(
            DomainClassifier, "classify", new=AsyncMock(return_value=(["finance"], 0.9))
        ) as classify_mock:
            out = await channel.fetch(**_fetch_kwargs())

        classify_mock.assert_awaited_once()
        assert len(out) == 1
        assert out[0]["triggers"] == ["T1:domain"]

    @pytest.mark.asyncio
    async def test_t1_t2_both_hit_or_merge(self, settings_on) -> None:
        """同一规则 T1/T2 双命中 → 触发器集合并集（OR 语义审计证据）。"""
        rule = _rule(entities=("报销",), domains=("finance",))
        queue = [
            _FakeResult(scalars=[rule]),  # T2 命中
            _FakeResult(all_rows=[]),  # 无高风险域 KB
            _FakeResult(scalars=["finance"]),  # T1 词汇表
            _FakeResult(scalars=[rule]),  # T1 域命中（同一条规则）
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ), patch.object(
            DomainClassifier, "classify", new=AsyncMock(return_value=(["finance"], 0.9))
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["triggers"] == ["T1:domain", "T2:entity"]

    @pytest.mark.asyncio
    async def test_t1_misjudged_domain_t4_fallback(self, settings_on) -> None:
        """域判错回归（设计思考题）— T1 判错域，T4 KB 级默认注入兜底。"""
        t4_rule = _rule(entities=("无关实体",), domains=("legal",))
        # T1 误判为 legal（实际是 finance 查询）→ 域匹配不命中 finance 规则；
        # T4 高风险 KB 全量注入兜底
        # 队列（T2 实体空短路）：1) T4 判定 2) T1 词汇表 3) T4 全量 4) T1 域匹配
        queue = [
            _FakeResult(all_rows=[(KB_ID, "finance")]),  # T4 判定命中
            _FakeResult(scalars=["finance", "legal"]),  # T1 词汇表
            _FakeResult(scalars=[t4_rule]),  # T4 全量规则
            _FakeResult(scalars=[]),  # T1 域匹配（legal 误判，finance 规则不命中）
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ), patch.object(
            DomainClassifier,
            "classify",
            new=AsyncMock(return_value=(["legal"], 0.9)),
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["triggers"] == ["T4:kb_domain"]

    @pytest.mark.asyncio
    async def test_t1_llm_failure_t4_unaffected(self, settings_on) -> None:
        """T1 LLM 超时/异常 → fail-open，T4 兜底注入照常。"""
        t4_rule = _rule(entities=("无关实体",), domains=("finance",))
        # 队列（T2 实体空短路）：1) T4 判定 2) T1 词汇表 3) T4 全量
        # （T1 抛异常 → 无域匹配查询）
        queue = [
            _FakeResult(all_rows=[(KB_ID, "finance")]),  # T4 判定命中
            _FakeResult(scalars=["finance"]),  # T1 词汇表
            _FakeResult(scalars=[t4_rule]),  # T4 全量规则
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ), patch.object(
            DomainClassifier,
            "classify",
            new=AsyncMock(side_effect=RuntimeError("llm timeout")),
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["triggers"] == ["T4:kb_domain"]

    @pytest.mark.asyncio
    async def test_t1_below_floor_skips_domain_query(
        self, settings_on, monkeypatch
    ) -> None:
        """conf < FLOOR → 无结论（零域匹配查询），不产生排除决策。"""
        settings = get_settings()
        monkeypatch.setattr(settings, "CONSTRAINT_DOMAIN_CONFIDENCE_FLOOR", 0.8)
        # 队列（T2 实体空短路）：1) T4 判定 2) T1 词汇表
        queue = [
            _FakeResult(all_rows=[]),  # 无高风险域 KB
            _FakeResult(scalars=["finance"]),  # T1 词汇表
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ), patch.object(
            DomainClassifier, "classify", new=AsyncMock(return_value=([], 0.5))
        ) as classify_mock:
            out = await channel.fetch(**_fetch_kwargs())

        classify_mock.assert_awaited_once()
        assert out == []  # 无域匹配查询（队列只消费 3 项）


# ======================================================================
# T3 意图触发 — Phase 3（零 LLM 复用 IntentResult）
# ======================================================================


def _intent_result(
    intent_value: str = "rag_search",
    mandatory_keywords: list[str] | None = None,
) -> Any:
    """构造 IntentResult（T3 输入）。"""
    from app.intent.router import IntentConstraints, IntentResult

    constraints = (
        IntentConstraints(hard={"mandatory_keywords": mandatory_keywords})
        if mandatory_keywords is not None
        else None
    )
    return IntentResult(
        intent=intent_value,
        confidence=0.9,
        constraints=constraints,
        use_shortcut=True,
    )


class TestRouterT3:
    """Router — match_by_intents / t3_tags / _mandatory_keywords。"""

    @pytest.mark.asyncio
    async def test_match_by_intents_none_returns_empty(self) -> None:
        """intent=None（IntentRouter 关闭/失败）→ T3 短路，零查询成本。"""
        session = _FakeSession([])  # 不应有任何查询
        router = ConstraintRouter()
        rules = await router.match_by_intents(
            intent=None, kb_ids=[KB_ID], session=session
        )
        assert rules == []

    @pytest.mark.asyncio
    async def test_match_by_intents_returns_rules(self) -> None:
        """T3 — overlap 查询结果透传（大小写兼容由 SQL 侧保证）。"""
        matched = [_rule(intents=("RAG_SEARCH",))]
        session = _FakeSession([_FakeResult(scalars=matched)])
        router = ConstraintRouter()
        rules = await router.match_by_intents(
            intent=_intent_result("rag_search"), kb_ids=[KB_ID], session=session
        )
        assert rules == matched

    def test_t3_tags_intent_label_case_insensitive(self) -> None:
        """T3:intent — 规则存大写 'RAG_SEARCH'，意图为小写 'rag_search'。"""
        rule = _rule(intents=("RAG_SEARCH",), entities=())
        tags = ConstraintRouter.t3_tags(rule, _intent_result("rag_search"))
        assert tags == {"T3:intent"}

    def test_t3_tags_keyword_label(self) -> None:
        """T3:keyword — mandatory_keywords 命中 rule_text。"""
        rule = _rule(text="公开招标项目一律留档备查", entities=(), intents=())
        intent = _intent_result("rag_search", mandatory_keywords=["招标"])
        tags = ConstraintRouter.t3_tags(rule, intent)
        assert tags == {"T3:keyword"}

    def test_t3_tags_both_paths(self) -> None:
        """双路径命中 → 双标签（审计可区分哪路在兜底）。"""
        rule = _rule(text="公开招标项目一律留档备查", intents=("rag_search",))
        intent = _intent_result("rag_search", mandatory_keywords=["招标"])
        tags = ConstraintRouter.t3_tags(rule, intent)
        assert tags == {"T3:intent", "T3:keyword"}

    def test_t3_tags_no_match_empty(self) -> None:
        rule = _rule(entities=(), intents=(), text="无关条款")
        intent = _intent_result("rag_search", mandatory_keywords=["招标"])
        assert ConstraintRouter.t3_tags(rule, intent) == set()

    def test_mandatory_keywords_missing_constraints(self) -> None:
        """constraints 为 None / hard 无 keywords → 空列表。"""
        from app.rag.constraint_channel import _mandatory_keywords

        assert _mandatory_keywords(None) == []
        assert _mandatory_keywords(_intent_result("rag_search")) == []
        intent = _intent_result(
            "rag_search", mandatory_keywords=["招标", " ", None]
        )
        assert _mandatory_keywords(intent) == ["招标"]


class TestRouteT3Integration:
    """fetch(intent=...) 集成 — T3 与 T2/T4 的 OR 合并。"""

    @pytest.mark.asyncio
    async def test_t3_intent_hit_injects(self, settings_on) -> None:
        """查询口语化 T2 抽不到实体，T3 意图命中（设计思考题场景）。"""
        t3_rule = _rule(entities=("无关实体",), intents=("RAG_SEARCH",))
        # 队列（T2 实体空短路）：1) T3 匹配 2) T4 判定 3) T1 词汇表（空）
        queue = [
            _FakeResult(scalars=[t3_rule]),  # T3 意图命中
            _FakeResult(all_rows=[]),  # 无高风险域 KB
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ):
            out = await channel.fetch(
                **_fetch_kwargs(), intent=_intent_result("rag_search")
            )

        assert len(out) == 1
        assert out[0]["triggers"] == ["T3:intent"]

    @pytest.mark.asyncio
    async def test_t3_keyword_hit_injects(self, settings_on) -> None:
        """mandatory_keywords 命中条款文本 → T3:keyword 注入。"""
        t3_rule = _rule(
            text="公开招标项目一律留档备查", entities=(), intents=()
        )
        queue = [
            _FakeResult(scalars=[t3_rule]),  # T3 关键词命中
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),  # T1 词汇表为空
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=[]
        ):
            out = await channel.fetch(
                **_fetch_kwargs(),
                intent=_intent_result("rag_search", mandatory_keywords=["招标"]),
            )

        assert len(out) == 1
        assert out[0]["triggers"] == ["T3:keyword"]

    @pytest.mark.asyncio
    async def test_t3_t2_or_merge(self, settings_on) -> None:
        """同一规则 T2 实体 + T3 意图双命中 → 触发器集合并集。"""
        rule = _rule(entities=("报销",), intents=("rag_search",))
        # 队列：1) T2 实体命中 2) T3 意图命中 3) T4 判定 4) T1 词汇表
        queue = [
            _FakeResult(scalars=[rule]),  # T2
            _FakeResult(scalars=[rule]),  # T3（同一条规则）
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(
                **_fetch_kwargs(), intent=_intent_result("rag_search")
            )

        assert len(out) == 1
        assert out[0]["triggers"] == ["T2:entity", "T3:intent"]

    @pytest.mark.asyncio
    async def test_intent_none_t3_skipped(self, settings_on) -> None:
        """intent=None → T3 整路跳过（零查询），T2 照常。"""
        rule = _rule(entities=("报销",), intents=("rag_search",))
        # 队列（无 T3 项）：1) T2 2) T4 判定 3) T1 词汇表
        queue = [
            _FakeResult(scalars=[rule]),  # T2 命中
            _FakeResult(all_rows=[]),
            _FakeResult(scalars=[]),
        ]
        channel = ConstraintChannel(cache=None)
        with patch(
            "app.rag.constraint_channel.async_session_factory",
            _FakeSessionFactory(queue),
        ), patch.object(
            ConstraintRouter, "extract_entities", return_value=["报销"]
        ):
            out = await channel.fetch(**_fetch_kwargs())

        assert len(out) == 1
        assert out[0]["triggers"] == ["T2:entity"]

    @pytest.mark.asyncio
    async def test_safe_fetch_passes_intent(self) -> None:
        """engine 透传 — state['intent'] → fetch(intent=)。"""
        from app.rag.engine import AgenticRAGEngine

        expected = [{"source": "constraint", "rule_text": "r", "severity": "block"}]
        engine = AgenticRAGEngine.__new__(AgenticRAGEngine)
        engine._constraint_channel = SimpleNamespace(
            fetch=AsyncMock(return_value=expected)
        )
        engine.permission_filter = None
        intent = _intent_result("rag_search")
        state = {"query": "q", "session_id": "s", "user_id": "u", "intent": intent}
        out = await engine._safe_fetch_constraints(state, ["kb"])
        assert out == expected
        kwargs = engine._constraint_channel.fetch.call_args.kwargs
        assert kwargs["intent"] is intent
