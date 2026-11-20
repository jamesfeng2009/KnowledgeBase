#!/usr/bin/env python
"""种子文档生产灌库 CLI — 把内置样本文档 / test_documents 灌进知识库并可检索。

背景（Task 2 / P1「数据在哪」）：
    ``scripts/finetune/seed_documents.py`` 只把 40 篇样本文档的纯文本写进
    Document 表（status=published），**不解析、不切片、不建向量索引**，
    因此检索层召回不到它们——这就是 P1 十问「代码层有数据 ≠ 运行层有数据」
    没闭合的根因。本脚本补上一条可重复执行的「灌库 → 建索引 → 评测」路径。

复用（不重写）的现成组件：
    - ``finetune/seed_documents.get_seed_documents()`` — 40 篇内置文档（纯函数）
    - ``eval_resume_seed.ensure_tenant/user/kb`` — 种子租户/用户/知识库幂等创建
    - ``tasks.document_tasks._process_document_async`` — 解析→分块→向量→索引→发布 全流水线
    - ``utils.hash.compute_content_hash`` — 内容 SHA-256，灌库幂等键
    - ``app.rag.engine.AgenticRAGEngine.answer`` — 运行层检索验证

幂等保证：
    doc_id 由 ``uuid5(namespace, source|tenant|kb|key)`` 确定性生成；灌库前比对
    ``content_hash``，内容未变则跳过（不重复写、不重复建索引）。重复执行安全。

用法::

    cd backend
    # 1) 灌 40 篇内置样本文档（默认建/取名为「Task2 种子知识库」的 KB）
    .venv/bin/python scripts/seed_kb.py --source seed

    # 2) 灌 test_documents/ 下全部真实文件（md/docx/pdf/pptx/xls/xlsx）
    .venv/bin/python scripts/seed_kb.py --source test_documents

    # 3) 灌全部 + 跑 P1 十问运行层评测，输出 N/10 报告
    .venv/bin/python scripts/seed_kb.py --source all --verify

    # 4) 指定租户/知识库，只灌前 N 篇
    .venv/bin/python scripts/seed_kb.py --source seed --tenant <uuid> --kb-name 我的库 --count 5

退出码：
    0 = 全部成功；1 = 有文档失败或参数/依赖错误；2 = 评测阶段部分问题未命中（仅 --verify --strict）。

注意：
    运行层 --verify 的检索依赖向量库（OPENSEARCH_URL / MILVUS）与 embedding 服务
    （DEPLOY_MODE 对应的 DashScope / TEI）。这些服务未启动时，灌库仍可完成入库，
    但检索评测会因召回为空而判为未命中——脚本会明确打印被跳过的评测项及原因，
    不把环境缺失伪装成「数据不存在」。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# 确定性 doc_id 命名空间（固定常量，跨进程/跨机器一致）
_DOC_NAMESPACE = uuid.UUID("7f3a9c21-5b8e-4d67-9a10-2c4e8f1b6d30")

_DEFAULT_KB_NAME = "Task2 种子知识库"
_DEFAULT_TENANT_NAME = "Task2 灌库评测租户"
_DEFAULT_USER_EMAIL = "task2_seed@local.test"
_SEED_SOURCE_TAG = "seed_kb"

# P1 十问（来自 Task2 交付物 A）。expected_answer 用于运行层命中判定，
# 不依赖占位符 doc_id；verify 时改用检索召回的正文与期望要点做关键词覆盖比对。
P1_QUESTIONS: list[dict[str, Any]] = [
    {
        "q": "公司报销流程是什么",
        "points": ["报销单", "发票", "审批", "财务部"],
        "category": "OA审批",
    },
    {
        "q": "忘记登录密码如何重置",
        "points": ["sso", "验证码", "新密码", "IT 服务台"],
        "category": "IT运维",
    },
    {
        "q": "VPN 怎么配置远程接入",
        "points": ["VPN", "内网", "客户端", "公共网络"],
        "category": "IT运维",
    },
    {
        "q": "年假请假制度是怎样的",
        "points": ["年假", "工龄", "工作日", "OA"],
        "category": "HR人事",
    },
    {
        "q": "新员工入职需要准备哪些材料",
        "points": ["身份证", "学历", "离职证明", "银行卡"],
        "category": "HR人事",
    },
    {
        "q": "IT 设备故障如何报修",
        "points": ["IT 服务台", "工单", "热线", "值班工程师"],
        "category": "IT运维",
    },
    {
        "q": "差旅费报销标准是多少",
        "points": ["住宿", "城市", "交通费", "高铁"],
        "category": "OA审批",
    },
    {
        "q": "公司数据保密分级有哪些要求",
        "points": ["公开", "内部", "机密", "绝密"],
        "category": "边界说明",
    },
    {
        "q": "如何申请采购物资",
        "points": ["采购", "申请", "审批", "清单"],
        "category": "产品业务",
    },
    {
        "q": "文档上传后多久可以被检索到",
        "points": ["索引", "异步", "稍后", "检索"],
        "category": "边界说明",
    },
]


# ------------------------------------------------------------------
# 纯函数（可离线单测，无 DB / 无重依赖）
# ------------------------------------------------------------------


def deterministic_doc_id(source: str, tenant_id: str, kb_key: str, key: str) -> str:
    """按 source|tenant|kb|key 生成确定性文档 UUID（幂等键）。"""
    raw = f"{source}|{tenant_id}|{kb_key}|{key}"
    return str(uuid.uuid5(_DOC_NAMESPACE, raw))


def load_seed_module():
    """按文件路径加载 finetune/seed_documents.py（非 Python 包）。"""
    path = Path(_BACKEND_ROOT) / "scripts" / "finetune" / "seed_documents.py"
    spec = importlib.util.spec_from_file_location("_seed_documents_for_kb", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_seed_docs(count: int | None = None) -> list[dict[str, str]]:
    """读取 40 篇内置样本文档，统一成灌库输入结构。"""
    seed = load_seed_module()
    docs = seed.get_seed_documents()
    out = [
        {
            "key": f"seed:{d['title']}",
            "title": d["title"],
            "content": d["content"],
            "doc_type": "md",
            "classification": d["classification"],
            "category": d["category"],
            "source": _SEED_SOURCE_TAG,
        }
        for d in docs
    ]
    return out[:count] if count else out


_TEST_DOC_EXTS = {".md", ".docx", ".pdf", ".pptx", ".xls", ".xlsx", ".txt", ".html"}


def collect_test_docs(count: int | None = None) -> list[dict[str, str]]:
    """读取 test_documents/ 下全部真实文件，统一成灌库输入结构。"""
    root = Path(_BACKEND_ROOT).parent / "test_documents"
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEST_DOC_EXTS
    )
    out: list[dict[str, str]] = []
    for p in files:
        try:
            content = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # 二进制文件（pdf/docx/xlsx...）：内容留空，交给 _process_document_async 的 Docling 解析；
            # 用相对路径作为内容指纹，保证幂等。
            content = ""
        out.append(
            {
                "key": f"test_doc:{p.relative_to(root).as_posix()}",
                "title": p.stem,
                "content": content,
                "doc_type": p.suffix.lower().lstrip(".") or "md",
                "classification": "internal",
                "category": "test_documents",
                "source": _SEED_SOURCE_TAG,
                "file_path": str(p),
            }
        )
    return out[:count] if count else out


@dataclass
class SeedReport:
    """灌库结果统计。"""

    source: str
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    indexed: int = 0
    tenant_id: str = ""
    kb_id: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "skipped_unchanged": self.skipped,
            "failed": self.failed,
            "indexed": self.indexed,
            "tenant_id": self.tenant_id,
            "kb_id": self.kb_id,
            "errors": self.errors,
        }


def score_answer(question: dict[str, Any], retrieved_text: str) -> tuple[bool, float, list[str]]:
    """运行层命中判定：检索正文对期望要点（中文/英文不区分大小写）的关键词覆盖率。

    Returns:
        (是否命中, 覆盖率 0~1, 命中的要点列表)
        命中阈值：覆盖率 >= 0.5（4 个要点命中 2 个）。
    """
    if not retrieved_text:
        return False, 0.0, []
    text = retrieved_text.lower()
    points = question.get("points", [])
    hit = [pt for pt in points if pt.lower() in text]
    ratio = len(hit) / len(points) if points else 0.0
    return ratio >= 0.5, round(ratio, 2), hit


# ------------------------------------------------------------------
# DB 操作（重依赖延迟导入）
# ------------------------------------------------------------------


async def _ensure_tenant_user_kb(
    session: Any, *, tenant_id: str | None, kb_name: str
) -> tuple[Any, Any, Any]:
    """建/取租户、种子用户、知识库（复用 eval_resume_seed 的 ensure_* 语义）。"""
    from eval_resume_seed import ensure_kb, ensure_tenant, ensure_user

    if tenant_id:
        from sqlalchemy import select

        from app.models.billing import Tenant

        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == uuid.UUID(tenant_id)))
        ).scalar_one_or_none()
        if tenant is None:
            raise ValueError(f"租户不存在: {tenant_id}")
    else:
        tenant = await ensure_tenant(session)
        # 复用 ensure_tenant Built-in 名称；改成本脚本默认名以免和评测租户混淆
        tenant.name = _DEFAULT_TENANT_NAME

    user = await ensure_user(session, tenant.id)
    kb = await ensure_kb(session, user.id, tenant.id, kb_name)
    return tenant, user, kb


def _doc_id_for(report: SeedReport, tenant_id: uuid.UUID, kb_id: uuid.UUID, key: str) -> str:
    return deterministic_doc_id(report.source, str(tenant_id), str(kb_id), key)


async def _upsert_document(
    session: Any,
    doc_spec: dict[str, str],
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    owner_id: uuid.UUID,
    report: SeedReport,
) -> bool:
    """阶段 1：幂等写入单篇文档。返回 True 表示需要跑建索引流水线。

    内容未变且已 published → 跳过（返回 False）；新建/更新 → 返回 True。
    注意：此处只 flush，由调用方统一 commit —— 流水线用独立 session 重新加载
    文档，必须等文档落库可见后才能跑（否则报「文档不存在」）。
    """
    from sqlalchemy import select

    from app.models.knowledge import Document
    from app.utils.hash import compute_content_hash

    doc_id_str = _doc_id_for(report, tenant_id, kb_id, doc_spec["key"])
    content = doc_spec["content"]
    new_hash = compute_content_hash(content or doc_spec.get("file_path", ""))

    existing = (
        await session.execute(
            select(Document).where(Document.id == uuid.UUID(doc_id_str))
        )
    ).scalar_one_or_none()

    if existing is not None and existing.content_hash == new_hash and existing.status == "published":
        report.skipped += 1
        return False

    if existing is None:
        doc = Document(
            id=uuid.UUID(doc_id_str),
            kb_id=kb_id,
            owner_id=owner_id,
            tenant_id=tenant_id,
            title=doc_spec["title"],
            doc_type=doc_spec["doc_type"],
            content_text=content,
            content_html=content,
            classification=doc_spec["classification"],
            category=doc_spec["category"],
            status="processing",
            content_hash=new_hash,
            source=_SEED_SOURCE_TAG,
            char_count=len(content),
        )
        if doc_spec.get("file_path"):
            doc.file_path = doc_spec["file_path"]
        session.add(doc)
        report.created += 1
    else:
        # 已存在但内容变化（或上次未发布成功）→ 更新重建，不计入 created
        existing.content_text = content
        existing.content_hash = new_hash
        existing.status = "processing"
        if doc_spec.get("file_path"):
            existing.file_path = doc_spec["file_path"]
        report.updated += 1
    await session.flush()
    return True


async def _index_document(doc_id_str: str, title: str, report: SeedReport) -> None:
    """阶段 2：跑全流水线（解析 → 分块 → 向量 → 索引 → 发布）。"""
    from tasks.document_tasks import _process_document_async

    try:
        result = await _process_document_async(doc_id_str)
        if result.get("status") == "success":
            report.indexed += 1
        else:
            report.failed += 1
            report.errors.append(f"{title}: {result.get('error', 'unknown')[:160]}")
    except Exception as exc:
        report.failed += 1
        report.errors.append(f"{title}: EXC {type(exc).__name__} {str(exc)[:120]}")


async def run_seed(source: str, *, tenant_id: str | None, kb_name: str, count: int | None) -> SeedReport:
    """灌库主流程：两阶段——先全部写库 commit，再逐个跑建索引流水线。"""
    from app.database import async_session_factory

    if source == "seed":
        docs = collect_seed_docs(count)
    elif source == "test_documents":
        docs = collect_test_docs(count)
    elif source == "all":
        docs = collect_seed_docs(count) + collect_test_docs(count)
    else:
        raise ValueError(f"未知 source: {source}")

    report = SeedReport(source=source, total=len(docs))
    needs_index: list[tuple[str, str]] = []  # (doc_id, title)

    # 阶段 1：写库 + commit（流水线独立 session 需要看到已落库的文档）
    async with async_session_factory() as session:
        tenant, user, kb = await _ensure_tenant_user_kb(
            session, tenant_id=tenant_id, kb_name=kb_name
        )
        report.tenant_id = str(tenant.id)
        report.kb_id = str(kb.id)
        for spec in docs:
            try:
                should_index = await _upsert_document(
                    session,
                    spec,
                    tenant_id=tenant.id,
                    kb_id=kb.id,
                    owner_id=user.id,
                    report=report,
                )
                if should_index:
                    needs_index.append(
                        (_doc_id_for(report, tenant.id, kb.id, spec["key"]), spec["title"])
                    )
            except Exception as exc:  # 单篇失败不中断整批
                report.failed += 1
                report.errors.append(f"{spec['title']}: EXC {type(exc).__name__} {str(exc)[:120]}")
        await session.commit()

    # 阶段 2：逐个建索引（每个文档在独立 session 里加载）
    for doc_id_str, title in needs_index:
        await _index_document(doc_id_str, title, report)

    return report


async def run_verify(kb_name: str) -> dict[str, Any]:
    """运行层评测：用 AgenticRAGEngine 对 P1 十问检索，按要点覆盖率给 N/10。

    返回报告 dict；不抛异常（环境问题转成 SKIP，不伪装成数据不存在）。
    """
    from app.database import async_session_factory

    hits = 0
    details: list[dict[str, Any]] = []
    skip_reason: str | None = None

    try:
        from app.rag.engine import AgenticRAGEngine
    except Exception as exc:
        skip_reason = f"RAG 引擎不可用: {type(exc).__name__} {str(exc)[:120]}"
        AgenticRAGEngine = None  # type: ignore

    if AgenticRAGEngine is not None:
        async with async_session_factory() as session:
            from sqlalchemy import select

            from app.models.knowledge import KnowledgeBase

            kb = (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.name == kb_name)
                )
            ).scalar_one_or_none()
            kb_ids = [str(kb.id)] if kb else []
            engine = AgenticRAGEngine()
            for q in P1_QUESTIONS:
                try:
                    result = await engine.answer(
                        query=q["q"],
                        user_id="task2_seed@local.test",
                        session_id=None,
                        kb_ids=kb_ids,
                        db=session,
                    )
                    text = ""
                    if isinstance(result, dict):
                        text = str(
                            result.get("answer")
                            or result.get("content")
                            or " ".join(
                                str(c.get("content", ""))
                                for c in (result.get("contexts") or result.get("sources") or [])
                                if isinstance(c, dict)
                            )
                        )
                    ok, ratio, hit = score_answer(q, text)
                    if ok:
                        hits += 1
                    details.append({"q": q["q"], "hit": ok, "ratio": ratio, "points": hit})
                except Exception as exc:
                    details.append(
                        {"q": q["q"], "hit": False, "error": f"{type(exc).__name__} {str(exc)[:100]}"}
                    )

    return {
        "total": len(P1_QUESTIONS),
        "hits": hits,
        "report": f"{hits}/{len(P1_QUESTIONS)}",
        "skipped": skip_reason,
        "details": details,
    }


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="种子文档生产灌库 + P1 十问运行层评测")
    parser.add_argument(
        "--source",
        choices=["seed", "test_documents", "all"],
        default="seed",
        help="灌库来源：seed=40 篇内置样本文档；test_documents=真实文件；all=两者（默认 seed）",
    )
    parser.add_argument("--tenant", default=None, help="目标租户 UUID（不传则建/取默认种子租户）")
    parser.add_argument("--kb-name", default=_DEFAULT_KB_NAME, help="目标知识库名称")
    parser.add_argument("--count", type=int, default=None, help="只灌前 N 篇（默认全部）")
    parser.add_argument(
        "--verify", action="store_true", help="灌库后跑 P1 十问运行层评测，输出 N/10"
    )
    parser.add_argument(
        "--strict", action="store_true", help="配合 --verify：未全命中时退出码 2"
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出报告（便于 CI / 归档）"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(f"[SEED-KB] source={args.source} kb={args.kb_name} count={args.count or 'ALL'}")
    try:
        report = await run_seed(
            args.source,
            tenant_id=args.tenant,
            kb_name=args.kb_name,
            count=args.count,
        )
    except Exception as exc:
        print(f"[SEED-KB][FATAL] 灌库失败: {type(exc).__name__} {exc}")
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"[SEED-KB] 完成 tenant={report.tenant_id} kb={report.kb_id} "
            f"total={report.total} created={report.created} updated={report.updated} "
            f"skipped={report.skipped} indexed={report.indexed} failed={report.failed}"
        )
        for err in report.errors:
            print(f"  [FAIL] {err}")

    exit_code = 1 if report.failed else 0

    if args.verify:
        evaluate = await run_verify(args.kb_name)
        if args.json:
            print(json.dumps({"p1_eval": evaluate}, ensure_ascii=False, indent=2))
        else:
            if evaluate.get("skipped"):
                print(f"[VERIFY][SKIP] {evaluate['skipped']}")
            else:
                print(f"[VERIFY] P1 十问运行层命中: {evaluate['report']}")
                for d in evaluate["details"]:
                    flag = "OK " if d.get("hit") else "MISS"
                    print(f"  [{flag}] {d['q'][:30]:<32} ratio={d.get('ratio', 0)}")
        if args.strict and evaluate["hits"] < evaluate["total"]:
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
