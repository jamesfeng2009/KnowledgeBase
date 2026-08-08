#!/usr/bin/env python
"""开源数据集导入脚本 — 把 HuggingFace 公开数据集转成本项目微调 pipeline 的 4 类 jsonl 格式。

支持的数据集（每个对应一个纯函数转换器）：
    1. T2Ranking  (C-MTEB/T2Reranking, split=dev)            → embedding.jsonl
       原始字段：query(str) / positive(list[str]) / negative(list[str])
       映射规则：query→query, positive[0]→pos, negative[0]→neg
       说明：T2Reranking 每条含 1 个 query + N 个正例段落 + N 个难负例段落，
             本函数各取第一个非空项构成单条三元组；meta.neg_type="random"
             表示从负例池中采样（区别于精排阶段的 hard negative 挖掘）。
             注意：C-MTEB/T2Retrieval 只有 corpus/queries 两个 split（评测格式），
             三元组训练数据需用 C-MTEB/T2Reranking（多了 positive/negative 列）。

    2. DuReader   (HongzheBi/DuReader2.0, split=train)       → sft.jsonl（问答对）
       原始字段：question(str) / answers(list[{text:str, answer_start:int}]
                                      或 {text:[str,...]})
       映射规则：question→user message, answers[0].text→assistant message
       说明：DuReader 2.0 每条含 1 个 question + N 个 answers，本函数取第一个
             非空 answer 作为 assistant 回复。answers 字段在不同子集格式有差异
            （list[dict] / dict / list[str]），_extract_first_answer 兼容三种。

    3. COIG-CQIA  (m-a-p/COIG-CQIA, config=wiki, split=train) → sft.jsonl
       原始字段：instruction(str) / input(str) / output(str)
                                  / task_type(list) / domain(list)
       映射规则：instruction(+input)→user message, output→assistant message
       说明：COIG-CQIA 遵循 Alpaca 指令格式（instruction + input + output）。
             input 非空时拼接到 instruction 后作为 user 消息上下文。
             config 默认 wiki，可改为 exam/finance/zhihu 等其他子集。

    4. DPO-En-Zh  (shibing624/DPO-En-Zh-20k-Preference, config=zh, split=train) → dpo.jsonl
       原始字段：question(str) / response_chosen(str) / response_rejected(str)
                                  / system(str) / history(list)
       映射规则：question→prompt, response_chosen→chosen, response_rejected→rejected
       说明：该数据集字段名为 question/response_chosen/response_rejected
             （非标准 prompt/chosen/rejected），且 chosen/rejected 均为纯字符串
             （非 messages 列表），可直接映射。system/history 暂不入 prompt
             （保持与 train_dpo.py 的 load_dpo_jsonl 三字段格式对齐）。

输出格式（与 backend/scripts/finetune/ 训练脚本对齐）：
    embedding.jsonl {"query":"...","pos":"...","neg":"...","meta":{...}}
    sft.jsonl       {"messages":[{"role":"user","content":"..."},
                                  {"role":"assistant","content":"..."}],
                     "meta":{...}}
    dpo.jsonl       {"prompt":"...","chosen":"...","rejected":"...","meta":{...}}

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "datasets>=2.20" "huggingface_hub>=0.24"

运行示例：
    # 导入全部 4 类数据集（每类最多 5000 条，便于作品集快速跑通）
    python scripts/finetune/import_open_dataset.py --dataset all \\
        --output_dir data/finetune_imported

    # 仅导入 T2Ranking（embedding 训练数据），限 1000 条
    python scripts/finetune/import_open_dataset.py --dataset t2ranking \\
        --output_dir data/finetune_imported --limit 1000

    # 指定 split（覆盖各数据集预设值）
    python scripts/finetune/import_open_dataset.py --dataset coig_cqia \\
        --output_dir data/finetune_imported --split train
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

logger = logging.getLogger("import_open_dataset")

#: 各数据集的 HuggingFace 配置（名称 / config / split / 输出文件 / 来源标签）
DATASET_CONFIGS: dict[str, dict[str, str]] = {
    "t2ranking": {
        "hf_name": "C-MTEB/T2Reranking",
        "config": "default",
        "split": "dev",
        "output_file": "embedding.jsonl",
        "source_label": "t2ranking",
    },
    "dureader": {
        "hf_name": "HongzheBi/DuReader2.0",
        "config": "default",
        "split": "train",
        "output_file": "sft.jsonl",
        "source_label": "dureader",
    },
    "coig_cqia": {
        "hf_name": "m-a-p/COIG-CQIA",
        "config": "wiki",
        "split": "train",
        "output_file": "sft.jsonl",
        "source_label": "coig_cqia",
    },
    "dpo_en_zh": {
        "hf_name": "shibing624/DPO-En-Zh-20k-Preference",
        "config": "zh",
        "split": "train",
        "output_file": "dpo.jsonl",
        "source_label": "dpo_en_zh",
    },
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _first_nonempty(items: object) -> str | None:
    """从字符串或字符串列表中取第一个非空项（strip 后非空）。

    兼容两种输入：
        - str：直接 strip 后返回（若非空）
        - list[str]：遍历取第一个 strip 后非空的元素

    用于 T2Reranking 的 positive/negative 列表字段（每条含多个候选段落）。
    """
    if isinstance(items, str):
        return items.strip() or None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _extract_first_answer(answers: object) -> str | None:
    """从 DuReader answers 字段提取第一个非空回答文本。

    DuReader 不同版本/子集的 answers 格式存在差异：
        - HongzheBi/DuReader2.0: list[{"text": str, "answer_start": int}, ...]
        - luozhouyang/dureader:  {"text": [str, ...], "answer_start": [int, ...]}
        - 部分版本:              list[str]
    本函数兼容以上三种结构，统一返回第一个非空回答文本。
    """
    if isinstance(answers, list):
        for ans in answers:
            if isinstance(ans, dict):
                text = ans.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
            elif isinstance(ans, str) and ans.strip():
                return ans.strip()
    elif isinstance(answers, dict):
        texts = answers.get("text")
        if isinstance(texts, list):
            for text in texts:
                if isinstance(text, str) and text.strip():
                    return text.strip()
        elif isinstance(texts, str) and texts.strip():
            return texts.strip()
    return None


# ---------------------------------------------------------------------------
# 转换函数（纯函数，可独立测试，不依赖网络 / datasets 库）
# ---------------------------------------------------------------------------

def convert_t2ranking_record(raw: dict) -> dict | None:
    """T2Ranking (C-MTEB/T2Reranking) 单条记录 → embedding.jsonl 三元组。

    字段映射：
        raw["query"]    (str)         → query
        raw["positive"] (list[str])   → pos（取第一个非空段落）
        raw["negative"] (list[str])   → neg（取第一个非空段落）

    不合法（query/pos/neg 任一为空）返回 None。
    """
    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    # T2Reranking 的 positive/negative 是段落列表，各取第一个非空项
    pos = _first_nonempty(raw.get("positive"))
    neg = _first_nonempty(raw.get("negative"))
    if not pos or not neg:
        return None
    return {
        "query": query.strip(),
        "pos": pos,
        "neg": neg,
        "meta": {"source": "t2ranking", "neg_type": "random"},
    }


def convert_dureader_record(raw: dict) -> dict | None:
    """DuReader (HongzheBi/DuReader2.0) 单条记录 → sft.jsonl 问答对。

    字段映射：
        raw["question"] (str)                    → messages[0] (user)
        raw["answers"]  (list[{text}] / dict)    → messages[1] (assistant，取首个非空回答)

    不合法（question 为空或无有效 answer）返回 None。
    """
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    answer_text = _extract_first_answer(raw.get("answers"))
    if not answer_text:
        return None
    return {
        "messages": [
            {"role": "user", "content": question.strip()},
            {"role": "assistant", "content": answer_text},
        ],
        "meta": {"source": "dureader"},
    }


def convert_coig_cqia_record(raw: dict) -> dict | None:
    """COIG-CQIA (m-a-p/COIG-CQIA, subset=wiki) 单条记录 → sft.jsonl。

    字段映射：
        raw["instruction"] (str) → messages[0] (user)，若 input 非空则拼接
        raw["input"]       (str) → 拼接到 instruction 后作为上下文
        raw["output"]      (str) → messages[1] (assistant)

    不合法（instruction 或 output 为空）返回 None。
    """
    instruction = raw.get("instruction")
    output = raw.get("output")
    if not isinstance(instruction, str) or not instruction.strip():
        return None
    if not isinstance(output, str) or not output.strip():
        return None
    user_content = instruction.strip()
    input_text = raw.get("input")
    if isinstance(input_text, str) and input_text.strip():
        # Alpaca 格式：input 作为补充上下文拼接到 instruction 后
        user_content = f"{user_content}\n\n{input_text.strip()}"
    return {
        "messages": [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": output.strip()},
        ],
        "meta": {"source": "coig_cqia", "subset": "wiki"},
    }


def convert_dpo_record(raw: dict) -> dict | None:
    """shibing624/DPO-En-Zh-20k-Preference (config=zh) 单条记录 → dpo.jsonl。

    字段映射：
        raw["question"]          (str) → prompt
        raw["response_chosen"]   (str) → chosen
        raw["response_rejected"] (str) → rejected

    不合法（三字段任一为空，或 chosen==rejected）返回 None。
    """
    question = raw.get("question")
    chosen = raw.get("response_chosen")
    rejected = raw.get("response_rejected")
    if not all(isinstance(x, str) and x.strip() for x in (question, chosen, rejected)):
        return None
    if chosen.strip() == rejected.strip():
        # chosen 与 rejected 相同则无偏好信号，跳过（与 train_dpo.py load_dpo_jsonl 一致）
        return None
    return {
        "prompt": question.strip(),
        "chosen": chosen.strip(),
        "rejected": rejected.strip(),
        "meta": {"source": "dpo_en_zh"},
    }


#: 转换函数映射表（在函数定义后构建）
CONVERTERS = {
    "t2ranking": convert_t2ranking_record,
    "dureader": convert_dureader_record,
    "coig_cqia": convert_coig_cqia_record,
    "dpo_en_zh": convert_dpo_record,
}


# ---------------------------------------------------------------------------
# JSONL 写入
# ---------------------------------------------------------------------------

def write_jsonl(records: list[dict], path: str | Path) -> int:
    """将记录列表写入 JSONL 文件（ensure_ascii=False，中文原样落盘）。

    返回写入条数。父目录不存在时自动创建。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
    return len(records)


# ---------------------------------------------------------------------------
# HuggingFace 数据集下载 + 转换
# ---------------------------------------------------------------------------

def _load_hf_dataset(hf_name: str, config: str, split: str):
    """下载 HuggingFace 数据集（重依赖 datasets 延迟导入）。

    失败时抛出清晰的 ImportError / RuntimeError，提示用户安装依赖或检查网络。
    """
    try:
        from datasets import load_dataset  # 延迟导入：无网络环境可 import 本模块做单测
    except ImportError as exc:
        raise ImportError(
            f"无法导入 datasets 库：{exc}\n"
            '请安装 ML 工具链：pip install "datasets>=2.20" "huggingface_hub>=0.24"'
        ) from exc

    try:
        # trust_remote_code 兼容老式加载脚本（datasets<4.0）；4.0+ 移除该参数
        try:
            return load_dataset(hf_name, config, split=split, trust_remote_code=True)
        except TypeError:
            return load_dataset(hf_name, config, split=split)
    except Exception as exc:
        raise RuntimeError(
            f"下载 HuggingFace 数据集失败：{hf_name} (config={config}, split={split})\n"
            f"原因：{exc}\n"
            '请检查网络连接，并确保已安装：pip install "datasets>=2.20" "huggingface_hub>=0.24"'
        ) from exc


def load_and_convert(
    dataset_key: str, limit: int, split: str | None = None
) -> tuple[list[dict], int, int]:
    """下载 HuggingFace 数据集并逐条转换为本项目 jsonl 格式。

    Args:
        dataset_key: DATASET_CONFIGS 中的键（t2ranking / dureader / coig_cqia / dpo_en_zh）
        limit: 每类最大转换条数（达到后停止迭代）
        split: 覆盖默认 split；None 则使用 DATASET_CONFIGS 预设值

    Returns:
        (records, converted_count, skipped_count)
    """
    cfg = DATASET_CONFIGS[dataset_key]
    hf_split = split or cfg["split"]
    ds = _load_hf_dataset(cfg["hf_name"], cfg["config"], hf_split)

    convert_fn = CONVERTERS[dataset_key]
    records: list[dict] = []
    skipped = 0
    for raw in ds:
        if len(records) >= limit:
            break
        rec = convert_fn(raw)
        if rec is None:
            skipped += 1
        else:
            records.append(rec)
    return records, len(records), skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="开源数据集导入 — HuggingFace 公开数据集 → 微调 jsonl",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        choices=["t2ranking", "dureader", "coig_cqia", "dpo_en_zh", "all"],
        default="all",
        help="导入的数据集（all = 全部 4 类）",
    )
    parser.add_argument(
        "--output_dir",
        default="data/finetune_imported",
        help="jsonl 输出目录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="每类最大转换条数（便于作品集快速跑通）",
    )
    parser.add_argument(
        "--split",
        default=None,
        help="覆盖数据集 split（默认使用各数据集预设值，多数为 train；"
        "T2Reranking 仅有 dev）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    random.seed(42)  # 可复现性

    if args.dataset == "all":
        keys = list(DATASET_CONFIGS.keys())
    else:
        keys = [args.dataset]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 按输出文件分组收集记录（多个数据集可能输出到同一文件，如 sft.jsonl）
    file_records: dict[str, list[dict]] = {}
    stats: list[dict] = []

    for key in keys:
        cfg = DATASET_CONFIGS[key]
        logger.info("正在处理数据集：%s (%s, config=%s)", key, cfg["hf_name"], cfg["config"])
        try:
            records, converted, skipped = load_and_convert(key, args.limit, args.split)
        except (ImportError, RuntimeError) as exc:
            logger.error("%s", exc)
            stats.append({
                "dataset": key,
                "error": str(exc).splitlines()[0],
                "converted": 0,
                "skipped": 0,
                "output": str(output_dir / cfg["output_file"]),
            })
            continue

        out_file = cfg["output_file"]
        file_records.setdefault(out_file, []).extend(records)
        out_path = output_dir / out_file
        stats.append({
            "dataset": key,
            "hf_name": cfg["hf_name"],
            "converted": converted,
            "skipped": skipped,
            "output": str(out_path),
        })

    # 统一写入（同文件多数据集记录合并）
    for out_file, records in file_records.items():
        out_path = output_dir / out_file
        write_jsonl(records, out_path)
        logger.info("写入 %s：%d 条 → %s", out_file, len(records), out_path)

    # 打印统计
    print("\n===== 导入统计 =====")
    for s in stats:
        if s.get("error"):
            print(f"  [{s['dataset']}] 失败：{s['error']}")
        else:
            print(
                f"  [{s['dataset']}] 转换 {s['converted']} 条，"
                f"跳过 {s['skipped']} 条 → {s['output']}"
            )
    print("=" * 30)

    # 全部失败则返回 1
    if all(s.get("error") for s in stats):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
