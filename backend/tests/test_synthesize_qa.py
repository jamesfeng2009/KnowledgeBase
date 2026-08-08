"""合成 QA 脚本测试 — 纯函数 + LLM 解析 + PII 脱敏 + 端到端 mock。

验证点：
- 模块 import：py_compile + 顶层仅标准库（重依赖延迟导入）；
- 常量：CLASSIFICATION_WEIGHT / SYNTHETIC_USER_ID / MAX_DOC_CHARS / SYSTEM_PROMPT；
- build_synthesis_prompt：包含文档内容、n、JSON 输出要求、不编造约束、长文档截断；
- parse_qa_response：合法 JSON 数组 / markdown 代码块 / 空响应 / 降级行解析
  （Q:/A: 与 问：/答：）/ 完全无法解析 / 空 pair 过滤 / 非数组 JSON；
- apply_pii_mask：手机号 / 邮箱 / 身份证被脱敏（复用 data_cleaner.mask_pii）；
- parse_args：必填 --tenant_id / 默认值 / --dry_run 开关 / 自定义值 / choices 校验；
- run_synthesis 端到端 mock：mock LLM + mock DB，验证写入 QaQuestion/QaAnswer/
  SearchLog（tags=synthetic / is_accepted=True / is_ai_generated=True）、
  dry_run 不写库、密级超阈跳过、坏响应不中断、无文档返回空统计、PII 管线脱敏；
- main CLI：正常流程返回 0、无文档返回 0、dry_run 不写库。

mock 风格照 test_finetune_pipeline.py：AsyncMock/MagicMock + SimpleNamespace。
"""
from __future__ import annotations

import json
import py_compile
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from scripts.finetune.synthesize_qa import (
    CLASSIFICATION_WEIGHT,
    MAX_DOC_CHARS,
    SYNTHETIC_USER_ID,
    SYSTEM_PROMPT,
    apply_pii_mask,
    build_synthesis_prompt,
    main,
    parse_args,
    parse_qa_response,
    run_synthesis,
)


# ======================================================================
# 工具函数
# ======================================================================


def _doc(
    *,
    content_text: str = "密码重置流程：进入设置-账号安全-重置密码，验证身份后即可重置。",
    title: str = "密码重置帮助文档",
    classification: str = "internal",
    category: str = "SOP",
    tenant_id=None,
    owner_id=None,
) -> SimpleNamespace:
    """构造 Document mock（SimpleNamespace，字段对齐 ORM 模型）。"""
    return SimpleNamespace(
        id=uuid4(),
        kb_id=uuid4(),
        title=title,
        content_text=content_text,
        content_html=None,
        classification=classification,
        category=category,
        status="published",
        owner_id=owner_id or uuid4(),
        tenant_id=tenant_id,
        deleted_at=None,
        updated_at=None,
    )


async def _async_gen(items: list):
    """构造 async generator — 模拟 LLMProvider.chat 的返回值。"""
    for item in items:
        yield item


async def _async_gen_error(exc: Exception):
    """构造抛异常的 async generator — 模拟 LLM 调用失败。"""
    raise exc
    yield  # noqa: unreachable — 让函数成为 async generator


def _mock_llm(response_text: str) -> MagicMock:
    """构造 mock LLMProvider，chat 每次调用返回新的 async generator。"""
    llm = MagicMock()
    llm.chat = MagicMock(side_effect=lambda *a, **kw: _async_gen([response_text]))
    return llm


def _mock_llm_error(exc: Exception) -> MagicMock:
    """构造 mock LLMProvider，chat 返回抛异常的 async generator。"""
    llm = MagicMock()
    llm.chat = MagicMock(return_value=_async_gen_error(exc))
    return llm


def _exec_result(*, scalars: list | None = None) -> MagicMock:
    """构造 db.execute 返回值。"""
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    return result


def _mock_db(*, scalars: list | None = None) -> AsyncMock:
    """构造 mock AsyncSession — execute/add/commit 全部 mock。"""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(return_value=_exec_result(scalars=scalars))
    return db


def _qa_json(pairs: list[dict]) -> str:
    """将 QA 对列表序列化为 JSON 字符串。"""
    return json.dumps(pairs, ensure_ascii=False)


# ======================================================================
# 1. 模块 import / py_compile
# ======================================================================


class TestModuleImport:
    """脚本语法有效 + 顶层无重依赖。"""

    def test_py_compile(self) -> None:
        """脚本可通过 py_compile 编译。"""
        from pathlib import Path

        script = Path(__file__).parent.parent / "scripts" / "finetune" / "synthesize_qa.py"
        py_compile.compile(str(script), doraise=True)

    def test_import_no_heavy_deps(self) -> None:
        """import 模块后不得新增加载 torch/transformers 等重依赖。"""
        heavy = {"torch", "transformers", "peft", "trl", "datasets", "sentence_transformers"}
        before = set(sys.modules)
        # 强制重新导入以检测顶层副作用
        import importlib

        mod = importlib.import_module("scripts.finetune.synthesize_qa")
        assert mod is not None
        newly_loaded = (set(sys.modules) - before) & heavy
        assert not newly_loaded, f"顶层引入了重依赖: {newly_loaded}"


# ======================================================================
# 2. 常量
# ======================================================================


class TestConstants:
    """公开常量值校验。"""

    def test_classification_weight_values(self) -> None:
        """密级权重字典含 4 级，public < internal < confidential < secret。"""
        assert CLASSIFICATION_WEIGHT == {
            "public": 0,
            "internal": 1,
            "confidential": 2,
            "secret": 3,
        }

    def test_synthetic_user_id_is_zero_uuid(self) -> None:
        """SYNTHETIC_USER_ID 是全零 UUID 占位符。"""
        import uuid

        assert SYNTHETIC_USER_ID == uuid.UUID("00000000-0000-0000-0000-000000000000")

    def test_max_doc_chars_positive(self) -> None:
        """MAX_DOC_CHARS 是正整数。"""
        assert isinstance(MAX_DOC_CHARS, int)
        assert MAX_DOC_CHARS > 0

    def test_system_prompt_non_empty(self) -> None:
        """SYSTEM_PROMPT 是非空字符串。"""
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT.strip()) > 0


# ======================================================================
# 3. build_synthesis_prompt
# ======================================================================


class TestBuildSynthesisPrompt:
    """prompt 生成纯函数测试。"""

    def test_contains_doc_content(self) -> None:
        """prompt 必须包含文档内容。"""
        prompt = build_synthesis_prompt("这是一篇关于年假制度的文档内容", 3)
        assert "这是一篇关于年假制度的文档内容" in prompt

    def test_contains_n_count(self) -> None:
        """prompt 必须包含要求生成的问答对数量 n。"""
        prompt = build_synthesis_prompt("文档内容", 5)
        assert "5" in prompt

    def test_requires_json_array_output(self) -> None:
        """prompt 必须要求 JSON 数组输出格式。"""
        prompt = build_synthesis_prompt("文档内容", 3)
        assert "JSON" in prompt
        assert '"question"' in prompt
        assert '"answer"' in prompt

    def test_no_fabrication_constraint(self) -> None:
        """prompt 必须约束不编造文档中不存在的信息。"""
        prompt = build_synthesis_prompt("文档内容", 3)
        assert "不编造" in prompt or "不得编造" in prompt

    def test_long_doc_truncated(self) -> None:
        """超长文档内容被截断到 MAX_DOC_CHARS 并标注截断。"""
        long_content = "A" * (MAX_DOC_CHARS + 100)
        prompt = build_synthesis_prompt(long_content, 3)
        # 截断标记存在
        assert "截断" in prompt
        # 原始内容不全量出现（截断后的 A 数量 <= MAX_DOC_CHARS）
        assert prompt.count("A") <= MAX_DOC_CHARS + 10  # 允许少量格式字符


# ======================================================================
# 4. parse_qa_response
# ======================================================================


class TestParseQaResponse:
    """LLM 输出解析测试。"""

    def test_valid_json_array(self) -> None:
        """合法 JSON 数组 → QA 对列表。"""
        text = _qa_json(
            [
                {"question": "如何重置密码？", "answer": "进入设置-账号安全-重置密码。"},
                {"question": "重置密码需要验证身份吗？", "answer": "需要。"},
            ]
        )
        result = parse_qa_response(text)
        assert len(result) == 2
        assert result[0]["question"] == "如何重置密码？"
        assert result[1]["answer"] == "需要。"

    def test_markdown_codeblock_json(self) -> None:
        """带 ```json 代码块标记的 JSON → 正常解析。"""
        text = '```json\n[{"question": "Q1", "answer": "A1"}]\n```'
        result = parse_qa_response(text)
        assert len(result) == 1
        assert result[0]["question"] == "Q1"
        assert result[0]["answer"] == "A1"

    def test_empty_response_returns_empty(self) -> None:
        """空响应 / 纯空白 → 空列表。"""
        assert parse_qa_response("") == []
        assert parse_qa_response("   ") == []
        assert parse_qa_response("\n\n") == []

    def test_line_parse_q_a_english(self) -> None:
        """Q:/A: 英文前缀 → 降级行解析。"""
        text = "Q: 如何重置密码？\nA: 进入设置页面操作。\nQ: 需要验证吗？\nA: 需要。"
        result = parse_qa_response(text)
        assert len(result) == 2
        assert result[0]["question"] == "如何重置密码？"
        assert result[0]["answer"] == "进入设置页面操作。"
        assert result[1]["question"] == "需要验证吗？"
        assert result[1]["answer"] == "需要。"

    def test_line_parse_chinese_prefix(self) -> None:
        """问题：/答案： 中文前缀 → 降级行解析。"""
        text = "问题：如何申请 VPN？\n答案：在 OA 系统提交申请单。"
        result = parse_qa_response(text)
        assert len(result) == 1
        assert result[0]["question"] == "如何申请 VPN？"
        assert result[0]["answer"] == "在 OA 系统提交申请单。"

    def test_line_parse_q_a_chinese_colon(self) -> None:
        """q：/a： 英文前缀 + 中文冒号 → 降级行解析。"""
        text = "q：如何申请权限？\na：联系管理员开通。"
        result = parse_qa_response(text)
        assert len(result) == 1
        assert result[0]["question"] == "如何申请权限？"
        assert result[0]["answer"] == "联系管理员开通。"

    def test_completely_unparseable_returns_empty(self) -> None:
        """完全无法解析的随机文本 → 空列表（不抛异常）。"""
        result = parse_qa_response("这是一段无法解析的随机文本内容。")
        assert result == []

    def test_empty_pairs_filtered(self) -> None:
        """JSON 数组中 question/answer 为空的 pair 被过滤。"""
        text = _qa_json(
            [
                {"question": "", "answer": "有答案但没问题"},
                {"question": "有问题", "answer": ""},
                {"question": "有效问题", "answer": "有效答案"},
            ]
        )
        result = parse_qa_response(text)
        assert len(result) == 1
        assert result[0]["question"] == "有效问题"
        assert result[0]["answer"] == "有效答案"

    def test_non_array_json_returns_empty(self) -> None:
        """JSON 对象（非数组）→ 空列表。"""
        text = json.dumps({"question": "Q", "answer": "A"}, ensure_ascii=False)
        result = parse_qa_response(text)
        assert result == []


# ======================================================================
# 5. apply_pii_mask
# ======================================================================


class TestApplyPiiMask:
    """PII 脱敏纯函数测试（复用 data_cleaner.mask_pii）。"""

    def test_mask_phone(self) -> None:
        """手机号被脱敏为 [PHONE]。"""
        qa_list = [{"question": "请联系 13812345678", "answer": "已记录"}]
        result = apply_pii_mask(qa_list)
        assert result[0]["question"] == "请联系 [PHONE]"
        assert result[0]["answer"] == "已记录"

    def test_mask_email(self) -> None:
        """邮箱被脱敏为 [EMAIL]。"""
        qa_list = [{"question": "问题", "answer": "邮箱 zhang.san@example.com 备用"}]
        result = apply_pii_mask(qa_list)
        assert result[0]["answer"] == "邮箱 [EMAIL] 备用"

    def test_mask_idcard(self) -> None:
        """身份证号被脱敏为 [IDCARD]。"""
        qa_list = [{"question": "证件 11010119900307777X", "answer": "答案"}]
        result = apply_pii_mask(qa_list)
        assert result[0]["question"] == "证件 [IDCARD]"

    def test_no_pii_unchanged(self) -> None:
        """无 PII 的文本原样返回。"""
        qa_list = [{"question": "普通问题", "answer": "普通答案"}]
        result = apply_pii_mask(qa_list)
        assert result == qa_list

    def test_returns_new_list_not_mutating(self) -> None:
        """返回新列表，不修改原列表。"""
        original = [{"question": "电话 13812345678", "answer": "A1"}]
        result = apply_pii_mask(original)
        assert result is not original
        assert original[0]["question"] == "电话 13812345678"  # 原列表未被修改


# ======================================================================
# 6. parse_args
# ======================================================================


class TestParseArgs:
    """CLI 参数解析测试。"""

    def test_required_tenant_id_missing_exits(self) -> None:
        """缺少 --tenant_id → argparse 报错退出。"""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_default_values(self) -> None:
        """默认值：qa_per_doc=3 / limit_docs=20 / max_classification=internal / rate_limit=1.0。"""
        args = parse_args(["--tenant_id", "test-tenant"])
        assert args.tenant_id == "test-tenant"
        assert args.qa_per_doc == 3
        assert args.limit_docs == 20
        assert args.max_classification == "internal"
        assert args.rate_limit == 1.0
        assert args.dry_run is False
        assert args.user_id is None

    def test_dry_run_flag_default_false(self) -> None:
        """--dry_run 默认 False。"""
        args = parse_args(["--tenant_id", "t"])
        assert args.dry_run is False

    def test_dry_run_flag_set_true(self) -> None:
        """传入 --dry_run → True。"""
        args = parse_args(["--tenant_id", "t", "--dry_run"])
        assert args.dry_run is True

    def test_custom_values(self) -> None:
        """自定义参数值全部正确解析。"""
        args = parse_args(
            [
                "--tenant_id",
                "my-tenant",
                "--qa_per_doc",
                "5",
                "--limit_docs",
                "50",
                "--rate_limit",
                "2.5",
                "--max_classification",
                "confidential",
                "--user_id",
                "user-123",
            ]
        )
        assert args.tenant_id == "my-tenant"
        assert args.qa_per_doc == 5
        assert args.limit_docs == 50
        assert args.rate_limit == 2.5
        assert args.max_classification == "confidential"
        assert args.user_id == "user-123"

    def test_max_classification_invalid_choice_exits(self) -> None:
        """--max_classification 非法值 → argparse 报错退出。"""
        with pytest.raises(SystemExit):
            parse_args(["--tenant_id", "t", "--max_classification", "top_secret"])


# ======================================================================
# 7. run_synthesis 端到端 mock
# ======================================================================


class TestRunSynthesis:
    """run_synthesis 核心异步逻辑 mock 测试。"""

    @pytest.mark.asyncio
    async def test_normal_flow_writes_qa_and_searchlog(self) -> None:
        """正常流程：写入 QaQuestion/QaAnswer/SearchLog，字段正确。"""
        from app.models.analytics import SearchLog
        from app.models.qa import QaAnswer, QaQuestion

        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json(
            [{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}]
        )
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            qa_per_doc=2,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 1
        assert stats["qa_generated"] == 2
        assert stats["search_logs_written"] == 2
        assert stats["docs_failed"] == 0
        assert stats["source"] == "synthetic"
        # 2 QA × 3 对象 = 6 次 add
        assert db.add.call_count == 6
        db.commit.assert_awaited_once()

        # 验证写入的对象类型与字段
        added = [call.args[0] for call in db.add.call_args_list]
        questions = [o for o in added if isinstance(o, QaQuestion)]
        answers = [o for o in added if isinstance(o, QaAnswer)]
        logs = [o for o in added if isinstance(o, SearchLog)]
        assert len(questions) == 2
        assert len(answers) == 2
        assert len(logs) == 2

        # QaQuestion 字段
        assert questions[0].title == "Q1"
        assert questions[0].content == ""
        assert questions[0].status == "answered"
        assert questions[0].tags == "synthetic"
        assert questions[0].tenant_id == tenant_id
        assert questions[0].user_id == SYNTHETIC_USER_ID

        # QaAnswer 字段
        assert answers[0].is_accepted is True
        assert answers[0].is_ai_generated is True
        assert answers[0].content == "A1"
        assert answers[0].tenant_id == tenant_id
        # question_id 关联
        assert answers[0].question_id == questions[0].id
        # 合成溯源：doc_id + meta 标注
        assert answers[0].doc_id == doc.id
        assert answers[0].meta["source"] == "synthetic"
        assert answers[0].meta["doc_id"] == str(doc.id)
        assert answers[0].meta["category"] == doc.category
        assert answers[0].meta["classification"] == doc.classification

        # SearchLog 字段
        assert logs[0].query == "Q1"
        assert logs[0].clicked is True
        assert logs[0].clicked_doc_id == doc.id
        assert logs[0].source == "knowledge_base"
        assert logs[0].result_count == 1
        assert logs[0].tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_dry_run_no_writes(self) -> None:
        """dry_run=True → 不调 add/commit，但仍统计 qa_generated。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            dry_run=True,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 1
        assert stats["qa_generated"] == 1
        assert stats["search_logs_written"] == 0
        assert stats["dry_run"] is True
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_classification_filter_skips_secret_doc(self) -> None:
        """密级超阈（secret > internal）的文档被跳过。"""
        tenant_id = uuid4()
        doc = _doc(classification="secret", tenant_id=tenant_id)
        llm = _mock_llm(_qa_json([{"question": "Q1", "answer": "A1"}]))
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            max_classification="internal",
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_skipped_classification"] == 1
        assert stats["docs_processed"] == 0
        assert stats["qa_generated"] == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_llm_response_skips_doc(self) -> None:
        """LLM 返回空响应 → 该文档跳过，不中断，计入 docs_failed。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        llm = _mock_llm("")
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 0
        assert stats["docs_failed"] == 1
        assert stats["qa_generated"] == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_llm_response_skips_doc_not_crash(self) -> None:
        """LLM 抛异常 → 该文档计入 docs_failed，不中断整体流程。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        llm = _mock_llm_error(RuntimeError("LLM 服务不可用"))
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 0
        assert stats["docs_failed"] == 1
        assert stats["qa_generated"] == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_docs_returns_empty_stats(self) -> None:
        """无文档时返回空统计，不写库。"""
        tenant_id = uuid4()
        llm = _mock_llm("[]")
        db = _mock_db(scalars=[])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 0
        assert stats["qa_generated"] == 0
        assert stats["docs_failed"] == 0
        assert stats["docs_skipped_classification"] == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_docs_processed(self) -> None:
        """多篇文档 → 逐篇处理，统计累计。"""
        tenant_id = uuid4()
        doc1 = _doc(content_text="文档一内容", tenant_id=tenant_id, category="SOP")
        doc2 = _doc(content_text="文档二内容", tenant_id=tenant_id, category="FAQ")
        qa_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc1, doc2])

        stats = await run_synthesis(
            db,
            tenant_id,
            qa_per_doc=1,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 2
        assert stats["qa_generated"] == 2
        assert stats["search_logs_written"] == 2
        # 2 docs × 1 QA × 3 对象 = 6 次 add
        assert db.add.call_count == 6
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pii_masking_in_pipeline(self) -> None:
        """LLM 返回含 PII 的 QA → 管线中 apply_pii_mask 脱敏后写入。"""
        from app.models.qa import QaAnswer, QaQuestion

        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json(
            [
                {
                    "question": "我的手机号是 13812345678，怎么重置密码？",
                    "answer": "邮箱 test@example.com 已绑定，可自助重置。",
                }
            ]
        )
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["qa_generated"] == 1
        added = [call.args[0] for call in db.add.call_args_list]
        questions = [o for o in added if isinstance(o, QaQuestion)]
        answers = [o for o in added if isinstance(o, QaAnswer)]
        # question 中的手机号已脱敏
        assert "13812345678" not in questions[0].title
        assert "[PHONE]" in questions[0].title
        # answer 中的邮箱已脱敏
        assert "test@example.com" not in answers[0].content
        assert "[EMAIL]" in answers[0].content

    @pytest.mark.asyncio
    async def test_scene_distribution(self) -> None:
        """按文档 category 统计 QA 分布。"""
        tenant_id = uuid4()
        doc = _doc(category="技术文档", tenant_id=tenant_id)
        qa_response = _qa_json(
            [{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}]
        )
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            qa_per_doc=2,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["scene_distribution"] == {"技术文档": 2}

    @pytest.mark.asyncio
    async def test_commit_only_when_qa_generated(self) -> None:
        """qa_generated=0 时不调 commit（即使非 dry_run）。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        llm = _mock_llm("无法解析的随机文本")
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["qa_generated"] == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_content_doc_skipped(self) -> None:
        """content_text 为空白的文档被静默跳过（不计 docs_failed）。"""
        tenant_id = uuid4()
        doc = _doc(content_text="   \n  ", tenant_id=tenant_id)
        llm = _mock_llm(_qa_json([{"question": "Q1", "answer": "A1"}]))
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 0
        assert stats["qa_generated"] == 0
        assert stats["docs_failed"] == 0
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_id_override(self) -> None:
        """传入 user_id 时 QaQuestion/QaAnswer/SearchLog 使用该 user_id。"""
        from app.models.analytics import SearchLog
        from app.models.qa import QaAnswer, QaQuestion

        tenant_id = uuid4()
        custom_user_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
            user_id=custom_user_id,
        )

        assert stats["qa_generated"] == 1
        added = [call.args[0] for call in db.add.call_args_list]
        questions = [o for o in added if isinstance(o, QaQuestion)]
        answers = [o for o in added if isinstance(o, QaAnswer)]
        logs = [o for o in added if isinstance(o, SearchLog)]
        assert questions[0].user_id == custom_user_id
        assert answers[0].user_id == custom_user_id
        assert logs[0].user_id == custom_user_id

    @pytest.mark.asyncio
    async def test_title_truncated_to_500(self) -> None:
        """超长 question title 被截断到 500 字符。"""
        from app.models.qa import QaQuestion

        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        long_question = "问题" * 300  # 600 字符
        qa_response = _qa_json([{"question": long_question, "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        added = [call.args[0] for call in db.add.call_args_list]
        questions = [o for o in added if isinstance(o, QaQuestion)]
        assert len(questions[0].title) == 500

    @pytest.mark.asyncio
    async def test_mixed_pass_and_fail_docs(self) -> None:
        """混合文档：一篇正常、一篇 LLM 返回空 → 正常的处理，坏的跳过。"""
        tenant_id = uuid4()
        doc_ok = _doc(content_text="正常文档内容", tenant_id=tenant_id)
        doc_bad = _doc(content_text="另一篇文档", tenant_id=tenant_id)
        good_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = MagicMock()
        llm.chat = MagicMock(
            side_effect=[
                _async_gen([good_response]),  # doc_ok → 成功
                _async_gen(["无法解析的随机文本"]),  # doc_bad → 解析失败
            ]
        )
        db = _mock_db(scalars=[doc_ok, doc_bad])

        stats = await run_synthesis(
            db,
            tenant_id,
            rate_limit=0,
            llm_provider=llm,
        )

        assert stats["docs_processed"] == 1
        assert stats["docs_failed"] == 1
        assert stats["qa_generated"] == 1
        # 只有成功的 1 篇 × 1 QA × 3 对象 = 3 次 add
        assert db.add.call_count == 3
        db.commit.assert_awaited_once()


# ======================================================================
# 8. main CLI 端到端
# ======================================================================


class TestMainCli:
    """main() CLI 入口端到端测试 — 通过 asyncio.run 调用。"""

    def test_main_normal_flow_returns_0(self) -> None:
        """正常流程：main 返回 0，写入 QaQuestion/QaAnswer/SearchLog。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        ret = main(
            ["--tenant_id", str(tenant_id), "--rate_limit", "0"],
            llm_provider=llm,
            db=db,
        )

        assert ret == 0
        # 1 QA × 3 对象 = 3 次 add
        assert db.add.call_count == 3
        db.commit.assert_awaited_once()

    def test_main_no_docs_returns_0(self) -> None:
        """无文档时 main 返回 0（空统计不报错）。"""
        tenant_id = uuid4()
        llm = _mock_llm("[]")
        db = _mock_db(scalars=[])

        ret = main(
            ["--tenant_id", str(tenant_id), "--rate_limit", "0"],
            llm_provider=llm,
            db=db,
        )

        assert ret == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    def test_main_dry_run_returns_0(self) -> None:
        """dry_run 模式：main 返回 0，不写库。"""
        tenant_id = uuid4()
        doc = _doc(tenant_id=tenant_id)
        qa_response = _qa_json([{"question": "Q1", "answer": "A1"}])
        llm = _mock_llm(qa_response)
        db = _mock_db(scalars=[doc])

        ret = main(
            ["--tenant_id", str(tenant_id), "--rate_limit", "0", "--dry_run"],
            llm_provider=llm,
            db=db,
        )

        assert ret == 0
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    def test_main_missing_tenant_id_exits(self) -> None:
        """缺少 --tenant_id → argparse 报错 SystemExit。"""
        with pytest.raises(SystemExit):
            main([])
