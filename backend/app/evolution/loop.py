"""进化循环编排 — rollout → 诊断 → 提案 → 应用 → 门控 → 快照。

流程（单轮）：
1. 诊断：用当前指引在弱样本池（D_train）上 rollout + judge，取最低分
   Top-K 作为失败模式样本；
2. 提案：optimizer LLM 产出 ≤budget 条编辑（已拒清单回喂）；
3. 应用：有界编辑器（app.evolution.editor）生成候选指引区；
4. 评测：候选指引在 D_sel 固定评测集上 rollout + judge（缓存按文本
   哈希复用 — 同一指引不重复花钱）；
5. 门控：红线否决 + 严格更优 + 死区（app.evolution.gate）；
6. 快照：每轮候选与裁决写入 rounds/，接受则更新 best，拒绝编辑进
   rejected_edits.jsonl。

产物（审计链，全部落盘 run_dir/）：
- summary.json           run 级汇总（轮次历史、指标、裁决原因、用量）
- best_guidance.md       最优指引完整文件（候选指引区 + 原红线区）
- best.diff              原文件 vs 最优文件的 unified diff（人工 review 用）
- rounds/roundN.md       每轮候选文件全文 + 裁决
- rejected_edits.jsonl   被拒编辑与失败原因（下轮回喂 optimizer）

本模块不写回线上文件 — 合入由人工 review best.diff 后完成，git 即审计。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.prompt_files import (
    compose_guidance_prompt,
    render_guidance_file,
    split_guidance_sections,
)
from app.evolution.editor import EditOp, apply_edits
from app.evolution.gate import MetricsSnapshot, evaluate_gate
from app.evolution.optimizer import propose_edits
from app.llm.base import LLMProvider
from app.observability.llm_judge import EvalResult, LLMJudgeService
from app.rag.generator import Generator
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ======================================================================
# 配置与预算
# ======================================================================


@dataclass(frozen=True)
class EvolutionConfig:
    """进化循环参数（默认取自 settings.EVOLUTION_*）。"""

    edit_budget: int = 2
    deadband: float = 0.1
    max_rounds: int = 5
    patience: int = 3
    max_llm_calls: int = 200
    max_guidance_lines: int = 12
    diagnose_top_k: int = 5

    @classmethod
    def from_settings(cls) -> "EvolutionConfig":
        s = get_settings()
        return cls(
            edit_budget=s.EVOLUTION_EDIT_BUDGET,
            deadband=s.EVOLUTION_DEADBAND,
            max_rounds=s.EVOLUTION_MAX_ROUNDS,
            patience=s.EVOLUTION_PATIENCE,
            max_llm_calls=s.EVOLUTION_MAX_LLM_CALLS,
            max_guidance_lines=s.EVOLUTION_MAX_GUIDANCE_LINES,
            diagnose_top_k=s.EVOLUTION_DIAGNOSE_TOP_K,
        )


class BudgetExhausted(Exception):
    """LLM 调用预算耗尽 — 成本护栏触发，循环应优雅终止。"""


@dataclass
class LLMBudget:
    """单次 run 的 LLM 调用预算（rollout 生成 + judge + optimizer）。"""

    max_calls: int
    used: int = 0

    def spend(self, n: int = 1) -> None:
        if self.used + n > self.max_calls:
            raise BudgetExhausted(
                f"LLM 调用预算耗尽：已用 {self.used}/{self.max_calls}"
            )
        self.used += n


# ======================================================================
# 同类编辑去重（代码级守卫，兜底 optimizer prompt 约束）
# ======================================================================

# 归一化文本相似度 ≥ 该阈值视为「同类编辑」（0.6 = 大部分表述重合）
_DUPLICATE_SIMILARITY: float = 0.6

# 归一化时剥离的空白与中英文标点
_PUNCT_RE = re.compile(r"[\s。，；、！？：""''（）《》〈〉【】「」*,.;:!?'\"()<>\[\]{}]+")


def _normalize_edit_text(text: str) -> str:
    """编辑文本归一化 — 去空白与标点，用于同类编辑判定。"""
    return _PUNCT_RE.sub("", text)


def filter_duplicate_edits(
    edits: list[EditOp],
    rejected: list[dict[str, Any]],
) -> tuple[list[EditOp], list[dict[str, Any]]]:
    """过滤与已拒编辑「同类」的提案。

    同类判定（满足任一即过滤）：
    - 文本归一化后相似度 ≥ 0.6，且 op 相同或目标行相同；
    - 文本归一化后完全一致（无论 op / 行号 — 换个 op 重复同一文本同样浪费）。

    Returns:
        (保留的编辑, 被过滤的编辑及原因列表) — 被过滤项带
        reason=duplicate_of_rejected，指认所重复的已拒条目。
    """
    if not rejected:
        return edits, []

    norm_rejected = [
        {
            "round": item.get("round"),
            "op": item.get("op"),
            "line": item.get("line"),
            "reason": item.get("reason", ""),
            "norm": _normalize_edit_text(str(item.get("text", ""))),
        }
        for item in rejected
    ]

    kept: list[EditOp] = []
    skipped: list[dict[str, Any]] = []
    for edit in edits:
        norm = _normalize_edit_text(edit.text)
        dup_of: dict[str, Any] | None = None
        for item in norm_rejected:
            r_norm = item["norm"]
            if not norm or not r_norm:
                continue
            if norm == r_norm:
                dup_of = item  # 同文本重复（换 op/行号也算）
                break
            same_op = edit.op == item["op"]
            same_line = (
                edit.line is not None
                and item["line"] is not None
                and edit.line == item["line"]
            )
            if same_op or same_line:
                ratio = difflib.SequenceMatcher(None, norm, r_norm).ratio()
                if ratio >= _DUPLICATE_SIMILARITY:
                    dup_of = item
                    break
        if dup_of is not None:
            skipped.append(
                {
                    "edit": {
                        "op": edit.op,
                        "line": edit.line,
                        "text": edit.text,
                        "reason": edit.reason,
                    },
                    "reason": (
                        f"duplicate_of_rejected (重复 round "
                        f"{dup_of['round']}: {dup_of['op']} "
                        f"line={dup_of['line']})"
                    ),
                }
            )
        else:
            kept.append(edit)
    return kept, skipped


# ======================================================================
# 数据集
# ======================================================================


@dataclass(frozen=True)
class RolloutCase:
    """评测用例 — 携带预置检索上下文（控制变量：指引 A/B 共享同一上下文）。"""

    case_id: str
    query: str
    contexts: list[str]


def load_cases(path: Path) -> list[RolloutCase]:
    """加载 JSONL 评测集；缺少 case_id/query/contexts 的行直接报错。

    格式（每行一个 JSON 对象）::

        {"case_id": "...", "query": "...", "contexts": ["...", ...]}
    """
    cases: list[RolloutCase] = []
    invalid: list[str] = []
    with path.open(encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                invalid.append(f"line {idx}: JSON 解析失败 {exc}")
                continue
            case_id = str(raw.get("case_id", "")).strip()
            query = str(raw.get("query", "")).strip()
            contexts = [str(c) for c in raw.get("contexts", []) if str(c).strip()]
            if not case_id or not query or not contexts:
                invalid.append(
                    f"line {idx}: 缺少 case_id/query/非空 contexts"
                )
                continue
            cases.append(RolloutCase(case_id=case_id, query=query, contexts=contexts))
    if invalid:
        raise ValueError(
            f"评测集 {path} 存在 {len(invalid)} 条无效用例：\n" + "\n".join(invalid)
        )
    if not cases:
        raise ValueError(f"评测集 {path} 为空")
    return cases


# ======================================================================
# 主循环
# ======================================================================


@dataclass
class EvolutionLoop:
    """生成层指引进化循环。

    Args:
        llm: rollout 生成与 judge 共用的 LLM Provider（调用量大头）。
        target_file: 进化目标文件路径（app/rag/prompts/generate_base.md）。
        dataset_path: D_sel 固定评测集（JSONL）。
        weak_pool_path: D_train 弱样本池（JSONL），诊断用。
        run_dir: 审计链输出目录。
        config: 循环参数；None 时从 settings 读取。
        judge_llm: 独立 judge Provider；None 时与 llm 相同。
        optimizer_llm: 独立 optimizer Provider（P2：可用更强模型提升禁则
            遵循度 — 每轮仅 1 次调用，成本可控）；None 时与 llm 相同。
    """

    llm: LLMProvider
    target_file: Path
    dataset_path: Path
    weak_pool_path: Path
    run_dir: Path
    config: EvolutionConfig = field(default_factory=EvolutionConfig.from_settings)
    judge_llm: LLMProvider | None = None
    optimizer_llm: LLMProvider | None = None

    def __post_init__(self) -> None:
        self._judge = LLMJudgeService(judge_llm=self.judge_llm or self.llm)
        self._optimizer_llm = self.optimizer_llm or self.llm
        self._budget = LLMBudget(self.config.max_llm_calls)
        # 指引全文（候选）→ 指标 的缓存：同一指引不重复 rollout
        self._metrics_cache: dict[str, MetricsSnapshot] = {}
        self._original_text = self.target_file.read_text(encoding="utf-8")
        self._guidance, self._redline = split_guidance_sections(self._original_text)
        if not self._guidance:
            raise ValueError(
                f"进化目标 {self.target_file} 指引区为空（缺少「## 指引」节）"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # 数据集一次性加载校验（缺 case_id/contexts 立即失败，避免 run 中途炸）
        self._dataset = load_cases(self.dataset_path)
        try:
            self._weak_pool = load_cases(self.weak_pool_path)
        except (OSError, ValueError) as exc:
            logger.warning(
                "evolution.weak_pool_unavailable",
                extra={"path": str(self.weak_pool_path), "error": str(exc)},
            )
            self._weak_pool = []

    # ------------------------------------------------------------------
    # rollout
    # ------------------------------------------------------------------

    async def _rollout_once(self, base_text: str, case: RolloutCase) -> str:
        """单条 rollout：候选指引 + 用例预置上下文 → 生产生成路径答案。

        使用真实 Generator（与线上 _build_system_prompt 完全一致），
        通过 base_guidance 覆盖通道注入候选，不触碰线上文件。
        temperature=0（贪心解码）：D_sel 指标须跨轮可比 — 若采样随机性
        （默认温度下 ±0.3 均分波动）大于门控死区（0.1），门控裁决将
        沦为掷骰子；固定解码是严格更优判定的前提。
        """
        self._budget.spend()
        generator = Generator(self.llm, base_guidance=base_text)
        docs = [
            {
                "doc_id": f"{case.case_id}-c{i}",
                "title": f"引用片段 {i}",
                "content": ctx,
            }
            for i, ctx in enumerate(case.contexts, start=1)
        ]
        parts: list[str] = []
        async for token in generator.generate(
            query=case.query,
            retrieved_docs=docs,
            tool_results=[],
            temperature=0.0,
        ):
            if isinstance(token, str):
                parts.append(token)
        return "".join(parts).strip()

    async def _judge_once(
        self, case: RolloutCase, answer: str
    ) -> EvalResult:
        self._budget.spend()
        return await self._judge.evaluate_single(case.query, answer, case.contexts)

    # ------------------------------------------------------------------
    # 指标（带缓存）
    # ------------------------------------------------------------------

    @staticmethod
    def _metrics_key(guidance_lines: list[str], redline_lines: list[str]) -> str:
        rendered = compose_guidance_prompt(guidance_lines, redline_lines)
        return hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:16]

    async def _metrics(
        self, guidance_lines: list[str], redline_lines: list[str]
    ) -> MetricsSnapshot:
        """在 D_sel 上评测一组指引（结果按文本哈希缓存）。"""
        key = self._metrics_key(guidance_lines, redline_lines)
        cached = self._metrics_cache.get(key)
        if cached is not None:
            return cached

        base_text = compose_guidance_prompt(guidance_lines, redline_lines)
        results: list[EvalResult] = []
        started = time.monotonic()
        for case in self._dataset:
            answer = await self._rollout_once(base_text, case)
            results.append(await self._judge_once(case, answer))

        snapshot = _aggregate(results)
        self._metrics_cache[key] = snapshot
        logger.info(
            "evolution.metrics",
            extra={
                "guidance_sha": key,
                "avg_score": snapshot.avg_score,
                "n_cases": snapshot.n_cases,
                "elapsed_s": round(time.monotonic() - started, 1),
            },
        )
        return snapshot

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    async def _diagnose(self, guidance_lines: list[str]) -> list[dict[str, Any]]:
        """弱样本池 rollout + judge，取最低分 Top-K 作为诊断样本。"""
        base_text = compose_guidance_prompt(guidance_lines, self._redline)
        diagnosed: list[dict[str, Any]] = []
        for case in self._weak_pool:
            answer = await self._rollout_once(base_text, case)
            judge = await self._judge_once(case, answer)
            diagnosed.append(
                {
                    "case_id": case.case_id,
                    "query": case.query,
                    "answer_excerpt": answer[:200],
                    "total_score": judge.total_score,
                    "citation_accuracy": judge.citation_accuracy,
                    "completeness": judge.completeness,
                    "hallucination_inverse": judge.hallucination_inverse,
                    "reasoning": judge.reasoning,
                    "error": judge.error,
                }
            )
        diagnosed.sort(key=lambda d: d["total_score"])
        return diagnosed[: self.config.diagnose_top_k]

    # ------------------------------------------------------------------
    # 审计链落盘
    # ------------------------------------------------------------------

    def _rejected_buffer_path(self) -> Path:
        return self.run_dir / "rejected_edits.jsonl"

    def _load_rejected_summary(self) -> str:
        path = self._rejected_buffer_path()
        if not path.exists():
            return ""
        lines: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            edit = item.get("edit", {})
            lines.append(
                f"- [round {item.get('round')}] {edit.get('op')} "
                f"line={edit.get('line')} text={str(edit.get('text'))[:80]} "
                f"→ 拒绝原因：{item.get('reason')}"
            )
        return "\n".join(lines)

    def _load_rejected_edits(self) -> list[dict[str, Any]]:
        """结构化读取已拒编辑（供代码级同类去重）。"""
        path = self._rejected_buffer_path()
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            edit = item.get("edit", {})
            items.append(
                {
                    "round": item.get("round"),
                    "op": edit.get("op"),
                    "line": edit.get("line"),
                    "text": str(edit.get("text", "")),
                    "reason": str(item.get("reason", "")),
                }
            )
        return items

    def _append_rejected(self, round_no: int, skipped: list[dict]) -> None:
        if not skipped:
            return
        with self._rejected_buffer_path().open("a", encoding="utf-8") as f:
            for item in skipped:
                f.write(
                    json.dumps(
                        {"round": round_no, **item}, ensure_ascii=False
                    )
                    + "\n"
                )

    def _write_round(
        self,
        round_no: int,
        candidate_text: str,
        decision_reason: str,
        accepted: bool,
    ) -> None:
        rounds_dir = self.run_dir / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Round {round_no} — {'ACCEPTED' if accepted else 'REJECTED'}\n"
            f"裁决：{decision_reason}\n\n"
        )
        (rounds_dir / f"round{round_no}.md").write_text(
            header + candidate_text, encoding="utf-8"
        )

    def _write_optimizer_response(self, round_no: int, raw_response: str) -> None:
        """optimizer LLM 原始响应落盘（审计链 — 提案质量/禁则遵循可回溯）。"""
        rounds_dir = self.run_dir / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        (rounds_dir / f"round{round_no}_optimizer_response.txt").write_text(
            raw_response, encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        """执行进化循环，返回 run 级汇总（同时落盘审计链）。"""
        run_id = uuid.uuid4().hex[:12]
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()

        best_guidance = list(self._guidance)
        best_metrics = await self._metrics(best_guidance, self._redline)
        best_round = 0
        no_improve_rounds = 0
        rounds: list[dict[str, Any]] = []
        stop_reason = ""

        for round_no in range(1, self.config.max_rounds + 1):
            round_record: dict[str, Any] = {"round": round_no}
            try:
                # 1. 诊断（弱样本池）
                diagnoses = await self._diagnose(best_guidance)

                # 2. 提案（optimizer 可用独立强模型 — 禁则遵循度更好）
                self._budget.spend()
                edits, raw_response = await propose_edits(
                    self._optimizer_llm,
                    guidance_lines=best_guidance,
                    diagnoses=diagnoses,
                    rejected_summary=self._load_rejected_summary(),
                    budget=self.config.edit_budget,
                )
                self._write_optimizer_response(round_no, raw_response)
                round_record["n_proposed"] = len(edits)
                if not edits:
                    round_record["decision"] = "no_edits"
                    rounds.append(round_record)
                    stop_reason = "optimizer 无编辑提案"
                    break

                # 2.5 同类编辑去重（代码级守卫 — 兜底 optimizer 禁则）
                edits, duplicate_skipped = filter_duplicate_edits(
                    edits, self._load_rejected_edits()
                )
                if duplicate_skipped:
                    round_record["duplicates_filtered"] = len(duplicate_skipped)
                    logger.info(
                        "evolution.loop.duplicates_filtered",
                        extra={
                            "run_id": run_id,
                            "round": round_no,
                            "n": len(duplicate_skipped),
                        },
                    )
                if not edits:
                    round_record["skipped"] = duplicate_skipped
                    round_record["decision"] = "all_duplicates_skipped"
                    rounds.append(round_record)
                    no_improve_rounds += 1
                    if no_improve_rounds >= self.config.patience:
                        stop_reason = "提案全部为已拒同类编辑，早停"
                        break
                    continue

                # 3. 应用（有界编辑）
                outcome = apply_edits(
                    best_guidance,
                    edits,
                    budget=self.config.edit_budget,
                    max_lines=self.config.max_guidance_lines,
                )
                round_record["applied"] = outcome.applied
                round_record["skipped"] = [*duplicate_skipped, *outcome.skipped]
                if not outcome.has_changes:
                    self._append_rejected(round_no, outcome.skipped)
                    round_record["decision"] = "all_edits_skipped"
                    rounds.append(round_record)
                    no_improve_rounds += 1
                    if no_improve_rounds >= self.config.patience:
                        stop_reason = "连续无效编辑早停"
                        break
                    continue

                # 4. 候选评测
                candidate_metrics = await self._metrics(
                    outcome.lines, self._redline
                )

                # 5. 门控
                decision = evaluate_gate(
                    best_metrics, candidate_metrics, deadband=self.config.deadband
                )
                round_record["cur"] = best_metrics.to_dict()
                round_record["cand"] = candidate_metrics.to_dict()
                round_record["decision"] = (
                    "accepted" if decision.accepted else "rejected"
                )
                round_record["reason"] = decision.reason
                candidate_text = render_guidance_file(
                    outcome.lines, self._redline
                )
                self._write_round(
                    round_no, candidate_text, decision.reason, decision.accepted
                )

                if decision.accepted:
                    best_guidance = list(outcome.lines)
                    best_metrics = candidate_metrics
                    best_round = round_no
                    no_improve_rounds = 0
                else:
                    # 门控拒绝：已应用编辑连同拒绝原因进 buffer（回喂下轮
                    # optimizer，避免重复提交同类编辑 — rejected-edit buffer）
                    rejected_items = [
                        *outcome.skipped,
                        *(
                            {"edit": item, "reason": decision.reason}
                            for item in outcome.applied
                        ),
                    ]
                    self._append_rejected(round_no, rejected_items)
                    no_improve_rounds += 1
                    if no_improve_rounds >= self.config.patience:
                        stop_reason = "连续无改进早停"
                        rounds.append(round_record)
                        break
                rounds.append(round_record)
            except BudgetExhausted as exc:
                round_record["decision"] = "budget_exhausted"
                round_record["reason"] = str(exc)
                rounds.append(round_record)
                stop_reason = f"成本护栏触发：{exc}"
                break

        # 审计链：best 文件 + diff + summary
        best_text = render_guidance_file(best_guidance, self._redline)
        (self.run_dir / "best_guidance.md").write_text(best_text, encoding="utf-8")
        diff = "\n".join(
            difflib.unified_diff(
                self._original_text.splitlines(),
                best_text.splitlines(),
                fromfile=f"{self.target_file.name} (current)",
                tofile=f"{self.target_file.name} (best)",
                lineterm="",
            )
        )
        (self.run_dir / "best.diff").write_text(
            diff if diff else "（无差异）\n", encoding="utf-8"
        )

        summary: dict[str, Any] = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "target_file": str(self.target_file),
            "dataset": str(self.dataset_path),
            "weak_pool": str(self.weak_pool_path),
            "config": {
                "edit_budget": self.config.edit_budget,
                "deadband": self.config.deadband,
                "max_rounds": self.config.max_rounds,
                "patience": self.config.patience,
                "max_guidance_lines": self.config.max_guidance_lines,
            },
            "llm_calls_used": self._budget.used,
            "llm_calls_max": self.config.max_llm_calls,
            "stop_reason": stop_reason or "达到最大轮数",
            "best_round": best_round,
            "best_metrics": best_metrics.to_dict(),
            "rounds": rounds,
        }
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "evolution.run.done",
            extra={
                "run_id": run_id,
                "best_round": best_round,
                "llm_calls": self._budget.used,
                "stop_reason": stop_reason,
            },
        )
        return summary


def _aggregate(results: list[EvalResult]) -> MetricsSnapshot:
    """EvalResult 列表 → 指标聚合（错误用例不计入均值，由 n_cases 暴露）。"""
    valid = [r for r in results if r.error is None]
    n = len(valid)
    if n == 0:
        return MetricsSnapshot(
            avg_score=0.0,
            avg_citation_accuracy=0.0,
            avg_hallucination_inverse=0.0,
            n_cases=0,
        )
    return MetricsSnapshot(
        avg_score=sum(r.total_score for r in valid) / n,
        avg_citation_accuracy=sum(r.citation_accuracy for r in valid) / n,
        avg_hallucination_inverse=sum(r.hallucination_inverse for r in valid) / n,
        n_cases=n,
    )


def run_evolution_blocking(loop: EvolutionLoop) -> dict[str, Any]:
    """同步入口 — Celery worker 等非 async 环境调用。"""
    return asyncio.run(loop.run())
