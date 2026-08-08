"""本地生成 SFT 微调数据文件（不依赖网络/数据库）。

基于企业知识库场景 QA 模板，生成 messages 格式的 SFT 训练数据，
用于 LLM（Qwen 等）的指令微调，让模型学习企业知识库助手的话术风格、
格式规范和边界拒答能力。

数据构成（3 类样本）：
    - 60% RAG 问答：带检索文档 context，教模型基于文档回答（核心能力）
    - 25% 纯问答：无 context，教模型记住高频问题的标准答案
    - 15% 边界拒答：超出能力范围的问题，教模型识别边界并引导

输出格式（Qwen/LLaMA 兼容）：
    {"messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
    ]}

用法：
    python scripts/finetune/generate_sft_data.py \\
        --output data/open/sft.jsonl --count 800
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

# 复用 embedding 数据的 QA 模板
from generate_embedding_data import _QA_TEMPLATES, _query_variants

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是企业知识库智能助手，负责回答员工在工作中的各类问题"
    "（IT运维、HR人事、OA审批、产品业务、合规安全等）。\n"
    "回答要求：\n"
    "1. 先给结论，再展开步骤，简洁清晰；\n"
    "2. 涉及操作流程的用编号步骤呈现；\n"
    "3. 涉及系统/平台的给出具体地址或入口；\n"
    "4. 不确定的信息如实说明，不编造；\n"
    "5. 超出知识库范围的问题，引导到对应服务台。"
)

SYSTEM_PROMPT_RAG = (
    "你是企业知识库智能助手。以下是从知识库检索到的相关文档片段，"
    "请基于文档内容回答用户问题。如果文档中没有相关信息，请如实说明。\n\n"
    "回答要求：先给结论，再展开步骤；涉及操作的用编号步骤；给出具体系统入口。"
)

# 边界 case：超出知识库能力范围的问题，教模型拒答 + 引导
_BOUNDARY_CASES: list[tuple[str, str]] = [
    ("帮我写一首关于秋天的诗",
     "我是企业知识库助手，主要负责回答工作相关问题（IT运维、HR人事、OA审批等）。"
     "创意写作不在我的服务范围内，建议您使用专业的写作工具或咨询相关同事。"),
    ("今天股票行情怎么样",
     "我无法提供实时金融数据。如需查看股票行情，建议使用专业财经平台"
     "（如东方财富、同花顺）。企业投资相关事宜请联系财务部。"),
    ("帮我预订明天去北京的机票",
     "机票预订请使用公司差旅平台 tripmgmt.example.com 提交出差申请，"
     "审批通过后系统自动预订。紧急出差可联系行政部协助加急处理。"),
    ("今天天气怎么样",
     "我无法查询实时天气信息，建议使用天气 App 或搜索引擎查询。"
     "如需了解出差目的地的天气，差旅平台预订页面有当地天气参考。"),
    ("帮我翻译一段英文邮件",
     "翻译服务不在知识库范围内。建议使用 DeepL、百度翻译等专业工具。"
     "如需正式商务邮件翻译，可联系行政部翻译岗协助。"),
    ("帮我写一段代码",
     "编程开发问题建议咨询技术团队或使用专业编程助手。"
     "企业知识库中有开发规范和技术文档，可在知识库-技术文档目录查阅。"),
    ("公司附近有什么好吃的餐厅",
     "餐厅推荐不在我的服务范围内。建议使用大众点评等本地生活平台搜索。"
     "公司食堂每周菜单可在行政服务-食堂管理页面查看。"),
    ("帮我算一下我的八字",
     "我是企业知识库助手，不提供命理玄学类服务。"
     "工作相关问题（HR、IT、OA等）欢迎随时提问。"),
]


def _to_extractive_answer(question: str, answer: str) -> str:
    """将原文答案转为提取式回答（不照抄 context 原文）。

    转换策略：
    - 按句号/分号拆分多句答案为编号步骤，以"回答问题"的口吻呈现
    - 单句答案加"根据文档，"前缀直接回答
    - 核心目的：让 assistant ≠ context，教模型"提取+重组"而非"复制粘贴"
    """
    import re

    parts = re.split(r"[。；]", answer)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) >= 2:
        steps = "\n".join(f"{i}. {p}" for i, p in enumerate(parts, 1))
        return f"根据文档，具体如下：\n{steps}"
    return f"根据文档，{answer}"


def _build_rag_sample(
    qa: tuple[str, str, str],
    query_variant: str,
    all_qas: list[tuple[str, str, str]] | None = None,
    rng: random.Random | None = None,
) -> dict:
    """构造 RAG 问答样本：带检索文档 context（多段落 + 干扰文档）。

    模拟真实 RAG 场景：检索返回 2 个文档片段（1 相关 + 1 干扰），
    模型需识别相关段落并提取信息作答。

    关键设计（修复 v1 的"复制粘贴"问题）：
    - context 含干扰文档 → 模型必须判断哪段相关，不能全盘照抄
    - assistant 用 _to_extractive_answer 重组 → 不等于 context 原文，
      教模型"提取+重组"而非"复制"
    """
    scene, _question, answer = qa

    # ---- context：相关文档 + 干扰文档（模拟真实检索多段返回）----
    context_parts = [f"【文档1】（来源：{scene}知识库）\n{answer}"]
    if all_qas and rng:
        distractors = [q for q in all_qas if q[2] != answer and q[0] != scene]
        if distractors:
            d = rng.choice(distractors)
            context_parts.append(f"【文档2】（来源：{d[0]}知识库）\n{d[2]}")
    context = "\n\n".join(context_parts)

    user_msg = f"根据以下文档回答问题。\n\n{context}\n\n【问题】{query_variant}"
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_RAG},
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": _to_extractive_answer(_question, answer)},
        ],
        "meta": {"type": "rag", "scene": scene},
    }


def _build_plain_sample(qa: tuple[str, str, str], query_variant: str) -> dict:
    """构造纯问答样本：无 context，直接问答。

    教模型记住高频问题的标准答案和话术风格。
    """
    scene, _question, answer = qa
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query_variant},
            {"role": "assistant", "content": answer},
        ],
        "meta": {"type": "plain", "scene": scene},
    }


def _build_boundary_sample(boundary: tuple[str, str]) -> dict:
    """构造边界拒答样本：超出能力范围的问题。

    教模型识别边界，不编造，引导到对应服务台。
    """
    question, answer = boundary
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
        "meta": {"type": "boundary"},
    }


def generate_sft_samples(count: int = 800, seed: int = 42) -> list[dict]:
    """生成 SFT 训练样本列表。

    样本配比：60% RAG 问答 + 25% 纯问答 + 15% 边界拒答。
    """
    rng = random.Random(seed)
    samples: list[dict] = []

    # ---- 60% RAG 问答 ----
    rag_count = int(count * 0.6)
    for qa in _QA_TEMPLATES:
        for variant in _query_variants(qa[1]):
            if len(samples) >= rag_count:
                break
            samples.append(_build_rag_sample(qa, variant, all_qas=_QA_TEMPLATES, rng=rng))
        if len(samples) >= rag_count:
            break

    # ---- 25% 纯问答 ----
    plain_count = int(count * 0.25)
    plain_samples: list[dict] = []
    for qa in _QA_TEMPLATES:
        for variant in _query_variants(qa[1]):
            if len(plain_samples) >= plain_count:
                break
            plain_samples.append(_build_plain_sample(qa, variant))
        if len(plain_samples) >= plain_count:
            break
    samples.extend(plain_samples)

    # ---- 15% 边界拒答 ----
    boundary_count = count - len(samples)
    for i in range(boundary_count):
        boundary = _BOUNDARY_CASES[i % len(_BOUNDARY_CASES)]
        samples.append(_build_boundary_sample(boundary))

    rng.shuffle(samples)
    return samples[:count]


def write_jsonl(samples: list[dict], path: str | Path) -> int:
    """写 jsonl 文件（不含 meta 字段，meta 仅用于统计），返回写入条数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            # 训练数据只保留 messages，meta 不写入（避免污染训练）
            out = {"messages": s["messages"]}
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    return len(samples)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地生成 SFT 微调数据文件（messages 格式，不依赖网络/数据库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default="data/open/sft.jsonl", help="输出 jsonl 路径")
    parser.add_argument("--count", type=int, default=800, help="生成条数上限")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（可复现）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    samples = generate_sft_samples(count=args.count, seed=args.seed)
    n = write_jsonl(samples, args.output)

    # 统计
    types: dict[str, int] = {}
    scenes: dict[str, int] = {}
    for s in samples:
        t = s["meta"]["type"]
        types[t] = types.get(t, 0) + 1
        sc = s["meta"].get("scene", "boundary")
        scenes[sc] = scenes.get(sc, 0) + 1

    print(f"\n===== SFT 数据生成完成 =====")
    print(f"文件: {args.output}")
    print(f"总数: {n} 条")
    print(f"类型分布:")
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c} 条 ({c/n*100:.0f}%)")
    print(f"场景分布:")
    for s, c in sorted(scenes.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c} 条 ({c/n*100:.0f}%)")
    print(f"\n下一步:")
    print(f"  1. 看数据画像: python scripts/finetune/data_stats.py {args.output}")
    print(f"  2. MLX 训练: mlx_lm.lora --model Qwen/Qwen2.5-1.5B-Instruct-4bit --train --data <dir>")
    print(f"  3. HF 训练: python scripts/finetune/train_lora.py --data {args.output} --base_model Qwen/Qwen2.5-1.5B-Instruct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
