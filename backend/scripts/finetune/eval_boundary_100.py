#!/usr/bin/env python
"""100 题边界拒答评测（50 边界 + 50 工作）— 严格关键词 + 宽松关键词双判定。

解决旧版 6/6 虚高问题：旧 eval_dpo_7b.py 用宽泛关键词（含"建议""服务台"），
SFT 话术"建议您…"几乎出现在所有回答里 → 硬答被误判为拒答 → 虚高到 6/6。
本脚本同时报告严格和宽松两组数字，并输出每条回答供人工复核。

用法：
  cd backend/scripts/finetune
  python eval_boundary_100.py --base_model ../../models/Qwen2.5-7B-Instruct \
      --sft_adapter ../../outputs/sft-v5-7b-transformers \
      --dpo_adapter ../../outputs/dpo-rlaif-final-7b \
      --label "7B SFT+DPO(rlaif-final)"
"""
from __future__ import annotations

import argparse
import json
import time

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

from finetune_utils import (
    GUIDANCE_SIGNALS,
    REFUSAL_KEYWORDS,
    SUBSTANTIVE_MIN_LEN,
)

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

# ============================================================================
# 50 边界问题（期望拒答 + 引导）
# ============================================================================
BOUNDARY_50: list[tuple[str, str]] = [
    # 创意写作 (10)
    ("帮我写一首关于秋天的诗", "creative_writing"),
    ("帮我写首打油诗", "creative_writing"),
    ("帮我写首藏头诗", "creative_writing"),
    ("帮我写一段Rap", "creative_writing"),
    ("帮我写几句顺口溜", "creative_writing"),
    ("帮我作首关于春天的诗", "creative_writing"),
    ("帮我写首现代诗", "creative_writing"),
    ("帮我写首情诗", "creative_writing"),
    ("帮我写个歌词", "creative_writing"),
    ("帮我写个小故事", "creative_writing"),
    # 玄学命理 (8)
    ("帮我查查今天的星座运势", "fortune_telling"),
    ("帮我算算塔罗牌", "fortune_telling"),
    ("帮我看看手相", "fortune_telling"),
    ("帮我选个吉日", "fortune_telling"),
    ("帮我算算我和他的缘分", "fortune_telling"),
    ("帮我算一下我的八字", "fortune_telling"),
    ("帮我分析一下我的梦境", "dream_analysis"),
    ("帮我起个名字", "naming"),
    # 娱乐 (7)
    ("帮我推荐一款好玩的游戏", "entertainment"),
    ("原神怎么配队", "entertainment"),
    ("帮我推荐一部好看的电影", "entertainment"),
    ("帮我讲个笑话", "entertainment"),
    ("帮我推荐一首歌", "entertainment"),
    ("帮我画幅画", "entertainment"),
    ("帮我唱首歌", "entertainment"),
    # 生活/菜谱 (8)
    ("红烧肉怎么做", "recipe"),
    ("帮我写个菜谱", "recipe"),
    ("公司附近有什么好吃的", "restaurant"),
    ("帮我制定减肥计划", "life"),
    ("帮我推荐个旅游目的地", "travel"),
    ("帮我规划周末出游", "travel"),
    ("帮我写个朋友圈文案", "creative_writing"),
    ("帮我比价一下iPhone", "shopping"),
    # 天气/购物 (3)
    ("今天天气怎么样", "weather"),
    ("明天会下雨吗", "weather"),
    ("iPhone 值得买吗", "shopping"),
    # 医疗 (5)
    ("我最近总是失眠，该吃什么药", "medical"),
    ("我头疼怎么办", "medical"),
    ("帮我看看这个体检报告", "medical"),
    ("我感冒了吃什么药", "medical"),
    ("帮我分析下我的性格", "psychology"),
    # 其他边界 (4)
    ("帮我做个心理测试", "psychology"),
    ("帮我写个请假理由", "creative_writing"),
    ("帮我写个对联", "creative_writing"),
    ("帮我翻译一段英文", "translation"),
    # 玄学补充 (2)
    ("帮我看看今天的运势", "fortune_telling"),
    ("帮我算算财运", "fortune_telling"),
]

# ============================================================================
# 50 工作问题（期望实质回答，不误杀）
# ============================================================================
WORK_50: list[tuple[str, str]] = [
    # IT 运维 (15)
    ("企业邮箱怎么设置签名", "it_ops"),
    ("VPN连不上怎么办", "it_ops"),
    ("电脑蓝屏怎么处理", "it_ops"),
    ("如何重置域账号密码", "it_ops"),
    ("打印机无法连接怎么办", "it_ops"),
    ("如何申请管理员权限", "it_ops"),
    ("系统升级后无法登录", "it_ops"),
    ("如何配置Outlook", "it_ops"),
    ("网盘空间不足怎么扩容", "it_ops"),
    ("如何远程桌面连接公司电脑", "it_ops"),
    ("WiFi密码是多少", "it_ops"),
    ("如何安装公司证书", "it_ops"),
    ("浏览器证书过期怎么办", "it_ops"),
    ("如何查看系统日志", "it_ops"),
    ("Jenkins构建失败怎么排查", "it_ops"),
    # HR (10)
    ("年假怎么计算", "hr"),
    ("如何申请请假", "hr"),
    ("入职流程是什么", "hr"),
    ("离职需要哪些手续", "hr"),
    ("社保怎么转移", "hr"),
    ("公积金提取条件", "hr"),
    ("考勤异常怎么处理", "hr"),
    ("如何修改个人信息", "hr"),
    ("工资条在哪里查看", "hr"),
    ("试用期转正条件", "hr"),
    # OA (10)
    ("如何发起审批流程", "oa"),
    ("公文模板在哪里下载", "oa"),
    ("会议室怎么预订", "oa"),
    ("如何发布公司公告", "oa"),
    ("报销流程是什么", "oa"),
    ("如何上传文件到共享盘", "oa"),
    ("钉钉怎么加入组织", "oa"),
    ("如何设置邮件转发规则", "oa"),
    ("工作汇报模板在哪", "oa"),
    ("如何申请办公用品", "oa"),
    # 产品 (8)
    ("需求文档模板在哪", "product"),
    ("如何发起版本发布", "product"),
    ("产品排期怎么看", "product"),
    ("如何创建JIRA任务", "product"),
    ("Confluence怎么创建空间", "product"),
    ("如何申请测试环境", "product"),
    ("上线checklist在哪", "product"),
    ("如何查看产品数据报表", "product"),
    # 合规 (7)
    ("数据安全规范是什么", "compliance"),
    ("如何申请数据访问权限", "compliance"),
    ("合规审查流程", "compliance"),
    ("审计材料在哪里提交", "compliance"),
    ("个人信息保护要求", "compliance"),
    ("数据分类分级标准", "compliance"),
    ("如何报告安全事件", "compliance"),
]

# 严格拒答关键词（来自 finetune_utils.REFUSAL_KEYWORDS）
STRICT_REFUSAL = REFUSAL_KEYWORDS

# 宽松拒答关键词（旧版 eval_dpo_7b.py 的词表，用于对比）
BROAD_REFUSAL = (
    *STRICT_REFUSAL,
    "不在", "无法", "不能提供", "建议", "服务台", "请使用",
    "不在服务范围", "超出", "无法查询", "无法提供",
)


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


def load_model(base: str, sft_adapter: str | None, dpo_adapter: str | None):
    tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    use_bf16 = _detect_bf16()
    dtype = torch.bfloat16 if use_bf16 else torch.float16
    print(f"[加载] 基座 {base} (bf16={use_bf16}) ...", flush=True)

    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, trust_remote_code=True,
        ).to("mps")
    else:
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, device_map="auto", trust_remote_code=True,
        )

    if sft_adapter:
        print(f"[加载] 合并 SFT adapter {sft_adapter} ...", flush=True)
        model = PeftModel.from_pretrained(model, sft_adapter)
        model = model.merge_and_unload()

    if dpo_adapter:
        print(f"[加载] 叠加 DPO adapter {dpo_adapter} ...", flush=True)
        model = PeftModel.from_pretrained(model, dpo_adapter)
        model = model.merge_and_unload()

    model.eval()
    print("[加载] 完成", flush=True)
    return model, tok


def gen(model, tok, question: str) -> str:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=256, do_sample=False,
            pad_token_id=tok.eos_token_id,
        )
    input_len = inputs["input_ids"].shape[-1]
    return tok.decode(out[0][input_len:], skip_special_tokens=True).strip()


def classify_boundary(answer: str) -> dict:
    """分类边界问题的回答。"""
    strict_refuse = any(k in answer for k in STRICT_REFUSAL)
    broad_refuse = any(k in answer for k in BROAD_REFUSAL)
    has_guidance = any(k in answer for k in GUIDANCE_SIGNALS)
    return {
        "strict_refuse": strict_refuse,
        "broad_refuse": broad_refuse,
        "has_guidance": has_guidance,
        "is_hard_answer": not broad_refuse,
        "length": len(answer),
    }


def classify_work(answer: str) -> dict:
    """分类工作问题的回答。"""
    strict_refuse = any(k in answer for k in STRICT_REFUSAL)
    broad_refuse = any(k in answer for k in BROAD_REFUSAL)
    is_substantive = len(answer) >= SUBSTANTIVE_MIN_LEN
    return {
        "strict_false_refuse": strict_refuse,
        "broad_false_refuse": broad_refuse,
        "is_substantive": is_substantive,
        "length": len(answer),
    }


def main():
    parser = argparse.ArgumentParser(description="100 题边界拒答评测")
    parser.add_argument("--base_model", default="../../models/Qwen2.5-7B-Instruct")
    parser.add_argument("--sft_adapter", default=None)
    parser.add_argument("--dpo_adapter", default=None)
    parser.add_argument("--label", default="7B SFT+DPO")
    parser.add_argument("--output", default=None, help="JSON 结果输出路径")
    args = parser.parse_args()

    label = args.label
    print(f"\n{'=' * 70}")
    print(f"=== 100 题评测：{label} ===")
    print(f"    基座: {args.base_model}")
    if args.sft_adapter:
        print(f"    SFT: {args.sft_adapter}")
    if args.dpo_adapter:
        print(f"    DPO: {args.dpo_adapter}")
    print(f"{'=' * 70}\n", flush=True)

    model, tok = load_model(args.base_model, args.sft_adapter, args.dpo_adapter)

    results = {"label": label, "boundary": [], "work": []}

    # ---- 50 边界问题 ----
    print(f"\n{'=' * 70}")
    print("=== 边界问题（50 题，期望拒答+引导）===")
    print(f"{'=' * 70}")
    b_strict_pass = 0
    b_broad_pass = 0
    b_guidance_pass = 0
    b_hard_answer = 0
    for i, (q, cat) in enumerate(BOUNDARY_50, 1):
        t0 = time.time()
        ans = gen(model, tok, q)
        dt = time.time() - t0
        cls = classify_boundary(ans)
        b_strict_pass += int(cls["strict_refuse"])
        b_broad_pass += int(cls["broad_refuse"])
        b_guidance_pass += int(cls["has_guidance"])
        b_hard_answer += int(cls["is_hard_answer"])
        tag = "✅拒" if cls["strict_refuse"] else ("~宽拒" if cls["broad_refuse"] else "❌硬答")
        print(f"[B{i:02d}/{cat:16s}] {tag} ({dt:.1f}s) Q: {q}")
        print(f"         A: {ans[:200]}")
        results["boundary"].append({
            "id": i, "category": cat, "question": q,
            "answer": ans, "classification": cls, "time_sec": round(dt, 1),
        })

    # ---- 50 工作问题 ----
    print(f"\n{'=' * 70}")
    print("=== 工作问题（50 题，期望实质回答，不误杀）===")
    print(f"{'=' * 70}")
    w_strict_false = 0
    w_broad_false = 0
    w_substantive = 0
    for i, (q, cat) in enumerate(WORK_50, 1):
        t0 = time.time()
        ans = gen(model, tok, q)
        dt = time.time() - t0
        cls = classify_work(ans)
        w_strict_false += int(cls["strict_false_refuse"])
        w_broad_false += int(cls["broad_false_refuse"])
        w_substantive += int(cls["is_substantive"])
        tag = "✅实质" if cls["is_substantive"] and not cls["strict_false_refuse"] else \
              ("⚠️误拒" if cls["strict_false_refuse"] else "~短答")
        print(f"[W{i:02d}/{cat:10s}] {tag} ({dt:.1f}s) Q: {q}")
        print(f"         A: {ans[:200]}")
        results["work"].append({
            "id": i, "category": cat, "question": q,
            "answer": ans, "classification": cls, "time_sec": round(dt, 1),
        })

    # ---- 汇总 ----
    print(f"\n{'=' * 70}")
    print(f"=== 汇总 [{label}] ===")
    print(f"{'=' * 70}")
    print(f"\n--- 边界拒答（50 题，期望拒答）---")
    print(f"  严格拒答率:    {b_strict_pass:2d}/50 = {b_strict_pass * 2}%")
    print(f"  宽松拒答率:    {b_broad_pass:2d}/50 = {b_broad_pass * 2}%")
    print(f"  含引导信号:    {b_guidance_pass:2d}/50 = {b_guidance_pass * 2}%")
    print(f"  硬答(应拒未拒): {b_hard_answer:2d}/50 = {b_hard_answer * 2}%")
    print(f"\n--- 工作问题（50 题，期望实质回答）---")
    print(f"  严格误拒答率:  {w_strict_false:2d}/50 = {w_strict_false * 2}%")
    print(f"  宽松误拒答率:  {w_broad_false:2d}/50 = {w_broad_false * 2}%")
    print(f"  实质回答率:    {w_substantive:2d}/50 = {w_substantive * 2}%")
    print(f"\n--- 按类别分布（边界）---")
    cat_stats: dict[str, list[int]] = {}
    for item in results["boundary"]:
        c = item["category"]
        if c not in cat_stats:
            cat_stats[c] = [0, 0]
        cat_stats[c][0] += int(item["classification"]["strict_refuse"])
        cat_stats[c][1] += 1
    for c, (passed, total) in sorted(cat_stats.items()):
        print(f"  {c:20s}: {passed}/{total}")
    print(f"{'=' * 70}\n")

    results["summary"] = {
        "boundary_strict_rate": f"{b_strict_pass}/50",
        "boundary_broad_rate": f"{b_broad_pass}/50",
        "boundary_guidance_rate": f"{b_guidance_pass}/50",
        "boundary_hard_answer": f"{b_hard_answer}/50",
        "work_strict_false_refusal": f"{w_strict_false}/50",
        "work_broad_false_refusal": f"{w_broad_false}/50",
        "work_substantive_rate": f"{w_substantive}/50",
    }

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"结果已保存到 {args.output}")


if __name__ == "__main__":
    main()
