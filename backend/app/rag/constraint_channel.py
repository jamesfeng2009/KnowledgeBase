"""约束注入通道 — 确定性注入，零相似度、零阈值、零归一化。

设计：constraint-recall-design §6。与 HybridRetriever 的职责分离：
    HybridRetriever    按相似度召回文档 chunk（概率域，归一化分数）
    ConstraintChannel  按触发器注入规则条款（确定域，不进 HybridRetriever）

触发器（T1 域分类 + T2/T3/T4 三重零 LLM）：
    T1 域标签    轻量 LLM 多标签域分类（五重中唯一用 LLM 的一重），
                 只缩小范围、无否决权；conf < FLOOR 不出结论由 T4 兜底；
                 词汇表自适应（范围内规则实际使用的 trigger_domains，
                 无域标签规则的 KB 零 LLM 成本）
    T2 实体触发   EntityRegistry.expand_query 识别的实体名 GIN 匹配
                  trigger_entities（复用图谱召回的实体识别，零成本）
    T3 意图触发   IntentRouter 已算好的 IntentResult 直接传入（零 LLM
                  复用）：intent ∈ trigger_intents（大小写兼容）或
                  constraints.hard.mandatory_keywords 命中 rule_text
    T4 域默认注入 kb.category ∈ CONSTRAINT_HIGH_RISK_DOMAINS 时该 KB
                  全部 active 规则无条件进候选（域判错 / T1 失效的兜底）
    （T5 宪法兜底为既有 constitution.py 常驻前缀，不在本通道。）

失效场景兜底（设计 §6.1 思考题）：
    域判错 / 实体抽不到 / LLM 超时 — T4 KB 级默认注入不受影响；
    T1/T2/T3/T4 互相独立，任一命中即进候选（OR 语义）；
    T1 LLM 异常 → 本路静默失效（fail-open，不产生排除决策）；
    intent 为 None（IntentRouter 关闭/失败）→ T3 跳过。

权限链（设计 §6.3）：规则候选转 dict 后复用请求级权限过滤器
（PermissionService.filter_retrieval_candidates 封装）— I1 状态 +
I3 密级 + I4 归属三项复检，fail-closed。pinned 与普通候选走同一条链。

缓存：T4 的 KB 全量 active 规则（pinned 语义）缓存进 TokenCache
（key 含 pinned:{kb_id}，不含 query），写入 doc_ids 反向索引 — 约束
文档更新时 invalidate_by_doc_id 立即失效（cache.py 既有机制）；纯
规则增删（不动文档）最多延迟到 L1 TTL，Phase 3 若需缩短再评估。
T2 GIN 匹配每次直查（命中行数少，索引查询 <5ms，无需缓存）。
T1 域分类每次直查 LLM（查询几乎不重复，缓存收益低于失效成本）。

审计：每次路由命中的规则 × 实际处置落 constraint_audit_records
（injected / skipped_observe / filtered_perm / expired）— 灰度期
observe 模式的对比数据基础；triggers 字段记录每条规则命中的
触发器集合（T1/T2/T4），trigger_hit_distribution 指标的数据源。

总开关：CONSTRAINT_ENABLED=False 时 fetch 直接短路（一键回滚，
通道关闭即回到现状）；CONSTRAINT_INJECT_MODE=observe 时只审计不注入。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import false, func, or_, select

from app.config import get_settings
from app.database import async_session_factory
from app.models.constraint import ConstraintAuditRecord, ConstraintRule
from app.models.knowledge import KnowledgeBase
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

# 审计 action 取值（与迁移 DDL / 模型 docstring 一致）
ACTION_INJECTED = "injected"
ACTION_SKIPPED_OBSERVE = "skipped_observe"
ACTION_FILTERED_PERM = "filtered_perm"
ACTION_EXPIRED = "expired"

# 审计 query 截断长度
_AUDIT_QUERY_MAX = 500

# severity 排序权重（预算分槽用：block > confirm > warn）
_SEVERITY_ORDER = {"block": 0, "confirm": 1, "warn": 2}

# T1 域分类 prompt 的查询截断长度
_DOMAIN_QUERY_MAX = 200


def _mandatory_keywords(intent: Any) -> list[str]:
    """提取 intent.constraints.hard.mandatory_keywords（去空，保序）。"""
    constraints = getattr(intent, "constraints", None)
    if constraints is None:
        return []
    hard = getattr(constraints, "hard", None) or {}
    if not isinstance(hard, dict):
        return []
    raw = hard.get("mandatory_keywords") or []
    if not isinstance(raw, list):
        return []
    return [str(k).strip() for k in raw if k is not None and str(k).strip()]


@dataclass
class RouteResult:
    """路由结果 — 命中规则 × 触发器集合（审计证据）。"""

    rules: list[Any] = field(default_factory=list)
    # rule_id → 命中的触发器集合（{"T2:entity", "T4:kb_domain"}）
    detail: dict[UUID, set[str]] = field(default_factory=dict)


def _to_uuids(kb_ids: list[str] | None) -> list[UUID]:
    """str kb_id → UUID（非法值跳过）。"""
    out: list[UUID] = []
    for kb in kb_ids or []:
        try:
            out.append(UUID(str(kb)))
        except (ValueError, TypeError):
            continue
    return out


class DomainClassifier:
    """T1 域分类器 — 轻量 LLM 多标签域分类（五重触发唯一用 LLM 的一重）。

    设计约束（§6.1 PRINCIPLE 01）：只缩小范围、无否决权 — 输出仅用于
    匹配 trigger_domains（注入更多候选），永不产生排除决策。

    置信度地板：conf < CONSTRAINT_DOMAIN_CONFIDENCE_FLOOR 时返回空
    （本路不出任何结论，T4 KB 级默认注入兜底）。

    词汇表自适应：prompt 的候选域列表来自范围内规则实际使用的
    trigger_domains（由 Router.distinct_domains 提供）— 词汇表为空
    时零 LLM 成本（无域标签规则可匹配，T1 无意义）。
    """

    async def classify(
        self, query: str, vocabulary: list[str]
    ) -> tuple[list[str], float]:
        """查询 → （命中域标签列表, 整体置信度）。

        Returns:
            (domains, confidence) — 置信度低于地板 / LLM 异常 /
            解析失败时返回 ([], 0.0)（fail-open，由 T4 兜底）。
        """
        if not query.strip() or not vocabulary:
            return [], 0.0

        settings = get_settings()
        vocab = sorted(set(vocabulary))
        prompt = (
            "你是企业知识库的查询域分类器。判断用户查询属于以下哪些业务域（可多选）：\n"
            + "、".join(vocab)
            + "\n\n用户查询："
            + query[:_DOMAIN_QUERY_MAX]
            + "\n\n只输出 JSON：{\"domains\": [\"命中的域标签\"], \"confidence\": 0.0到1.0}\n"
            "规则：domains 只能取上述列表中的标签；域不明确时 domains 为空数组；\n"
            "confidence 为整体判断置信度；只输出 JSON，不要解释。"
        )

        started = time.monotonic()
        try:
            response = await self._generate(prompt)
            domains, confidence = self._parse(response, vocab)
        except Exception as exc:
            # fail-open：T1 静默失效，不产生排除决策，T4 兜底
            log.warning(
                "constraint.t1.classify_failed",
                error=str(exc)[:200],
                vocab_size=len(vocab),
            )
            return [], 0.0

        domains = (
            domains
            if confidence >= settings.CONSTRAINT_DOMAIN_CONFIDENCE_FLOOR
            else []
        )
        log.info(
            "constraint.t1.classified",
            domains=domains,
            confidence=round(confidence, 3),
            below_floor=not domains,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return domains, confidence

    def _resolve_llm(self) -> Any:
        """轻量分类模型解析链 — DOMAIN → CONSTRAINT → MEMORY_SIDECAR → 主模型。"""
        from app.llm.factory import get_llm_provider, get_llm_provider_by_model

        settings = get_settings()
        for model_id in (
            settings.CONSTRAINT_DOMAIN_MODEL,
            settings.CONSTRAINT_MODEL,
            settings.MEMORY_SIDECAR_MODEL,
        ):
            if model_id:
                try:
                    return get_llm_provider_by_model(model_id)
                except Exception as exc:
                    log.warning(
                        "constraint.t1.llm_fallback",
                        model_id=model_id,
                        error=str(exc)[:200],
                    )
        return get_llm_provider()

    async def _generate(self, prompt: str) -> str:
        """调用轻量模型（temperature=0 语义，短输出）。"""
        from app.llm.base import Message

        llm = self._resolve_llm()
        result = ""
        async for chunk in llm.chat(
            [Message(role="user", content=prompt)],
            stream=False,
            max_tokens=120,
            temperature=0.0,
        ):
            if isinstance(chunk, str):
                result += chunk
        return result.strip()

    @staticmethod
    def _parse(response: str, vocabulary: list[str]) -> tuple[list[str], float]:
        """解析分类输出 — 域标签过滤到词汇表，置信度截断到 [0,1]。"""
        if not response:
            return [], 0.0
        text = response.strip()
        # 去除可能的 markdown 代码块包裹
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return [], 0.0
        if not isinstance(payload, dict):
            return [], 0.0

        raw_domains = payload.get("domains")
        vocab_set = set(vocabulary)
        domains: list[str] = []
        if isinstance(raw_domains, list):
            for d in raw_domains:
                label = str(d).strip()
                # 标签必须 ∈ 词汇表（防 LLM 幻觉出无规则可匹配的域）
                if label and label in vocab_set and label not in domains:
                    domains.append(label)

        try:
            confidence = min(max(float(payload.get("confidence") or 0.0), 0.0), 1.0)
        except (ValueError, TypeError):
            confidence = 0.0
        return domains, confidence


class ConstraintRouter:
    """五重触发路由（T1 域分类 + T2 实体 + T4 KB 域默认；T3 Phase 3 后续）。

    OR 语义 — 任一触发器命中即进候选。T2/T4 两重零 LLM；T1 是唯一
    用 LLM 的一重，只缩小范围、无否决权（§6.1 PRINCIPLE 01）。
    """

    def extract_entities(self, query: str) -> list[str]:
        """T2 前置 — EntityRegistry 词典识别（零 LLM，图谱召回同源）。

        词典未初始化 / 异常时返回空列表（T2 静默失效，T4 仍兜底）。
        """
        try:
            from app.ontology.entity_registry import EntityRegistry

            _, entity_names = EntityRegistry.expand_query(query)
            return list(entity_names)
        except Exception as exc:
            log.warning("constraint.router.entity_extract_failed", error=str(exc))
            return []

    async def match_by_entities(
        self,
        *,
        entity_names: list[str],
        kb_ids: list[UUID],
        session: Any,
        tenant_id: UUID | None = None,
    ) -> list[Any]:
        """T2 实体触发 — GIN 匹配（trigger_entities && :names）。"""
        if not entity_names:
            return []
        stmt = select(ConstraintRule).where(
            ConstraintRule.status == "active",
            ConstraintRule.kb_id.in_(kb_ids),
            ConstraintRule.trigger_entities.overlap(entity_names),
        )
        stmt = apply_tenant_filter(stmt, ConstraintRule, tenant_id)
        return list((await session.execute(stmt)).scalars())

    async def match_by_intents(
        self,
        *,
        intent: Any,
        kb_ids: list[UUID],
        session: Any,
        tenant_id: UUID | None = None,
    ) -> list[Any]:
        """T3 意图触发 — 零 LLM，两条匹配路径 OR 合并（设计 §6.2）。

        路径一：intent.intent ∈ trigger_intents（数组 overlap，
        大小写兼容存储 — IntentType 值为小写 'rag_search'，
        规则侧可能存 'RAG_SEARCH'）。
        路径二：intent.constraints.hard.mandatory_keywords 命中
        rule_text（用户显式约束词与条款文本相交 — 如"必须包含招标"
        → 招标相关条款注入；关键词取前 3，防 ILIKE 扫描失控）。

        命中路径的区分（T3:intent / T3:keyword 审计标签）由
        t3_tags 在内存中对（小规模）命中集判定，单次查询即可。

        intent 为 None（IntentRouter 关闭 / 失败）时返回空列表
        （fail-open，其余触发器不受影响）。
        """
        if intent is None or not hasattr(intent, "intent"):
            return []

        intent_value = getattr(intent.intent, "value", str(intent.intent))
        intent_values = [intent_value, intent_value.upper()]
        keyword_clauses = []
        for kw in _mandatory_keywords(intent)[:3]:
            # ILIKE 转义 — 关键词含 %/_ 时按字面匹配
            escaped = kw.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            keyword_clauses.append(ConstraintRule.rule_text.ilike(f"%{escaped}%"))

        stmt = select(ConstraintRule).where(
            ConstraintRule.status == "active",
            ConstraintRule.kb_id.in_(kb_ids),
            ConstraintRule.trigger_intents.overlap(intent_values)
            | (or_(*keyword_clauses) if keyword_clauses else false()),
        )
        stmt = apply_tenant_filter(stmt, ConstraintRule, tenant_id)
        return list((await session.execute(stmt)).scalars())

    @staticmethod
    def t3_tags(rule: Any, intent: Any) -> set[str]:
        """判定规则命中的 T3 匹配路径 — 审计标签粒度。

        T3:intent — trigger_intents 含当前意图值（大小写不敏感）
        T3:keyword — rule_text 含任一 mandatory_keywords（大小写不敏感，
        与 SQL ILIKE 口径一致）
        """
        tags: set[str] = set()
        intent_value = getattr(
            getattr(intent, "intent", None), "value", None
        ) or str(getattr(intent, "intent", ""))
        rule_intents = {str(v).upper() for v in (rule.trigger_intents or [])}
        if intent_value.upper() in rule_intents:
            tags.add("T3:intent")
        text = (rule.rule_text or "").lower()
        if any(kw.lower() in text for kw in _mandatory_keywords(intent)):
            tags.add("T3:keyword")
        return tags

    async def match_by_domains(
        self,
        *,
        domains: list[str],
        kb_ids: list[UUID],
        session: Any,
        tenant_id: UUID | None = None,
    ) -> list[Any]:
        """T1 域触发 — GIN 匹配（trigger_domains && :domains）。

        domains 为空时返回空列表（T1 无结论，零查询成本）。
        """
        if not domains:
            return []
        stmt = select(ConstraintRule).where(
            ConstraintRule.status == "active",
            ConstraintRule.kb_id.in_(kb_ids),
            ConstraintRule.trigger_domains.overlap(domains),
        )
        stmt = apply_tenant_filter(stmt, ConstraintRule, tenant_id)
        return list((await session.execute(stmt)).scalars())

    async def distinct_domains(
        self,
        kb_ids: list[UUID],
        session: Any,
        tenant_id: UUID | None = None,
    ) -> list[str]:
        """范围内规则实际使用的域标签（T1 词汇表 + 零成本开关）。

        返回去重域标签列表 — 为空表示无 trigger_domains 规则，
        T1 整路跳过（零 LLM 成本）。
        """
        stmt = (
            select(func.unnest(ConstraintRule.trigger_domains))
            .where(
                ConstraintRule.status == "active",
                ConstraintRule.kb_id.in_(kb_ids),
                ConstraintRule.trigger_domains != [],
            )
            .distinct()
        )
        stmt = apply_tenant_filter(stmt, ConstraintRule, tenant_id)
        return [row for row in (await session.execute(stmt)).scalars() if row]

    async def high_risk_kb_ids(
        self, kb_ids: list[UUID], session: Any
    ) -> list[UUID]:
        """T4 判定 — 范围内 KB 的 category ∈ 高风险域列表。"""
        stmt = select(KnowledgeBase.id, KnowledgeBase.category).where(
            KnowledgeBase.id.in_(kb_ids)
        )
        rows = (await session.execute(stmt)).all()
        return [
            row[0]
            for row in rows
            if row[1] and row[1] in get_settings().CONSTRAINT_HIGH_RISK_DOMAINS
        ]

    async def load_kb_rules(
        self,
        kb_ids: list[UUID],
        session: Any,
        tenant_id: UUID | None = None,
    ) -> list[Any]:
        """按 KB 全量取 active 规则（T4 注入集 / pinned 缓存的 DB 侧）。"""
        stmt = select(ConstraintRule).where(
            ConstraintRule.status == "active",
            ConstraintRule.kb_id.in_(kb_ids),
        )
        stmt = apply_tenant_filter(stmt, ConstraintRule, tenant_id)
        return list((await session.execute(stmt)).scalars())


class ConstraintChannel:
    """确定性注入通道 — 路由 → 生效窗 → 权限链 → 审计 → 注入列表。

    不进 HybridRetriever：契约差异（无相似度域 / 需要 intent），
    且与 retriever.search 并行执行（互不阻塞 — 本通道查 PG 不查向量库）。
    """

    def __init__(self, cache: Any = None) -> None:
        self._cache = cache  # TokenCache 实例（可选，None 时跳过缓存）
        self._router = ConstraintRouter()
        self._classifier = DomainClassifier()

    async def fetch(
        self,
        *,
        query: str,
        kb_ids: list[str] | None,
        tenant_id: str | UUID | None = None,
        session_id: str = "",
        user_id: str | UUID | None = None,
        perm_filter: Any = None,
        intent: Any = None,
    ) -> list[dict[str, Any]]:
        """获取本次查询应注入的约束条款。

        Args:
            query: 用户查询原文（T2 实体识别 / T1 域分类）。
            kb_ids: 检索范围（str；空列表短路）。
            tenant_id / session_id / user_id: 审计上下文。
            perm_filter: 请求级权限过滤器（engine state 的
                permission_filter，PermissionService 封装）— None 时
                fail-closed 不注入（密级未知不放行，§6.3）。
            intent: IntentRouter 已算好的 IntentResult（T3 意图触发，
                零 LLM 复用）— None（IntentRouter 关闭/失败）时 T3 跳过。

        Returns:
            注入条目列表（generator 红线段格式，block 先行）：
            [{source, rule_id, rule_text, normalized, severity, triggers, kb_id}]
        """
        settings = get_settings()
        if not settings.CONSTRAINT_ENABLED or not kb_ids:
            return []

        uuid_kb_ids = _to_uuids(kb_ids)
        if not uuid_kb_ids:
            return []

        tenant_uuid = self._to_uuid(tenant_id)
        user_uuid = self._to_uuid(user_id)
        audit_ctx = dict(
            query=query,
            kb_ids=list(kb_ids),
            tenant_id=tenant_uuid,
            session_id=session_id,
            user_id=user_uuid,
        )

        # 1. 路由（T2/T3 GIN 直查 + T1 域分类 + T4 全量规则，OR 合并）
        route = await self._route(
            query=query,
            kb_ids=uuid_kb_ids,
            tenant_id=tenant_uuid,
            intent=intent,
        )
        if not route.rules:
            return []

        # 2. 生效窗过滤（条款级，覆盖文档级 recency）
        active_rules: list[Any] = []
        expired: list[Any] = []
        today = date.today()
        for rule in route.rules:
            (active_rules if self._in_effective_window(rule, today) else expired).append(rule)
        if expired:
            await self._audit(ACTION_EXPIRED, expired, route, **audit_ctx)
        if not active_rules:
            return []

        # 3. 权限链 — 规则转候选 dict 走请求级过滤器
        #    （I1 状态 + I3 密级 + I4 归属三项复检，与普通候选同一条链）
        if perm_filter is None:
            # fail-closed：无法复检密级 → 不注入（宁可漏注入不可越权）
            log.warning(
                "constraint.channel.no_perm_filter_fail_closed",
                rules=len(active_rules),
            )
            await self._audit(ACTION_FILTERED_PERM, active_rules, route, **audit_ctx)
            return []

        candidates = [
            {
                "doc_id": str(rule.document_id),
                "kb_id": str(rule.kb_id),
                "content": rule.rule_text,
            }
            for rule in active_rules
        ]
        try:
            allowed = await perm_filter(candidates)
        except Exception as exc:
            log.error("constraint.channel.perm_filter_error", error=str(exc))
            allowed = []
        allowed_docs = {c.get("doc_id") for c in allowed}
        perm_allowed = [
            r for r in active_rules if str(r.document_id) in allowed_docs
        ]
        perm_blocked = [
            r for r in active_rules if str(r.document_id) not in allowed_docs
        ]
        if perm_blocked:
            await self._audit(
                ACTION_FILTERED_PERM, perm_blocked, route, **audit_ctx
            )
        if not perm_allowed:
            return []

        # 4. 注入模式 — observe 只审计不注入（灰度对比一周再放开）
        action = (
            ACTION_INJECTED
            if settings.CONSTRAINT_INJECT_MODE == "enforce"
            else ACTION_SKIPPED_OBSERVE
        )
        await self._audit(action, perm_allowed, route, **audit_ctx)
        if action == ACTION_SKIPPED_OBSERVE:
            return []

        # 5. 输出（severity 排序：block 先行，供预算分槽）
        perm_allowed.sort(
            key=lambda r: (
                _SEVERITY_ORDER.get(r.severity, 9),
                -(getattr(r, "classifier_confidence", 0) or 0),
            )
        )
        return [
            {
                "source": "constraint",
                "rule_id": str(rule.id),
                "rule_text": rule.rule_text,
                "normalized": rule.normalized or {},
                "severity": rule.severity,
                "actions": list(rule.actions or ["inject"]),
                "triggers": sorted(route.detail.get(rule.id, set())),
                "kb_id": str(rule.kb_id),
            }
            for rule in perm_allowed
        ]

    # ------------------------------------------------------------------
    # 内部 — 路由（T4 走 pinned 缓存）与审计
    # ------------------------------------------------------------------

    async def _route(
        self,
        *,
        query: str,
        kb_ids: list[UUID],
        tenant_id: UUID | None,
        intent: Any = None,
    ) -> RouteResult:
        """T1 + T2 + T3 + T4 路由 — OR 合并，任一命中即进候选。

        T3 意图触发零 LLM，与 T2 同 session 执行；T1 域分类（轻量
        LLM）与 T4 pinned 缓存加载并行执行，LLM 延迟被 PG 查询隐藏；
        T1 失败/低置信 → 本路无结论（fail-open，T4 兜底），不影响
        T2/T3/T4。intent 为 None（IntentRouter 关闭/失败）时 T3 整路
        跳过。
        """
        result = RouteResult()
        hits: dict[UUID, set[str]] = {}
        rules_by_id: dict[UUID, Any] = {}

        entity_names = self._router.extract_entities(query)
        async with async_session_factory() as session:
            # T2 实体触发（GIN 直查 — 命中行数少，不缓存）
            for rule in await self._router.match_by_entities(
                entity_names=entity_names, kb_ids=kb_ids,
                session=session, tenant_id=tenant_id,
            ):
                rules_by_id[rule.id] = rule
                hits.setdefault(rule.id, set()).add("T2:entity")

            # T3 意图触发（零 LLM — intent 由 IntentRouter 已算好直接传入）
            for rule in await self._router.match_by_intents(
                intent=intent, kb_ids=kb_ids,
                session=session, tenant_id=tenant_id,
            ):
                rules_by_id[rule.id] = rule
                hits.setdefault(rule.id, set()).update(
                    self._router.t3_tags(rule, intent)
                )

            # T4 高风险域判定（kb.category）
            high_risk = await self._router.high_risk_kb_ids(kb_ids, session)

            # T1 词汇表 — 范围内规则实际使用的域标签（空则 T1 整路跳过）
            vocabulary = await self._router.distinct_domains(
                kb_ids, session, tenant_id
            )

        # T1 域分类与 T4 pinned 缓存加载并行（LLM 延迟被隐藏）
        t1_task = (
            asyncio.create_task(self._classifier.classify(query, vocabulary))
            if vocabulary
            else None
        )

        # T4 注入集 — KB 全量 active 规则走 pinned 缓存（doc_ids 反向索引）
        if high_risk:
            t4_rules = await self._load_pinned_rules(high_risk, tenant_id)
            for rule in t4_rules:
                rules_by_id.setdefault(rule.id, rule)
                hits.setdefault(rule.id, set()).add("T4:kb_domain")

        # T1 域触发 — 分类有结论才查（conf ≥ FLOOR；无结论零查询成本）
        if t1_task is not None:
            try:
                domains, _confidence = await t1_task
            except Exception as exc:
                # classify 内部已兜底，此处防御任务级异常 — 只降级 T1，
                # 不丢弃已算好的 T2/T4 结果
                log.warning("constraint.t1.route_failed", error=str(exc)[:200])
                domains = []
            if domains:
                async with async_session_factory() as session:
                    t1_rules = await self._router.match_by_domains(
                        domains=domains,
                        kb_ids=kb_ids,
                        session=session,
                        tenant_id=tenant_id,
                    )
                for rule in t1_rules:
                    rules_by_id.setdefault(rule.id, rule)
                    hits.setdefault(rule.id, set()).add("T1:domain")

        result.rules = list(rules_by_id.values())
        result.detail = hits
        return result

    async def _load_pinned_rules(
        self, kb_ids: list[UUID], tenant_id: UUID | None
    ) -> list[Any]:
        """T4 注入集 — 按 KB 全量缓存（TokenCache + doc_ids 反向索引）。"""
        rules: list[Any] = []
        tenant_str = str(tenant_id) if tenant_id else None
        cached_kbs: set[UUID] = set()

        if self._cache is not None:
            for kb in kb_ids:
                try:
                    payload = await self._cache.get(
                        f"pinned:{kb}", tenant_id=tenant_str
                    )
                except Exception:
                    payload = None
                if payload:
                    try:
                        group = self._deserialize_rules(payload)
                        rules.extend(group)
                        cached_kbs.add(kb)
                    except Exception as exc:
                        log.warning(
                            "constraint.channel.cache_deserialize_failed",
                            error=str(exc),
                        )

        missing = [kb for kb in kb_ids if kb not in cached_kbs]
        if not missing:
            return rules

        # 未命中 KB 直查 PG（部分索引 ix_constraint_rules_lookup）
        async with async_session_factory() as session:
            fresh = await self._router.load_kb_rules(missing, session, tenant_id)
        rules.extend(fresh)

        if self._cache is not None and fresh:
            # 按 KB 分组写缓存；doc_ids 反向索引 — 约束文档更新即失效
            by_kb: dict[UUID, list[Any]] = {}
            for rule in fresh:
                by_kb.setdefault(rule.kb_id, []).append(rule)
            for kb, group in by_kb.items():
                try:
                    await self._cache.set(
                        f"pinned:{kb}",
                        self._serialize_rules(group),
                        tenant_id=tenant_str,
                        doc_ids=[str(r.document_id) for r in group],
                    )
                except Exception as exc:
                    log.warning(
                        "constraint.channel.cache_set_failed", error=str(exc)
                    )
        return rules

    @staticmethod
    def _serialize_rules(rules: list[Any]) -> str:
        """规则集 → JSON（缓存 value）。"""
        rows = [
            {
                "id": str(r.id),
                "kb_id": str(r.kb_id),
                "document_id": str(r.document_id),
                "rule_text": r.rule_text,
                "normalized": r.normalized or {},
                "severity": r.severity,
                "actions": list(r.actions or ["inject"]),
                "trigger_entities": list(r.trigger_entities or []),
                "effective_from": (
                    r.effective_from.isoformat() if r.effective_from else None
                ),
                "effective_to": (
                    r.effective_to.isoformat() if r.effective_to else None
                ),
                "classifier_confidence": getattr(r, "classifier_confidence", 0) or 0,
            }
            for r in rules
        ]
        return json.dumps(rows, ensure_ascii=False)

    @staticmethod
    def _deserialize_rules(payload: str) -> list[Any]:
        """JSON → 规则对象（SimpleNamespace 承载 — 只读消费，不入 session）。"""
        rules: list[Any] = []
        for row in json.loads(payload):
            rules.append(
                SimpleNamespace(
                    id=UUID(row["id"]),
                    kb_id=UUID(row["kb_id"]),
                    document_id=UUID(row["document_id"]),
                    rule_text=row["rule_text"],
                    normalized=row["normalized"],
                    severity=row["severity"],
                    actions=row.get("actions") or ["inject"],
                    trigger_entities=row["trigger_entities"],
                    effective_from=(
                        date.fromisoformat(row["effective_from"])
                        if row["effective_from"]
                        else None
                    ),
                    effective_to=(
                        date.fromisoformat(row["effective_to"])
                        if row["effective_to"]
                        else None
                    ),
                    classifier_confidence=row.get("classifier_confidence", 0),
                )
            )
        return rules

    @staticmethod
    def _in_effective_window(rule: Any, today: date) -> bool:
        """条款级生效窗 — NULL 表示无界。"""
        if rule.effective_from and today < rule.effective_from:
            return False
        if rule.effective_to and today > rule.effective_to:
            return False
        return True

    async def _audit(
        self,
        action: str,
        rules: list[Any],
        route: RouteResult,
        *,
        query: str,
        kb_ids: list[str],
        tenant_id: UUID | None,
        session_id: str,
        user_id: UUID | None,
    ) -> None:
        """审计落表 — 失败仅记日志，不阻塞检索主链路。"""
        if not rules:
            return
        try:
            records = [
                ConstraintAuditRecord(
                    tenant_id=tenant_id,
                    session_id=session_id or "",
                    user_id=user_id,
                    query=query[:_AUDIT_QUERY_MAX],
                    kb_ids=kb_ids,
                    rule_id=rule.id,
                    action=action,
                    severity=rule.severity or "",
                    triggers=sorted(route.detail.get(rule.id, set())),
                )
                for rule in rules
            ]
            async with async_session_factory() as session:
                session.add_all(records)
                await session.commit()
        except Exception as exc:
            log.warning(
                "constraint.audit_write_failed",
                action=action,
                count=len(rules),
                error=str(exc),
            )

    @staticmethod
    def _to_uuid(value: str | UUID | None) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (ValueError, TypeError):
            return None


_constraint_channel: ConstraintChannel | None = None


def get_constraint_channel() -> ConstraintChannel | None:
    """单例工厂 — 总开关关闭 / 初始化失败返回 None（引擎侧降级）。"""
    global _constraint_channel
    if _constraint_channel is not None:
        return _constraint_channel
    if not get_settings().CONSTRAINT_ENABLED:
        return None
    try:
        from app.rag.cache import TokenCache

        _constraint_channel = ConstraintChannel(cache=TokenCache())
    except Exception as exc:
        log.warning("constraint.channel_init_failed", error=str(exc))
        _constraint_channel = None
    return _constraint_channel
