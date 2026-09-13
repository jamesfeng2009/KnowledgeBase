"""沉淀候选池测试 — 覆盖加权支持度 / 归簇 / 幂等 / 晋升 / 过期 / 驳回 / API。

测试覆盖：
- TestRecountFromEvents: 加权口径（praise=1, accepted=2 — D1）与用户并集
- TestUpsertFromSignal: 信号入池
    - 新建候选（praise/accepted 分别计权）
    - 归簇：相似问题追加事件并重算计数（D1 加权累加）
    - 幂等：(source_type, source_id) 已在池 → Outbox 重试不重复计数
    - 未归簇新建（低于阈值）
    - 晋升后同簇新信号 → skipped/duplicate_existing 留痕
    - 嵌入不可用回退词汇相似度（fail-open）
- TestPromoteCandidate: 晋升流程
    - 缺 compounding 注入 → ValueError
    - 无答案来源 → skipped
    - 权威查重拦截 → dismissed 留痕
    - 全链路成功 → promoted + 审批携带支持度
    - 无可归属用户 → ValueError
- TestExpireAndDismiss: TTL 过期 rowcount / 手动驳回布尔返回
- TestListCandidates: 分页 + 状态过滤
- TestCandidateApiHelpers: 权限门槛 / 序列化
- TestScheduledTask: 配置跳过 / 异步晋升-过期编排 / 单簇失败隔离
- TestBeatRegistration: beat 注册与 30 分钟周期
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings

# ------------------------------------------------------------------
# Mock celery before importing app modules（与 test_knowledge_compounding 一致）
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# 辅助
# ======================================================================


def _result(
    rows: list | None = None,
    row: Any = None,
    scalar: Any = None,
    rowcount: int = 0,
):
    """构造 mock DB execute 结果 — 支持 fetchall/fetchone/scalar/rowcount。"""
    m = MagicMock()
    m.fetchall = MagicMock(return_value=rows or [])
    m.fetchone = MagicMock(return_value=row)
    m.scalar = MagicMock(return_value=scalar)
    m.rowcount = rowcount
    return m


def _cand_row(
    q: str = "公司差旅报销标准是什么",
    events: list | None = None,
    variants: list | None = None,
    cid: uuid.UUID | None = None,
    embedding: list | None = None,
):
    """构造活跃候选行替身（SQL Row 属性访问语义）。"""
    return SimpleNamespace(
        id=cid or uuid.uuid4(),
        representative_question=q,
        question_embedding=embedding,
        question_variants=variants if variants is not None else [q],
        answer_draft=None,
        support_events=events if events is not None else [],
    )


def _praise_event(source_id: str = "fb-1", user_id: str | None = "11111111-1111-4111-8111-111111111111") -> dict:
    return {
        "source_type": "chat_feedback",
        "source_id": source_id,
        "signal": "praise",
        "user_id": user_id,
        "message_id": "m-1",
        "ts": "2026-09-12T00:00:00+00:00",
    }


def _accept_event(source_id: str = "acc-1", user_id: str | None = "11111111-1111-4111-8111-111111111111") -> dict:
    return {
        "source_type": "qa_accepted",
        "source_id": source_id,
        "signal": "accepted",
        "user_id": user_id,
        "message_id": "m-1",
        "ts": "2026-09-12T00:00:00+00:00",
    }


def _make_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _make_pool(db: MagicMock, tenant_id=None, compounding=None):
    from app.services.knowledge_compounding.knowledge_candidates import (
        KnowledgeCandidatePool,
    )

    return KnowledgeCandidatePool(db, tenant_id=tenant_id, compounding=compounding)


# ======================================================================
# 加权支持度（D1）
# ======================================================================


class TestRecountFromEvents:
    def test_empty_events(self):
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events([])
        assert counts == {
            "support_count": 0,
            "praise_count": 0,
            "accept_count": 0,
            "distinct_users": 0,
        }

    def test_praise_weight_is_1(self):
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events([_praise_event(), _praise_event(source_id="fb-2")])
        assert counts["praise_count"] == 2
        assert counts["support_count"] == 2

    def test_accept_weight_is_2(self):
        """D1：采纳信号计双倍权重。"""
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events([_accept_event()])
        assert counts["accept_count"] == 1
        assert counts["support_count"] == 2

    def test_mixed_weights(self):
        """1 条采纳(2) + 2 条好评(1+1) → 加权支持度 4。"""
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events(
            [_accept_event(), _praise_event("fb-1"), _praise_event("fb-2")]
        )
        assert counts["support_count"] == 4
        assert counts["praise_count"] == 2
        assert counts["accept_count"] == 1

    def test_distinct_users_union_and_anonymous(self):
        """distinct_users 为提交者并集；无 user_id 事件计入计数不贡献用户。"""
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events(
            [
                _praise_event("fb-1", user_id="u1"),
                _praise_event("fb-2", user_id="u1"),  # 同用户重复
                _accept_event("acc-1", user_id="u2"),
                _praise_event("fb-3", user_id=None),  # 匿名
            ]
        )
        assert counts["distinct_users"] == 2
        assert counts["support_count"] == 1 + 1 + 2 + 1

    def test_unknown_source_type_ignored(self):
        from app.services.knowledge_compounding.knowledge_candidates import (
            recount_from_events,
        )

        counts = recount_from_events([{"source_type": "unknown", "source_id": "x"}])
        assert counts["support_count"] == 0


# ======================================================================
# 信号入池
# ======================================================================


class TestUpsertFromSignal:
    @pytest.mark.asyncio
    async def test_new_candidate_praise(self):
        """无相似簇 → 新建候选，praise 计权 1。"""
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[_result(rows=[]), _result(rows=[]), _result(rowcount=1)]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-new",
                user_id="u1",
                question_hint="公司差旅报销标准是什么",
            )

        assert out["status"] == "queued"
        insert_call = db.execute.call_args_list[2]
        assert "INSERT INTO knowledge_candidates" in str(insert_call[0][0])
        params = insert_call[0][1]
        assert params["support_count"] == 1
        assert params["praise_count"] == 1
        assert params["distinct_users"] == 1

    @pytest.mark.asyncio
    async def test_new_candidate_accept_weight_2(self):
        """D1：采纳信号新建候选计权 2。"""
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[_result(rows=[]), _result(rows=[]), _result(rowcount=1)]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            await pool.upsert_from_signal(
                source_type="qa_accepted",
                source_id="acc-new",
                user_id="u1",
                question_hint="公司差旅报销标准是什么",
            )
        params = db.execute.call_args_list[2][0][1]
        assert params["support_count"] == 2
        assert params["accept_count"] == 1

    @pytest.mark.asyncio
    async def test_idempotent_already_in_pool(self):
        """(source_type, source_id) 已在池 → skipped，Outbox 重试不重复计数。"""
        existing_id = uuid.uuid4()
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[
                _result(
                    rows=[
                        _cand_row(
                            cid=existing_id,
                            events=[_praise_event("fb-dup", user_id="u1")],
                        )
                    ]
                ),
            ]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-dup",
                user_id="u1",
                question_hint="公司差旅报销标准是什么",
            )

        assert out == {
            "status": "skipped",
            "reason": "already_in_pool",
            "candidate_id": str(existing_id),
        }
        # 仅发生 1 次查询，无 UPDATE/INSERT
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_cluster_merge_recounts(self):
        """相似问题归簇 — 追加事件、变体去重追加、加权重算。"""
        existing_id = uuid.uuid4()
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[
                _result(
                    rows=[
                        _cand_row(
                            cid=existing_id,
                            events=[_praise_event("fb-1")],  # 簇内已有 1 条好评
                        )
                    ]
                ),
                _result(rowcount=1),
            ]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-2",
                user_id="22222222-2222-4222-8222-222222222222",
                question_hint="公司差旅报销标准是什么？",  # 归一化后与代表问题一致
            )

        assert out == {"status": "queued", "candidate_id": str(existing_id)}
        update_call = db.execute.call_args_list[1]
        assert "UPDATE knowledge_candidates" in str(update_call[0][0])
        params = update_call[0][1]
        events = json.loads(params["events"])
        assert len(events) == 2
        # 原 praise(u1)=1 + 新 praise(u2)=1 → 加权 2、用户并集 2
        assert params["support_count"] == 2
        assert params["distinct_users"] == 2
        variants = json.loads(params["variants"])
        assert len(variants) == 2  # 新变体追加

    @pytest.mark.asyncio
    async def test_no_cluster_below_threshold_creates_new(self):
        """不相似问题不归簇 — 走新建路径。"""
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[
                _result(rows=[_cand_row(q="公司差旅报销标准是什么")]),
                _result(rows=[]),
                _result(rowcount=1),
            ]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-3",
                user_id="u3",
                question_hint="今天午餐吃什么好",
            )
        assert out["status"] == "queued"
        assert "INSERT INTO knowledge_candidates" in str(db.execute.call_args_list[2][0][0])

    @pytest.mark.asyncio
    async def test_post_promotion_signal_traced(self):
        """晋升后同簇新信号 — 向已发布簇留痕，返回 duplicate_existing。"""
        pid = uuid.uuid4()
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[
                _result(rows=[]),  # 活跃候选为空
                _result(rows=[(pid,)]),  # 已晋升簇
                _result(
                    row=_cand_row(
                        cid=pid,
                        q="公司差旅报销标准是什么",
                        events=[_praise_event("fb-old")],
                    )
                ),
                _result(rowcount=1),  # UPDATE 留痕
            ]
        )
        pool = _make_pool(db)
        with patch.object(
            type(pool), "_embed_question", new=AsyncMock(return_value=None)
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-new",
                user_id="22222222-2222-4222-8222-222222222222",
                question_hint="公司差旅报销标准是什么？",
            )
        assert out == {
            "status": "skipped",
            "reason": "duplicate_existing",
            "candidate_id": str(pid),
        }

    @pytest.mark.asyncio
    async def test_embed_failure_falls_back_to_lexical(self):
        """嵌入不可用 → 回退词汇相似度，不阻塞入池（fail-open）。"""
        db = _make_db()
        db.execute = AsyncMock(
            side_effect=[
                _result(rows=[_cand_row(cid=uuid.uuid4())]),
                _result(rowcount=1),  # 归簇 UPDATE
            ],
        )
        pool = _make_pool(db)
        with patch(
            "app.llm.embedder.get_embedder",
            side_effect=RuntimeError("embedder unavailable"),
        ):
            out = await pool.upsert_from_signal(
                source_type="chat_feedback",
                source_id="fb-4",
                user_id="22222222-2222-4222-8222-222222222222",
                question_hint="公司差旅报销标准是什么？",
            )
        assert out["status"] == "queued"  # 归簇成功（词汇相似度路径）


# ======================================================================
# 晋升流程
# ======================================================================


def _eligible_candidate(events: list | None = None, draft: str | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        representative_question="公司差旅报销标准是什么",
        question_variants=["公司差旅报销标准是什么"],
        answer_draft=draft,
        support_events=events if events is not None else [_praise_event()],
        support_count=4,
        praise_count=2,
        accept_count=1,
        distinct_users=2,
        first_seen_at=None,
    )


def _make_compounding():
    svc = MagicMock()
    svc._llm_extract_faq = AsyncMock(
        return_value={
            "question": "公司差旅报销标准是什么？",
            "answer": "经济舱按实报销，酒店限额 500 元每晚。",
            "tags": ["差旅"],
            "confidence": 0.9,
        }
    )
    asset = MagicMock(id=uuid.uuid4(), doc_id=uuid.uuid4())
    svc._precipitate_faq_asset = AsyncMock(return_value=asset)
    svc._detect_conflicts_for_assets = AsyncMock(return_value=[])
    svc._submit_faq_for_review = AsyncMock()
    return svc, asset


class TestPromoteCandidate:
    @pytest.mark.asyncio
    async def test_requires_compounding(self):
        pool = _make_pool(_make_db())
        with pytest.raises(ValueError):
            await pool.promote_candidate(_eligible_candidate(), target_kb_id=uuid.uuid4())

    @pytest.mark.asyncio
    async def test_no_answer_source_skipped(self):
        """无草稿且好评事件无 message_id → skipped/no_answer_source。"""
        db = _make_db()
        cand = _eligible_candidate(
            events=[{**_praise_event(), "message_id": None}], draft=None
        )
        db.execute = AsyncMock(return_value=_result(row=None))
        pool = _make_pool(db, compounding=MagicMock())
        out = await pool.promote_candidate(cand, target_kb_id=uuid.uuid4())
        assert out == {"status": "skipped", "reason": "no_answer_source", "id": str(cand.id)}

    @pytest.mark.asyncio
    async def test_dedup_blocked_dismisses(self):
        """权威查重拦截 → dismissed 留痕，不走沉淀。"""
        db = _make_db()
        cand = _eligible_candidate(draft="经济舱按实报销。")
        svc, _asset = _make_compounding()
        pool = _make_pool(db, compounding=svc)

        gate_instance = MagicMock()
        gate_instance.check_after_extract = AsyncMock(
            return_value={"action": "skip", "reason": "duplicate_existing"}
        )
        with patch(
            "app.services.knowledge_compounding.distillation_gate.DistillationGate",
            return_value=gate_instance,
        ):
            out = await pool.promote_candidate(cand, target_kb_id=uuid.uuid4())

        assert out == {"status": "dismissed", "reason": "duplicate_existing", "id": str(cand.id)}
        svc._precipitate_faq_asset.assert_not_awaited()
        # 状态置 dismissed 且留痕
        dismiss_sql = str(db.execute.call_args_list[1][0][0])
        assert "dismissed" in dismiss_sql

    @pytest.mark.asyncio
    async def test_promote_success_submits_with_support(self):
        """全链路晋升 — 提取/沉淀/冲突/审批，审批携带候选支持度。"""
        db = _make_db()
        cand = _eligible_candidate(draft=None)
        cand.support_events = [_praise_event()]  # message_id 存在 → 可回查答案
        svc, asset = _make_compounding()
        pool = _make_pool(db, compounding=svc)

        db.execute = AsyncMock(
            side_effect=[
                _result(row=SimpleNamespace(content="经济舱按实报销。")),  # messages 查询
                _result(rowcount=1),  # promoting
                _result(rowcount=1),  # promoted 回填
            ]
        )
        gate_instance = MagicMock()
        gate_instance.check_after_extract = AsyncMock(return_value={"action": "pass"})
        with patch(
            "app.services.knowledge_compounding.distillation_gate.DistillationGate",
            return_value=gate_instance,
        ):
            out = await pool.promote_candidate(cand, target_kb_id=uuid.uuid4())

        assert out["status"] == "promoted"
        assert out["asset_id"] == str(asset.id)
        # 每簇一次 LLM 提取
        svc._llm_extract_faq.assert_awaited_once()
        svc._precipitate_faq_asset.assert_awaited_once()
        svc._submit_faq_for_review.assert_awaited_once()
        submit_kwargs = svc._submit_faq_for_review.await_args.kwargs
        assert submit_kwargs["support"] == {
            "support_count": 4,
            "praise_count": 2,
            "accept_count": 1,
            "distinct_users": 2,
        }
        # 状态回填 promoted
        assert "promoted" in str(db.execute.call_args_list[2][0][0])

    @pytest.mark.asyncio
    async def test_no_owner_raises(self):
        """事件均无 user_id → 无法归属文档 owner，抛错回滚。"""
        db = _make_db()
        cand = _eligible_candidate(
            events=[{**_praise_event(), "user_id": None}], draft="草稿答案"
        )
        svc, _ = _make_compounding()
        pool = _make_pool(db, compounding=svc)
        db.execute = AsyncMock(
            side_effect=[
                _result(rowcount=1),  # promoting
            ]
        )
        gate_instance = MagicMock()
        gate_instance.check_after_extract = AsyncMock(return_value={"action": "pass"})
        with patch(
            "app.services.knowledge_compounding.distillation_gate.DistillationGate",
            return_value=gate_instance,
        ):
            with pytest.raises(ValueError):
                await pool.promote_candidate(cand, target_kb_id=uuid.uuid4())


# ======================================================================
# 过期 / 驳回 / 列表
# ======================================================================


class TestExpireAndDismiss:
    @pytest.mark.asyncio
    async def test_expire_stale_returns_rowcount(self):
        db = _make_db()
        db.execute = AsyncMock(return_value=_result(rowcount=3))
        pool = _make_pool(db)
        assert await pool.expire_stale() == 3

    @pytest.mark.asyncio
    async def test_expire_stale_zero_on_none(self):
        db = _make_db()
        db.execute = AsyncMock(return_value=_result(rowcount=None))
        pool = _make_pool(db)
        assert await pool.expire_stale() == 0

    @pytest.mark.asyncio
    async def test_dismiss_true_and_false(self):
        db = _make_db()
        db.execute = AsyncMock(return_value=_result(rowcount=1))
        pool = _make_pool(db, tenant_id=uuid.uuid4())
        assert await pool.dismiss(uuid.uuid4(), "质量不足") is True

        db2 = _make_db()
        db2.execute = AsyncMock(return_value=_result(rowcount=0))
        pool2 = _make_pool(db2, tenant_id=uuid.uuid4())
        assert await pool2.dismiss(uuid.uuid4(), "质量不足") is False


class TestListCandidates:
    @pytest.mark.asyncio
    async def test_pagination_and_total(self):
        db = _make_db()
        rows = [_cand_row(), _cand_row()]
        db.execute = AsyncMock(
            side_effect=[_result(scalar=5), _result(rows=rows)]
        )
        pool = _make_pool(db, tenant_id=uuid.uuid4())
        got, total = await pool.list_candidates(status="candidate", page=2, size=2)
        assert total == 5
        assert got == rows
        # 第二次查询带 LIMIT/OFFSET
        page_sql = str(db.execute.call_args_list[1][0][0])
        assert "LIMIT" in page_sql and "OFFSET" in page_sql

    @pytest.mark.asyncio
    async def test_no_status_filter(self):
        db = _make_db()
        db.execute = AsyncMock(side_effect=[_result(scalar=0), _result(rows=[])])
        pool = _make_pool(db)
        got, total = await pool.list_candidates()
        assert total == 0 and got == []


# ======================================================================
# API 辅助
# ======================================================================


class TestCandidateApiHelpers:
    def test_require_admin_rejects_editor(self):
        from fastapi import HTTPException

        from app.api.v1.knowledge_candidates import _require_admin

        editor = SimpleNamespace(role="editor")
        with pytest.raises(HTTPException) as exc:
            _require_admin(editor)
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("role", ["admin", "kb_admin"])
    def test_require_admin_allows(self, role):
        from app.api.v1.knowledge_candidates import _require_admin

        _require_admin(SimpleNamespace(role=role))  # 不抛即通过

    def test_candidate_to_dict(self):
        from app.api.v1.knowledge_candidates import _candidate_to_dict

        cid = uuid.uuid4()
        row = SimpleNamespace(
            id=cid,
            tenant_id=None,
            asset_type="chat_faq",
            status="candidate",
            representative_question="Q",
            question_variants=["Q"],
            answer_draft=None,
            support_events=[_praise_event()],
            support_count=1,
            praise_count=1,
            accept_count=0,
            distinct_users=1,
            first_seen_at=None,
            last_seen_at=None,
            promoted_asset_id=None,
            promoted_doc_id=None,
            dismissed_reason=None,
            created_at=None,
            updated_at=None,
        )
        d = _candidate_to_dict(row)
        assert d["id"] == str(cid)
        assert d["support_count"] == 1
        assert d["support_events"][0]["source_id"] == "fb-1"
        assert d["first_seen_at"] is None


# ======================================================================
# 定时任务
# ======================================================================


def _task_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.CHAT_FAQ_COMPOUNDING_ENABLED = True
    s.CHAT_FAQ_CANDIDATE_POOL_ENABLED = True
    s.FAQ_KB_ID = str(uuid.uuid4())
    s.CHAT_FAQ_PROMOTE_BATCH_SIZE = 10
    s.CHAT_FAQ_CANDIDATE_TTL_DAYS = 30
    s.CHAT_FAQ_PROMOTE_MIN_SUPPORT = 3
    s.CHAT_FAQ_PROMOTE_MIN_USERS = 2
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestScheduledTask:
    def test_disabled_by_config(self):
        from tasks.scheduled_tasks import promote_knowledge_candidates

        settings = _task_settings(CHAT_FAQ_CANDIDATE_POOL_ENABLED=False)
        with patch("app.config.get_settings", return_value=settings):
            out = promote_knowledge_candidates()
        assert out == {"status": "skipped", "reason": "candidate_pool_disabled"}

    def test_skipped_without_faq_kb_id(self):
        from tasks.scheduled_tasks import promote_knowledge_candidates

        settings = _task_settings(FAQ_KB_ID="")
        with patch("app.config.get_settings", return_value=settings):
            out = promote_knowledge_candidates()
        assert out == {"status": "skipped", "reason": "faq_kb_id_not_configured"}

    @pytest.mark.asyncio
    async def test_async_flow_expire_then_promote(self):
        """编排顺序：先 TTL 过期（独立事务），再逐簇晋升（每簇独立事务）。"""
        from tasks.scheduled_tasks import _promote_knowledge_candidates_async

        db = _make_db()
        row = _eligible_candidate()
        row.tenant_id = None

        pool_instance = MagicMock()
        pool_instance.expire_stale = AsyncMock(return_value=2)
        pool_instance.list_eligible = AsyncMock(return_value=[row])
        pool_instance.promote_candidate = AsyncMock(
            return_value={"status": "promoted", "id": str(row.id), "asset_id": "a", "conflicts": 0}
        )

        @asynccontextmanager
        async def fake_session():
            yield db

        svc_cls = MagicMock()
        with patch("app.database.task_db_session", fake_session), patch(
            "app.llm.factory.get_llm_provider", return_value=MagicMock()
        ), patch(
            "app.services.knowledge_compounding.KnowledgeCompoundingService", svc_cls
        ), patch(
            "app.services.knowledge_compounding.knowledge_candidates.KnowledgeCandidatePool",
            MagicMock(return_value=pool_instance),
        ):
            out = await _promote_knowledge_candidates_async(_task_settings())

        assert out["status"] == "success"
        assert out["expired"] == 2
        assert out["promoted"] == 1
        assert out["failed"] == 0
        # 过期事务先提交，晋升事务随后提交
        assert db.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_async_flow_single_failure_isolated(self):
        """单簇晋升失败只回滚该簇，不影响汇总计数。"""
        from tasks.scheduled_tasks import _promote_knowledge_candidates_async

        db = _make_db()
        row = _eligible_candidate()
        row.tenant_id = None

        pool_instance = MagicMock()
        pool_instance.expire_stale = AsyncMock(return_value=0)
        pool_instance.list_eligible = AsyncMock(return_value=[row])
        pool_instance.promote_candidate = AsyncMock(side_effect=RuntimeError("llm boom"))

        @asynccontextmanager
        async def fake_session():
            yield db

        with patch("app.database.task_db_session", fake_session), patch(
            "app.llm.factory.get_llm_provider", return_value=MagicMock()
        ), patch(
            "app.services.knowledge_compounding.KnowledgeCompoundingService", MagicMock()
        ), patch(
            "app.services.knowledge_compounding.knowledge_candidates.KnowledgeCandidatePool",
            MagicMock(return_value=pool_instance),
        ):
            out = await _promote_knowledge_candidates_async(_task_settings())

        assert out["failed"] == 1
        assert out["promoted"] == 0
        db.rollback.assert_awaited()


class TestBeatRegistration:
    def test_beat_entry_registered(self):
        """beat 每 30 分钟调度候选池任务。"""
        from celery_app import celery_app

        entry = celery_app.conf.beat_schedule.get("promote-knowledge-candidates-30min")
        assert entry is not None
        assert entry["task"] == "tasks.scheduled_tasks.promote_knowledge_candidates"
