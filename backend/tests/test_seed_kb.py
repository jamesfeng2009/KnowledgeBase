"""seed_kb.py 离线单元测试（T1 灌库入口）。

验证目标：
1. py_compile + import 不报错（重依赖延迟导入，无 DB 可 import）；
2. deterministic_doc_id：确定性（同输入同 ID）、不同 source/key 不同；
3. collect_seed_docs：40 篇、字段齐全、--count 截断；
4. collect_test_documents：能枚举 test_documents/ 下真实文件（101+）；
5. score_answer：命中阈值、空文本、大小写不敏感；
6. SeedReport.as_dict：字段完整；
7. _upsert_and_index 幂等：内容未变且 published → 跳过且不跑流水线；
   内容变化 → 重灌并跑流水线。

mock 风格照 test_finetune_seed_docs.py：AsyncMock / MagicMock。
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SEED_KB_PATH = Path(__file__).parent.parent / "scripts" / "seed_kb.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_test_seed_kb", SEED_KB_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时会按 cls.__module__ 回查 sys.modules，必须先注册再 exec
    sys.modules["_test_seed_kb"] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------
# 静态 / 纯函数
# ------------------------------------------------------------------


def test_compile_and_import():
    py_compile.compile(str(SEED_KB_PATH), doraise=True)
    m = _load_module()
    assert callable(m.deterministic_doc_id)
    assert callable(m.score_answer)


def test_deterministic_doc_id_stable_and_distinct():
    m = _load_module()
    a = m.deterministic_doc_id("seed", "tenant-1", "kb-1", "seed:标题")
    b = m.deterministic_doc_id("seed", "tenant-1", "kb-1", "seed:标题")
    c = m.deterministic_doc_id("seed", "tenant-1", "kb-1", "seed:另一标题")
    d = m.deterministic_doc_id("test_documents", "tenant-1", "kb-1", "seed:标题")
    assert a == b
    assert a != c and a != d
    # 必须是合法 UUID
    import uuid

    assert str(uuid.UUID(a)) == a


def test_collect_seed_docs():
    m = _load_module()
    docs = m.collect_seed_docs()
    assert 35 <= len(docs) <= 45, len(docs)
    required = {
        "key",
        "title",
        "content",
        "doc_type",
        "classification",
        "category",
        "source",
    }
    for d in docs:
        assert required <= set(d)
        assert d["title"] and len(d["content"]) >= 50
    assert len(m.collect_seed_docs(5)) == 5


def test_collect_test_documents():
    m = _load_module()
    docs = m.collect_test_docs()
    assert len(docs) >= 100, len(docs)
    assert all(d["key"].startswith("test_doc:") for d in docs)
    assert all(d["title"] and d["doc_type"] for d in docs)


def test_score_answer():
    m = _load_module()
    q = {"points": ["报销单", "发票", "审批", "财务部"], "q": "报销流程"}
    ok, ratio, hit = m.score_answer(
        q, "员工需先在OA填写报销单并附发票，经审批后交财务部。"
    )
    assert ok is True and ratio >= 0.5 and len(hit) >= 2
    ok2, ratio2, _ = m.score_answer(q, "无相关信息")
    assert ok2 is False and ratio2 == 0.0
    # 大小写不敏感（英文要点）
    q_en = {"points": ["VPN", "SSO"], "q": "x"}
    assert m.score_answer(q_en, "use vpn and sso to login")[1] == 1.0


def test_seed_report_as_dict():
    m = _load_module()
    r = m.SeedReport(source="seed", total=10, created=8, indexed=8, failed=2)
    d = r.as_dict()
    assert d["total"] == 10 and d["indexed"] == 8 and d["failed"] == 2
    assert d["source"] == "seed"


# ------------------------------------------------------------------
# 幂等逻辑（mock DB + mock 流水线）
# ------------------------------------------------------------------


def _doc_mock(status: str, content_hash: str):
    doc = MagicMock()
    doc.status = status
    doc.content_hash = content_hash
    doc.content_text = ""
    doc.file_path = None
    return doc


def _session_with_doc(doc):
    """execute 是 async，返回 MagicMock 结果，scalar_one_or_none 同步返回 doc。"""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = doc
    session.execute.return_value = (
        result  # 显式 MagicMock，避免 AsyncMock 链式生成 coroutine
    )
    return session


@pytest.mark.asyncio
async def test_upsert_skips_when_unchanged_and_published():
    m = _load_module()
    from app.utils.hash import compute_content_hash

    content = "这是一段未变化的正文内容，用于验证幂等跳过逻辑。" * 3
    doc = _doc_mock("published", compute_content_hash(content))
    session = _session_with_doc(doc)

    report = m.SeedReport(source="seed")
    spec = {
        "key": "seed:doc-a",
        "title": "doc-a",
        "content": content,
        "doc_type": "md",
        "classification": "internal",
        "category": "测试",
        "source": "seed_kb",
    }
    needs_index = await m._upsert_document(
        session,
        spec,
        tenant_id=MagicMock(),
        kb_id=MagicMock(),
        owner_id=MagicMock(),
        report=report,
    )
    assert needs_index is False
    assert report.skipped == 1 and report.created == 0 and report.updated == 0


@pytest.mark.asyncio
async def test_upsert_reindexes_when_content_changed():
    m = _load_module()
    from app.utils.hash import compute_content_hash

    doc = _doc_mock("published", compute_content_hash("旧内容"))
    session = _session_with_doc(doc)

    report = m.SeedReport(source="seed")
    spec = {
        "key": "seed:doc-b",
        "title": "doc-b",
        "content": "这是变化后的新内容，应当重新灌库并重建索引。" * 3,
        "doc_type": "md",
        "classification": "internal",
        "category": "测试",
        "source": "seed_kb",
    }
    needs_index = await m._upsert_document(
        session,
        spec,
        tenant_id=MagicMock(),
        kb_id=MagicMock(),
        owner_id=MagicMock(),
        report=report,
    )
    assert needs_index is True
    assert doc.status == "processing"
    assert report.updated == 1 and report.created == 0

    # 阶段 2：建索引
    proc_result = {"status": "success", "chunk_count": 2}
    with patch(
        "tasks.document_tasks._process_document_async",
        new_callable=AsyncMock,
        return_value=proc_result,
    ) as proc:
        await m._index_document("doc-id", "doc-b", report)
        proc.assert_awaited_once()
    assert report.indexed == 1 and report.failed == 0
