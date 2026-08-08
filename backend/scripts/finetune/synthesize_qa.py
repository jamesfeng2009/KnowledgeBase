#!/usr/bin/env python
"""文档合成 QA 脚本 — 微调数据飞轮核心：用 LLM 从知识库文档批量生成问答对。

从 Document 表读取指定租户的文档（按密级过滤），调用项目已有的 LLM provider
让 LLM 为每篇文档生成 N 个问答对，经 PII 脱敏后写入 QaQuestion + QaAnswer 表
（is_accepted=True，is_ai_generated=True），同时为每个合成问题生成一条
SearchLog（模拟"用户搜了这个问题并点击了该文档"），供 dataset_builder 的
SFT 和 Embedding 构建管线消费。

设计要点：
1. 复用项目 LLM provider（app.llm.factory.get_llm_provider），不自己写 HTTP；
   provider.chat() 为异步生成器，非流式模式 yield 完整文本。
2. 重依赖（sqlalchemy / app 模块 / LLM provider）延迟导入到函数内，模块顶层
   仅标准库 —— 满足 test_finetune_scripts 的"import 不报错 + 无重依赖"约束。
3. 纯函数（build_synthesis_prompt / parse_qa_response / apply_pii_mask）独立可测，
   不依赖 DB 与外部服务。
4. 答案风格约束在 prompt 中：先结论、再步骤、引用文档、不确定时说明
   ——与 dataset_builder.SFT_SYSTEM_PROMPT 的 RAG 助手定位一致。

运行示例::

    cd backend && .venv/bin/python scripts/finetune/synthesize_qa.py \
        --tenant_id 00000000-0000-0000-0000-000000000001 \
        --max_classification internal \
        --qa_per_doc 3 --limit_docs 20 --rate_limit 1.0

    # dry run（只打印不写库）
    .venv/bin/python scripts/finetune/synthesize_qa.py \
        --tenant_id 00000000-0000-0000-0000-000000000001 --dry_run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Any

logger = logging.getLogger("synthesize_qa")

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

#: 文档密级权重 —— 镜像 dataset_builder.CLASSIFICATION_WEIGHT（permission_service._CLEARANCE_ORDER 口径）
#: 复制定义而非导入，避免模块顶层引入 app.finetune.dataset_builder 的全部重依赖。
CLASSIFICATION_WEIGHT: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}

#: 合成 QA 的系统占位用户 ID —— 未传 --user_id 时使用。
#: 真实部署中应由操作者传入一个存在的 bot/system 用户 ID。
SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

#: 送入 LLM 的文档内容最大字符数（防止超出上下文窗口）
MAX_DOC_CHARS: int = 6000

#: system prompt —— 定义 LLM 的角色与任务
SYSTEM_PROMPT: str = (
    "你是企业知识库问答数据标注专家。你的任务是根据给定的企业文档，"
    "生成高质量的问答对，用于微调企业知识库智能助手。"
)


# ------------------------------------------------------------------
# 纯函数（可独立单测，不依赖 DB / LLM / app 模块）
# ------------------------------------------------------------------


def build_synthesis_prompt(doc_content: str, n: int) -> str:
    """生成送给 LLM 的 user prompt —— 纯函数，可测。

    包含文档内容、要求生成 N 个问答对、答案风格约束、JSON 输出格式要求。

    Args:
        doc_content: 文档纯文本内容。
        n: 要求生成的问答对数量。

    Returns:
        完整的 user prompt 字符串。
    """
    content = doc_content[:MAX_DOC_CHARS]
    if len(doc_content) > MAX_DOC_CHARS:
        content += "\n\n...(文档内容已截断)"
    return (
        f"请根据以下企业知识库文档内容，生成 {n} 个高质量的问答对。\n"
        f"\n"
        f"【文档内容】\n"
        f"{content}\n"
        f"\n"
        f"【生成要求】\n"
        f"1. 生成恰好 {n} 个问答对，问题要具体、有业务价值，模拟企业员工真实会问的问题；\n"
        f"2. 答案必须准确基于文档内容，不得编造文档中不存在的信息；\n"
        f"3. 答案风格：先给结论，再分步骤说明，引用文档依据，不确定时明确说明；\n"
        f"4. 问题与答案覆盖文档的不同方面，避免重复。\n"
        f"\n"
        f"【输出格式】\n"
        f"输出一个 JSON 数组，每个元素包含 question 和 answer 两个字段，格式如下：\n"
        f'[{{"question": "问题内容", "answer": "答案内容"}}, ...]\n'
        f"\n"
        f"只输出 JSON 数组，不要输出任何其他内容。"
    )


def parse_qa_response(llm_output: str) -> list[dict[str, str]]:
    """解析 LLM 输出为问答对列表 —— 纯函数，可测。

    解析顺序（容错降级）：
    1. 直接 JSON 解析（标准 JSON 数组）；
    2. 从 markdown 代码块或包围文本中提取 JSON 数组再解析；
    3. 逐行解析（question:/answer: 或 问题:/答案: 格式）；
    4. 全部失败返回空列表。

    Args:
        llm_output: LLM 原始输出文本。

    Returns:
        问答对列表，每个元素为 {"question": str, "answer": str}。
    """
    if not llm_output or not llm_output.strip():
        return []

    text = llm_output.strip()

    # 1. 直接 JSON 解析
    pairs = _try_parse_json(text)
    if pairs:
        return pairs

    # 2. 从 markdown 代码块或包围文本中提取 JSON 数组
    extracted = _extract_json_array(text)
    if extracted:
        pairs = _try_parse_json(extracted)
        if pairs:
            return pairs

    # 3. 逐行解析降级
    return _parse_line_by_line(text)


def apply_pii_mask(qa_list: list[dict[str, str]]) -> list[dict[str, str]]:
    """对问答对列表做 PII 脱敏 —— 纯函数，可测。

    套用 app.finetune.data_cleaner.mask_pii，对 question 和 answer 逐一脱敏。
    延迟导入 mask_pii（其本身仅依赖 re/hashlib，无重依赖）。

    Args:
        qa_list: 问答对列表。

    Returns:
        脱敏后的问答对列表（新列表，不修改原列表）。
    """
    from app.finetune.data_cleaner import mask_pii

    return [
        {
            "question": mask_pii(qa.get("question", "")),
            "answer": mask_pii(qa.get("answer", "")),
        }
        for qa in qa_list
    ]


# ------------------------------------------------------------------
# 解析辅助函数
# ------------------------------------------------------------------


def _try_parse_json(text: str) -> list[dict[str, str]]:
    """尝试将文本解析为 JSON 数组并提取问答对。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    pairs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("q") or "").strip()
        answer = str(item.get("answer") or item.get("a") or "").strip()
        if question and answer:
            pairs.append({"question": question, "answer": answer})
    return pairs


def _extract_json_array(text: str) -> str | None:
    """从文本中提取第一个 JSON 数组片段（处理 markdown 代码块包裹）。"""
    # 去除 markdown 代码围栏
    if "```" in text:
        lines = text.split("\n")
        code_lines: list[str] = []
        in_block = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                code_lines.append(line)
        if code_lines:
            candidate = "\n".join(code_lines)
            if "[" in candidate:
                return candidate
    # 定位最外层 [ ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return None


# 逐行解析时识别的前缀（小写匹配，覆盖中英文）
_LINE_PREFIXES: tuple[str, ...] = (
    "question:",
    "q:",
    "问题:",
    "问题：",
    "q：",
    "answer:",
    "a:",
    "答案:",
    "答案：",
    "a：",
)


def _is_question_line(lower: str) -> bool:
    return lower.startswith(("question:", "q:", "问题:", "问题：", "q："))


def _is_answer_line(lower: str) -> bool:
    return lower.startswith(("answer:", "a:", "答案:", "答案：", "a："))


def _strip_prefix(line: str) -> str:
    """去除行首的 Q:/A:/问题:/答案: 等前缀，返回剩余内容。"""
    lower = line.lower()
    for prefix in _LINE_PREFIXES:
        if lower.startswith(prefix):
            return line[len(prefix) :].strip()
    return line


def _parse_line_by_line(text: str) -> list[dict[str, str]]:
    """逐行解析 Q:/A: 或 问题:/答案: 格式的问答对（降级容错）。"""
    pairs: list[dict[str, str]] = []
    current_q: str | None = None
    current_a: str | None = None

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        lower = line.lower()
        if _is_question_line(lower):
            # 结算上一对
            if current_q and current_a:
                pairs.append({"question": current_q, "answer": current_a})
            current_q = _strip_prefix(line)
            current_a = None
        elif _is_answer_line(lower):
            current_a = _strip_prefix(line)
        elif current_a is not None:
            # 答案续行
            current_a += "\n" + line.strip()
        elif current_q is not None:
            # 问题续行
            current_q += "\n" + line.strip()

    if current_q and current_a:
        pairs.append({"question": current_q, "answer": current_a})

    return pairs


# ------------------------------------------------------------------
# LLM 调用辅助
# ------------------------------------------------------------------


async def _call_llm(llm_provider: Any, messages: list[dict[str, str]], **kwargs: Any) -> str:
    """消费 provider.chat() 异步生成器，拼接返回完整文本。

    provider.chat() 在非流式模式下 yield 完整文本（str）和可能的 usage dict；
    本函数只收集 str 片段，忽略 dict（usage / tool_use）。

    Args:
        llm_provider: LLMProvider 实例（或 mock）。
        messages: 消息列表（role + content）。
        **kwargs: 透传给 chat 的生成参数（temperature / max_tokens 等）。

    Returns:
        LLM 输出的完整文本。
    """
    parts: list[str] = []
    async for chunk in llm_provider.chat(messages, stream=False, **kwargs):
        if isinstance(chunk, str):
            parts.append(chunk)
    return "".join(parts)


# ------------------------------------------------------------------
# 核心异步合成逻辑
# ------------------------------------------------------------------


async def run_synthesis(
    db: Any,
    tenant_id: uuid.UUID | None,
    *,
    max_classification: str = "internal",
    qa_per_doc: int = 3,
    limit_docs: int = 20,
    rate_limit: float = 1.0,
    dry_run: bool = False,
    llm_provider: Any = None,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """核心异步合成逻辑 —— 从文档生成 QA 并写入业务表。

    流程：
    1. 查询指定租户的文档（按密级过滤，只处理 <= max_classification 的）；
    2. 对每篇文档调用 LLM 生成 QA 对；
    3. PII 脱敏；
    4. 写入 QaQuestion + QaAnswer（is_accepted=True, is_ai_generated=True）
       + SearchLog（模拟点击行为）；
    5. 限流 + 单篇失败不中断整体。

    Args:
        db: AsyncSession（或测试 mock）。
        tenant_id: 租户 ID，None 时不过滤（单租户兜底）。
        max_classification: 允许处理的最大密级。
        qa_per_doc: 每篇文档生成的问答对数量。
        limit_docs: 最多处理的文档数。
        rate_limit: 限流（每秒处理文档数），0 表示不限流。
        dry_run: True 时只打印不写库。
        llm_provider: 可选 LLM provider 注入（测试用）；None 时通过工厂获取。
        user_id: 合成 QA 的归属用户 ID；None 时使用系统占位用户。

    Returns:
        统计字典（docs_processed / docs_skipped / qa_generated / ...）。
    """
    # 延迟导入重依赖
    from sqlalchemy import select

    from app.models.analytics import SearchLog
    from app.models.knowledge import Document
    from app.models.qa import QaAnswer, QaQuestion
    from app.utils.tenant import apply_tenant_filter

    if llm_provider is None:
        from app.llm.factory import get_llm_provider

        llm_provider = get_llm_provider()

    bot_user_id = user_id or SYNTHETIC_USER_ID
    threshold = CLASSIFICATION_WEIGHT.get(max_classification, 1)

    # 1. 查询文档
    stmt = select(Document).where(
        Document.deleted_at.is_(None),
        Document.content_text.isnot(None),
    )
    stmt = apply_tenant_filter(stmt, Document, tenant_id)
    stmt = stmt.limit(limit_docs)
    result = await db.execute(stmt)
    all_docs = list(result.scalars().all())

    # 2. 密级过滤
    docs: list[Any] = []
    skipped_classification = 0
    for doc in all_docs:
        weight = CLASSIFICATION_WEIGHT.get(getattr(doc, "classification", "internal"), 1)
        if weight <= threshold:
            docs.append(doc)
        else:
            skipped_classification += 1

    stats: dict[str, Any] = {
        "docs_processed": 0,
        "docs_skipped_classification": skipped_classification,
        "docs_failed": 0,
        "qa_generated": 0,
        "search_logs_written": 0,
        "source": "synthetic",
        "dry_run": dry_run,
        "scene_distribution": {},
    }

    # 3. 逐篇合成
    for doc in docs:
        try:
            content = getattr(doc, "content_text", None) or ""
            if not content.strip():
                continue

            prompt = build_synthesis_prompt(content, qa_per_doc)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]

            llm_output = await _call_llm(
                llm_provider,
                messages,
                temperature=0.3,
                max_tokens=4096,
            )

            qa_pairs = parse_qa_response(llm_output)
            if not qa_pairs:
                logger.warning("文档 %s QA 解析失败（空结果），跳过", getattr(doc, "id", "?"))
                stats["docs_failed"] += 1
                continue

            qa_pairs = apply_pii_mask(qa_pairs)

            for qa in qa_pairs:
                if dry_run:
                    logger.info("[dry-run] 文档 %s Q: %s", getattr(doc, "id", "?"), qa["question"])
                    logger.info("[dry-run] 文档 %s A: %s", getattr(doc, "id", "?"), qa["answer"])
                else:
                    question_id = uuid.uuid4()
                    question = QaQuestion(
                        id=question_id,
                        user_id=bot_user_id,
                        tenant_id=tenant_id,
                        title=qa["question"][:500],
                        content="",
                        status="answered",
                        tags="synthetic",
                    )
                    db.add(question)

                    answer = QaAnswer(
                        question_id=question_id,
                        user_id=bot_user_id,
                        tenant_id=tenant_id,
                        content=qa["answer"],
                        is_accepted=True,
                        is_ai_generated=True,
                        doc_id=getattr(doc, "id", None),
                        meta={
                            "source": "synthetic",
                            "doc_id": str(getattr(doc, "id", None)),
                            "category": getattr(doc, "category", None),
                            "classification": getattr(doc, "classification", None),
                        },
                    )
                    db.add(answer)

                    search_log = SearchLog(
                        user_id=bot_user_id,
                        query=qa["question"],
                        clicked=True,
                        clicked_doc_id=getattr(doc, "id", None),
                        tenant_id=tenant_id,
                        source="knowledge_base",
                        result_count=1,
                    )
                    db.add(search_log)

                stats["qa_generated"] += 1
                if not dry_run:
                    stats["search_logs_written"] += 1

            stats["docs_processed"] += 1
            # 按场景分布（文档自动分类）
            category = getattr(doc, "category", None) or "uncategorized"
            stats["scene_distribution"][category] = (
                stats["scene_distribution"].get(category, 0) + len(qa_pairs)
            )

        except Exception as exc:
            logger.warning("文档 %s 合成失败: %s", getattr(doc, "id", "?"), exc)
            stats["docs_failed"] += 1
            continue

        # 限流
        if rate_limit > 0:
            await asyncio.sleep(1.0 / rate_limit)

    # 4. 提交
    if not dry_run and stats["qa_generated"] > 0:
        await db.commit()

    return stats


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="用 LLM 从知识库文档批量合成问答对，写入业务表供微调数据飞轮消费"
    )
    parser.add_argument(
        "--tenant_id",
        required=True,
        help="租户 ID（UUID 格式）",
    )
    parser.add_argument(
        "--max_classification",
        default="internal",
        choices=list(CLASSIFICATION_WEIGHT.keys()),
        help="允许处理的最大文档密级（默认 internal）",
    )
    parser.add_argument(
        "--qa_per_doc",
        type=int,
        default=3,
        help="每篇文档生成的问答对数量（默认 3）",
    )
    parser.add_argument(
        "--limit_docs",
        type=int,
        default=20,
        help="最多处理的文档数（默认 20）",
    )
    parser.add_argument(
        "--rate_limit",
        type=float,
        default=1.0,
        help="限流：每秒处理文档数（默认 1.0，0 表示不限流）",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="只打印不写库",
    )
    parser.add_argument(
        "--user_id",
        default=None,
        help="合成 QA 的归属用户 ID（不提供时使用系统占位用户）",
    )
    return parser.parse_args(argv)


def _print_stats(stats: dict[str, Any]) -> None:
    """打印合成统计。"""
    logger.info("=" * 60)
    logger.info("文档合成 QA 统计")
    logger.info("=" * 60)
    logger.info("处理文档数:        %d", stats["docs_processed"])
    logger.info("密级超阈跳过:      %d", stats["docs_skipped_classification"])
    logger.info("合成失败:          %d", stats["docs_failed"])
    logger.info("生成 QA 数:        %d", stats["qa_generated"])
    logger.info("写入 SearchLog 数: %d", stats["search_logs_written"])
    logger.info("数据来源:          %s", stats["source"])
    logger.info("Dry run:           %s", stats["dry_run"])
    if stats["scene_distribution"]:
        logger.info("按场景分布:")
        for scene, count in sorted(stats["scene_distribution"].items()):
            logger.info("  %-20s %d", scene, count)
    logger.info("=" * 60)


async def _run(
    args: argparse.Namespace,
    *,
    llm_provider: Any = None,
    db: Any = None,
) -> int:
    """异步执行入口 —— 创建 DB 会话并调用 run_synthesis。

    Args:
        args: 已解析的命令行参数。
        llm_provider: 可选 LLM provider 注入（测试用）。
        db: 可选已打开的 AsyncSession 注入（测试用）；None 时通过 task_db_session 创建。
    """
    tenant_id = uuid.UUID(args.tenant_id) if args.tenant_id else None
    user_id = uuid.UUID(args.user_id) if args.user_id else None

    if db is not None:
        # 测试/注入模式 —— db 已打开
        stats = await run_synthesis(
            db,
            tenant_id,
            max_classification=args.max_classification,
            qa_per_doc=args.qa_per_doc,
            limit_docs=args.limit_docs,
            rate_limit=args.rate_limit,
            dry_run=args.dry_run,
            llm_provider=llm_provider,
            user_id=user_id,
        )
    else:
        from app.database import task_db_session

        async with task_db_session() as session:
            stats = await run_synthesis(
                session,
                tenant_id,
                max_classification=args.max_classification,
                qa_per_doc=args.qa_per_doc,
                limit_docs=args.limit_docs,
                rate_limit=args.rate_limit,
                dry_run=args.dry_run,
                llm_provider=llm_provider,
                user_id=user_id,
            )

    _print_stats(stats)
    return 0


def main(argv: list[str] | None = None, *, llm_provider: Any = None, db: Any = None) -> int:
    """CLI 入口 —— 解析参数并通过 asyncio.run 执行异步逻辑。

    Args:
        argv: 命令行参数（None 时取 sys.argv[1:]）。
        llm_provider: 可选 LLM provider 注入（测试用）。
        db: 可选已打开的 AsyncSession 注入（测试用）。

    Returns:
        0 成功，1 失败。
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args, llm_provider=llm_provider, db=db))
    except Exception as exc:
        logger.error("合成失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
