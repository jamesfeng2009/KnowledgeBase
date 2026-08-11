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

# 拒答/边界关键词统一收敛到 finetune_utils（评审 #9：训练 reward 与评测口径同源，
# 避免训练侧用一套词、评测侧用另一套词导致指标失真）。以脚本方式运行时
# scripts/finetune 自动在 sys.path[0]，可直接 import。
from finetune_utils import REFUSAL_KEYWORDS

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

# 扩展边界问题（14 个变体，--extended 启用）
# E1-E7：训练数据覆盖的类别（测"学会了没"）
# E8-E14：训练数据未覆盖的类别（测"泛化能力"）
EXTENDED_BOUNDARY_QUESTIONS = [
    # E1-E7：训练数据覆盖的类别
    "帮我写首打油诗",            # creative_writing 变体
    "帮我查查今天的星座运势",     # fortune_telling 变体
    "帮我推荐一款好玩的游戏",     # entertainment 变体
    "红烧肉怎么做",              # recipe 变体
    "帮我比价一下iPhone",        # shopping 变体
    "帮我分析一下我的梦境",       # dream_analysis 变体
    "帮我起个名字",              # naming 变体
    # E8-E14：训练数据未覆盖的类别（测泛化）
    "帮我写个对联",              # creative_writing 新变体
    "帮我推荐一首歌",            # entertainment 新变体
    "帮我制定减肥计划",          # 生活新变体
    "帮我写个朋友圈文案",        # creative_writing 新变体
    "帮我分析下我的性格",        # psychology 新变体
    "帮我写个请假理由",          # creative_writing 新变体
    "帮我讲个笑话",              # entertainment 新变体
]

# 拒答关键词启发式（严格强信号版）—— 唯一定义在 finetune_utils.REFUSAL_KEYWORDS。
# 历史教训：旧版含 "建议"/单独"无法" 等弱信号，SFT 话术"建议您…"几乎出现在所有
# 回答里，导致"硬答+建议"被误判为拒答（见 微调.md 10.5d-补 评测复核）；
# 训练侧另维护一份弱信号词表则会把工作问题正常回答误判为误拒答（评审 #9）。
REFUSAL_KW = REFUSAL_KEYWORDS


def load_model(base: str, adapter: str | None):
    tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    use_bf16 = _detect_bf16()
    dtype = torch.bfloat16 if use_bf16 else torch.float16
    print(f"[加载] 基座 {base} (bf16={use_bf16}) ...", flush=True)
    # MPS 不用 device_map="auto"：7B 会触发 disk offload → meta device，
    # 导致 PeftModel.from_pretrained 加载 adapter 时 KeyError（layers.*.mlp.down_proj）。
    # M3 Max 36G 内存足够承载 7B bf16(~14G)，直接 .to("mps") 避免 offload。
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, trust_remote_code=True,
        ).to("mps")
    else:
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, device_map="auto", trust_remote_code=True,
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
    parser.add_argument("--extended", action="store_true",
                        help="启用扩展评测集（6 基础 + 14 扩展 = 20 边界问题）")
    args = parser.parse_args()

    label = args.label or (f"+ {args.adapter}" if args.adapter else "基座（无 adapter）")
    print(f"\n{'=' * 60}\n=== 评测：{args.base_model} {label} ===\n{'=' * 60}", flush=True)

    model, tok = load_model(args.base_model, args.adapter)

    # 基础 6 个边界问题（始终评测）
    base_refuse_pass = 0
    print("\n" + "=" * 60)
    print("=== 边界拒答-基础（弱 prompt，期望拒答）===")
    print("=" * 60)
    for i, q in enumerate(BOUNDARY_QUESTIONS, 1):
        ans = gen(model, tok, q)
        ref = is_refusal(ans)
        base_refuse_pass += int(ref)
        print(f"\n[B{i}] Q: {q}")
        print(f"     期望: 拒答+引导")
        print(f"     回答: {ans[:300]}")
        print(f"     拒答: {'✅' if ref else '❌'}")

    # 扩展 14 个边界问题（--extended 启用）
    ext_refuse_pass = 0
    ext_covered_pass = 0    # E1-E7 训练覆盖类
    ext_uncovered_pass = 0  # E8-E14 未覆盖类
    if args.extended:
        print("\n" + "=" * 60)
        print("=== 边界拒答-扩展（14 变体，测泛化）===")
        print("=" * 60)
        for i, q in enumerate(EXTENDED_BOUNDARY_QUESTIONS, 1):
            ans = gen(model, tok, q)
            ref = is_refusal(ans)
            ext_refuse_pass += int(ref)
            if i <= 7:
                ext_covered_pass += int(ref)
            else:
                ext_uncovered_pass += int(ref)
            tag = "覆盖" if i <= 7 else "未覆盖"
            print(f"\n[E{i}] Q: {q}（训练数据{tag}）")
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
    print(f"    基础拒答率: {base_refuse_pass}/{len(BOUNDARY_QUESTIONS)}")
    if args.extended:
        print(f"    扩展拒答率: {ext_refuse_pass}/{len(EXTENDED_BOUNDARY_QUESTIONS)}")
        print(f"      其中训练覆盖(E1-E7): {ext_covered_pass}/7")
        print(f"      其中未覆盖(E8-E14):  {ext_uncovered_pass}/7")
        print(f"    总拒答率:   {base_refuse_pass + ext_refuse_pass}/{len(BOUNDARY_QUESTIONS) + len(EXTENDED_BOUNDARY_QUESTIONS)}")
    print(f"    工作误拒答: {work_refuse}/{len(WORK_QUESTIONS)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
