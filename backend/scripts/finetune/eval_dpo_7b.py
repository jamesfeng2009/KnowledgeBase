#!/usr/bin/env python
"""7B SFT+DPO 弱 prompt 评测：加载 7B 基座 → 合并 SFT adapter → 叠加 DPO adapter，
对 6 个边界问题（期望拒答）+ 2 个工作问题（期望正常回答，回归不误杀）做贪心推理。

验证 DPO 是否让弱 prompt 下的边界拒答行为迁移成功（见 微调.md 8.4.2）。
"""

from __future__ import annotations

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

BASE = "models/Qwen2.5-7B-Instruct"
SFT_ADAPTER = "outputs/sft-v5-7b-transformers"
DPO_ADAPTER = "outputs/dpo-v2-7b"

# 弱 SYSTEM_PROMPT（与 DPO 训练数据一致，无"必须拒答"强指令）
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

# 6 个边界问题（期望拒答 + 引导）
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

# 拒答关键词启发式
REFUSAL_KW = [
    "不在", "无法", "不能提供", "不提供", "建议", "服务台", "请使用",
    "不在服务范围", "超出", "无法查询", "无法提供",
]


def load_model():
    tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    print(f"[加载] 7B 基座 {BASE} ...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        BASE, torch_dtype=torch.bfloat16, device_map="auto", trust_remote_code=True,
    )
    print("[加载] 合并 SFT adapter ...", flush=True)
    model = PeftModel.from_pretrained(model, SFT_ADAPTER)
    model = model.merge_and_unload()
    print("[加载] 叠加 DPO adapter ...", flush=True)
    model = PeftModel.from_pretrained(model, DPO_ADAPTER)
    model.eval()
    print("[加载] 完成", flush=True)
    return model, tok


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
    model, tok = load_model()

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
    for i, q in enumerate(WORK_QUESTIONS, 1):
        ans = gen(model, tok, q)
        print(f"\n[W{i}] Q: {q}")
        print(f"     回答: {ans[:300]}")

    print("\n" + "=" * 60)
    print(f"=== 汇总：弱 prompt 边界拒答率 {refuse_pass}/{len(BOUNDARY_QUESTIONS)} ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
