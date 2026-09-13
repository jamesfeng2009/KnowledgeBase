"""沉淀前置闸门 — 单一职责：FAQ 回流的产出查重与支持度统计。

判据来源（Better Harness 借鉴评估，knowledge-distillation-hardening-plan）：
值得沉淀的经验必须「跨相似任务重复出现」且「被最终产出支持」。
本模块把判据收敛为两个检查点，均为廉价操作（不消耗 LLM 对话）：

    check_before_extract — LLM 提取前：
        ① 与既有 chat_faq 资产标题做词汇相似度比对（租户内，高阈值 0.92，
           只拦截明显重复；漏网由提取后权威查重兜底）
        ② 统计支持度摘要 SupportSummary（近 N 天相似问题的好评/采纳信号），
           供审批环节展示证据并参与自动通过判定（D3）
    check_after_extract — LLM 提取后：
        ③ 提取问题 vs FAQ KB 已发布文档标题的嵌入余弦查重（权威判定），
           嵌入不可用时 fail-open 放行，不阻塞沉淀。

分层设计：
    - 词汇相似度（difflib）用于支持度统计与前置廉价查重 — 零外部调用；
    - 嵌入余弦仅用于提取后权威查重 — 每次 1 次批量 embed 调用，
      文档标题嵌入带进程内 TTL 缓存（FAQ 文档变更频率低）。
    - 闸门关闭（CHAT_FAQ_GATE_ENABLED=false）时调用方完全跳过本模块，
      行为回退到闸门上线前。
"""

from __future__ import annotations

import time
import uuid
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# 标题嵌入进程内缓存 — {title_text: (embedding, cached_at_monotonic)}
# TTL 10 分钟，容量 2000（超出逐出最旧）。FAQ 文档标题变更频率低，命中率高。
# ---------------------------------------------------------------------------
_TITLE_EMBED_CACHE: dict[str, tuple[list[float], float]] = {}
_TITLE_EMBED_TTL_SECONDS = 600.0
_TITLE_EMBED_CACHE_MAX = 2000


def _normalize(text_value: str) -> str:
    """文本归一化 — 小写、去空白与中英文标点，供词汇相似度比对。"""
    return "".join(
        ch for ch in text_value.lower() if ch.isalnum()
    )


def lexical_similarity(a: str, b: str) -> float:
    """词汇相似度 — 归一化后 SequenceMatcher 比率（0.0~1.0）。

    对中英文均可用（按字符序列比对）。任一为空返回 0.0。
    """
    na, nb = _normalize(a or ""), _normalize(b or "")
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度 — 零向量或维度不一致返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _build_support_summary(
    praise_events: list[dict[str, Any]],
    accept_events: list[dict[str, Any]],
    user_id: UUID | str | None,
) -> dict[str, Any]:
    """组装支持度摘要 — 当前信号自身计入（它本身就是支持证据）。

    加权口径（D1）：praise=1，accepted=2。
    distinct_users 为两条信号通道（好评/采纳）提交者加上当前用户的并集大小。
    """
    praise_count = 1
    accept_count = 0
    users: set[str] = set()
    if user_id is not None:
        users.add(str(user_id))
    for ev in praise_events:
        praise_count += 1
        if ev.get("user_id"):
            users.add(str(ev["user_id"]))
    for ev in accept_events:
        accept_count += 1
        if ev.get("user_id"):
            users.add(str(ev["user_id"]))
    return {
        "support_count": praise_count + 2 * accept_count,
        "praise_count": praise_count,
        "accept_count": accept_count,
        "distinct_users": len(users),
        "window_days": get_settings().CHAT_FAQ_SUPPORT_WINDOW_DAYS,
    }


class DistillationGate:
    """沉淀前置闸门 — 产出查重 + 支持度统计。

    依赖注入：
        - db: AsyncSession（只读查询，不产生写入）
        - tenant_id: 租户 ID（所有查询租户隔离）
    """

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 检查点 ①②：LLM 提取前
    # ------------------------------------------------------------------

    async def check_before_extract(
        self,
        question_hint: str,
        source_type: str,
        source_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """提取前检查 — 廉价词汇查重 + 支持度统计。

        与既有 chat_faq 资产标题（含 pending_review，它们终将进入 KB）
        做词汇相似度比对，>= CHAT_FAQ_PRECHECK_SIM_THRESHOLD 判明显重复。
        资产量为租户内个位到百位级，直接全量加载比对。

        Returns:
            {action: "proceed"|"skip", reason: str|None, support: dict}
            skip 时 reason ∈ duplicate_asset；support 始终携带（供审批展示）。
        """
        settings = get_settings()
        support = await self.build_support_summary(
            question_hint=question_hint,
            exclude_source_type=source_type,
            exclude_source_id=source_id,
            user_id=user_id,
        )

        # ① 廉价查重：与既有 chat_faq 资产标题比对。
        # fail-open：查询失败放行（闸门是优化层，不阻塞沉淀管线）
        try:
            rows = list(
                (
                    await self.db.execute(
                        text(
                            """
                            SELECT id, title FROM knowledge_assets
                            WHERE asset_type = 'chat_faq'
                              AND status IN ('draft', 'pending_review', 'active')
                              AND (:tid::uuid IS NULL OR tenant_id = :tid::uuid)
                              AND deleted_at IS NULL
                            ORDER BY created_at DESC
                            LIMIT 500
                            """
                        ),
                        {"tid": self._tenant_id},
                    )
                ).fetchall()
            )
        except Exception as exc:
            log.warning(
                "distillation_gate.precheck_query_failed", error=str(exc)[:200]
            )
            return {"action": "proceed", "reason": None, "support": support}
        for asset_id, title in rows:
            if lexical_similarity(question_hint, title or "") >= (
                settings.CHAT_FAQ_PRECHECK_SIM_THRESHOLD
            ):
                log.info(
                    "distillation_gate.precheck_duplicate_asset",
                    asset_id=str(asset_id),
                    source_type=source_type,
                )
                return {
                    "action": "skip",
                    "reason": f"duplicate_asset:{asset_id}",
                    "support": support,
                }

        return {"action": "proceed", "reason": None, "support": support}

    # ------------------------------------------------------------------
    # 检查点 ③：LLM 提取后（权威查重）
    # ------------------------------------------------------------------

    async def check_after_extract(
        self,
        question: str,
        target_kb_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        """提取后权威查重 — 提取问题 vs FAQ KB 已发布文档标题（嵌入余弦）。

        查重范围：target_kb_id 内 status='published' 的 FAQ 文档（近 500 篇）。
        嵌入不可用 / KB 内无已发布文档时 fail-open 放行。

        Returns:
            {action: "proceed"|"skip", reason: str|None}
            skip 时 reason = duplicate_existing:<doc_id>。
        """
        settings = get_settings()

        # 已发布 FAQ 文档标题。fail-open：查询失败放行
        try:
            rows = list(
                (
                    await self.db.execute(
                        text(
                            """
                            SELECT id, title FROM documents
                            WHERE kb_id = :kb_id::uuid
                              AND status = 'published'
                              AND category = 'FAQ'
                              AND deleted_at IS NULL
                            ORDER BY created_at DESC
                            LIMIT 500
                            """
                        ),
                        {"kb_id": str(target_kb_id)},
                    )
                ).fetchall()
            )
        except Exception as exc:
            log.warning(
                "distillation_gate.dedup_query_failed", error=str(exc)[:200]
            )
            return {"action": "proceed", "reason": None}
        if not rows:
            return {"action": "proceed", "reason": None}

        titles = [title or "" for _, title in rows]
        question_vec, title_vecs = await self._embed_cached([question, *titles])
        if question_vec is None:
            # 嵌入不可用 → fail-open（放行），不阻塞沉淀
            log.warning("distillation_gate.embed_unavailable_fail_open")
            return {"action": "proceed", "reason": None}

        for (doc_id, _), title_vec in zip(rows, title_vecs):
            if cosine_similarity(question_vec, title_vec) >= (
                settings.CHAT_FAQ_DEDUP_SIM_THRESHOLD
            ):
                log.info(
                    "distillation_gate.dedup_duplicate_existing",
                    doc_id=str(doc_id),
                )
                return {
                    "action": "skip",
                    "reason": f"duplicate_existing:{doc_id}",
                }
        return {"action": "proceed", "reason": None}

    # ------------------------------------------------------------------
    # 支持度统计（信号回溯）
    # ------------------------------------------------------------------

    async def build_support_summary(
        self,
        question_hint: str,
        exclude_source_type: str,
        exclude_source_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """统计支持度摘要 — 近 N 天内与当前问题相似的好评/采纳信号。

        两条信号通道（各取近 100 条、租户内）：
            好评：feedbacks(praise) → 关联 assistant 消息 → 同会话前一条 user
                  消息文本（问题锚点），词汇相似度 >= 0.6 计入；
            采纳：qa_answers(is_accepted) → qa_questions.title，
                  词汇相似度 >= 0.6 计入。
        当前信号自身（exclude_source_*）不计入回溯统计，由
        _build_support_summary 统一以 1 次好评/1 次采纳计入。

        统计为审批展示用的参考证据（advisory），采用零成本的词汇相似度；
        语义级判定由提取后嵌入查重承担。
        """
        import datetime as _dt

        settings = get_settings()
        since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=settings.CHAT_FAQ_SUPPORT_WINDOW_DAYS
        )
        threshold = 0.6

        praise_events: list[dict[str, Any]] = []
        try:
            rows = (
                await self.db.execute(
                    text(
                        """
                        SELECT f.id, f.user_id, f.related_message_id, q.content
                        FROM feedbacks f
                        JOIN messages m ON m.id = f.related_message_id
                        LEFT JOIN LATERAL (
                            SELECT content FROM messages qq
                            WHERE qq.conversation_id = m.conversation_id
                              AND qq.role = 'user'
                              AND qq.created_at <= m.created_at
                            ORDER BY qq.created_at DESC LIMIT 1
                        ) q ON true
                        WHERE f.type = 'praise'
                          AND f.created_at >= :since
                          AND (:tid::uuid IS NULL OR f.tenant_id = :tid::uuid)
                          AND NOT (:stype = 'chat_feedback'
                                   AND f.id = :sid::uuid)
                        ORDER BY f.created_at DESC
                        LIMIT 100
                        """
                    ),
                    {
                        "since": since,
                        "tid": self._tenant_id,
                        "stype": exclude_source_type,
                        "sid": str(exclude_source_id),
                    },
                )
            ).fetchall()
            for _fid, _uid, _mid, question_text in rows:
                if lexical_similarity(question_hint, question_text or "") >= threshold:
                    praise_events.append({"user_id": _uid})
        except Exception as exc:
            log.warning(
                "distillation_gate.support_praise_query_failed", error=str(exc)[:200]
            )

        accept_events: list[dict[str, Any]] = []
        try:
            rows = (
                await self.db.execute(
                    text(
                        """
                        SELECT a.id, a.user_id, q.title
                        FROM qa_answers a
                        JOIN qa_questions q ON q.id = a.question_id
                        WHERE a.is_accepted = true
                          AND a.created_at >= :since
                          AND (:tid::uuid IS NULL OR a.tenant_id = :tid::uuid)
                          AND NOT (:stype = 'qa_accepted' AND a.id = :sid::uuid)
                        ORDER BY a.created_at DESC
                        LIMIT 100
                        """
                    ),
                    {
                        "since": since,
                        "tid": self._tenant_id,
                        "stype": exclude_source_type,
                        "sid": str(exclude_source_id),
                    },
                )
            ).fetchall()
            for _aid, _uid, title in rows:
                if lexical_similarity(question_hint, title or "") >= threshold:
                    accept_events.append({"user_id": _uid})
        except Exception as exc:
            log.warning(
                "distillation_gate.support_accept_query_failed", error=str(exc)[:200]
            )

        return _build_support_summary(praise_events, accept_events, user_id)

    # ------------------------------------------------------------------
    # 内部：嵌入（带标题缓存）
    # ------------------------------------------------------------------

    @staticmethod
    async def _embed_cached(
        texts: list[str],
    ) -> tuple[list[float] | None, list[list[float] | None]]:
        """批量嵌入 — 首文本（提取问题）不缓存，其余（文档标题）走 TTL 缓存。

        Returns:
            (question_vec, title_vecs)；嵌入不可用时两者均为 None 占位。
        """
        from app.llm.embedder import get_embedder

        try:
            embedder = get_embedder()
        except Exception as exc:
            log.warning(
                "distillation_gate.embedder_init_failed", error=str(exc)[:200]
            )
            return None, [None] * (len(texts) - 1)

        now = time.monotonic()
        need_embed_idx: list[int] = []
        vecs: list[list[float] | None] = [None] * len(texts)

        for i, t in enumerate(texts):
            if i == 0:
                need_embed_idx.append(i)
                continue
            cached = _TITLE_EMBED_CACHE.get(t)
            if cached is not None and now - cached[1] < _TITLE_EMBED_TTL_SECONDS:
                vecs[i] = cached[0]
            else:
                need_embed_idx.append(i)

        try:
            batch = [texts[i] for i in need_embed_idx]
            result = await embedder.embed(batch) if batch else []
        except Exception as exc:
            log.warning("distillation_gate.embed_failed", error=str(exc)[:200])
            return None, [None] * (len(texts) - 1)

        for pos, i in enumerate(need_embed_idx):
            vec = result[pos] if pos < len(result) else None
            vecs[i] = vec
            if i != 0 and vec is not None:
                # 缓存逐出：超出容量时移除最早写入项
                if len(_TITLE_EMBED_CACHE) >= _TITLE_EMBED_CACHE_MAX:
                    oldest = min(
                        _TITLE_EMBED_CACHE, key=lambda k: _TITLE_EMBED_CACHE[k][1]
                    )
                    _TITLE_EMBED_CACHE.pop(oldest, None)
                _TITLE_EMBED_CACHE[texts[i]] = (vec, now)

        question_vec = vecs[0]
        if question_vec is None:
            return None, [None] * (len(texts) - 1)
        return question_vec, vecs[1:]
