"""反思器 — LLM 阅读诊断样本与已拒清单，产出有界 JSON 编辑提案。

Optimizer prompt 硬规则（SkillOpt 纪律的 LLM 侧约束）：
- 只修多样本共性的系统性问题，不修单条轶事；
- 禁止硬编码具体问答对（指引必须可泛化）；
- 最多 budget 条编辑；无值得修改之处输出 {"edits": []}；
- 红线区不在输入中（代码侧守卫），LLM 无从要求编辑它。
"""

from __future__ import annotations

import json
from typing import Any

from app.evolution.editor import EditOp
from app.llm.base import LLMProvider, Message
from app.utils.logger import get_logger

logger = get_logger(__name__)

_OPTIMIZER_SYSTEM_PROMPT = """你是「技能文档优化器」。你的任务是根据失败诊断，改进一段「生成层基础指引」（企业知识库 RAG 答案的通用行为指引）。

## 输入
1. 当前指引（每行带行号，行号相对指引区，从 1 开始）；
2. 弱样本诊断：近期答得不好的问题、答案摘录与判官评分理由；
3. 已拒编辑清单（禁区）：此前被门控拒绝的编辑及其失败原因。

## 编辑规则（必须全部遵守）
1. 只输出 JSON，格式：{{"edits": [{{"op": "replace|append|delete", "line": 行号, "text": "新文本", "reason": "编辑理由"}}]}}；
2. 最多 {budget} 条编辑；replace/delete 必须给有效行号，append 不需要 line；
3. 每条编辑只针对**多样本共性**的系统性问题，给出**一般化**的行为指引；禁止针对单个问题硬编码具体问答对；
4. 禁止在 text 中出现 "##" 或 "红线" 字样；text 必须是单行；
5. 保持指引精简：优先 replace 现有行，谨慎 append；删除明显冗余或有害的行；
6. 已拒编辑清单是**禁区**：禁止再次提交与其相同文本、相同目标行、或仅换措辞的同类编辑 —
   代码侧会对同类提案直接过滤（浪费提案名额）。若你判断确需再次修改同一位置，
   text 必须与已拒版本有实质差异，且 reason 必须先引用上次的拒绝原因、
   再说明本次方案如何避开它；
7. 若诊断不构成系统性问题、或除禁区外无值得修改之处、或你不确定改进有帮助，
   输出 {{"edits": []}} — 不确定或重复的编辑会被过滤/拒绝并浪费一轮。

只输出 JSON，不要其他内容。"""


def _format_guidance(guidance_lines: list[str]) -> str:
    return "\n".join(
        f"{i}. {line}" for i, line in enumerate(guidance_lines, start=1)
    )


def _format_diagnoses(diagnoses: list[dict[str, Any]]) -> str:
    if not diagnoses:
        return "（无弱样本诊断）"
    blocks: list[str] = []
    for d in diagnoses:
        blocks.append(
            f"- 问题：{d.get('query', '')}\n"
            f"  答案摘录：{str(d.get('answer_excerpt', ''))[:200]}\n"
            f"  判官分：总分 {d.get('total_score')}，"
            f"引用 {d.get('citation_accuracy')}，"
            f"完整 {d.get('completeness')}，"
            f"无幻觉 {d.get('hallucination_inverse')}\n"
            f"  判官理由：{str(d.get('reasoning', ''))[:200]}"
        )
    return "\n".join(blocks)


def _format_rejected(rejected_summary: str) -> str:
    return rejected_summary or "（无已拒编辑）"


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 响应中稳健提取 JSON 对象（容忍 markdown 代码块与前后杂文）。"""
    cleaned = text.strip()
    # 剥离 markdown 代码围栏
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 兜底：找第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _parse_edits(payload: dict[str, Any] | None) -> list[EditOp]:
    if not payload or not isinstance(payload.get("edits"), list):
        return []
    ops: list[EditOp] = []
    for raw in payload["edits"]:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op", "")).strip().lower()
        line = raw.get("line")
        line_no: int | None
        try:
            line_no = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_no = None
        ops.append(
            EditOp(
                op=op,
                line=line_no,
                text=str(raw.get("text", "")),
                reason=str(raw.get("reason", "")),
            )
        )
    return ops


async def propose_edits(
    llm: LLMProvider,
    *,
    guidance_lines: list[str],
    diagnoses: list[dict[str, Any]],
    rejected_summary: str,
    budget: int,
) -> tuple[list[EditOp], str]:
    """调用 optimizer LLM 产出编辑提案。

    Args:
        llm: optimizer 使用的 LLM Provider（可与 judge 共用）。
        guidance_lines: 当前指引区行列表。
        diagnoses: 弱样本诊断列表（loop 侧组装）。
        rejected_summary: 已拒编辑清单文本（回喂）。
        budget: 每轮编辑上限（写入 prompt 硬规则）。

    Returns:
        (编辑提案列表, LLM 原始响应文本) — 原始响应入审计链。
    """
    system_prompt = _OPTIMIZER_SYSTEM_PROMPT.format(budget=budget)
    user_prompt = (
        f"## 当前指引\n{_format_guidance(guidance_lines)}\n\n"
        f"## 弱样本诊断\n{_format_diagnoses(diagnoses)}\n\n"
        f"## 已拒编辑清单（禁区 — 同类编辑会被直接过滤）\n"
        f"{_format_rejected(rejected_summary)}\n\n"
        "请输出编辑提案 JSON。"
    )
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response_text = ""
    async for chunk in llm.chat(messages, stream=False):
        if isinstance(chunk, str):
            response_text += chunk

    edits = _parse_edits(_extract_json(response_text))
    logger.info(
        "evolution.optimizer.proposed",
        extra={"n_edits": len(edits), "response_len": len(response_text)},
    )
    return edits, response_text
