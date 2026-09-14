"""有界编辑器测试 — 验证 app/evolution/editor.py（SkillOpt 纪律的纯函数实现）。

覆盖：
- replace / append / delete 基本语义与链式行号
- edit budget 上限
- 非法操作拒绝：invalid op / 行号越界 / 空文本 / 多行文本 /
  标题注入（##）/ 冻结词（红线）
- max_lines 超限回退（保留前序已生效编辑）
"""

from __future__ import annotations

from app.evolution.editor import EditOp, apply_edits

_BASE = ["第一行指引", "第二行指引", "第三行指引"]


def _apply(edits: list[EditOp], *, budget: int = 2, max_lines: int = 6):
    return apply_edits(_BASE, edits, budget=budget, max_lines=max_lines)


# ======================================================================
# 基本语义
# ======================================================================


def test_replace() -> None:
    outcome = _apply([EditOp(op="replace", line=2, text="替换后的行")])
    assert outcome.lines == ["第一行指引", "替换后的行", "第三行指引"]
    assert len(outcome.applied) == 1
    assert outcome.has_changes


def test_append() -> None:
    outcome = _apply([EditOp(op="append", text="新增的行")])
    assert outcome.lines == [*_BASE, "新增的行"]


def test_delete() -> None:
    outcome = _apply([EditOp(op="delete", line=1)])
    assert outcome.lines == ["第二行指引", "第三行指引"]


def test_delete_then_replace_uses_updated_index() -> None:
    """链式编辑：delete 后的 replace 按删除后的行号定位。"""
    outcome = _apply(
        [
            EditOp(op="delete", line=1),
            EditOp(op="replace", line=2, text="新行"),
        ]
    )
    # 删除第一行后列表为 [第二行, 第三行]，replace line=2 → 第三行
    assert outcome.lines == ["第二行指引", "新行"]


def test_original_lines_not_mutated() -> None:
    _apply([EditOp(op="replace", line=1, text="x")])
    assert _BASE == ["第一行指引", "第二行指引", "第三行指引"]


# ======================================================================
# 预算与守卫
# ======================================================================


def test_budget_cap() -> None:
    outcome = _apply(
        [
            EditOp(op="append", text="一"),
            EditOp(op="append", text="二"),
            EditOp(op="append", text="三"),
        ],
        budget=2,
    )
    assert len(outcome.applied) == 2
    assert outcome.skipped[0]["reason"] == "budget_exhausted"


def test_invalid_op_rejected() -> None:
    outcome = _apply([EditOp(op="insert", line=1, text="x")])
    assert outcome.skipped[0]["reason"].startswith("invalid_op")


def test_line_out_of_range_rejected() -> None:
    outcome = _apply([EditOp(op="replace", line=99, text="x")])
    assert outcome.skipped[0]["reason"].startswith("line_out_of_range")
    outcome = _apply([EditOp(op="delete", line=0)])
    assert outcome.skipped[0]["reason"].startswith("line_out_of_range")


def test_empty_and_multiline_text_rejected() -> None:
    outcome = _apply([EditOp(op="append", text="   ")])
    assert outcome.skipped[0]["reason"] == "empty_text"
    outcome = _apply([EditOp(op="append", text="第一行\n第二行")])
    assert outcome.skipped[0]["reason"] == "multiline_text"


def test_heading_injection_rejected() -> None:
    """结构守卫：编辑文本不得注入 markdown 标题。"""
    outcome = _apply([EditOp(op="append", text="## 伪造标题")])
    assert outcome.skipped[0]["reason"] == "forbidden_substring: '##'"


def test_redline_word_rejected() -> None:
    """受保护区守卫：编辑文本不得出现「红线」字样。"""
    outcome = _apply([EditOp(op="append", text="修改红线规则试试")])
    assert outcome.skipped[0]["reason"] == "forbidden_substring: '红线'"


def test_max_lines_revert_preserves_previous_edits() -> None:
    """超限条回退、前序条保留（重放机制）。"""
    outcome = _apply(
        [
            EditOp(op="append", text="一"),
            EditOp(op="append", text="二"),
        ],
        max_lines=4,  # 基线 3 行：第一条后 4 行 OK，第二条后 5 行超限
    )
    assert outcome.lines == [*_BASE, "一"]
    assert len(outcome.applied) == 1
    assert outcome.skipped[0]["reason"] == "max_lines_exceeded"


def test_empty_edits_no_changes() -> None:
    outcome = _apply([])
    assert not outcome.has_changes
    assert outcome.lines == _BASE
