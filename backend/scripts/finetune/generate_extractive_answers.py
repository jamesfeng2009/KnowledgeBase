#!/usr/bin/env python
"""用基座模型为 QA 模板生成提取式答案（self-distillation）。

问题：v2 的 _to_extractive_answer 只做格式重组（拆句号+加编号），
教不了"理解问题+提取具体答案"。年假问题应回答"10天"而非复述政策全文。

解法：基座 Qwen 1.5B（未微调）本来就会提取——
用好的 prompt 引导它直接回答，把输出作为 SFT 训练目标。
这就是 self-distillation：用基座模型的提取能力生成训练数据，
教 SFT 模型保持提取行为（而非学到"照抄 context"）。

用法：
    python scripts/finetune/generate_extractive_answers.py \
        --base_model models/Qwen2.5-1.5B-Instruct \
        --output data/open/extractive_answers.json

产出：{template_index: "提取式答案"} 的 JSON，供 generate_sft_data.py 加载。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("generate_extractive_answers")

# 引导基座模型做提取式回答的 system prompt
EXTRACT_SYS = (
    "你是文档问答助手。根据文档直接回答问题，"
    "不要复述文档原文，用一两句话给出具体答案。"
    "如果问题包含具体数值（如工龄、天数），直接给出对应的答案。"
)


def generate_for_template(
    model,
    tokenizer,
    scene: str,
    question: str,
    answer: str,
) -> str:
    """用基座模型为单个模板生成提取式答案。

    构造 RAG 场景（文档+问题），让基座模型直接回答。
    """
    messages = [
        {"role": "system", "content": EXTRACT_SYS},
        {
            "role": "user",
            "content": (
                f"文档：{answer}\n\n"
                f"问题：{question}"
            ),
        },
    ]
    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    import torch

    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=100,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
        )
    resp = tokenizer.decode(
        out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
    )
    return resp.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用基座模型生成提取式答案（self-distillation）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base_model",
        default="models/Qwen2.5-1.5B-Instruct",
        help="基座模型路径",
    )
    parser.add_argument(
        "--output",
        default="data/open/extractive_answers.json",
        help="输出 JSON 路径",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # 延迟导入 QA 模板
    sys.path.insert(0, str(Path(__file__).parent))
    from generate_embedding_data import _QA_TEMPLATES

    logger.info("加载基座模型 %s ...", args.base_model)
    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, trust_remote_code=True
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        dtype=torch.bfloat16,
        device_map="mps",
        trust_remote_code=True,
    )

    answers: dict[str, str] = {}
    logger.info("为 %d 个模板生成提取式答案 ...", len(_QA_TEMPLATES))
    for i, (scene, question, original_answer) in enumerate(_QA_TEMPLATES):
        extractive = generate_for_template(
            model, tokenizer, scene, question, original_answer
        )
        answers[str(i)] = extractive
        if (i + 1) % 10 == 0 or i == 0:
            logger.info(
                "[%d/%d] [%s] Q: %s → A: %s",
                i + 1,
                len(_QA_TEMPLATES),
                scene,
                question,
                extractive[:60],
            )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("提取式答案已保存至 %s（%d 条）", output_path, len(answers))

    # 打印样例对比
    print("\n===== 提取式答案 vs 原文答案对比（前5个）=====")
    for i in range(min(5, len(_QA_TEMPLATES))):
        scene, question, original = _QA_TEMPLATES[i]
        extractive = answers.get(str(i), "")
        print(f"[{scene}] {question}")
        print(f"  原文: {original[:80]}")
        print(f"  提取: {extractive[:80]}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
