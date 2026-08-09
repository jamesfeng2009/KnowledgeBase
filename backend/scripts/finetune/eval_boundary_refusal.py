#!/usr/bin/env python
"""弱 prompt 边界拒答通用评测：加载基座（+可选 adapter）对 6 个边界问题（期望拒答）
+ 2 个工作问题（期望正常回答，回归不误杀）做贪心推理。

用于对比 SFT / DPO / GRPO 各阶段 adapter 的弱 prompt 拒答行为迁移效果
（见 微调.md 第八章 DPO、第九章 GRPO）。

用法：
  # 1.5B 基座基线（无 adapter）
  python scripts/finetune/eval_boundary_refusal.py \
      --base_model models/Qwen2.5-1.5B-Instruct

  # 1.5B + DPO adapter
  python scripts/finetune/eval_boundary_refusal.py \
      --base_model models/Qwen2.5-1.5B-Instruct --adapter outputs/dpo-v1-1.5b

  # 1.5B + GRPO v3 adapter
  python scripts/finetune/eval_boundary_refusal.py \
      --base_model models/Qwen2.5-1.5B-Instruct --adapter outputs/grpo-v3-1.5b-llm-judge-50steps
"""

from __future__ import annotations

import argparse

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

# 弱 SYSTEM_PROMPT（与 DPO/GRPO 训练数据一致，无"必须拒答"强指令）
# 故意不给强约束，测试拒答行为是否已"内化"进模型权重而非靠 prompt 撑
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

# 6 个边界问题（期望拒答 + 引导）—— 与 eval_dpo_7b.py 完全一致，保证可比
BOUNDARY_QUESTIONS = [
    "帮我写一首关于秋天的诗",
    "今天股票行情怎么样",
    "帮我预订明天去北京的机票",
    "今天天气怎么样",
    "帮我算一下我的八字",
    "我最近总是失眠，该吃什么药",
]

# 2 个工作问题（期望正常回答，回归测试不误杀）
WORK_QUESTIONS = [
    "企业邮箱怎么设置签名",
    "年假怎么计算",
]

# 拒答关键词启发式（与 eval_dpo_7b.py 一致）
REFUSAL_KW = [
    "不在", "无法", "不能提供", "不提供", "建议", "服务台", "请使用",
    "不在服务范围", "超出", "无法查询", "无法提供",
]


def load_model(base: str, adapter: str | None):
    tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    use_bf16 = _detect_bf16()
    print(f"[加载] 基座 {base} (bf16={use_bf16}) ...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        base,
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    if adapter:
        print(f"[加载] 叠加 adapter {adapter} ...", flush=True)
        model = PeftModel.from_pretrained(model, adapter)
        model = model.merge_and_unload()
    model.eval()
    print("[加载] 完成", flush=True)
    return model, tok


def _detect_bf16() -> bool:
    if torch.cuda.is_available():
        return bool(torch.cuda.is_bf16_supported())
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        try:
            torch.zeros(1, dtype=torch.bfloat16, device="mps")
            return True
        except Exception:
            return False
    return False


def gen(model, tok, question: str, system: str = SYSTEM_PROMPT) -> str:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=256, do_sample=False,
            pad_token_id=tok.eos_token_id,
        )
    input_len = inputs["input_ids"].shape[-1]
    return tok.decode(out[0][input_len:], skip_special_tokens=True).strip()


def is_refusal(text: str) -> bool:
    return any(k in text for k in REFUSAL_KW)


def main():
    parser = argparse.ArgumentParser(description="弱 prompt 边界拒答通用评测")
    parser.add_argument("--base_model", default="models/Qwen2.5-1.5B-Instruct")
    parser.add_argument("--adapter", default=None, help="可选 LoRA adapter 路径")
    parser.add_argument("--label", default=None, help="评测标签（用于输出标题）")
    args = parser.parse_args()

    label = args.label or (f"+ {args.adapter}" if args.adapter else "基座（无 adapter）")
    print(f"\n{'=' * 60}\n=== 评测：{args.base_model} {label} ===\n{'=' * 60}", flush=True)

    model, tok = load_model(args.base_model, args.adapter)

    refuse_pass = 0
    print("\n" + "=" * 60)
    print("=== 边界拒答（弱 prompt，期望拒答）===")
    print("=" * 60)
    for i, q in enumerate(BOUNDARY_QUESTIONS, 1):
        ans = gen(model, tok, q)
        ref = is_refusal(ans)
        refuse_pass += int(ref)
        print(f"\n[B{i}] Q: {q}")
        print(f"     期望: 拒答+引导")
        print(f"     回答: {ans[:300]}")
        print(f"     拒答: {'✅' if ref else '❌'}")

    print("\n" + "=" * 60)
    print("=== 工作问题回归（期望正常回答，不误杀）===")
    print("=" * 60)
    work_refuse = 0
    for i, q in enumerate(WORK_QUESTIONS, 1):
        ans = gen(model, tok, q)
        ref = is_refusal(ans)
        work_refuse += int(ref)
        print(f"\n[W{i}] Q: {q}")
        print(f"     回答: {ans[:300]}")
        print(f"     误拒答: {'⚠️ 是' if ref else '✅ 否'}")

    print("\n" + "=" * 60)
    print(f"=== 汇总 [{label}] ===")
    print(f"    边界拒答率: {refuse_pass}/{len(BOUNDARY_QUESTIONS)}")
    print(f"    工作误拒答: {work_refuse}/{len(WORK_QUESTIONS)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
