"""微调数据飞轮测试 — data_cleaner 纯函数 + builder mock DB + API 端到端。

验证点：
- data_cleaner：PII 四类脱敏（手机号/身份证/邮箱/银行卡）、替换顺序
  （身份证优先于银行卡）、哈希去重、长度过滤；
- dataset_builder：SFT 从 QA 采纳/chat 好评构建、密级超阈剔除、DPO 配对
  （feedback/no_feedback/qa_adopted）、Embedding 三元组、Golden 上限；
- API：Celery 提交 mock、非 admin 拒绝、preview/download 读临时文件、
  多租户隔离（按 request.state.tenant_id 过滤查询）。

mock 风格照 test_quality_service.py：AsyncMock/MagicMock + SimpleNamespace，
不依赖外部服务。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.finetune.data_cleaner import (
    MAX_SAMPLE_CHARS,
    MIN_SAMPLE_CHARS,
    DedupFilter,
    check_length,
    content_hash,
    mask_pii,
    new_filtered_stats,
)
from app.finetune.dataset_builder import (
    build_dpo_dataset,
    build_embedding_dataset,
    build_golden_set,
    build_sft_dataset,
)
from app.finetune.exporter import export_jsonl, make_version, read_jsonl_head


# ======================================================================
# 工具函数
# ======================================================================


def _exec_result(
    *,
    scalars: list | None = None,
    rows: list | None = None,
    scalar_one=None,
    scalar_one_or_none=None,
) -> MagicMock:
    """构造 db.execute 返回值（区分 scalars().all()/all()/scalar_one 等消费方式）。"""
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    result.all.return_value = rows or []
    result.scalar_one.return_value = scalar_one
    result.scalar_one_or_none.return_value = scalar_one_or_none
    return result


def _db_with_execute(side_effect: list) -> AsyncMock:
    """构造带 execute side_effect 的 AsyncSession mock。"""
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=side_effect)
    return db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _question(title: str = "如何重置密码", content: str = "忘记了登录密码，需要重置") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), title=title, content=content, deleted_at=None)


def _answer(content: str, *, question_id=None, is_accepted: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        question_id=question_id or uuid4(),
        content=content,
        is_accepted=is_accepted,
        deleted_at=None,
        created_at=_now(),
    )


def _feedback(fb_type: str, message_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        type=fb_type,
        related_message_id=message_id,
        created_at=_now(),
    )


def _message(
    role: str,
    content: str,
    *,
    conversation_id=None,
    sources: list | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        conversation_id=conversation_id or uuid4(),
        role=role,
        content=content,
        sources=sources,
        created_at=created_at or _now(),
    )


def _document(
    *,
    content_text: str = "这是一篇关于密码重置流程的帮助文档内容。",
    classification: str = "internal",
    kb_id=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        kb_id=kb_id or uuid4(),
        content_text=content_text,
        classification=classification,
        deleted_at=None,
    )


def _export_record(**overrides) -> SimpleNamespace:
    """构造 DatasetExport 记录 mock（API 序列化用）。"""
    data = {
        "id": uuid4(),
        "tenant_id": None,
        "dataset_type": "sft",
        "version": "v20260808-100000",
        "status": "completed",
        "sample_count": 3,
        "filtered_stats": {"duplicate": 1},
        "file_size_bytes": 128,
        "celery_task_id": "celery-task-x",
        "created_at": _now(),
        "completed_at": _now(),
        "file_path": None,
        "params": {"days": 90},
    }
    data.update(overrides)
    return SimpleNamespace(**data)


# ======================================================================
# data_cleaner 纯函数测试
# ======================================================================


class TestMaskPii:
    """PII 四类脱敏测试。"""

    def test_mask_phone(self) -> None:
        assert mask_pii("请联系 13812345678 获取帮助") == "请联系 [PHONE] 获取帮助"

    def test_mask_idcard(self) -> None:
        text = "身份证号 11010119900307777X 已登记"
        assert mask_pii(text) == "身份证号 [IDCARD] 已登记"

    def test_mask_email(self) -> None:
        assert mask_pii("邮箱 zhang.san@example.com 备用") == "邮箱 [EMAIL] 备用"

    def test_mask_bankcard(self) -> None:
        # 16 位银行卡
        assert mask_pii("卡号 6222020200112233 工行") == "卡号 [BANKCARD] 工行"

    def test_idcard_not_eaten_by_bankcard(self) -> None:
        """替换顺序保证：18 位纯数字身份证命中 [IDCARD] 而非 [BANKCARD]。"""
        text = "证件 110101199003077718"
        assert mask_pii(text) == "证件 [IDCARD]"

    def test_no_pii_returns_original(self) -> None:
        text = "这是一条不含敏感信息的普通文本"
        assert mask_pii(text) == text

    def test_empty_text(self) -> None:
        assert mask_pii("") == ""


class TestDedupAndLength:
    """哈希去重与长度过滤测试。"""

    def test_content_hash_strip_normalized(self) -> None:
        """首尾空白归一化后哈希一致。"""
        assert content_hash("  同一段内容 \n") == content_hash("同一段内容")

    def test_dedup_filter(self) -> None:
        dedup = DedupFilter()
        assert dedup.is_duplicate("样本甲") is False
        assert dedup.is_duplicate("样本甲") is True
        assert dedup.is_duplicate("样本乙") is False

    def test_check_length_too_short(self) -> None:
        assert check_length("短", MIN_SAMPLE_CHARS, MAX_SAMPLE_CHARS) == "too_short"

    def test_check_length_too_long(self) -> None:
        assert check_length("长" * (MAX_SAMPLE_CHARS + 1)) == "too_long"

    def test_check_length_pass(self) -> None:
        assert check_length("这是一段长度合格的样本内容") is None

    def test_new_filtered_stats_keys(self) -> None:
        stats = new_filtered_stats()
        assert set(stats) == {
            "classification",
            "duplicate",
            "too_short",
            "too_long",
            "pii_masked",
            "golden_excluded",
            "sft_reserved",
        }
        assert all(v == 0 for v in stats.values())


# ======================================================================
# exporter 测试
# ======================================================================


class TestExporter:
    """JSONL 导出与读取测试。"""

    def test_export_and_read_head(self, tmp_path) -> None:
        samples = [{"idx": i, "text": f"样本{i}"} for i in range(3)]
        file_path, size = export_jsonl(
            samples, None, "sft", "v20260808-100000", base_dir=tmp_path
        )

        assert file_path == tmp_path / "default" / "v20260808-100000" / "sft.jsonl"
        assert size == file_path.stat().st_size > 0

        items = read_jsonl_head(file_path, 2)
        assert items == samples[:2]

    def test_make_version_format(self) -> None:
        version = make_version(datetime(2026, 8, 8, 10, 30, 59))
        assert version == "v20260808-103059"


# ======================================================================
# SFT 构建测试
# ======================================================================


class TestBuildSftDataset:
    """SFT 数据集构建测试（mock DB）。"""

    @pytest.mark.asyncio
    async def test_build_from_qa_adopted(self) -> None:
        """QA 社区已采纳答案 → SFT 样本（user=标题+详情，assistant=采纳回答）。"""
        # 标题/内容选取哈希落入 SFT 侧（非 Golden 桶）的文本（评审 #3 互斥）
        question = _question(title="如何申请年假", content="入职满一年，想休年假")
        adopted = _answer("在 OA 系统提交年假申请，主管审批后生效。", question_id=question.id)
        db = _db_with_execute([
            _exec_result(rows=[(adopted, question)]),  # 1. QA 采纳
            _exec_result(scalars=[]),  # 2. chat 好评反馈为空 → 提前返回
            # QA 样本无 doc_ids → 密级查询跳过
        ])

        samples, stats = await build_sft_dataset(db, None)

        assert len(samples) == 1
        sample = samples[0]
        assert [m["role"] for m in sample["messages"]] == ["system", "user", "assistant"]
        assert "如何申请年假" in sample["messages"][1]["content"]
        assert sample["messages"][2]["content"] == "在 OA 系统提交年假申请，主管审批后生效。"
        assert sample["meta"]["source"] == "qa_adopted"
        assert stats["classification"] == 0
        assert stats["golden_excluded"] == 0

    @pytest.mark.asyncio
    async def test_golden_bucket_excluded_from_sft(self) -> None:
        """评审 #3：落入 Golden 桶（哈希 % 10 == 0）的问答对不得进 SFT 训练集。"""
        # 默认 _question 文本经 SHA-256 分桶恰好落入 Golden 桶（bucket 0）
        question = _question()
        adopted = _answer("打开设置-账号安全-重置密码即可。", question_id=question.id)
        db = _db_with_execute([
            _exec_result(rows=[(adopted, question)]),
            _exec_result(scalars=[]),
        ])

        samples, stats = await build_sft_dataset(db, None)

        assert samples == []
        assert stats["golden_excluded"] == 1

    @pytest.mark.asyncio
    async def test_build_from_chat_rated(self) -> None:
        """chat 好评问答对 → SFT 样本（同会话前一条 user 消息作 prompt）。"""
        conv_id = uuid4()
        t0 = _now()
        user_msg = _message("user", "请问报销流程具体是什么？", conversation_id=conv_id, created_at=t0)
        assistant_msg = _message(
            "assistant",
            "报销流程：提交申请 → 主管审批 → 财务打款。",
            conversation_id=conv_id,
            sources=[{"doc_id": str(uuid4()), "title": "报销制度"}],
            created_at=t0,
        )
        praised_fb = _feedback("praise", assistant_msg.id)
        db = _db_with_execute([
            _exec_result(rows=[]),  # 1. QA 采纳为空
            _exec_result(scalars=[praised_fb]),  # 2. 好评反馈
            _exec_result(scalars=[assistant_msg]),  # 3. assistant 消息
            _exec_result(scalars=[user_msg]),  # 4. 会话 user 消息
            _exec_result(rows=[(assistant_msg.sources[0]["doc_id"], "internal")]),  # 5. 密级
        ])

        samples, stats = await build_sft_dataset(db, None, min_rating=4)

        assert len(samples) == 1
        assert samples[0]["messages"][1]["content"] == "请问报销流程具体是什么？"
        assert samples[0]["meta"]["source"] == "chat_rated"
        assert len(samples[0]["meta"]["doc_ids"]) == 1
        assert stats["classification"] == 0

    @pytest.mark.asyncio
    async def test_classification_filtered(self) -> None:
        """关联文档密级超阈（secret > internal）的样本被剔除。"""
        conv_id = uuid4()
        t0 = _now()
        secret_doc_id = uuid4()
        user_msg = _message("user", "核心算法的实现细节有哪些？", conversation_id=conv_id, created_at=t0)
        assistant_msg = _message(
            "assistant",
            "核心算法实现细节如下所述内容。",
            conversation_id=conv_id,
            sources=[{"doc_id": str(secret_doc_id)}],
            created_at=t0,
        )
        db = _db_with_execute([
            _exec_result(rows=[]),
            _exec_result(scalars=[_feedback("praise", assistant_msg.id)]),
            _exec_result(scalars=[assistant_msg]),
            _exec_result(scalars=[user_msg]),
            _exec_result(rows=[(secret_doc_id, "secret")]),  # 密级超阈
        ])

        samples, stats = await build_sft_dataset(
            db, None, max_classification="internal"
        )

        assert samples == []
        assert stats["classification"] == 1

    @pytest.mark.asyncio
    async def test_pii_masked_not_filtered(self) -> None:
        """PII 命中只脱敏不剔除，样本保留且内容已改写。"""
        question = _question(content="我的手机号是 13812345678，请问如何重置密码？")
        adopted = _answer("请携带工卡到前台办理重置手续。", question_id=question.id)
        db = _db_with_execute([
            _exec_result(rows=[(adopted, question)]),
            _exec_result(scalars=[]),
        ])

        samples, stats = await build_sft_dataset(db, None)

        assert len(samples) == 1
        assert "13812345678" not in samples[0]["messages"][1]["content"]
        assert "[PHONE]" in samples[0]["messages"][1]["content"]
        assert stats["pii_masked"] == 1


# ======================================================================
# DPO 构建测试
# ======================================================================


class TestBuildDpoDataset:
    """DPO 偏好对构建测试（mock DB）。"""

    @pytest.mark.asyncio
    async def test_feedback_pair(self) -> None:
        """同一 user 消息下好评 × 差评 → pair_type=feedback。"""
        conv_id = uuid4()
        t0 = _now()
        user_msg = _message("user", "请问年假一共有多少天？", conversation_id=conv_id, created_at=t0)
        chosen_msg = _message("assistant", "正式员工年假 10 天起。", conversation_id=conv_id, created_at=t0)
        rejected_msg = _message("assistant", "不太清楚具体的年假天数。", conversation_id=conv_id, created_at=t0)
        feedbacks = [
            _feedback("praise", chosen_msg.id),
            _feedback("complaint", rejected_msg.id),
        ]
        db = _db_with_execute([
            _exec_result(scalars=feedbacks),  # 1. 反馈
            _exec_result(scalars=[chosen_msg, rejected_msg]),  # 2. 反馈关联消息
            _exec_result(scalars=[user_msg, chosen_msg, rejected_msg]),  # 3. 会话全部消息
            _exec_result(rows=[]),  # 4. QA 采纳为空
            _exec_result(scalars=[]),  # 6. 高风险拦截为空（5. 未采纳查询跳过）
            # doc_ids 为空 → 密级查询跳过
        ])

        samples, stats = await build_dpo_dataset(db, None)

        assert len(samples) == 1
        sample = samples[0]
        assert sample["prompt"] == "请问年假一共有多少天？"
        assert sample["chosen"] == "正式员工年假 10 天起。"
        assert sample["rejected"] == "不太清楚具体的年假天数。"
        assert sample["meta"]["pair_type"] == "feedback"

    @pytest.mark.asyncio
    async def test_no_feedback_pair(self) -> None:
        """差评不足时以无反馈答案充当 rejected → pair_type=no_feedback。"""
        conv_id = uuid4()
        t0 = _now()
        user_msg = _message("user", "请问如何申请 VPN 权限？", conversation_id=conv_id, created_at=t0)
        chosen_msg = _message("assistant", "在 OA 系统提交 VPN 申请单即可。", conversation_id=conv_id, created_at=t0)
        neutral_msg = _message("assistant", "建议咨询 IT 服务台处理。", conversation_id=conv_id, created_at=t0)
        db = _db_with_execute([
            _exec_result(scalars=[_feedback("praise", chosen_msg.id)]),
            _exec_result(scalars=[chosen_msg]),
            _exec_result(scalars=[user_msg, chosen_msg, neutral_msg]),
            _exec_result(rows=[]),
            _exec_result(scalars=[]),
        ])

        samples, _ = await build_dpo_dataset(db, None)

        assert len(samples) == 1
        assert samples[0]["meta"]["pair_type"] == "no_feedback"
        assert samples[0]["rejected"] == "建议咨询 IT 服务台处理。"

    @pytest.mark.asyncio
    async def test_qa_adopted_pair(self) -> None:
        """QA 社区采纳 × 未采纳答案配对 → pair_type=qa_adopted。"""
        question = _question()
        adopted = _answer("标准做法是先备份再升级版本。", question_id=question.id)
        unadopted = _answer("直接升级应该没问题。", question_id=question.id, is_accepted=False)
        db = _db_with_execute([
            _exec_result(scalars=[]),  # 1. 反馈为空 → 跳过消息查询
            _exec_result(rows=[(adopted, question)]),  # 4. QA 采纳
            _exec_result(scalars=[unadopted]),  # 5. 未采纳答案
            _exec_result(scalars=[]),  # 6. 高风险拦截为空
        ])

        samples, _ = await build_dpo_dataset(db, None)

        assert len(samples) == 1
        assert samples[0]["meta"]["pair_type"] == "qa_adopted"
        assert samples[0]["chosen"] == "标准做法是先备份再升级版本。"
        assert samples[0]["rejected"] == "直接升级应该没问题。"


# ======================================================================
# Embedding 构建测试
# ======================================================================


class TestBuildEmbeddingDataset:
    """Embedding 检索三元组构建测试（mock DB）。"""

    @pytest.mark.asyncio
    async def test_triplet_built(self) -> None:
        """点击行为 → (query, pos, neg) 三元组，负例随机同库文档。"""
        kb_id = uuid4()
        pos_doc = _document(content_text="密码重置的完整流程说明文档。", kb_id=kb_id)
        neg_doc = _document(content_text="办公用品申领流程说明文档。", kb_id=kb_id)
        click_log = SimpleNamespace(
            id=uuid4(),
            query="请问如何重置登录密码？",
            clicked=True,
            clicked_doc_id=pos_doc.id,
            created_at=_now(),
        )
        db = _db_with_execute([
            _exec_result(scalars=[click_log]),  # 1. 点击行为
            _exec_result(scalars=[pos_doc]),  # 2. 正例文档
            _exec_result(scalars=[neg_doc]),  # 3. 负例候选池
        ])

        samples, stats = await build_embedding_dataset(
            db, None, rng=random.Random(42)
        )

        assert len(samples) == 1
        sample = samples[0]
        assert sample["query"] == "请问如何重置登录密码？"
        assert sample["pos"] == "密码重置的完整流程说明文档。"
        assert sample["neg"] == "办公用品申领流程说明文档。"
        assert sample["meta"]["neg_type"] == "random"
        assert sample["meta"]["pos_doc_id"] == str(pos_doc.id)
        assert sample["meta"]["neg_doc_id"] == str(neg_doc.id)
        assert stats["classification"] == 0

    @pytest.mark.asyncio
    async def test_pos_classification_filtered(self) -> None:
        """正例文档密级超阈时三元组被剔除。"""
        pos_doc = _document(classification="secret")
        click_log = SimpleNamespace(
            id=uuid4(),
            query="核心算法文档在哪里查阅？",
            clicked=True,
            clicked_doc_id=pos_doc.id,
            created_at=_now(),
        )
        db = _db_with_execute([
            _exec_result(scalars=[click_log]),
            _exec_result(scalars=[pos_doc]),
            _exec_result(scalars=[_document()]),
        ])

        samples, stats = await build_embedding_dataset(
            db, None, max_classification="internal"
        )

        assert samples == []
        assert stats["classification"] == 1

    @pytest.mark.asyncio
    async def test_no_clicks_returns_empty(self) -> None:
        """无点击行为时直接返回空（仅 1 次查询）。"""
        db = _db_with_execute([_exec_result(scalars=[])])

        samples, stats = await build_embedding_dataset(db, None)

        assert samples == []
        assert db.execute.await_count == 1


# ======================================================================
# Golden 构建测试
# ======================================================================


class TestBuildGoldenSet:
    """Golden 冻结评测集构建测试（mock DB）。"""

    @pytest.mark.asyncio
    async def test_golden_format(self) -> None:
        """输出 {query, expected_answer, expected_doc_ids, meta}，frozen=True。"""
        question = _question()
        adopted = _answer("重置密码需验证身份后由管理员操作。", question_id=question.id)
        db = _db_with_execute([
            _exec_result(rows=[(adopted, question)]),
            _exec_result(scalars=[]),
        ])

        samples, _ = await build_golden_set(db, None)

        assert len(samples) == 1
        sample = samples[0]
        assert "如何重置密码" in sample["query"]
        assert sample["expected_answer"] == "重置密码需验证身份后由管理员操作。"
        assert sample["expected_doc_ids"] == []
        assert sample["meta"]["frozen"] is True
        assert sample["meta"]["source"] == "qa_adopted"

    @pytest.mark.asyncio
    async def test_golden_limit(self) -> None:
        """limit 上限生效：3 条 Golden 桶采纳源仅产出 2 条。"""
        # 评测问题{0,2,4} 的标题+详情经 SHA-256 分桶均落入 Golden 桶（bucket 0）
        rows = []
        for i in (0, 2, 4):
            q = _question(title=f"评测问题{i}标题内容", content=f"评测问题{i}的详细描述内容")
            rows.append((_answer(f"评测问题{i}的采纳回答内容。", question_id=q.id), q))
        db = _db_with_execute([
            _exec_result(rows=rows),
            _exec_result(scalars=[]),
        ])

        samples, _ = await build_golden_set(db, None, limit=2)

        assert len(samples) == 2

    @pytest.mark.asyncio
    async def test_sft_bucket_reserved_from_golden(self) -> None:
        """评审 #3：非 Golden 桶的问答对不得进 Golden 评测集（留给 SFT）。"""
        # "如何申请年假" 文本分桶落在 SFT 侧（bucket 7）
        question = _question(title="如何申请年假", content="入职满一年，想休年假")
        adopted = _answer("在 OA 系统提交年假申请，主管审批后生效。", question_id=question.id)
        db = _db_with_execute([
            _exec_result(rows=[(adopted, question)]),
            _exec_result(scalars=[]),
        ])

        samples, stats = await build_golden_set(db, None)

        assert samples == []
        assert stats["sft_reserved"] == 1


# ======================================================================
# API 测试
# ======================================================================


def _make_user(role: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), role=role, is_active=True, email="a@test.com", name="管理员"
    )


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 测试认证强制。"""
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()


def _install_overrides(user: SimpleNamespace, db: AsyncMock) -> None:
    """安装认证与 DB 覆盖。"""
    from app.database import get_db_session
    from app.deps import get_current_user
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    async def override_user():
        return user

    async def override_db():
        yield db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db_session] = override_db


@pytest_asyncio.fixture
async def admin_client():
    """admin 用户客户端（db 为 AsyncMock，测试内自行配置 execute side_effect）。"""
    from app.main import app

    db = _db_with_execute([])
    _install_overrides(_make_user("admin"), db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, db
    app.dependency_overrides.clear()


class TestFinetuneAuth:
    """认证与权限测试。"""

    @pytest.mark.asyncio
    async def test_export_requires_auth(self, raw_client: httpx.AsyncClient) -> None:
        """未携带 token 创建导出任务应返回 401。"""
        response = await raw_client.post(
            "/api/v1/finetune/datasets/export", json={"dataset_type": "sft"}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, raw_client: httpx.AsyncClient) -> None:
        """未携带 token 查询列表应返回 401。"""
        response = await raw_client.get("/api/v1/finetune/datasets")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_export_requires_admin(self) -> None:
        """非 admin 角色创建导出任务应返回 403。"""
        from app.main import app

        _install_overrides(_make_user("editor"), _db_with_execute([]))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/finetune/datasets/export", json={"dataset_type": "sft"}
            )
        app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["code"] == 403


class TestExportEndpoint:
    """POST /api/v1/finetune/datasets/export 测试。"""

    @pytest.mark.asyncio
    async def test_export_submits_celery_task(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """admin 创建导出任务：落库 pending 记录并提交 Celery，返回 export_id/task_id。"""
        client, db = admin_client

        # refresh mock：模拟 INSERT 后回填主键/时间戳
        async def _refresh(record):
            record.id = uuid4()
            record.created_at = _now()
            record.updated_at = _now()

        db.refresh = AsyncMock(side_effect=_refresh)

        mock_task = MagicMock()
        mock_task.delay = MagicMock(return_value=SimpleNamespace(id="celery-task-ft"))
        with patch("tasks.finetune_tasks.build_dataset_task", mock_task):
            response = await client.post(
                "/api/v1/finetune/datasets/export",
                json={
                    "dataset_type": "sft",
                    "max_classification": "internal",
                    "days": 90,
                    "min_rating": 4,
                    "limit": 10000,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["status"] == "building"
        assert data["data"]["task_id"] == "celery-task-ft"
        # export_id 与 Celery 提交参数一致（同一记录）
        assert mock_task.delay.call_args.kwargs["export_id"] == data["data"]["export_id"]
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_export_invalid_dataset_type(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """非法数据集类型应返回 422。"""
        client, _ = admin_client
        response = await client.post(
            "/api/v1/finetune/datasets/export", json={"dataset_type": "lora"}
        )
        assert response.status_code == 422


class TestListAndDetail:
    """GET /datasets 与 /datasets/{export_id} 测试。"""

    @pytest.mark.asyncio
    async def test_list_paged(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """分页列表返回 items/total/pages。"""
        client, db = admin_client
        records = [_export_record(), _export_record(dataset_type="dpo")]
        db.execute = AsyncMock(side_effect=[
            _exec_result(scalar_one=2),  # count
            _exec_result(scalars=records),  # items
        ])

        response = await client.get("/api/v1/finetune/datasets", params={"page": 1, "size": 20})

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["total"] == 2
        assert data["data"]["pages"] == 1
        assert len(data["data"]["items"]) == 2
        assert data["data"]["items"][0]["dataset_type"] == "sft"
        assert data["data"]["items"][0]["filtered_stats"] == {"duplicate": 1}

    @pytest.mark.asyncio
    async def test_get_detail(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """单条详情返回 params 与 file_path。"""
        client, db = admin_client
        record = _export_record(file_path="/tmp/x/sft.jsonl")
        db.execute = AsyncMock(side_effect=[_exec_result(scalar_one_or_none=record)])

        response = await client.get(f"/api/v1/finetune/datasets/{record.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["id"] == str(record.id)
        assert data["data"]["params"] == {"days": 90}
        assert data["data"]["file_path"] == "/tmp/x/sft.jsonl"

    @pytest.mark.asyncio
    async def test_get_detail_not_found(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """记录不存在（或非本租户被过滤）返回 404。"""
        client, db = admin_client
        db.execute = AsyncMock(side_effect=[_exec_result(scalar_one_or_none=None)])

        response = await client.get(f"/api/v1/finetune/datasets/{uuid4()}")

        assert response.status_code == 200
        assert response.json()["code"] == 404


class TestPreviewAndDownload:
    """preview / download 端点测试（读临时 JSONL 文件）。"""

    @pytest.mark.asyncio
    async def test_preview_reads_jsonl(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock], tmp_path
    ) -> None:
        """preview 返回 JSONL 前 N 行。"""
        client, db = admin_client
        samples = [{"query": f"问题{i}", "expected_answer": f"答案{i}"} for i in range(3)]
        file_path, _ = export_jsonl(
            samples, None, "golden", "v20260808-100000", base_dir=tmp_path
        )
        record = _export_record(
            dataset_type="golden", file_path=str(file_path), status="completed"
        )
        db.execute = AsyncMock(side_effect=[_exec_result(scalar_one_or_none=record)])

        response = await client.get(
            f"/api/v1/finetune/datasets/{record.id}/preview", params={"limit": 2}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["items"] == samples[:2]

    @pytest.mark.asyncio
    async def test_preview_not_completed(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock]
    ) -> None:
        """构建未完成时 preview 返回 400。"""
        client, db = admin_client
        record = _export_record(status="building", file_path=None)
        db.execute = AsyncMock(side_effect=[_exec_result(scalar_one_or_none=record)])

        response = await client.get(f"/api/v1/finetune/datasets/{record.id}/preview")

        assert response.status_code == 200
        assert response.json()["code"] == 400

    @pytest.mark.asyncio
    async def test_download_returns_file(
        self, admin_client: tuple[httpx.AsyncClient, AsyncMock], tmp_path
    ) -> None:
        """download 返回 application/x-ndjson 文件流，文件名含类型与版本。"""
        client, db = admin_client
        samples = [{"messages": [{"role": "user", "content": "问题内容文本"}]}]
        file_path, size = export_jsonl(
            samples, None, "sft", "v20260808-100000", base_dir=tmp_path
        )
        record = _export_record(file_path=str(file_path), file_size_bytes=size)
        db.execute = AsyncMock(side_effect=[_exec_result(scalar_one_or_none=record)])

        response = await client.get(f"/api/v1/finetune/datasets/{record.id}/download")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "sft-v20260808-100000.jsonl" in response.headers["content-disposition"]
        lines = [l for l in response.text.strip().split("\n") if l]
        assert [json.loads(l) for l in lines] == samples
