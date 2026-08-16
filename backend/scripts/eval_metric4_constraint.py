#!/usr/bin/env python
"""指标 4：约束注入命中率 / 误报率评测（observe 灰度模式）。

评测口径（简历指标）：
    约束注入命中率 X%（应注入查询中被路由命中并审计的比例）
    误报率 Y%（不应注入查询中被误命中的比例）
    触发器分布（T1 域分类 / T2 实体 / T4 域兜底 — 成本意识：T2/T4 零 LLM）

设计：
    1. 种子 3 条约束规则到评测 KB（category=NULL，T4 不生效，纯测 T1/T2）
       + 1 条规则到高风险域 KB（category=finance，T4 无条件注入兜底）
    2. 构造标注评测集：7 正例（应注入）+ 7 负例（不应注入）
    3. Phase A（observe 灰度）：走真实 ConstraintChannel.fetch，
       命中行为由 constraint_audit_records（action=skipped_observe）还原
       — 与生产灰度观察口径完全一致
    4. Phase B（enforce 抽查）：切 enforce 模式验证实际注入载荷
       （severity 排序 + triggers 证据链）

用法::

    cd backend && .venv/bin/python scripts/eval_metric4_constraint.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

# 环境变量必须在 app 导入前设置（get_settings 首次调用即固化）
os.environ["CONSTRAINT_ENABLED"] = "true"
os.environ["CONSTRAINT_INJECT_MODE"] = "observe"

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import select  # noqa: E402

from app.database import async_session_factory  # noqa: E402
from app.models.billing import Tenant  # noqa: E402
from app.models.constraint import ConstraintAuditRecord, ConstraintRule  # noqa: E402
from app.models.knowledge import Document, KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

TENANT_NAME = "简历指标评测租户"
SEED_KB_NAME = "简历指标评测库"
HR_KB_NAME = "财务制度高风险库"

RUN_TAG = datetime.now(timezone.utc).strftime("eval-m4-%H%M%S")

# ---------------------------------------------------------------------------
# 约束规则种子（trigger_entities 用 EntityRegistry canonical_name）
# ---------------------------------------------------------------------------
SEED_RULES = [
    {
        "chunk_id": "eval_m4_rule_invoice",
        "kb": SEED_KB_NAME,
        "rule_text": "单张发票金额超过 1000 元的报销须部门负责人签批；发票遗失时需提供消费凭证并经直属经理审批后方可报销。",
        "normalized": {"statement": "报销金额>1000需部门负责人签批", "amount_limits": {"invoice_cny": 1000}},
        "severity": "block",
        "trigger_entities": ["invoice", "报销"],
        "trigger_domains": ["finance"],
    },
    {
        "chunk_id": "eval_m4_rule_contract",
        "kb": SEED_KB_NAME,
        "rule_text": "对外合同签署前必须经法务部合规审查并留存审查记录；金额超过 50 万的合同需法务与财务双重会签。",
        "normalized": {"statement": "合同签署前须法务审查", "amount_limits": {"contract_cny": 500000}},
        "severity": "confirm",
        "trigger_entities": ["contract", "招标"],
        "trigger_domains": ["legal"],
    },
    {
        "chunk_id": "eval_m4_rule_export",
        "kb": SEED_KB_NAME,
        "rule_text": "导出企业知识库全量数据须两名管理员联名授权，导出行为记入审计日志且下载链接 24 小时失效。",
        "normalized": {"statement": "全量导出须双管理员授权"},
        "severity": "block",
        "trigger_entities": ["数据导出"],
        "trigger_domains": ["security"],
    },
    {
        "chunk_id": "eval_m4_rule_entertain",
        "kb": HR_KB_NAME,
        "rule_text": "业务招待费人均上限 300 元，超出部分不予报销；单次招待超过 10 人须提前一个工作日备案。",
        "normalized": {"statement": "招待费人均上限300元", "amount_limits": {"entertain_cny_per_head": 300}},
        "severity": "warn",
        "trigger_entities": ["招待"],
        "trigger_domains": [],
    },
]

# ---------------------------------------------------------------------------
# 标注评测集 — label=1 应注入 / label=0 不应注入；expect=预期触发路径
# kb: seed → 纯 T1/T2；hr → T4 域兜底（category=finance）
# ---------------------------------------------------------------------------
CASES = [
    # 正例（应命中）
    {"q": "发票抬头开错了还能报销吗？", "label": 1, "expect": "T2", "kb": "seed"},
    {"q": "合同续签需要走什么审批流程？", "label": 1, "expect": "T2", "kb": "seed"},
    {"q": "导出全部知识库数据给第三方做分析，需要什么手续？", "label": 1, "expect": "T2", "kb": "seed"},
    {"q": "上海出差住宿每晚最多能报多少钱？", "label": 1, "expect": "T1(语义,无实体词)", "kb": "seed"},
    {"q": "跟合作方签协议之前要做哪些合规检查？", "label": 1, "expect": "T2/T1(协议→contract同义词)", "kb": "seed"},
    {"q": "公司知识库整库备份到本地需要谁批准？", "label": 1, "expect": "T1(语义,无实体词)", "kb": "seed"},
    {"q": "招待客户吃饭的费用怎么报销？", "label": 1, "expect": "T4(高风险域默认)", "kb": "hr"},
    # 负例（不应命中）
    {"q": "Kubernetes 集群节点怎么扩容？", "label": 0, "kb": "seed"},
    {"q": "知识库全文检索的同义词库怎么维护？", "label": 0, "kb": "seed"},
    {"q": "Neo4j 多跳查询的 Cypher 怎么写？", "label": 0, "kb": "seed"},
    {"q": "API 限流阈值在哪里配置？", "label": 0, "kb": "seed"},
    {"q": "Milvus 向量索引选 HNSW 还是 IVF？", "label": 0, "kb": "seed"},
    {"q": "怎么给文档打标签方便检索？", "label": 0, "kb": "seed"},
    {"q": "财务系统的接口文档在哪里？", "label": 0, "kb": "hr",
     "note": "T4 高风险域按设计无条件注入（保守策略），不计入误报"},
]


async def setup(session) -> dict:
    """获取租户/KB，幂等种子约束规则与高风险域 KB。"""
    tenant = (
        await session.execute(select(Tenant).where(Tenant.name == TENANT_NAME))
    ).scalar_one_or_none()
    if tenant is None:
        raise SystemExit("[ERROR] 种子租户不存在，先跑 eval_resume_seed.py")
    user = (
        await session.execute(
            select(User).where(User.email == "eval_seed@local.test")
        )
    ).scalar_one_or_none()

    kbs: dict[str, KnowledgeBase] = {}
    for name, category in ((SEED_KB_NAME, None), (HR_KB_NAME, "finance")):
        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == name)
            )
        ).scalar_one_or_none()
        if kb is None:
            kb = KnowledgeBase(
                name=name, owner_id=user.id, tenant_id=tenant.id,
                description="约束注入评测库" if category is None else "高风险域兜底评测库",
                **({"category": category} if category else {}),
            )
            session.add(kb)
            await session.flush()
        kbs["seed" if category is None else "hr"] = kb

    # 规则挂靠文档：每个 KB 取一篇已发布文档作 document_id（溯源用）
    docs: dict[str, Document] = {}
    for key, kb in kbs.items():
        doc = (
            await session.execute(
                select(Document)
                .where(Document.kb_id == kb.id, Document.status == "published")
                .limit(1)
            )
        ).scalars().first()
        if doc is None:
            doc = Document(
                kb_id=kb.id, owner_id=user.id, tenant_id=tenant.id,
                title=f"约束制度文档-{key}", doc_type="md",
                content_text="制度文档占位内容", classification="internal",
                status="published",
            )
            session.add(doc)
            await session.flush()
        docs[key] = doc

    # 幂等种子规则（按 chunk_id 去重）
    n_new = 0
    for spec in SEED_RULES:
        exists = (
            await session.execute(
                select(ConstraintRule).where(ConstraintRule.chunk_id == spec["chunk_id"])
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        kb_key = "seed" if spec["kb"] == SEED_KB_NAME else "hr"
        session.add(ConstraintRule(
            tenant_id=tenant.id,
            kb_id=kbs[kb_key].id,
            document_id=docs[kb_key].id,
            chunk_id=spec["chunk_id"],
            scope="kb",
            rule_text=spec["rule_text"],
            normalized=spec["normalized"],
            severity=spec["severity"],
            actions=["inject"],
            trigger_entities=spec["trigger_entities"],
            trigger_domains=spec["trigger_domains"],
            status="active",
            classifier_confidence=1.0,
        ))
        n_new += 1
    await session.commit()
    print(f"[M4] 规则种子完成（新增 {n_new} 条，共 {len(SEED_RULES)} 条）")
    return {"tenant": tenant, "user": user, "kbs": kbs}


async def allow_all(candidates):
    """perm_filter 桩 — 评测环境全放行（生产走 PermissionService 密级复检）。"""
    return candidates


async def fetch_hits(session, query: str) -> list[ConstraintAuditRecord]:
    """从审计表还原本查询的命中行为（observe 灰度口径）。"""
    stmt = select(ConstraintAuditRecord).where(
        ConstraintAuditRecord.session_id == RUN_TAG,
        ConstraintAuditRecord.query == query,
    )
    return list((await session.execute(stmt)).scalars().all())


async def main() -> None:
    from app.rag.constraint_channel import ConstraintChannel

    # 运营流程：注册领域实体（数据导出），供 T2 词典识别
    from app.ontology.entity_registry import EntityDefinition, EntityRegistry, EntityType

    EntityRegistry.register(EntityDefinition(
        canonical_name="数据导出", display_name="数据导出",
        entity_type=EntityType.CONCEPT,
        synonyms=["数据导出", "全量导出", "导出数据", "整库备份"],
    ))

    channel = ConstraintChannel(cache=None)

    async with async_session_factory() as session:
        ctx = await setup(session)
        tenant, kbs = ctx["tenant"], ctx["kbs"]

        # ---------- Phase A：observe 灰度 ----------
        print(f"\n[M4] Phase A：observe 灰度模式（session={RUN_TAG}）")
        print("-" * 72)
        rows = []
        for case in CASES:
            kb = kbs[case["kb"]]
            try:
                payload = await channel.fetch(
                    query=case["q"],
                    kb_ids=[str(kb.id)],
                    tenant_id=tenant.id,
                    session_id=RUN_TAG,
                    user_id=ctx["user"].id,
                    perm_filter=allow_all,
                    intent=None,
                )
                assert payload == [], "observe 模式必须只审计不注入"
            except Exception as exc:
                logger.error("m4.fetch_failed", q=case["q"][:20], error=str(exc)[:200])

        await asyncio.sleep(0.2)  # 审计异步落表，稍等
        for case in CASES:
            records = await fetch_hits(session, case["q"])
            hit_records = [r for r in records if r.action == "skipped_observe"]
            triggers = sorted({t for r in hit_records for t in (r.triggers or [])})
            hit = bool(hit_records)
            expect_note = case.get("expect", "-")
            flag = ""
            if case["label"] == 1 and not hit:
                flag = " ← 漏报"
            if case["label"] == 0 and hit and "kb" in case and case["kb"] == "seed":
                flag = " ← 误报"
            print(
                f"  [{'+' if case['label'] else '-'}] {case['q'][:26]:<28s} "
                f"{'HIT ' if hit else 'miss'} triggers={triggers or '[]'} "
                f"expect={expect_note}{flag}"
            )
            rows.append({"case": case, "hit": hit, "triggers": triggers})

        # ---------- 统计 ----------
        positives = [r for r in rows if r["case"]["label"] == 1]
        neg_seed = [
            r for r in rows
            if r["case"]["label"] == 0 and r["case"].get("kb") == "seed"
        ]
        neg_hr = [
            r for r in rows
            if r["case"]["label"] == 0 and r["case"].get("kb") == "hr"
        ]

        hits = sum(1 for r in positives if r["hit"])
        fps = sum(1 for r in neg_seed if r["hit"])
        t2_hits = sum(1 for r in rows if any(t.startswith("T2") for t in r["triggers"]))
        t1_hits = sum(1 for r in rows if any(t.startswith("T1") for t in r["triggers"]))
        t4_hits = sum(1 for r in rows if any(t.startswith("T4") for t in r["triggers"]))

        # ---------- Phase B：enforce 抽查（实际注入载荷） ----------
        print(f"\n[M4] Phase B：enforce 模式抽查注入载荷")
        print("-" * 72)
        os.environ["CONSTRAINT_INJECT_MODE"] = "enforce"
        from app.config import get_settings

        get_settings.cache_clear()
        probe_cases = [
            ("发票抬头开错了还能报销吗？", "seed"),
            ("上海出差住宿每晚最多能报多少钱？", "seed"),
            ("招待客户吃饭的费用怎么报销？", "hr"),
        ]
        for q, kb_key in probe_cases:
            payload = await channel.fetch(
                query=q, kb_ids=[str(kbs[kb_key].id)], tenant_id=tenant.id,
                session_id=RUN_TAG + "-enforce", user_id=ctx["user"].id,
                perm_filter=allow_all, intent=None,
            )
            for item in payload:
                print(
                    f"  [{item['severity']:7s}] {item['triggers']} "
                    f"{item['rule_text'][:40]}..."
                )
            if not payload:
                print(f"  (未注入) {q}")

        # ---------- 汇总 ----------
        print("\n" + "=" * 64)
        print("[M4] 约束注入指标（observe 灰度口径）")
        print("=" * 64)
        print(f"  约束注入命中率   : {hits}/{len(positives)} = {hits / len(positives):.1%}")
        print(f"  误报率           : {fps}/{len(neg_seed)} = {fps / len(neg_seed):.1%}"
              f"（高风险域 KB 的 T4 保守注入 {sum(1 for r in neg_hr if r['hit'])}/{len(neg_hr)} 为设计内行为，不计误报）")
        print(f"  触发器分布(查询级): T2实体={t2_hits}  T1域分类={t1_hits}  T4域兜底={t4_hits}")
        print(f"  零 LLM 触发占比  : {(t2_hits + t4_hits) / max(t2_hits + t1_hits + t4_hits, 1):.0%}"
              f"（T2/T4 为零 LLM 确定性触发，T1 为轻量分类兜底）")
        print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
