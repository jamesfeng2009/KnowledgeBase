#!/usr/bin/env python
"""指标 3：知识回流量评测（模拟好评反馈 → FAQ 沉淀 → 审批管线）。

评测口径（简历指标）：
    日均回流 N 条 FAQ，审批通过率 X%（含自动通过率）

模拟策略（本地无生产流量，按用户选择"造模拟数据演示"）：
    1. 构造 7 天 × 2 条/天 = 14 条真实形态的对话
       （单轮干净 QA / 多轮含指代 QA / 噪声闲聊前缀 / 近重复问题 混合）
    2. 每条对话创建 praise 反馈（关联 assistant 消息）
    3. 走真实回流管线：LLM 提取 Q-A → 沉淀 KnowledgeAsset+Document
       → LLM 冲突检测 → 自动审批分流（confidence>=0.9 自动通过）
    4. pending 部分模拟人工审批（>=0.75 通过，<0.75 拒绝，近重复拒绝）
    5. 统计：FAQ 沉淀量 / 日均回流 / 审批通过率 / 自动通过率（省人力）

说明：反馈与对话数据为模拟，回流管线（提取/沉淀/冲突/审批）为真实代码路径。

用法::

    cd backend && .venv/bin/python scripts/eval_metric3_feedback.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session_factory  # noqa: E402
from app.models.billing import Tenant  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.feedback import Feedback  # noqa: E402
from app.models.knowledge import KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

TENANT_NAME = "简历指标评测租户"
USER_EMAIL = "eval_seed@local.test"
FAQ_KB_NAME = "FAQ 回流评测库"
SIM_DAYS = 7

# ---------------------------------------------------------------------------
# 模拟对话数据 — 覆盖企业知识库真实使用场景，三种质量档位：
#   clean: 干净单轮 QA（预期高置信度，可能自动通过）
#   multi: 多轮含指代（测 StandaloneQueryRewriter 独立化改写）
#   noisy: 闲聊前缀/长答案（预期中低置信度，走人工审批）
#   dup:   与前面问题近重复（预期触发冲突检测/人工拒绝）
# ---------------------------------------------------------------------------
SIM_DIALOGS: list[dict] = [
    # --- Day 1 ---
    {
        "day": 1, "quality": "clean", "history": [],
        "q": "企业知识库支持上传哪些文档格式？",
        "a": "支持 Markdown、PDF、Word（docx）、Excel、PPT、纯文本、HTML 共 7 种格式。"
             "其中 PDF 支持扫描件 OCR 识别，Word 和 Markdown 保留标题层级用于语义分块。"
             "单文件大小上限 50MB，批量上传单次最多 20 个文件。",
    },
    {
        "day": 1, "quality": "multi",
        "history": [
            {"role": "user", "content": "我们公司的知识库用的是哪个向量数据库？"},
            {"role": "assistant", "content": "当前生产环境使用 Milvus 作为向量数据库，向量维度 1024，使用 HNSW 索引，支持增量更新。"},
        ],
        "q": "它的相似度检索用的是哪种度量方式？",
        "a": "Milvus 集合使用余弦相似度（COSINE）度量。检索时 top_k 默认取 20，"
             "再经过重排序模型精排到最终 5 条。阈值过滤为 score >= 0.55，低于阈值的结果会被丢弃并触发兜底话术。",
    },
    # --- Day 2 ---
    {
        "day": 2, "quality": "noisy",
        "history": [],
        "q": "嘿嘿在吗，顺便问一下哈，就是那个文档权限是怎么申请的来着，我上次没记住",
        "a": "文档权限申请流程：第一步在知识库文档详情页点击「申请权限」，选择需要的权限级别"
             "（只读/评论/编辑）；第二步系统自动发送审批请求给文档所有者或知识库管理员；"
             "第三步审批通过后权限即时生效，无需重新登录。机密级文档还需要部门负责人二级审批，"
             "平均审批时长 2 小时，超过 48 小时未审批自动过期需重新申请。另外补充一句，"
             "权限变更全部记录在审计日志中，可以在「设置-审计日志」页面查询。",
    },
    {
        "day": 2, "quality": "clean", "history": [],
        "q": "知识库的全文检索支持哪些查询语法？",
        "a": "全文检索基于 OpenSearch，支持：关键词检索、短语检索（英文双引号包裹）、"
             "布尔运算（AND/OR/NOT）、字段限定（title:xxx content:xxx）、通配符（* 和 ?）。"
             "中文使用 IK 分词器，支持同义词扩展，同义词在管理后台的「检索配置-同义词库」中维护。",
    },
    # --- Day 3 ---
    {
        "day": 3, "quality": "clean", "history": [],
        "q": "API 的速率限制是多少？超限后会怎样？",
        "a": "API 速率限制按租户套餐分级：免费版 60 次/分钟，专业版 600 次/分钟，"
             "企业版 6000 次/分钟。超限后返回 HTTP 429，响应头包含 X-RateLimit-Reset 字段"
             "指示重置时间戳。限流基于 Redis 令牌桶算法实现，令牌按固定速率补充，突发流量允许短暂突发到桶容量上限。",
    },
    {
        "day": 3, "quality": "multi",
        "history": [
            {"role": "user", "content": "图谱抽取的三元组存在哪里？"},
            {"role": "assistant", "content": "三元组抽取后存入 Neo4j 图数据库，节点类型包括 Concept、Policy、Document 等 6 类，关系类型 8 种。"},
        ],
        "q": "那它跟向量库是什么关系，检索时会一起用吗？",
        "a": "Neo4j 图谱与 Milvus 向量库是互补关系，检索时四路混合召回：向量语义检索、"
             "BM25 全文检索、跨模态检索、图谱多跳检索。图谱路负责实体关联扩展——当查询命中"
             "注册实体时，沿关系边扩展 0..2 跳找到关联文档，按跳数衰减计分后与其他三路结果融合重排。",
    },
    # --- Day 4 ---
    {
        "day": 4, "quality": "clean", "history": [],
        "q": "如何配置 SSO 单点登录？",
        "a": "SSO 配置步骤：管理后台进入「设置-身份认证-SSO」，选择协议（SAML 2.0 / OIDC），"
             "填写 IdP 元数据 URL 或手动上传证书，配置用户属性映射（邮箱、姓名、部门），"
             "保存后强制验证一次测试登录。SAML 需要 IdP 侧将 ACS 回调地址配置为 "
             "https://your-domain/api/v1/auth/saml/acs。配置生效后可开启「强制 SSO 登录」开关禁用密码登录。",
    },
    {
        "day": 4, "quality": "noisy",
        "history": [],
        "q": "哇这个系统真好用，对了想问下，就是我上传的那个扫描件PDF它识别不出来文字是为什么呀，急急急",
        "a": "扫描件 PDF 识别失败常见原因：一，扫描分辨率过低，建议 300dpi 以上；二，"
             "图片倾斜角度过大，OCR 前会自动纠偏但超过 15 度效果下降；三，手写字体识别率低，"
             "目前 OCR 对印刷体支持最好；四，文件是加密 PDF，需要先解密再上传。"
             "可以在「文档详情-处理日志」里查看具体失败原因。如果反复失败，"
             "建议转成图片格式后用「图片 OCR」入口单独上传试试。",
    },
    # --- Day 5 ---
    {
        "day": 5, "quality": "clean", "history": [],
        "q": "知识库的数据备份策略是怎样的？",
        "a": "备份策略分三层：PostgreSQL 数据库每日凌晨 2 点全量备份，保留 30 天，"
             "每小时增量 WAL 归档；Milvus 向量数据每周日全量导出，保留 8 周；"
             "Neo4j 图数据库每周六 dump 全量，保留 8 周。备份存储在独立的备份对象存储桶，"
             "跨可用区冗余。恢复演练每季度执行一次，RPO 小于 1 小时，RTO 小于 4 小时。",
    },
    {
        "day": 5, "quality": "dup",
        "history": [],
        "q": "请问知识库支持上传什么格式的文件？",
        "a": "系统支持 Markdown、PDF、Word、Excel、PPT、TXT、HTML 等格式的文档上传，"
             "扫描件 PDF 会自动 OCR。单文件上限 50MB。",
    },
    # --- Day 6 ---
    {
        "day": 6, "quality": "clean", "history": [],
        "q": "多租户之间数据是怎么隔离的？",
        "a": "多租户隔离分四层：一，数据库层所有业务表带 tenant_id 列，查询统一经过"
             " apply_tenant_filter 强制过滤；二，向量库按租户分 partition；三，"
             "OpenSearch 索引文档级 security filter；四，Neo4j 图谱节点带 tenant_id 属性，"
             "Cypher 查询动态拼接租户条件。缓存层的 key 也强制包含 tenant_id 前缀，防止跨租户缓存污染。",
    },
    {
        "day": 6, "quality": "multi",
        "history": [
            {"role": "user", "content": "审批工作流有哪几种状态？"},
            {"role": "assistant", "content": "知识回流审批有 pending、approved、rejected、expired 四种状态，pending 超过 7 天自动过期。"},
        ],
        "q": "那质量分达到多少可以不走人工直接过？",
        "a": "自动通过需同时满足三个条件：置信度分数 >= 0.9、冲突检测零冲突、"
             "PII 扫描零命中。任一条件不满足即转入人工审批队列。"
             "其中置信度由 LLM 提取时自评，范围 0-1；冲突检测会与同类型已有资产逐一比对；"
             "PII 检测覆盖手机号、身份证、邮箱、银行卡四类敏感信息。",
    },
    # --- Day 6 补充：真实低质反馈（预期走人工审批或跳过） ---
    {
        "day": 6, "quality": "lowq",
        "history": [],
        "q": "谢谢啦，那我知道了，辛苦辛苦~",
        "a": "不客气！很高兴能帮到你。如果还有其他问题随时来问，"
             "祝工作顺利，使用愉快~",
    },
    {
        "day": 6, "quality": "lowq",
        "history": [],
        "q": "备份要多久做一次比较好啊？",
        "a": "呃，这个我也不太确定诶，印象里好像是一周还是一个月来着，"
             "具体的你可以问问管理员，或者看看帮助文档里有没有写，"
             "我记不太清了，不好意思哈。",
    },
    # --- Day 7 ---
    {
        "day": 7, "quality": "noisy", "history": [],
        "q": "在吗在吗，帮个忙呗，我想把知识库的数据导出来做个备份，在哪里操作啊，找了半天没找到入口",
        "a": "数据导出入口在「管理后台-数据管理-数据导出」，支持两种方式："
             "一，按知识库导出，格式选 Markdown 或 PDF，包含文档全文和附件；"
             "二，全量导出，格式为标准 JSON 归档，包含文档、知识资产、图谱三元组，"
             "仅企业版可用且需要两个管理员联名授权。导出任务异步执行，"
             "完成后下载链接有效期 24 小时。单次导出上限 10GB。",
    },
    {
        "day": 7, "quality": "clean", "history": [],
        "q": "审计日志保留多久？能查到哪些操作？",
        "a": "审计日志默认保留 180 天，企业版可配置最长 3 年。记录的操作类型覆盖："
             "登录登出、文档上传下载、权限变更、检索查询、管理配置修改、API 调用、"
             "数据导出七大类。支持按用户、时间范围、操作类型、IP 地址四个维度筛选，"
             "支持导出 CSV。日志写入采用异步批量落盘，不影响主链路性能。",
    },
]


async def get_or_create_setup(session):
    """获取种子租户/用户，创建 FAQ 专用知识库。"""
    tenant = (
        await session.execute(select(Tenant).where(Tenant.name == TENANT_NAME))
    ).scalar_one_or_none()
    if tenant is None:
        raise SystemExit("[ERROR] 种子租户不存在，先跑 eval_resume_seed.py")

    user = (
        await session.execute(select(User).where(User.email == USER_EMAIL))
    ).scalar_one_or_none()
    if user is None:
        raise SystemExit("[ERROR] 种子用户不存在，先跑 eval_resume_seed.py")

    kb = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.name == FAQ_KB_NAME)
        )
    ).scalar_one_or_none()
    if kb is None:
        kb = KnowledgeBase(
            name=FAQ_KB_NAME,
            owner_id=user.id,
            tenant_id=tenant.id,
            description="好评反馈 FAQ 回流评测库",
        )
        session.add(kb)
        await session.flush()
        logger.info("m3.faq_kb_created", kb_id=str(kb.id))
    return tenant, user, kb


async def simulate_one(session, svc, approval_svc, idx: int, dlg: dict, tenant, user) -> dict:
    """构造一条对话+好评反馈，走完整回流管线，返回处置结果。"""
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=(SIM_DAYS - dlg["day"]), hours=idx)

    conv = Conversation(
        user_id=user.id, title=f"评测对话-{idx}", tenant_id=tenant.id,
    )
    conv.created_at = base
    session.add(conv)
    await session.flush()

    # 历史消息（多轮）
    t = base + timedelta(seconds=30)
    for h in dlg["history"]:
        m = Message(
            conversation_id=conv.id, role=h["role"], content=h["content"],
            tenant_id=tenant.id, created_at=t,
        )
        session.add(m)
        t += timedelta(seconds=30)
        await session.flush()

    # user 提问 → assistant 回答
    user_msg = Message(
        conversation_id=conv.id, role="user", content=dlg["q"],
        tenant_id=tenant.id, created_at=t,
    )
    session.add(user_msg)
    await session.flush()
    asst_msg = Message(
        conversation_id=conv.id, role="assistant", content=dlg["a"],
        model_used="qwen-turbo", tenant_id=tenant.id,
        created_at=t + timedelta(seconds=30),
    )
    session.add(asst_msg)
    await session.flush()

    # praise 反馈（真实触发条件：type=praise + related_message_id）
    fb = Feedback(
        user_id=user.id, type="praise",
        content="回答很准确，解决了我的问题",
        related_message_id=asst_msg.id, tenant_id=tenant.id,
        created_at=t + timedelta(seconds=60),
    )
    session.add(fb)
    await session.flush()

    # 真实回流管线：LLM 提取 → 沉淀 → 冲突检测 → 审批分流
    result = await svc.extract_from_chat_feedback(
        feedback_id=fb.id, target_kb_id=_KB_ID[0],
    )
    return {
        "idx": idx, "day": dlg["day"], "quality": dlg["quality"],
        "question": dlg["q"][:30], "result": result,
    }


_KB_ID: list = []  # 闭包外传递 kb_id


async def main() -> None:
    settings = get_settings()
    run_start = datetime.now(timezone.utc)
    print(f"[M3] LLM provider: {settings.DEPLOY_MODE}")
    print(f"[M3] 模拟 {len(SIM_DIALOGS)} 条好评反馈，覆盖 {SIM_DAYS} 天\n")

    from app.llm.factory import get_llm_provider
    from app.services.knowledge_approval_service import KnowledgeApprovalService
    from app.services.knowledge_compounding import KnowledgeCompoundingService
    from app.models.knowledge_approval import KnowledgeApproval

    llm = get_llm_provider()

    async with async_session_factory() as session:
        tenant, user, kb = await get_or_create_setup(session)
        _KB_ID.clear()
        _KB_ID.append(kb.id)

        svc = KnowledgeCompoundingService(llm, session, tenant_id=tenant.id)
        approval_svc = KnowledgeApprovalService(session, tenant_id=tenant.id)

        # ---------- Phase 1: 模拟回流 ----------
        rows = []
        for idx, dlg in enumerate(SIM_DIALOGS, 1):
            try:
                row = await simulate_one(session, svc, approval_svc, idx, dlg, tenant, user)
            except Exception as exc:
                logger.error("m3.simulate_failed", idx=idx, error=str(exc)[:300])
                row = {"idx": idx, "day": dlg["day"], "quality": dlg["quality"],
                       "question": dlg["q"][:30],
                       "result": {"status": "failed", "error": str(exc)[:120]}}
            rows.append(row)
            r = row["result"]
            print(
                f"  #{idx:02d} D{row['day']} [{row['quality']:5s}] {row['question']}... "
                f"→ {r.get('status', 'unknown')}"
                + (f" conf={r.get('confidence', '')}" if r.get("confidence") is not None else "")
                + (f" conflicts={r['conflicts']}" if r.get("conflicts") else "")
            )
        await session.commit()

        # ---------- Phase 2: 人工审批模拟 ----------
        # 仅统计本轮产生的审批记录（created_at >= 脚本启动时间），隔离重跑数据
        stmt = select(KnowledgeApproval).where(
            KnowledgeApproval.created_at >= run_start,
        ).order_by(KnowledgeApproval.created_at)
        approvals = list((await session.execute(stmt)).scalars().all())

        auto_appr, manual_appr = [], []
        for ap in approvals:
            if ap.status == "pending":
                # 模拟人工审批策略：置信度 >= 0.75 且无冲突 → 通过；否则拒绝
                if ap.conflict_count and ap.conflict_count > 0:
                    ap.status = "rejected"
                    ap.review_note = "与已有 FAQ 重复，不予收录"
                elif (ap.quality_score or 0) >= 0.75:
                    ap.status = "approved"
                    ap.review_note = "人工复核通过"
                else:
                    ap.status = "rejected"
                    ap.review_note = "置信度不足，质量不达标"
                ap.reviewed_at = datetime.now(timezone.utc)
                ap.reviewer_id = user.id
                manual_appr.append(ap)
            else:
                auto_appr.append(ap)
        await session.commit()

        print("\n  审批明细（confidence / 自动|人工 / 最终状态）：")
        for ap in approvals:
            route = "自动" if ap in auto_appr else "人工"
            print(
                f"    score={ap.quality_score:.2f} conflicts={ap.conflict_count} "
                f"[{route}] → {ap.status}"
                + (f"（{ap.review_note}）" if ap.review_note else "")
            )

        # ---------- Phase 3: 统计 ----------
        n_total = len(rows)
        n_success = sum(1 for r in rows if r["result"].get("status") == "success")
        n_skipped = sum(1 for r in rows if r["result"].get("status") == "skipped")
        n_failed = sum(1 for r in rows if r["result"].get("status") == "failed")

        approved_total = sum(1 for ap in approvals if ap.status == "approved")
        rejected_total = sum(1 for ap in approvals if ap.status == "rejected")
        pass_rate = approved_total / (approved_total + rejected_total) if (approved_total + rejected_total) else 0
        auto_rate = len(auto_appr) / len(approvals) if approvals else 0

        # 质量档位 × 结果分布
        by_quality: dict[str, dict[str, int]] = {}
        for r in rows:
            bucket = by_quality.setdefault(r["quality"], {"success": 0, "other": 0})
            bucket["success" if r["result"].get("status") == "success" else "other"] += 1

        print("\n" + "=" * 64)
        print("[M3] 知识回流指标（模拟 7 天流量）")
        print("=" * 64)
        print(f"  好评反馈总数     : {n_total}")
        print(f"  FAQ 成功沉淀     : {n_success}（跳过 {n_skipped}，失败 {n_failed}）")
        print(f"  日均回流 FAQ     : {n_success / SIM_DAYS:.1f} 条/天")
        print(f"  审批记录总数     : {len(approvals)}")
        print(f"    自动通过       : {len(auto_appr)}（自动通过率 {auto_rate:.1%}）")
        print(f"    人工审批       : {len(manual_appr)}")
        print(f"  审批通过率       : {pass_rate:.1%}（通过 {approved_total} / 拒绝 {rejected_total}）")
        print(f"  质量档位分布     : {by_quality}")
        stats = await approval_svc.get_stats()
        print(f"  全库审批统计(参考): {stats}")
        print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
