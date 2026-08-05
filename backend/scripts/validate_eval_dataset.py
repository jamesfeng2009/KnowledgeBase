#!/usr/bin/env python
"""
评测数据集验证脚本 — 检查 JSONL 数据集格式、必填字段、标签覆盖度。

CI 集成：在 CI 流水线中作为 PR 门禁，确保评测数据集格式合法。

使用示例::

    python scripts/validate_eval_dataset.py eval_datasets/
    python scripts/validate_eval_dataset.py eval_datasets/p0_mandatory.jsonl

退出码：
    0 — 全部通过
    1 — 存在格式错误或字段缺失
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 必填字段
REQUIRED_FIELDS = {"query", "expected_doc_ids"}

# 可选字段（§5.6 规则评分 / §7.3 上下文管理扩展字段已登记）
OPTIONAL_FIELDS = {
    "expected_answer",
    "kb_ids",
    "tags",
    "case_id",
    "case_type",
    "must_have_points",
    "forbidden_content",
    "context_expect",
}

#: 合法用例类型（case_type 字段）
VALID_CASE_TYPES = {"normal", "negative", "golden"}

#: context_expect 支持的判定字段（§9.3 p4_context 七类样本）
CONTEXT_EXPECT_KEYS = {
    "type",
    "required_files",
    "distractor_files",
    "forbidden_files",
    "stale_refs",
    "required_after_compact",
}

# 标准评测维度（tags 中应包含至少一个）
STANDARD_DIMENSIONS = {
    "exact_match",
    "semantic",
    "synonym",
    "cross_lingual",
    "fuzzy",
    "multi_constraint",
    "ordering",
    "generation",
    "boundary",
    "tenant_isolation",
    "context",
}

#: 允许 expected_doc_ids 为空列表的标签（安全/边界/注入类负样本）
EMPTY_DOC_IDS_TAGS = {"boundary", "tenant_isolation", "prompt_injection"}

# 各数据集文件期望的用例数量下限
MIN_CASES = {
    "p0_mandatory": 100,
    "p1_complete": 200,
    "p2_security": 15,
    "p4_context": 7,
    "p5_generation": 6,
    "sample": 1,
}


def validate_jsonl_file(file_path: str) -> list[str]:
    """验证单个 JSONL 文件，返回错误列表。"""
    errors: list[str] = []
    file_name = Path(file_path).stem
    line_no = 0
    case_count = 0
    dimensions_found: set[str] = set()

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line_no += 1
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{file_path}:{line_no} JSON 解析失败: {exc}")
                continue

            case_count += 1

            # 检查必填字段
            for field in REQUIRED_FIELDS:
                if field not in data:
                    errors.append(
                        f"{file_path}:{line_no} 缺少必填字段: {field}"
                    )
                elif field == "expected_doc_ids":
                    if not isinstance(data[field], list):
                        errors.append(
                            f"{file_path}:{line_no} expected_doc_ids 必须为列表"
                        )
                    # 允许空列表（安全/边界/注入负样本期望不返回任何文档）
                    # 普通用例空列表给出警告
                    elif len(data[field]) == 0:
                        tags = set(data.get("tags", []))
                        is_negative = data.get("case_type") == "negative"
                        if not tags & EMPTY_DOC_IDS_TAGS and not is_negative:
                            errors.append(
                                f"{file_path}:{line_no} expected_doc_ids 为空列表"
                                f"（仅 boundary/tenant_isolation/prompt_injection"
                                f"/negative 用例允许）"
                            )

            # 校验 case_type 合法性
            case_type = data.get("case_type")
            if case_type is not None and case_type not in VALID_CASE_TYPES:
                errors.append(
                    f"{file_path}:{line_no} case_type 非法: {case_type}"
                    f"（应为 {sorted(VALID_CASE_TYPES)} 之一）"
                )

            # 校验 context_expect 结构
            context_expect = data.get("context_expect")
            if context_expect is not None:
                if not isinstance(context_expect, dict):
                    errors.append(
                        f"{file_path}:{line_no} context_expect 必须为对象"
                    )
                else:
                    unknown_keys = set(context_expect.keys()) - CONTEXT_EXPECT_KEYS
                    if unknown_keys:
                        errors.append(
                            f"{file_path}:{line_no} context_expect 含未知字段:"
                            f" {sorted(unknown_keys)}"
                        )

            # 检查 query 非空
            if "query" in data and not isinstance(data["query"], str) or (
                "query" in data and not data["query"].strip()
            ):
                errors.append(f"{file_path}:{line_no} query 不能为空")

            # 检查 tags 包含至少一个标准维度
            tags = data.get("tags", [])
            if isinstance(tags, list):
                dims = STANDARD_DIMENSIONS & set(tags)
                if dims:
                    dimensions_found.update(dims)

            # 检查未知字段（warn 级别，不报错）
            all_known = REQUIRED_FIELDS | OPTIONAL_FIELDS
            unknown = set(data.keys()) - all_known
            if unknown:
                # 未知字段不报错，只记录
                pass

    # 检查用例数量
    expected_min = MIN_CASES.get(file_name, 0)
    if expected_min > 0 and case_count < expected_min:
        errors.append(
            f"{file_path}: 用例数量 {case_count} 少于期望下限 {expected_min}"
        )

    # 检查维度覆盖（仅对非 sample 文件）
    if file_name != "sample" and case_count > 0:
        if not dimensions_found:
            errors.append(
                f"{file_path}: 未找到任何标准评测维度标签 "
                f"(应为 {STANDARD_DIMENSIONS} 之一)"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        print("用法: python validate_eval_dataset.py <path>", file=sys.stderr)
        return 1

    target = args[0]
    jsonl_files: list[str] = []

    if os.path.isdir(target):
        for fname in sorted(os.listdir(target)):
            if fname.endswith(".jsonl"):
                jsonl_files.append(os.path.join(target, fname))
    elif os.path.isfile(target) and target.endswith(".jsonl"):
        jsonl_files.append(target)
    else:
        print(f"[ERROR] 路径不存在: {target}", file=sys.stderr)
        return 1

    if not jsonl_files:
        print(f"[ERROR] 未找到 JSONL 文件: {target}", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    total_cases = 0

    for fpath in jsonl_files:
        errors = validate_jsonl_file(fpath)
        all_errors.extend(errors)
        # 统计用例数
        with open(fpath, encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip() and not line.startswith("#"))
        total_cases += count
        status = "PASS" if not errors else "FAIL"
        print(f"  [{status}] {fpath} ({count} cases)")
        for err in errors:
            print(f"         {err}")

    print("-" * 60)
    print(f"总计: {len(jsonl_files)} 文件, {total_cases} 用例, {len(all_errors)} 错误")

    if all_errors:
        print("[FAIL] 数据集验证未通过", file=sys.stderr)
        return 1

    print("[PASS] 数据集验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
