"""有界编辑器 — 对技能指引区施加受限的结构化编辑（纯函数，可单测）。

SkillOpt 纪律的工程化落点：
- edit budget：每轮最多 budget 条编辑被应用，超出即跳过；
- 受保护区守卫：编辑器只接收指引区行列表，红线区**从不进入本模块** —
  结构上不可修改（对应 SkillOpt 的 frozen section）；
- 结构守卫：禁止注入 markdown 标题（##）、禁止出现"红线"字样、
  禁止多行文本 — 保持指引区扁平可 diff；
- 行数上限：应用编辑后超出 max_lines 即回退该条，防技能文档膨胀。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 支持的编辑操作
VALID_OPS: frozenset[str] = frozenset({"replace", "append", "delete"})

#: 编辑文本中禁止出现的模式（结构守卫）
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("##", "红线")


@dataclass(frozen=True)
class EditOp:
    """单条编辑提案（由 optimizer 产出，经 JSON 解析而来）。

    Attributes:
        op: 操作类型 — replace（替换指定行）/ append（末尾追加）/
            delete（删除指定行）。
        line: 1-based 行号，相对指引区；append 时忽略。
        text: 新文本（replace/append 必填，delete 忽略）。
        reason: 编辑理由（审计链用，可空）。
    """

    op: str
    line: int | None = None
    text: str = ""
    reason: str = ""


@dataclass
class EditOutcome:
    """编辑应用结果。

    Attributes:
        lines: 应用后的指引区行列表。
        applied: 成功应用的编辑记录（含行号定位）。
        skipped: 被跳过的编辑记录，含拒绝原因 — 回喂 rejected buffer。
    """

    lines: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.applied)


def apply_edits(
    guidance_lines: list[str],
    edits: list[EditOp],
    *,
    budget: int,
    max_lines: int,
) -> EditOutcome:
    """对指引区行列表应用有界编辑。

    Args:
        guidance_lines: 当前指引区行列表（不会被原地修改）。
        edits: 编辑提案列表（按序处理）。
        budget: 最多应用的编辑条数。
        max_lines: 应用后允许的最大行数（超出则回退该条编辑）。

    Returns:
        EditOutcome：新行列表 + applied/skipped 明细。
    """
    outcome = EditOutcome(lines=list(guidance_lines))
    applied_count = 0

    for edit in edits:
        reject = _validate(edit, outcome.lines)

        if reject is None and applied_count >= budget:
            reject = "budget_exhausted"

        if reject is not None:
            outcome.skipped.append(
                {"edit": _edit_dict(edit), "reason": reject}
            )
            continue

        # 应用编辑（此时 reject is None，op/text 已校验合法）
        if edit.op == "delete":
            assert edit.line is not None
            del outcome.lines[edit.line - 1]
        elif edit.op == "replace":
            assert edit.line is not None
            outcome.lines[edit.line - 1] = edit.text.strip()
        else:  # append
            outcome.lines.append(edit.text.strip())

        # 行数上限守卫：超限回退该条（重放前序已生效编辑，丢弃本条）
        if len(outcome.lines) > max_lines:
            outcome.lines = _replay(guidance_lines, outcome.applied)
            outcome.skipped.append(
                {"edit": _edit_dict(edit), "reason": "max_lines_exceeded"}
            )
            continue

        applied_count += 1
        outcome.applied.append(_edit_dict(edit))

    return outcome


def _replay(
    guidance_lines: list[str],
    applied: list[dict],
) -> list[str]:
    """从原始行重放已成功的编辑（用于超限回退，保持前序编辑生效）。

    按序重放到与逐条应用时相同的中间状态 — delete/replace 记录的行号
    均为「应用时」的行号，按同样顺序重放即可复现。
    """
    lines = list(guidance_lines)
    for item in applied:
        op = item.get("op")
        line_no = item.get("line")
        text = item.get("text", "")
        if op == "delete" and line_no is not None:
            del lines[line_no - 1]
        elif op == "replace" and line_no is not None:
            lines[line_no - 1] = text
        elif op == "append":
            lines.append(text)
    return lines


def _validate(edit: EditOp, current_lines: list[str]) -> str | None:
    """校验单条编辑；返回拒绝原因，None 表示通过。"""
    if edit.op not in VALID_OPS:
        return f"invalid_op: {edit.op!r}"

    if edit.op == "delete":
        if not _valid_line(edit.line, current_lines):
            return f"line_out_of_range: {edit.line}"
        return None

    # replace / append 需要非空文本
    text = edit.text.strip()
    if not text:
        return "empty_text"
    if "\n" in text:
        return "multiline_text"
    for banned in _FORBIDDEN_SUBSTRINGS:
        if banned in text:
            return f"forbidden_substring: {banned!r}"

    if edit.op == "replace" and not _valid_line(edit.line, current_lines):
        return f"line_out_of_range: {edit.line}"
    return None


def _valid_line(line: int | None, lines: list[str]) -> bool:
    return line is not None and 1 <= line <= len(lines)


def _edit_dict(edit: EditOp) -> dict:
    return {
        "op": edit.op,
        "line": edit.line,
        "text": edit.text.strip() if edit.op != "delete" else "",
        "reason": edit.reason,
    }
