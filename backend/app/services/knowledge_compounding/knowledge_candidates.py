"""沉淀候选池 — 单一职责：相似信号的归簇、支持度累加与晋升判定。

P3 状态机（Better Harness 判据落地，knowledge-distillation-hardening-plan 05 节）：

    信号层（不变，仍走 Outbox 派发）
        好评 praise / 采纳 accepted
            │ upsert_from_signal
            ▼
    证据层（本模块）
        相似问题归簇（余弦 ≥ CHAT_FAQ_CANDIDATE_SIM_THRESHOLD，嵌入不可用回退词汇相似度）
        幂等：(source_type, source_id) 在 support_events 内查重（抵御 Outbox 重试）
            │ scan_and_promote（beat 定时）
            ▼
    晋升判定
        support_count ≥ CHAT_FAQ_PROMOTE_MIN_SUPPORT
        且 distinct_users ≥ CHAT_FAQ_PROMOTE_MIN_USERS
        且 check_after_extract 权威查重通过
            │ 复用既有沉淀+审批（每簇一次 LLM 提取）
            ▼
    沉淀层（KnowledgeCompoundingService，不改动语义）

状态流转（无物理删除）：candidate → promoting → promoted；
查重拦截 → dismissed(duplicate_existing)；TTL 未达标 → expired；管理端驳回 → dismissed。

支持事件加权口径（D1）：chat_feedback(praise)=1，qa_accepted(accepted)=2。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# 信号源 → 事件类型与加权（D1：采纳计双倍支持度）
_SIGNAL_WEIGHTS: dict[str, int] = {"chat_feedback": 1, "qa_accepted": 2}


def recount_from_events(events: list[dict[str, Any]]) -> dict[str, int]:
    """从支持事件列表重算加权计数 — praise=1，accepted=2（D1）。

    distinct_users 为事件提交者并集大小；无 user_id 的事件计入计数
    但不贡献用户并集。
    """
    praise_count = 0
    accept_count = 0
    users: set[str] = set()
    for ev in events or []:
        st = ev.get("source_type")
        if st not in _SIGNAL_WEIGHTS:
            continue
        if st == "chat_feedback":
            praise_count += 1
        else:
            accept_count += 1
        if ev.get("user_id"):
            users.add(str(ev["user_id"]))
    return {
        "support_count": praise_count * _SIGNAL_WEIGHTS["chat_feedback"]
        + accept_count * _SIGNAL_WEIGHTS["qa_accepted"],
        "praise_count": praise_count,
        "accept_count": accept_count,
        "distinct_users": len(users),
    }


class KnowledgeCandidatePool:
    """沉淀候选池 — 归簇 / 晋升 / 过期 / 驳回。

    依赖注入：
        - db: AsyncSession
        - tenant_id: 租户 ID（None 表示全局视角，仅 beat 任务使用）
        - compounding: KnowledgeCompoundingService（晋升时复用既有沉淀+审批，
          信号路径入池不需要）
    """

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: UUID | str | None = None,
        compounding: Any | None = None,
    ) -> None:
        self.db = db
        self._tenant_id = tenant_id
        self._compounding = compounding

    # ------------------------------------------------------------------
    # 信号入池
    # ------------------------------------------------------------------

    async def upsert_from_signal(
        self,
        source_type: str,
        source_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        question_hint: str,
        message_id: uuid.UUID | str | None = None,
        answer_draft: str | None = None,
    ) -> dict[str, Any]:
        """信号入池 — 幂等查重 → 归簇累加 / 新建候选。

        Returns:
            {"status": "queued", "candidate_id": str}
            {"status": "skipped", "reason": "already_in_pool"|"duplicate_existing",
             "candidate_id": str}
        """
        settings = get_settings()
        hint_vec = await self._embed_question(question_hint)

        # 活跃候选（candidate/promoting）：幂等查重 + 归簇匹配
        rows = list(
            (
                await self.db.execute(
                    text(
                        """
                        SELECT id, representative_question, question_embedding,
                               question_variants, answer_draft, support_events
                        FROM knowledge_candidates
                        WHERE status IN ('candidate', 'promoting')
                          AND (:tid::uuid IS NULL OR tenant_id = :tid::uuid)
                        ORDER BY last_seen_at DESC
                        LIMIT 300
                        """
                    ),
                    {"tid": self._tenant_id},
                )
            ).fetchall()
        )

        event = self._build_event(
            source_type=source_type,
            source_id=source_id,
            user_id=user_id,
            message_id=message_id,
        )

        threshold = settings.CHAT_FAQ_CANDIDATE_SIM_THRESHOLD
        best_id: uuid.UUID | None = None
        best_sim = -1.0
        best_row = None
        for row in rows:
            events = list(row.support_events or [])
            # 幂等键查重：(source_type, source_id) 已在池 → Outbox 重试不重复计数
            if any(
                e.get("source_type") == source_type
                and str(e.get("source_id")) == str(source_id)
                for e in events
            ):
                return {
                    "status": "skipped",
                    "reason": "already_in_pool",
                    "candidate_id": str(row.id),
                }
            sim = self._match_similarity(
                question_hint,
                row.representative_question,
                hint_vec,
                list(row.question_embedding or []) or None,
            )
            if sim > best_sim:
                best_sim = sim
                best_id = row.id
                best_row = row

        if best_row is not None and best_sim >= threshold:
            # 归簇：追加事件并重算计数
            events = list(best_row.support_events or [])
            events.append(event)
            counts = recount_from_events(events)
            variants = list(best_row.question_variants or [])
            if question_hint not in variants:
                variants.append(question_hint)
            await self.db.execute(
                text(
                    """
                    UPDATE knowledge_candidates
                    SET support_events = :events::jsonb,
                        question_variants = :variants::jsonb,
                        answer_draft = COALESCE(answer_draft, :draft),
                        support_count = :support_count,
                        praise_count = :praise_count,
                        accept_count = :accept_count,
                        distinct_users = :distinct_users,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :cid::uuid
                    """
                ),
                {
                    "events": _dump_json(events),
                    "variants": _dump_json(variants),
                    "draft": answer_draft,
                    **counts,
                    "cid": str(best_id),
                },
            )
            log.info(
                "candidates.clustered",
                candidate_id=str(best_id),
                source_type=source_type,
                similarity=round(best_sim, 3),
                support_count=counts["support_count"],
            )
            return {"status": "queued", "candidate_id": str(best_id)}

        # 未归簇 → 已晋升同簇检查（留痕，不再新建）
        promoted = (
            await self.db.execute(
                text(
                    """
                    SELECT id FROM knowledge_candidates
                    WHERE status = 'promoted'
                      AND (:tid::uuid IS NULL OR tenant_id = :tid::uuid)
                    ORDER BY last_seen_at DESC
                    LIMIT 100
                    """
                ),
                {"tid": self._tenant_id},
            )
        ).fetchall()
        for (pid,) in promoted:
            prow = (
                await self.db.execute(
                    text(
                        "SELECT representative_question, question_embedding, "
                        "support_events FROM knowledge_candidates "
                        "WHERE id = :pid::uuid"
                    ),
                    {"pid": str(pid)},
                )
            ).fetchone()
            if prow is None:
                continue
            events = list(prow.support_events or [])
            if any(
                e.get("source_type") == source_type
                and str(e.get("source_id")) == str(source_id)
                for e in events
            ):
                continue
            sim = self._match_similarity(
                question_hint,
                prow.representative_question,
                hint_vec,
                list(prow.question_embedding or []) or None,
            )
            if sim >= threshold:
                # 晋升后同簇新信号：向已发布 FAQ 的 skipped 留痕
                events.append(event)
                await self.db.execute(
                    text(
                        "UPDATE knowledge_candidates "
                        "SET support_events = :events::jsonb, updated_at = NOW() "
                        "WHERE id = :pid::uuid"
                    ),
                    {"events": _dump_json(events), "pid": str(pid)},
                )
                log.info(
                    "candidates.post_promotion_signal",
                    candidate_id=str(pid),
                    source_type=source_type,
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate_existing",
                    "candidate_id": str(pid),
                }

        # 新建候选
        embedding = _dump_json(hint_vec) if hint_vec else None
        new_id = uuid.uuid4()
        await self.db.execute(
            text(
                """
                INSERT INTO knowledge_candidates (
                    id, tenant_id, asset_type, status, representative_question,
                    question_embedding, question_variants, answer_draft,
                    support_events, support_count, praise_count, accept_count,
                    distinct_users
                ) VALUES (
                    :id::uuid, :tid::uuid, 'chat_faq', 'candidate', :question,
                    :embedding::jsonb, :variants::jsonb, :draft,
                    :events::jsonb, :support_count, :praise_count, :accept_count,
                    :distinct_users
                )
                """
            ),
            {
                "id": str(new_id),
                "tid": self._tenant_id,
                "question": question_hint,
                "embedding": embedding,
                "variants": _dump_json([question_hint]),
                "draft": answer_draft,
                "events": _dump_json([event]),
                **recount_from_events([event]),
            },
        )
        log.info(
            "candidates.created",
            candidate_id=str(new_id),
            source_type=source_type,
        )
        return {"status": "queued", "candidate_id": str(new_id)}

    # ------------------------------------------------------------------
    # 晋升扫描（beat）
    # ------------------------------------------------------------------

    async def list_eligible(self, limit: int = 10) -> list[Any]:
        """查询达到晋升门槛的候选 — support ≥ N 且 distinct_users ≥ M。"""
        settings = get_settings()
        return list(
            (
                await self.db.execute(
                    text(
                        """
                        SELECT id, tenant_id, representative_question,
                               question_variants, answer_draft, support_events,
                               support_count, praise_count, accept_count,
                               distinct_users, first_seen_at
                        FROM knowledge_candidates
                        WHERE status = 'candidate'
                          AND support_count >= :min_support
                          AND distinct_users >= :min_users
                        ORDER BY last_seen_at DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "min_support": settings.CHAT_FAQ_PROMOTE_MIN_SUPPORT,
                        "min_users": settings.CHAT_FAQ_PROMOTE_MIN_USERS,
                        "limit": limit,
                    },
                )
            ).fetchall()
        )

    async def promote_candidate(
        self,
        candidate: Any,
        target_kb_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        """晋升单个候选 — 查重 → 提取/沉淀 → 冲突检测 → 审批提交。

        每簇一次 LLM 提取（有 answer_draft 的采纳簇免 LLM）。
        状态流转：candidate → promoting → promoted / dismissed。
        失败时由调用方回滚事务，候选留在池内下轮重试。
        """
        if self._compounding is None:
            raise ValueError("晋升需要注入 KnowledgeCompoundingService")

        cid = candidate.id
        rep_question = candidate.representative_question
        events = list(candidate.support_events or [])

        # 1. 解析答案来源：采纳草稿优先，否则取首个好评事件的 assistant 消息
        answer = candidate.answer_draft
        if not answer:
            msg_id = next(
                (
                    e.get("message_id")
                    for e in events
                    if e.get("source_type") == "chat_feedback" and e.get("message_id")
                ),
                None,
            )
            if msg_id:
                mrow = (
                    await self.db.execute(
                        text("SELECT content FROM messages WHERE id = :mid::uuid"),
                        {"mid": str(msg_id)},
                    )
                ).fetchone()
                answer = mrow.content if mrow else None
        if not answer:
            log.warning(
                "candidates.promote_no_answer_source", candidate_id=str(cid)
            )
            return {"status": "skipped", "reason": "no_answer_source", "id": str(cid)}

        # 2. 标记晋升中（防并发重复晋升）
        await self.db.execute(
            text(
                "UPDATE knowledge_candidates SET status = 'promoting', "
                "updated_at = NOW() WHERE id = :cid::uuid AND status = 'candidate'"
            ),
            {"cid": str(cid)},
        )

        # 3. 权威查重（嵌入余弦 vs FAQ KB 已发布文档标题）— 拦截则驳回留痕
        from app.services.knowledge_compounding.distillation_gate import (
            DistillationGate,
        )

        gate = DistillationGate(self.db, tenant_id=self._tenant_id)
        verdict = await gate.check_after_extract(rep_question, target_kb_id)
        if verdict.get("action") == "skip":
            reason = verdict.get("reason") or "duplicate_existing"
            await self.db.execute(
                text(
                    "UPDATE knowledge_candidates SET status = 'dismissed', "
                    "dismissed_reason = :reason, updated_at = NOW() "
                    "WHERE id = :cid::uuid"
                ),
                {"reason": reason, "cid": str(cid)},
            )
            log.info(
                "candidates.promote_dedup_blocked",
                candidate_id=str(cid),
                reason=reason,
            )
            return {"status": "dismissed", "reason": reason, "id": str(cid)}

        # 4. 提取 Q-A（每簇一次 LLM；草稿存在时直接降级路径复用原文）
        context = {
            "user_query": rep_question,
            "assistant_answer": answer,
            "feedback_content": "",
            "history": [],
        }
        extracted = await self._compounding._llm_extract_faq(context)
        question = (extracted.get("question") or "").strip() or rep_question
        answer = (extracted.get("answer") or "").strip() or answer

        # 5. 沉淀 + 冲突检测 + 审批提交（复用既有 5 步框架，不改语义）
        owner_id = next(
            (
                uuid.UUID(str(e["user_id"]))
                for e in events
                if e.get("user_id")
            ),
            None,
        )
        if owner_id is None:
            raise ValueError(f"候选 {cid} 无可归属用户，无法创建文档")

        from app.models.knowledge_compounding import CompoundingTask

        task = CompoundingTask(
            task_type="promotion",
            status="running",
            trigger_source="candidate_pool",
        )
        self.db.add(task)
        await self.db.flush()

        asset = await self._compounding._precipitate_faq_asset(
            question=question,
            answer=answer,
            source_type="candidate_pool",
            source_id=cid,
            owner_id=owner_id,
            target_kb_id=target_kb_id,
            task_id=task.id,
            tags=extracted.get("tags", []),
            confidence=extracted.get("confidence", 0.8),
        )
        conflicts = await self._compounding._detect_conflicts_for_assets([asset])
        await self._compounding._submit_faq_for_review(
            asset=asset,
            target_kb_id=target_kb_id,
            conflict_count=len(conflicts),
            support={
                "support_count": candidate.support_count,
                "praise_count": candidate.praise_count,
                "accept_count": candidate.accept_count,
                "distinct_users": candidate.distinct_users,
            },
        )

        # 6. 回填晋升结果
        await self.db.execute(
            text(
                "UPDATE knowledge_candidates SET status = 'promoted', "
                "promoted_asset_id = :aid::uuid, promoted_doc_id = :did::uuid, "
                "updated_at = NOW() WHERE id = :cid::uuid"
            ),
            {
                "aid": str(asset.id),
                "did": str(asset.doc_id) if asset.doc_id else None,
                "cid": str(cid),
            },
        )
        log.info(
            "candidates.promoted",
            candidate_id=str(cid),
            asset_id=str(asset.id),
            conflicts=len(conflicts),
        )
        return {
            "status": "promoted",
            "id": str(cid),
            "asset_id": str(asset.id),
            "conflicts": len(conflicts),
        }

    # ------------------------------------------------------------------
    # 过期 / 驳回 / 列表
    # ------------------------------------------------------------------

    async def expire_stale(self) -> int:
        """TTL 过期 — first_seen 超期未达标的 candidate 置 expired（不删除）。"""
        settings = get_settings()
        result = await self.db.execute(
            text(
                """
                UPDATE knowledge_candidates SET status = 'expired', updated_at = NOW()
                WHERE status = 'candidate'
                  AND first_seen_at < NOW() - (:ttl_days || ' days')::interval
                """
            ),
            {"ttl_days": settings.CHAT_FAQ_CANDIDATE_TTL_DAYS},
        )
        return result.rowcount or 0

    async def dismiss(self, candidate_id: uuid.UUID | str, reason: str) -> bool:
        """管理端手动驳回 — 仅 candidate 状态可驳回。返回是否驳回成功。"""
        result = await self.db.execute(
            text(
                """
                UPDATE knowledge_candidates
                SET status = 'dismissed', dismissed_reason = :reason, updated_at = NOW()
                WHERE id = :cid::uuid AND status = 'candidate'
                  AND (:tid::uuid IS NULL OR tenant_id = :tid::uuid)
                """
            ),
            {"reason": reason, "cid": str(candidate_id), "tid": self._tenant_id},
        )
        return bool(result.rowcount)

    async def list_candidates(
        self,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Any], int]:
        """管理端分页查询 — 可按状态过滤（租户隔离）。"""
        conditions = ["(:tid::uuid IS NULL OR tenant_id = :tid::uuid)"]
        params: dict[str, Any] = {"tid": self._tenant_id}
        if status:
            conditions.append("status = :status")
            params["status"] = status
        where_clause = " AND ".join(conditions)

        total = (
            await self.db.execute(
                text(f"SELECT count(*) FROM knowledge_candidates WHERE {where_clause}"),
                params,
            )
        ).scalar() or 0

        rows = list(
            (
                await self.db.execute(
                    text(
                        f"SELECT * FROM knowledge_candidates WHERE {where_clause} "
                        "ORDER BY last_seen_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {**params, "limit": size, "offset": (page - 1) * size},
                )
            ).fetchall()
        )
        return rows, int(total)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _build_event(
        source_type: str,
        source_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        message_id: uuid.UUID | str | None,
    ) -> dict[str, Any]:
        """构造支持事件 — 与 MemoryFact verdict 证据结构预留同构。"""
        return {
            "source_type": source_type,
            "source_id": str(source_id),
            "signal": "praise" if source_type == "chat_feedback" else "accepted",
            "user_id": str(user_id) if user_id else None,
            "message_id": str(message_id) if message_id else None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _match_similarity(
        hint: str,
        rep_question: str,
        hint_vec: list[float] | None,
        rep_vec: list[float] | None,
    ) -> float:
        """归簇相似度 — 双方有嵌入用余弦，否则回退词汇相似度。"""
        from app.services.knowledge_compounding.distillation_gate import (
            cosine_similarity,
            lexical_similarity,
        )

        if hint_vec and rep_vec:
            return cosine_similarity(hint_vec, rep_vec)
        return lexical_similarity(hint, rep_question)

    @staticmethod
    async def _embed_question(question: str) -> list[float] | None:
        """嵌入代表问题 — 失败返回 None（回退词汇相似度，不阻塞入池）。"""
        from app.llm.embedder import get_embedder

        try:
            embedder = get_embedder()
            vecs = await embedder.embed([question])
            return vecs[0] if vecs else None
        except Exception as exc:
            log.warning(
                "candidates.embed_failed_fallback_lexical", error=str(exc)[:200]
            )
            return None


def _dump_json(value: Any) -> str | None:
    """JSONB 参数序列化 — None 原样传（SQL 侧 ::jsonb 转换）。"""
    import json

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)
