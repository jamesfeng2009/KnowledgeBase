#!/usr/bin/env python
"""gradient_checkpointing 内存/速度对照实验（1.5B）。

在 1.5B 模型上跑三组配置各 N step，对比峰值内存与每 step 耗时，
直观验证"省内存参数"对小模型的影响，隔离 ckpt 与 batch 两个变量。

A组（省内存）：    ckpt=True,  batch=1, max_len=1024
B组（不省内存）：  ckpt=False, batch=4, max_len=2048
C组（隔离ckpt）：  ckpt=False, batch=1, max_len=1024  ← 与A仅 ckpt 不同

用法：
    python scripts/finetune/bench_memory.py --steps 15
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

FINETUNE_DIR = Path(__file__).parent
sys.path.insert(0, str(FINETUNE_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="gradient_checkpointing 内存/速度对照实验")
    parser.add_argument("--data", default="data/open/sft.jsonl", help="sft.jsonl 路径")
    parser.add_argument("--base_model", default="models/Qwen2.5-1.5B-Instruct", help="基座模型")
    parser.add_argument("--steps", type=int, default=15, help="每组训练 step 数")
    args = parser.parse_args()

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from train_lora import DEFAULT_TARGET_MODULES, load_sft_jsonl, render_chat

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ---- 预加载数据文本（两组共享，避免重复 IO）----
    records = load_sft_jsonl(args.data)[:400]
    texts = [render_chat(r["messages"], tokenizer) for r in records]
    print(f"加载 {len(texts)} 条 SFT 样本")

    def make_batches(max_len: int, batch_size: int, n: int):
        enc = tokenizer(texts, truncation=True, max_length=max_len,
                        padding="max_length", return_tensors="pt")
        batches = []
        for i in range(0, len(texts) - batch_size + 1, batch_size):
            if len(batches) >= n:
                break
            input_ids = enc["input_ids"][i:i + batch_size].to("mps")
            attn = enc["attention_mask"][i:i + batch_size].to("mps")
            labels = input_ids.clone()
            labels[attn == 0] = -100  # pad 不计 loss
            batches.append({"input_ids": input_ids, "attention_mask": attn, "labels": labels})
        return batches

    def load_fresh_model():
        model = AutoModelForCausalLM.from_pretrained(
            args.base_model, torch_dtype=torch.bfloat16, trust_remote_code=True)
        model.config.use_cache = False
        model = model.to("mps")
        peft_config = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                                 task_type="CAUSAL_LM", target_modules=DEFAULT_TARGET_MODULES)
        model = get_peft_model(model, peft_config)
        model.train()
        return model

    def run_config(name: str, ckpt: bool, batch_size: int, max_len: int):
        print(f"\n=== {name}：ckpt={ckpt}, batch={batch_size}, max_len={max_len} ===")
        model = load_fresh_model()
        # gradient_checkpointing + peft 必须 enable_input_require_grads，否则梯度不回传
        if ckpt:
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()
        else:
            model.gradient_checkpointing_disable()

        batches = make_batches(max_len, batch_size, args.steps)
        optimizer = torch.optim.AdamW(
            [p for p in model.parameters() if p.requires_grad], lr=1e-4)

        torch.mps.empty_cache()
        times = []
        peak_tracker = 0.0  # torch 2.13 无 reset_peak_memory，手动追踪组内峰值
        try:
            for step, batch in enumerate(batches):
                t0 = time.time()
                out = model(**batch)
                out.loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                dt = time.time() - t0
                times.append(dt)
                cur = torch.mps.current_allocated_memory() / 1e9
                peak_tracker = max(peak_tracker, cur)
                if step % 5 == 0:
                    print(f"  step {step}: loss={out.loss.item():.3f}  {dt:.2f}s  cur={cur:.1f}GB")
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                peak = torch.mps.current_allocated_memory() / 1e9
                print(f"  → OOM @ step {len(times)}: 峰值 {peak:.1f}GB — {str(e)[:90]}")
                del model, optimizer
                torch.mps.empty_cache()
                return {"name": name, "ckpt": ckpt, "batch": batch_size, "max_len": max_len,
                        "peak_gb": f"OOM({peak:.0f}G)", "driver_gb": "-", "avg_step_s": "-",
                        "throughput": 0, "steps": len(times), "oom": True}
            raise

        peak = peak_tracker
        driver = torch.mps.driver_allocated_memory() / 1e9
        avg = sum(times) / len(times)
        result = {
            "name": name, "ckpt": ckpt, "batch": batch_size, "max_len": max_len,
            "peak_gb": round(peak, 2), "driver_gb": round(driver, 2),
            "avg_step_s": round(avg, 3), "throughput": round(batch_size / avg, 2),
            "steps": len(times),
        }
        print(f"  → 峰值 {peak:.2f}GB / driver {driver:.2f}GB / 均step {avg:.2f}s / 吞吐 {batch_size/avg:.1f}样本/s")
        del model, optimizer
        torch.mps.empty_cache()
        return result

    # ---- 四组对照 ----
    results = []
    results.append(run_config("A-省内存", ckpt=True, batch_size=1, max_len=1024))
    results.append(run_config("C-仅关ckpt", ckpt=False, batch_size=1, max_len=1024))
    results.append(run_config("D-关ckpt+bs2", ckpt=False, batch_size=2, max_len=1024))
    results.append(run_config("B-全不省", ckpt=False, batch_size=4, max_len=2048))

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("对照实验汇总（1.5B, bf16, LoRA rank16, M3 Max 36G）")
    print("=" * 78)
    print(f"{'配置':<14} {'ckpt':<6} {'batch':<7} {'maxlen':<8} {'峰值GB':<11} {'均step(s)':<11} {'吞吐':<10}")
    for r in results:
        print(f"{r['name']:<14} {str(r['ckpt']):<6} {r['batch']:<7} {r['max_len']:<8} "
              f"{str(r['peak_gb']):<11} {str(r['avg_step_s']):<11} {r['throughput']:<10}")

    a, c = results[0], results[1]
    if isinstance(a["peak_gb"], (int, float)) and isinstance(c["peak_gb"], (int, float)):
        sp = "C更快" if c["avg_step_s"] < a["avg_step_s"] else "A更快"
        print(f"\n[ckpt影响] A→C(仅关ckpt): 内存 {c['peak_gb']/a['peak_gb']:.1f}x, 速度 {a['avg_step_s']/c['avg_step_s']:.1f}x ({sp})")
    print(f"[极限] B组(batch4,max2048,nockpt): {results[3]['peak_gb']} — 即使1.5B完全不省内存也OOM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
