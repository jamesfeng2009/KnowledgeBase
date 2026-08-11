"""
微调数据集构建器 — 单一职责：从业务数据构建 4 类微调训练样本。

数据源映射（基于项目现有模型，无新增依赖）：
- SFT：① QA 社区已采纳答案（QaAnswer.is_accepted=True）
        ② chat 中好评反馈（Feedback.type 映射分值 >= min_rating）的问答对
          （related_message_id → Message 取 assistant 内容，同会话前一条
          user 消息作 user）
- DPO：同一 query 下好评（chosen）× 差评/质量拦截（rejected）答案配对；
       点踩不足时回退"同 query 无反馈答案"，meta.pair_type 区分来源
- Embedding：SearchLog（query + clicked_doc_id 点击行为）→ (query, pos, neg)，
       负例为同知识库内随机未点击文档（meta.neg_type="random"）
- Golden：从高质量 SFT 源（采纳答案 + 好评问答）抽样冻结为评测集，
       与 SFT 按 user_text 哈希分桶严格互斥（见 _GOLDEN_HASH_MODULO）

设计说明（与项目现状对齐）：
- Feedback 模型无 rating 数值列，type → 分值映射见 FEEDBACK_RATING，
  min_rating=4 等价于仅 praise 视为好评；
- UserBehavior 模型无 query 列，检索三元组的 query 取自 SearchLog；
- QA 回答无直接文档外链，chat 样本的 doc_ids 来自 Message.sources 链路；
- 每条样本关联文档密级权重 > max_classification 时剔除（防高密级内容泄漏）。
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.finetune.data_cleaner import (
    MAX_SAMPLE_CHARS,
    MIN_SAMPLE_CHARS,
    DedupFilter,
    check_length,
    content_hash,
    mask_pii,
    new_filtered_stats,
)
from app.models.analytics import SearchLog
from app.models.conversation import Message
from app.models.feedback import Feedback
from app.models.high_risk import HighRiskAuditRecord
from app.models.knowledge import Document
from app.models.qa import QaAnswer, QaQuestion
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

#: SFT 样本的 system prompt — 与项目 RAG 生成器的助手定位一致
SFT_SYSTEM_PROMPT: str = (
    "你是企业知识库智能助手。基于知识库内容准确、简洁地回答用户问题，"
    "不得编造知识库中不存在的信息；不确定时明确说明。"
)

#: Feedback.type → 评分映射（模型无 rating 数值列）。
#: praise=5（点赞）、suggestion=3（中性建议）、complaint=2（点踩）、bug=1（错误反馈）。
FEEDBACK_RATING: dict[str, int] = {
    "praise": 5,
    "suggestion": 3,
    "complaint": 2,
    "bug": 1,
}

#: 差评反馈阈值 — 评分 <= 此值视为点踩（rejected 候选）
_NEGATIVE_RATING_MAX: int = 2

#: 文档密级权重 — 与 permission_service._CLEARANCE_ORDER 口径一致
CLASSIFICATION_WEIGHT: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}

#: Embedding 三元组中正/负例文档内容截取长度
_DOC_CONTENT_CHARS: int = 500
#: Embedding 负例候选池大小（同租户随机抽）
_NEG_POOL_SIZE: int = 200

#: Golden/SFT 哈希分桶模数（评审 #3）：user_text 内容哈希 % 10 == 0 的问答对
#: 仅可进 Golden 评测集，其余仅可进 SFT 训练集。此前两者共用
#: _collect_sft_raw_pairs 且无隔离，评测题可原样出现在训练集中（指标虚高）；
#: 且 Golden 按 created_at+limit 截取，新数据进入会导致评测集成员漂移。
#: 哈希分桶后成员资格只取决于内容本身：互斥、稳定、可复现。
_GOLDEN_HASH_MODULO: int = 10


def _in_golden_bucket(user_text: str) -> bool:
    """稳定哈希分桶：同一 user_text 永远落入同一侧（Golden 或 SFT），保证互斥。"""
    return int(content_hash(user_text), 16) % _GOLDEN_HASH_MODULO == 0


@dataclass
class _RawPair:
    """原始问答对 — 清洗前的中间结构。"""

    user_text: str
    assistant_text: str
    source: str  # qa_adopted / chat_rated
    doc_ids: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 公共工具
# ------------------------------------------------------------------


def _since(days: int) -> datetime:
    """时间窗口起点（UTC）。"""
    return datetime.now(timezone.utc) - timedelta(days=days)


def _tenant_str(tenant_id: uuid.UUID | None) -> str | None:
    return str(tenant_id) if tenant_id else None


def _feedback_rating(feedback_type: str) -> int:
    """Feedback.type → 分值；未知类型按中性 3 分。"""
    return FEEDBACK_RATING.get(feedback_type, 3)


def _exceeds_classification(
    doc_ids: list[str],
    cls_map: dict[str, str],
    max_classification: str,
) -> bool:
    """任一关联文档密级权重超过阈值即视为超阈（安全口径：宁缺毋滥）。

    文档已从库中删除（不在 cls_map）时按默认密级 internal 处理，
    与 Document.classification 列默认值一致。
    """
    threshold = CLASSIFICATION_WEIGHT.get(max_classification, 1)
    for doc_id in doc_ids:
        weight = CLASSIFICATION_WEIGHT.get(cls_map.get(doc_id, "internal"), 1)
        if weight > threshold:
            return True
    return False


async def _fetch_doc_classifications(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    doc_ids: set[str],
) -> dict[str, str]:
    """批量查询文档密级 — 返回 {str(doc_id): classification}。"""
    if not doc_ids:
        return {}
    valid_ids: list[uuid.UUID] = []
    for raw in doc_ids:
        try:
            valid_ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            # sources 中混入非法 doc_id 时跳过（不影响其他文档查询）
            continue
    if not valid_ids:
        return {}
    stmt = select(Document.id, Document.classification).where(
        Document.id.in_(valid_ids),
        Document.deleted_at.is_(None),
    )
    stmt = apply_tenant_filter(stmt, Document, tenant_id)
    rows = (await db.execute(stmt)).all()
    return {str(doc_id): classification for doc_id, classification in rows}


def _extract_source_doc_ids(sources: list | None) -> list[str]:
    """从 Message.sources（JSONB 引用卡片列表）提取 doc_id 列表。"""
    doc_ids: list[str] = []
    for source in sources or []:
        if isinstance(source, dict) and source.get("doc_id"):
            doc_ids.append(str(source["doc_id"]))
    return doc_ids


def _clean_sample_texts(
    texts: list[str],
    stats: dict[str, int],
    *,
    min_chars: int,
    max_chars: int,
) -> tuple[list[str], str | None]:
    """样本级清洗：逐字段脱敏 + 长度检查。

    Returns:
        (脱敏后的文本列表, 剔除原因)；剔除原因为 None 表示通过。
        PII 命中不剔除，仅累加 pii_masked 统计（由调用方写入 meta）。
    """
    masked_texts: list[str] = []
    pii_hit = False
    for text in texts:
        masked = mask_pii(text)
        if masked != text:
            pii_hit = True
        masked_texts.append(masked)
    if pii_hit:
        stats["pii_masked"] += 1
    for masked in masked_texts:
        reason = check_length(masked, min_chars, max_chars)
        if reason is not None:
            stats[reason] += 1
            return masked_texts, reason
    return masked_texts, None


# ------------------------------------------------------------------
# SFT 原始问答对采集（build_sft_dataset / build_golden_set 共用）
# ------------------------------------------------------------------


async def _collect_qa_pairs(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    since: datetime,
    limit: int,
) -> list[_RawPair]:
    """采集 QA 社区已采纳答案 → 原始问答对。

    user = 问题标题 + 问题详情；assistant = 被采纳回答内容。
    QA 回答无直接文档外链（Answer → doc 链路不存在），doc_ids 为空，
    不参与密级过滤。
    """
    stmt = (
        select(QaAnswer, QaQuestion)
        .join(QaQuestion, QaAnswer.question_id == QaQuestion.id)
        .where(
            QaAnswer.is_accepted.is_(True),
            QaAnswer.deleted_at.is_(None),
            QaQuestion.deleted_at.is_(None),
            QaAnswer.created_at >= since,
        )
        .order_by(QaAnswer.created_at.desc())
        .limit(limit)
    )
    stmt = apply_tenant_filter(stmt, QaAnswer, tenant_id)
    rows = (await db.execute(stmt)).all()

    pairs: list[_RawPair] = []
    for answer, question in rows:
        user_text = question.title
        if question.content:
            user_text = f"{question.title}\n{question.content}"
        pairs.append(
            _RawPair(
                user_text=user_text.strip(),
                assistant_text=answer.content,
                source="qa_adopted",
                doc_ids=[],
            )
        )
    return pairs


async def _collect_chat_rated_pairs(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    since: datetime,
    min_rating: int,
    limit: int,
) -> list[_RawPair]:
    """采集 chat 好评问答对 → 原始问答对。

    链路：Feedback（rating >= min_rating 且关联消息）
        → Message（assistant 内容 + sources 引用文档）
        → 同会话前一条 user 消息（prompt）。

    查询顺序（共 3 次 DB 查询，供测试 mock 参考）：
    1. feedbacks；2. assistant 消息；3. 涉及会话的 user 消息。
    """
    # 1. 好评反馈（关联了消息）
    fb_stmt = select(Feedback).where(
        Feedback.related_message_id.isnot(None),
        Feedback.created_at >= since,
    )
    fb_stmt = apply_tenant_filter(fb_stmt, Feedback, tenant_id)
    feedbacks = list((await db.execute(fb_stmt)).scalars().all())
    praised = [
        f
        for f in feedbacks
        if _feedback_rating(f.type) >= min_rating and f.related_message_id
    ]
    if not praised:
        return []

    # 2. 关联的 assistant 消息
    msg_stmt = select(Message).where(
        Message.id.in_({f.related_message_id for f in praised})
    )
    messages = list((await db.execute(msg_stmt)).scalars().all())
    msg_map = {m.id: m for m in messages if m.role == "assistant"}
    if not msg_map:
        return []

    # 3. 涉及会话的全部 user 消息（按时间正序，供"前一条 user 消息"匹配）
    conv_ids = {m.conversation_id for m in msg_map.values()}
    user_stmt = (
        select(Message)
        .where(Message.conversation_id.in_(conv_ids), Message.role == "user")
        .order_by(Message.created_at.asc())
    )
    user_messages = list((await db.execute(user_stmt)).scalars().all())
    conv_user_msgs: dict[uuid.UUID, list[Message]] = {}
    for m in user_messages:
        conv_user_msgs.setdefault(m.conversation_id, []).append(m)

    pairs: list[_RawPair] = []
    for f in praised:
        assistant_msg = msg_map.get(f.related_message_id)
        if assistant_msg is None:
            continue
        # 同会话内时间早于（或等于）该 assistant 消息的最后一条 user 消息
        user_msg = next(
            (
                m
                for m in reversed(conv_user_msgs.get(assistant_msg.conversation_id, []))
                if m.created_at <= assistant_msg.created_at
            ),
            None,
        )
        if user_msg is None:
            continue
        pairs.append(
            _RawPair(
                user_text=user_msg.content,
                assistant_text=assistant_msg.content,
                source="chat_rated",
                doc_ids=_extract_source_doc_ids(assistant_msg.sources),
            )
        )
        if len(pairs) >= limit:
            break
    return pairs


async def _collect_sft_raw_pairs(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    days: int,
    min_rating: int,
    limit: int,
) -> list[_RawPair]:
    """采集全部 SFT 原始问答对（QA 采纳 + chat 好评）。"""
    since = _since(days)
    qa_pairs = await _collect_qa_pairs(db, tenant_id, since=since, limit=limit)
    chat_pairs = await _collect_chat_rated_pairs(
        db, tenant_id, since=since, min_rating=min_rating, limit=limit
    )
    return qa_pairs + chat_pairs


# ------------------------------------------------------------------
# 4 类数据集构建函数（统一签名：返回 (样本列表, 过滤统计)）
# ------------------------------------------------------------------


async def build_sft_dataset(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    max_classification: str = "internal",
    days: int = 90,
    min_rating: int = 4,
    limit: int = 10000,
    min_chars: int = MIN_SAMPLE_CHARS,
    max_chars: int = MAX_SAMPLE_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建 SFT 指令微调数据集。

    输出格式::

        {"messages": [{"role": "system", ...}, {"role": "user", ...},
                      {"role": "assistant", ...}],
         "meta": {"source": "qa_adopted|chat_rated", "doc_ids": [...],
                  "tenant_id": "..."}}

    Returns:
        (样本列表, 过滤统计) — 统计口径见 data_cleaner.new_filtered_stats。
    """
    stats = new_filtered_stats()
    dedup = DedupFilter()
    raw_pairs = await _collect_sft_raw_pairs(
        db, tenant_id, days=days, min_rating=min_rating, limit=limit
    )

    # 批量取密级（仅 chat_rated 样本有 doc_ids）
    all_doc_ids = {d for p in raw_pairs for d in p.doc_ids}
    cls_map = await _fetch_doc_classifications(db, tenant_id, all_doc_ids)

    samples: list[dict[str, Any]] = []
    for pair in raw_pairs:
        if len(samples) >= limit:
            break
        # 0. Golden 互斥（评审 #3）：落入 Golden 桶的问答对不得进 SFT 训练集
        if _in_golden_bucket(pair.user_text):
            stats["golden_excluded"] += 1
            continue
        # 1. 密级过滤（安全硬过滤，先于其他清洗）
        if _exceeds_classification(pair.doc_ids, cls_map, max_classification):
            stats["classification"] += 1
            continue
        # 2. 脱敏 + 长度过滤
        (user_text, assistant_text), reason = _clean_sample_texts(
            [pair.user_text, pair.assistant_text],
            stats,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if reason is not None:
            continue
        # 3. 哈希去重（user + assistant 联合键）
        if dedup.is_duplicate(f"{user_text}\n{assistant_text}"):
            stats["duplicate"] += 1
            continue
        samples.append(
            {
                "messages": [
                    {"role": "system", "content": SFT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ],
                "meta": {
                    "source": pair.source,
                    "doc_ids": pair.doc_ids,
                    "tenant_id": _tenant_str(tenant_id),
                },
            }
        )
    log.info(
        "finetune.sft_built",
        samples=len(samples),
        filtered=stats,
        tenant_id=_tenant_str(tenant_id),
    )
    return samples, stats


async def build_dpo_dataset(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    max_classification: str = "internal",
    days: int = 90,
    min_rating: int = 4,
    limit: int = 10000,
    min_chars: int = MIN_SAMPLE_CHARS,
    max_chars: int = MAX_SAMPLE_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建 DPO 偏好对数据集。

    配对来源（meta.pair_type 区分）：
    - feedback：同一条 user 消息下好评（chosen）× 差评（rejected）答案；
    - no_feedback：差评不足时，以同 query 的无反馈答案充当 rejected；
    - qa_adopted：QA 社区同问题下采纳答案（chosen）× 未采纳答案（rejected）；
    - quality_blocked：同 query 的好评答案（chosen）× 高风险拦截答案（rejected，
      来自 HighRiskAuditRecord.answer_snippet）。

    输出格式：{"prompt", "chosen", "rejected", "meta": {...}}

    查询顺序（共 6 次 DB 查询，供测试 mock 参考）：
    1. feedbacks；2. 反馈关联消息；3. 涉及会话的全部消息；
    4. QA 采纳答案（join 问题）；5. 这些问题的未采纳答案；6. 高风险拦截记录。
    """
    stats = new_filtered_stats()
    dedup = DedupFilter()
    since = _since(days)

    # ---- 1-3. chat 反馈配对 ----
    fb_stmt = select(Feedback).where(
        Feedback.related_message_id.isnot(None),
        Feedback.created_at >= since,
    )
    fb_stmt = apply_tenant_filter(fb_stmt, Feedback, tenant_id)
    feedbacks = list((await db.execute(fb_stmt)).scalars().all())

    rating_by_msg: dict[uuid.UUID, int] = {}
    for f in feedbacks:
        if f.related_message_id:
            # 同一消息多条反馈时取最高评分（好评优先，避免被单条差评误伤）
            rating_by_msg[f.related_message_id] = max(
                rating_by_msg.get(f.related_message_id, 0),
                _feedback_rating(f.type),
            )

    chat_pairs: list[tuple[str, str, str, str, list[str]]] = []
    praised_by_query: dict[str, str] = {}
    if rating_by_msg:
        msg_stmt = select(Message).where(Message.id.in_(set(rating_by_msg)))
        rated_msgs = list((await db.execute(msg_stmt)).scalars().all())
        conv_ids = {m.conversation_id for m in rated_msgs}
        all_stmt = (
            select(Message)
            .where(Message.conversation_id.in_(conv_ids))
            .order_by(Message.created_at.asc())
        )
        all_msgs = list((await db.execute(all_stmt)).scalars().all())

        # 按会话遍历：user 消息 → 其后的 assistant 消息组
        conv_msgs: dict[uuid.UUID, list[Message]] = {}
        for m in all_msgs:
            conv_msgs.setdefault(m.conversation_id, []).append(m)
        for msgs in conv_msgs.values():
            current_user: Message | None = None
            group: list[Message] = []
            for m in msgs + [None]:  # type: ignore[list-item]
                if m is None or m.role == "user":
                    # 结算上一组
                    if current_user is not None and group:
                        chat_pairs.extend(
                            _pair_feedback_group(
                                current_user.content,
                                group,
                                rating_by_msg,
                                min_rating=min_rating,
                            )
                        )
                        for a in group:
                            r = rating_by_msg.get(a.id, 0)
                            if r >= min_rating:
                                praised_by_query.setdefault(
                                    current_user.content.strip(), a.content
                                )
                    if m is None:
                        break
                    current_user = m
                    group = []
                elif m.role == "assistant":
                    group.append(m)

    # ---- 4-5. QA 采纳 × 未采纳配对 ----
    qa_stmt = (
        select(QaAnswer, QaQuestion)
        .join(QaQuestion, QaAnswer.question_id == QaQuestion.id)
        .where(
            QaAnswer.is_accepted.is_(True),
            QaAnswer.deleted_at.is_(None),
            QaQuestion.deleted_at.is_(None),
            QaAnswer.created_at >= since,
        )
        .limit(limit)
    )
    qa_stmt = apply_tenant_filter(qa_stmt, QaAnswer, tenant_id)
    adopted_rows = (await db.execute(qa_stmt)).all()

    qa_pairs: list[tuple[str, str, str, str, list[str]]] = []
    if adopted_rows:
        question_ids = {question.id for _, question in adopted_rows}
        rejected_stmt = select(QaAnswer).where(
            QaAnswer.question_id.in_(question_ids),
            QaAnswer.is_accepted.is_(False),
            QaAnswer.deleted_at.is_(None),
        )
        rejected_answers = list((await db.execute(rejected_stmt)).scalars().all())
        rejected_by_q: dict[uuid.UUID, list[QaAnswer]] = {}
        for a in rejected_answers:
            rejected_by_q.setdefault(a.question_id, []).append(a)
        for adopted, question in adopted_rows:
            prompt = question.title
            if question.content:
                prompt = f"{question.title}\n{question.content}"
            for rejected in rejected_by_q.get(question.id, []):
                qa_pairs.append(
                    (prompt.strip(), adopted.content, rejected.content, "qa_adopted", [])
                )

    # ---- 6. 高风险拦截答案（quality_blocked 配对）----
    hr_stmt = select(HighRiskAuditRecord).where(
        HighRiskAuditRecord.created_at >= since
    )
    hr_stmt = apply_tenant_filter(hr_stmt, HighRiskAuditRecord, tenant_id)
    blocked_records = list((await db.execute(hr_stmt)).scalars().all())
    blocked_pairs: list[tuple[str, str, str, str, list[str]]] = []
    for record in blocked_records:
        chosen = praised_by_query.get(record.query.strip())
        if chosen:
            blocked_pairs.append(
                (record.query.strip(), chosen, record.answer_snippet, "quality_blocked", [])
            )

    raw_pairs = chat_pairs + qa_pairs + blocked_pairs

    # ---- 清洗（密级 → 脱敏/长度 → 去重）----
    all_doc_ids = {d for *_, doc_ids in raw_pairs for d in doc_ids}
    cls_map = await _fetch_doc_classifications(db, tenant_id, all_doc_ids)

    samples: list[dict[str, Any]] = []
    for prompt, chosen, rejected, pair_type, doc_ids in raw_pairs:
        if len(samples) >= limit:
            break
        if _exceeds_classification(doc_ids, cls_map, max_classification):
            stats["classification"] += 1
            continue
        (prompt_m, chosen_m, rejected_m), reason = _clean_sample_texts(
            [prompt, chosen, rejected],
            stats,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if reason is not None:
            continue
        if dedup.is_duplicate(f"{prompt_m}\n{chosen_m}\n{rejected_m}"):
            stats["duplicate"] += 1
            continue
        samples.append(
            {
                "prompt": prompt_m,
                "chosen": chosen_m,
                "rejected": rejected_m,
                "meta": {
                    "source": "chat_feedback" if pair_type in ("feedback", "no_feedback") else pair_type,
                    "pair_type": pair_type,
                    "doc_ids": doc_ids,
                    "tenant_id": _tenant_str(tenant_id),
                },
            }
        )
    log.info(
        "finetune.dpo_built",
        samples=len(samples),
        filtered=stats,
        tenant_id=_tenant_str(tenant_id),
    )
    return samples, stats


def _pair_feedback_group(
    user_text: str,
    assistant_msgs: list[Message],
    rating_by_msg: dict[uuid.UUID, int],
    *,
    min_rating: int = 4,
) -> list[tuple[str, str, str, str, list[str]]]:
    """单条 user 消息下的 assistant 答案组配对。

    - 好评（rating >= min_rating）× 差评（rating <= 2）→ pair_type="feedback"；
    - 无差评时以无反馈答案充当 rejected → pair_type="no_feedback"。
    """
    praised = [m for m in assistant_msgs if rating_by_msg.get(m.id, 0) >= min_rating]
    downvoted = [
        m for m in assistant_msgs if 0 < rating_by_msg.get(m.id, 0) <= _NEGATIVE_RATING_MAX
    ]
    neutral = [m for m in assistant_msgs if m.id not in rating_by_msg]

    pairs: list[tuple[str, str, str, str, list[str]]] = []
    for chosen_msg in praised:
        doc_ids = _extract_source_doc_ids(chosen_msg.sources)
        if downvoted:
            rejected_msg = downvoted[0]
            pairs.append(
                (user_text, chosen_msg.content, rejected_msg.content, "feedback", doc_ids)
            )
        elif neutral:
            pairs.append(
                (user_text, chosen_msg.content, neutral[0].content, "no_feedback", doc_ids)
            )
    return pairs


async def build_embedding_dataset(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    max_classification: str = "internal",
    days: int = 90,
    min_rating: int = 4,  # 保留统一签名，Embedding 构建不使用
    limit: int = 10000,
    min_chars: int = MIN_SAMPLE_CHARS,
    max_chars: int = MAX_SAMPLE_CHARS,
    rng: random.Random | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建 Embedding 检索三元组数据集。

    正例：SearchLog 中"有 query 且发生文档点击"的 (query, clicked_doc)；
    负例：同知识库内随机一篇该用户未点击的文档（meta.neg_type="random"）。
    正/负例文档内容均截取前 500 字符，密级超阈剔除。

    输出格式：{"query", "pos", "neg", "meta": {...}}

    查询顺序（共 3 次 DB 查询，供测试 mock 参考）：
    1. 点击行为 SearchLog；2. 被点击文档；3. 负例候选文档池。
    """
    del min_rating  # 统一签名占位，避免误用
    stats = new_filtered_stats()
    dedup = DedupFilter()
    rng = rng or random.Random()
    since = _since(days)

    # 1. 点击行为（query + clicked_doc_id）
    log_stmt = (
        select(SearchLog)
        .where(
            SearchLog.clicked.is_(True),
            SearchLog.clicked_doc_id.isnot(None),
            SearchLog.created_at >= since,
        )
        .order_by(SearchLog.created_at.desc())
        .limit(limit)
    )
    log_stmt = apply_tenant_filter(log_stmt, SearchLog, tenant_id)
    click_logs = list((await db.execute(log_stmt)).scalars().all())
    if not click_logs:
        return [], stats

    # 2. 被点击文档（正例内容 + 密级 + 所属知识库）
    clicked_doc_ids = {log_entry.clicked_doc_id for log_entry in click_logs}
    doc_stmt = select(Document).where(
        Document.id.in_(clicked_doc_ids),
        Document.deleted_at.is_(None),
    )
    doc_stmt = apply_tenant_filter(doc_stmt, Document, tenant_id)
    pos_docs = {d.id: d for d in (await db.execute(doc_stmt)).scalars().all()}

    # 3. 负例候选池（同租户随机文档，排除被点击集合）
    pool_stmt = (
        select(Document)
        .where(
            Document.deleted_at.is_(None),
            Document.id.notin_(clicked_doc_ids),
            Document.content_text.isnot(None),
        )
        .limit(_NEG_POOL_SIZE)
    )
    pool_stmt = apply_tenant_filter(pool_stmt, Document, tenant_id)
    neg_pool = list((await db.execute(pool_stmt)).scalars().all())
    neg_by_kb: dict[uuid.UUID, list[Document]] = {}
    for d in neg_pool:
        neg_by_kb.setdefault(d.kb_id, []).append(d)

    threshold = CLASSIFICATION_WEIGHT.get(max_classification, 1)
    samples: list[dict[str, Any]] = []
    for log_entry in click_logs:
        if len(samples) >= limit:
            break
        pos_doc = pos_docs.get(log_entry.clicked_doc_id)
        if pos_doc is None or not pos_doc.content_text:
            continue
        # 密级过滤：正例超阈剔除
        if CLASSIFICATION_WEIGHT.get(pos_doc.classification, 1) > threshold:
            stats["classification"] += 1
            continue
        # 负例：优先同知识库，池为空时回退全局池
        kb_candidates = neg_by_kb.get(pos_doc.kb_id, [])
        candidates = kb_candidates or neg_pool
        if not candidates:
            continue
        neg_doc = rng.choice(candidates)
        if CLASSIFICATION_WEIGHT.get(neg_doc.classification, 1) > threshold:
            stats["classification"] += 1
            continue

        (query_m, pos_m, neg_m), reason = _clean_sample_texts(
            [
                log_entry.query,
                pos_doc.content_text[:_DOC_CONTENT_CHARS],
                neg_doc.content_text[:_DOC_CONTENT_CHARS],
            ],
            stats,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if reason is not None:
            continue
        if dedup.is_duplicate(f"{query_m}\n{pos_m}"):
            stats["duplicate"] += 1
            continue
        samples.append(
            {
                "query": query_m,
                "pos": pos_m,
                "neg": neg_m,
                "meta": {
                    "pos_doc_id": str(pos_doc.id),
                    "neg_doc_id": str(neg_doc.id),
                    "neg_type": "random",
                    "tenant_id": _tenant_str(tenant_id),
                },
            }
        )
    log.info(
        "finetune.embedding_built",
        samples=len(samples),
        filtered=stats,
        tenant_id=_tenant_str(tenant_id),
    )
    return samples, stats


async def build_golden_set(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    *,
    max_classification: str = "internal",
    days: int = 90,
    min_rating: int = 4,
    limit: int = 500,
    min_chars: int = MIN_SAMPLE_CHARS,
    max_chars: int = MAX_SAMPLE_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建 Golden 冻结评测集 — 从高质量 SFT 源抽样。

    来源与 SFT 相同（QA 采纳答案 + chat 好评问答），输出评测格式，
    上限默认 500 条（limit 参数）。

    评审 #3：与 SFT 训练集严格互斥 —— 仅 user_text 哈希落入 Golden 桶
    （约 1/10）的问答对可进本评测集，SFT 侧同步排除同桶样本；
    桶成员资格只取决于内容哈希，评测集不随新数据进入而漂移。
    采集上限相应放大 _GOLDEN_HASH_MODULO 倍以维持 limit 语义。

    输出格式：{"query", "expected_answer", "expected_doc_ids", "meta": {...}}
    """
    stats = new_filtered_stats()
    dedup = DedupFilter()
    raw_pairs = await _collect_sft_raw_pairs(
        db, tenant_id, days=days, min_rating=min_rating,
        limit=limit * _GOLDEN_HASH_MODULO,  # 哈希桶仅留 ~1/modulo，放大采集补足
    )

    all_doc_ids = {d for p in raw_pairs for d in p.doc_ids}
    cls_map = await _fetch_doc_classifications(db, tenant_id, all_doc_ids)

    samples: list[dict[str, Any]] = []
    for pair in raw_pairs:
        if len(samples) >= limit:
            break
        # Golden 互斥（评审 #3）：仅哈希落入 Golden 桶的问答对可进评测集，
        # 其余留给 SFT 训练集；桶成员资格只取决于内容，评测集不随新数据漂移
        if not _in_golden_bucket(pair.user_text):
            stats["sft_reserved"] += 1
            continue
        if _exceeds_classification(pair.doc_ids, cls_map, max_classification):
            stats["classification"] += 1
            continue
        (query_m, answer_m), reason = _clean_sample_texts(
            [pair.user_text, pair.assistant_text],
            stats,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if reason is not None:
            continue
        if dedup.is_duplicate(f"{query_m}\n{answer_m}"):
            stats["duplicate"] += 1
            continue
        samples.append(
            {
                "query": query_m,
                "expected_answer": answer_m,
                "expected_doc_ids": pair.doc_ids,
                "meta": {
                    "source": pair.source,
                    "frozen": True,
                    "tenant_id": _tenant_str(tenant_id),
                },
            }
        )
    log.info(
        "finetune.golden_built",
        samples=len(samples),
        filtered=stats,
        tenant_id=_tenant_str(tenant_id),
    )
    return samples, stats


#: dataset_type → 构建函数映射（Celery 任务分发用）
DATASET_BUILDERS: dict[str, Any] = {
    "sft": build_sft_dataset,
    "dpo": build_dpo_dataset,
    "embedding": build_embedding_dataset,
    "golden": build_golden_set,
}
