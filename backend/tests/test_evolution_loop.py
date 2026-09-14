"""进化循环 mock 冒烟测试 — 不触网、不调真实 LLM。

覆盖场景：
1. 接受路径：baseline → 提案 → 有界编辑 → 候选更优 → 门控接受 →
   第二轮 optimizer 无提案早停，审计链四件套齐全；
2. 拒绝路径：候选未更优 → 门控拒绝 → 已应用编辑连同原因进
   rejected_edits.jsonl（下轮回喂 optimizer），best 保持原指引。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.evolution.editor import EditOp
from app.evolution.loop import EvolutionConfig, EvolutionLoop, filter_duplicate_edits
from app.observability.llm_judge import EvalResult

_TARGET_TEXT = """## 指引
你是企业知识库助手。请基于检索上下文回答。
如果上下文不足，请明确说明。

## 红线（冻结区，禁止修改）
禁止编造未在上下文中出现的事实。
"""

_EDIT_JSON = json.dumps(
    {
        "edits": [
            {
                "op": "append",
                "text": "【改进标记】回答时优先使用结构化列表。",
                "reason": "诊断显示列举类问题回答凌乱",
            }
        ]
    },
    ensure_ascii=False,
)

_NO_EDITS_JSON = json.dumps({"edits": []}, ensure_ascii=False)


class FakeLLM:
    """假 LLM Provider — 按调用类型分流：optimizer / 生成。"""

    def __init__(self, optimizer_responses: list[str]) -> None:
        self._optimizer_responses = list(optimizer_responses)
        self.optimizer_calls = 0
        self.generation_system_prompts: list[str] = []

    def chat(self, messages, tools=None, stream=False, **kwargs):
        return self._agen(messages)

    async def _agen(self, messages):
        system = messages[0]["content"] if messages else ""
        if "技能文档优化器" in system:
            idx = min(self.optimizer_calls, len(self._optimizer_responses) - 1)
            self.optimizer_calls += 1
            yield self._optimizer_responses[idx]
        else:
            self.generation_system_prompts.append(system)
            if "改进标记" in system:
                yield "改进后的好答案"
            else:
                yield "好答案"


class FakeJudge:
    """假判官 — 候选指引生效（答案含"改进后的"）时打更高分。"""

    def __init__(self, fixed_score: float | None = None) -> None:
        self._fixed = fixed_score
        self.calls: list[tuple[str, str]] = []

    async def evaluate_single(
        self, question: str, answer: str, contexts: list[str]
    ) -> EvalResult:
        self.calls.append((question, answer))
        score = (
            self._fixed
            if self._fixed is not None
            else (4.5 if "改进后的" in answer else 3.5)
        )
        return EvalResult(
            question=question,
            answer=answer,
            citation_accuracy=4,
            completeness=4,
            hallucination_inverse=5,
            total_score=score,
            reasoning="mock",
        )


def _make_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    target = tmp_path / "generate_base.md"
    target.write_text(_TARGET_TEXT, encoding="utf-8")
    dataset = tmp_path / "sel_qa.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(c, ensure_ascii=False)
            for c in [
                {
                    "case_id": "c1",
                    "query": "报销流程是什么",
                    "contexts": ["报销需在 30 天内提交。"],
                },
                {
                    "case_id": "c2",
                    "query": "年假有几天",
                    "contexts": ["年假按工龄 5-15 天。"],
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    weak = tmp_path / "weak_pool.jsonl"
    weak.write_text(
        json.dumps(
            {
                "case_id": "w1",
                "query": "差旅标准是多少",
                "contexts": ["差旅住宿标准为每晚 400 元。"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return target, dataset, weak


def _make_loop(
    tmp_path: Path,
    optimizer_responses: list[str],
    judge: FakeJudge,
    *,
    max_rounds: int = 3,
    max_llm_calls: int = 200,
) -> EvolutionLoop:
    target, dataset, weak = _make_files(tmp_path)
    config = EvolutionConfig(
        edit_budget=2,
        deadband=0.1,
        max_rounds=max_rounds,
        patience=3,
        max_llm_calls=max_llm_calls,
        max_guidance_lines=12,
        diagnose_top_k=5,
    )
    loop = EvolutionLoop(
        llm=FakeLLM(optimizer_responses),
        target_file=target,
        dataset_path=dataset,
        weak_pool_path=weak,
        run_dir=tmp_path / "run",
        config=config,
    )
    loop._judge = judge  # 替换真实 judge，不触网
    return loop


@pytest.mark.asyncio
async def test_loop_accepts_improvement_and_stops_on_no_edits(
    tmp_path: Path,
) -> None:
    """候选更优 → 门控接受 → 下轮无提案早停，审计链齐全。"""
    judge = FakeJudge()
    loop = _make_loop(
        tmp_path,
        optimizer_responses=[_EDIT_JSON, _NO_EDITS_JSON],
        judge=judge,
    )

    summary = await loop.run()

    # 轮次与裁决
    assert summary["stop_reason"] == "optimizer 无编辑提案"
    assert summary["best_round"] == 1
    assert summary["rounds"][0]["decision"] == "accepted"
    assert summary["rounds"][1]["decision"] == "no_edits"

    # 指标：baseline 3.5 → best 4.5
    assert summary["best_metrics"]["avg_score"] == pytest.approx(4.5)

    # 审计链四件套
    run_dir: Path = loop.run_dir
    assert (run_dir / "summary.json").exists()
    best_text = (run_dir / "best_guidance.md").read_text(encoding="utf-8")
    assert "【改进标记】回答时优先使用结构化列表。" in best_text
    assert "禁止编造未在上下文中出现的事实。" in best_text  # 红线保留
    diff_text = (run_dir / "best.diff").read_text(encoding="utf-8")
    assert "改进标记" in diff_text
    round1 = (run_dir / "rounds" / "round1.md").read_text(encoding="utf-8")
    assert "ACCEPTED" in round1

    # 接受路径不产生已拒编辑
    assert not (run_dir / "rejected_edits.jsonl").exists()

    # 线上目标文件不被触碰（人工 review 后合入）
    assert loop.target_file.read_text(encoding="utf-8") == _TARGET_TEXT

    # LLM 预算记账 > 0（rollout + judge + optimizer 均计费）
    assert summary["llm_calls_used"] > 0


@pytest.mark.asyncio
async def test_loop_rejection_feeds_rejected_buffer(tmp_path: Path) -> None:
    """候选未更优 → 拒绝；已应用编辑连同原因进 rejected_edits.jsonl。"""
    judge = FakeJudge(fixed_score=3.5)  # 候选与 baseline 同分 → not_improved
    loop = _make_loop(
        tmp_path,
        optimizer_responses=[_EDIT_JSON],
        judge=judge,
        max_rounds=1,
    )

    summary = await loop.run()

    assert summary["best_round"] == 0  # 无任何接受
    assert summary["rounds"][0]["decision"] == "rejected"
    assert "not_improved" in summary["rounds"][0]["reason"]

    # rejected buffer：已应用编辑 + 拒绝原因落盘（下轮回喂 optimizer）
    buffer = loop.run_dir / "rejected_edits.jsonl"
    assert buffer.exists()
    items = [
        json.loads(line)
        for line in buffer.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(items) == 1
    assert items[0]["round"] == 1
    assert items[0]["edit"]["op"] == "append"
    assert "not_improved" in items[0]["reason"]

    # best 保持原指引（编辑未合入）
    best_text = (loop.run_dir / "best_guidance.md").read_text(encoding="utf-8")
    assert "改进标记" not in best_text
    assert "你是企业知识库助手" in best_text


@pytest.mark.asyncio
async def test_loop_budget_exhausted_graceful_stop(tmp_path: Path) -> None:
    """LLM 预算耗尽 → 循环优雅终止，stop_reason 标注成本护栏。"""
    judge = FakeJudge()
    loop = _make_loop(
        tmp_path,
        optimizer_responses=[_EDIT_JSON],
        judge=judge,
        max_rounds=5,
        max_llm_calls=6,  # baseline(2) + 诊断(2) + 提案(1) + 候选(2) 不够 → 中途断
    )

    summary = await loop.run()

    assert "成本护栏触发" in summary["stop_reason"]
    assert any(
        r.get("decision") == "budget_exhausted" for r in summary["rounds"]
    )
    # 审计链仍完整落盘
    assert (loop.run_dir / "summary.json").exists()
    assert (loop.run_dir / "best_guidance.md").exists()


@pytest.mark.asyncio
async def test_loop_invalid_dataset_fails_fast(tmp_path: Path) -> None:
    """评测集缺 contexts → 构造即失败（不进 run 中途炸）。"""
    target, _, _ = _make_files(tmp_path)
    bad_dataset = tmp_path / "bad.jsonl"
    bad_dataset.write_text(
        json.dumps({"case_id": "c1", "query": "只有问题没有上下文"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少 case_id/query/非空 contexts"):
        EvolutionLoop(
            llm=FakeLLM([]),
            target_file=target,
            dataset_path=bad_dataset,
            weak_pool_path=tmp_path / "weak_pool.jsonl",
            run_dir=tmp_path / "run",
            config=EvolutionConfig(),
        )


# ======================================================================
# 同类编辑去重守卫（filter_duplicate_edits）
# ======================================================================

# 复现 run_20260913_141250 Round 1 被拒的真实编辑
_REJECTED_R1 = [
    {
        "round": 1,
        "edit": {
            "op": "replace",
            "line": 2,
            "text": "如果上下文不足以回答，请明确说明并建议补充具体信息或查阅相关文档。",
            "reason": "增强对信息不足时的引导",
        },
        "reason": "redline_citation_regression (cand=4.950 < cur=5.000)",
    },
    {
        "round": 1,
        "edit": {
            "op": "append",
            "line": None,
            "text": "回答需严格基于提供的上下文和文档内容，避免推测或添加未提及的信息。",
            "reason": "强化准确性要求",
        },
        "reason": "redline_citation_regression (cand=4.950 < cur=5.000)",
    },
]


def _rejected(rejected_items: list[dict]) -> list[dict]:
    """测试用已拒条目拍平（与 _load_rejected_edits 输出同构）。"""
    return [
        {
            "round": item["round"],
            "op": item["edit"]["op"],
            "line": item["edit"]["line"],
            "text": item["edit"]["text"],
            "reason": item["reason"],
        }
        for item in rejected_items
    ]


@pytest.mark.asyncio
async def test_loop_optimizer_llm_routing(tmp_path: Path) -> None:
    """独立 optimizer_llm — 提案调用走强模型，rollout/judge 留在主模型。"""
    main_llm = FakeLLM([_NO_EDITS_JSON])  # 若误用作 optimizer 则直接无提案
    opt_llm = FakeLLM([_EDIT_JSON, _NO_EDITS_JSON])
    judge = FakeJudge()
    target, dataset, weak = _make_files(tmp_path)
    loop = EvolutionLoop(
        llm=main_llm,
        target_file=target,
        dataset_path=dataset,
        weak_pool_path=weak,
        run_dir=tmp_path / "run",
        config=EvolutionConfig(
            edit_budget=2,
            deadband=0.1,
            max_rounds=2,
            patience=3,
            max_llm_calls=200,
            max_guidance_lines=12,
            diagnose_top_k=5,
        ),
        optimizer_llm=opt_llm,
    )
    loop._judge = judge

    summary = await loop.run()

    # optimizer 调用全部落在独立 Provider 上
    assert opt_llm.optimizer_calls == 2
    assert main_llm.optimizer_calls == 0
    # rollout 生成走主 Provider（诊断 + D_sel 评测）
    assert len(main_llm.generation_system_prompts) > 0
    assert len(opt_llm.generation_system_prompts) == 0
    # 强模型产出的编辑正常流入循环并触发接受路径
    assert summary["best_round"] == 1


def test_dedup_filters_same_text_same_line_resubmission() -> None:
    """Round 2 复现 Round 1 的同文本同行 replace → 直接过滤。"""
    edits = [
        EditOp(
            op="replace",
            line=2,
            text="如果上下文不足以回答，请明确说明并建议补充具体信息或查阅相关文档。",
            reason="强化信息不足引导",
        )
    ]
    kept, skipped = filter_duplicate_edits(edits, _rejected(_REJECTED_R1))
    assert kept == []
    assert len(skipped) == 1
    assert skipped[0]["reason"].startswith("duplicate_of_rejected")
    assert "round 1" in skipped[0]["reason"]


def test_dedup_filters_rephrased_similar_text() -> None:
    """同 op 同行、仅换措辞（相似度 ≥ 0.6）→ 过滤。"""
    edits = [
        EditOp(
            op="replace",
            line=2,
            text="如果上下文不足以回答，请明确说明，并建议用户补充具体信息或查阅相关文档。",
            reason="措辞优化",
        )
    ]
    kept, skipped = filter_duplicate_edits(edits, _rejected(_REJECTED_R1))
    assert kept == []
    assert skipped[0]["reason"].startswith("duplicate_of_rejected")


def test_dedup_keeps_genuinely_different_edit_same_line() -> None:
    """同行但实质不同的方案（相似度 < 0.6）→ 保留。"""
    edits = [
        EditOp(
            op="replace",
            line=2,
            text="回答前先判断上下文是否覆盖问题全部要点，未覆盖时逐项列出缺口。",
            reason="换一种处理信息不足的方案",
        )
    ]
    kept, skipped = filter_duplicate_edits(edits, _rejected(_REJECTED_R1))
    assert len(kept) == 1
    assert skipped == []


def test_dedup_filters_same_text_with_different_op() -> None:
    """同文本换 op（delete 复读已拒 replace 的文本）→ 过滤。"""
    edits = [
        EditOp(
            op="delete",
            line=1,
            text="如果上下文不足以回答，请明确说明并建议补充具体信息或查阅相关文档。",
            reason="删除该行",
        )
    ]
    kept, skipped = filter_duplicate_edits(edits, _rejected(_REJECTED_R1))
    assert kept == []
    assert len(skipped) == 1


def test_dedup_filters_similar_append() -> None:
    """append 无行号，与已拒 append 文本相似 → 过滤。"""
    edits = [
        EditOp(
            op="append",
            line=None,
            text="回答需严格基于所提供的上下文与文档内容，避免推测或添加未提及信息。",
            reason="再强化一次",
        )
    ]
    kept, skipped = filter_duplicate_edits(edits, _rejected(_REJECTED_R1))
    assert kept == []
    assert len(skipped) == 1


def test_dedup_keeps_unrelated_edit_when_buffer_empty() -> None:
    """无已拒清单 → 全部保留（首轮不受影响）。"""
    edits = [EditOp(op="append", line=None, text="新指引", reason="r")]
    kept, skipped = filter_duplicate_edits(edits, [])
    assert kept == edits
    assert skipped == []
