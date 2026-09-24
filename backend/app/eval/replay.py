"""
线上轨迹 → 评测用例的回放导出器 — 单一职责：把真实执行过的 trace 变成
可回归的 EvalCase，让评测集随线上流量增长而不是靠人写。

为什么需要它：
    手写评测集有两个结构性问题：① 规模上不去（本仓 9 个 JSONL 共 427 条，
    p4_context 只有 7 条）；② 分布是「我们想到的问题」而不是「用户真的问
    的问题」。真正咬人的 bad case 往往在两者交集之外。回放把评测集的来源
    换成线上真实轨迹，每日回归才有意义。

诚实性约束（本模块的核心设计，不是附带说明）：
    1. **自动产物一律是 candidate，不是 case**。检索指标要有 ground truth
       才能算 recall，而 ground truth 只能来自人工确认。因此导出物默认
       ``needs_review``，且 ``label_source=unlabeled``；未标注的用例即使
       被 promote 也会被拒绝合入 —— 否则评测集会悄悄被「期望=系统当时
       检索到的东西」污染，变成给系统自己打满分的自证循环。
    2. **弱标注必须标出处**。允许用答案里实际引用的文档作为候选
       ground truth（``label_source=auto_citation``），但这类用例只用于
       趋势观察，门禁（P0 必过集）要求 ``label_source=human``。
    3. **去重按归一化 query + trace 内容哈希**。同一句问话被问一万次不该
       变成一万条用例，否则指标被高频问题主导。

输入形态：
    - LangFuse 导出（``observations`` 里带 span 的 JSON）；
    - 本地 SpanRecord 落盘（``SpanRecord.to_dict()`` 的列表）；
    - 两者都归一成 :class:`TraceSnapshot` 后再判定与导出。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.eval.dataset import EvalCase
from app.utils.logger import get_logger

log = get_logger(__name__)

__all__ = [
    "ReplayExporter",
    "TraceSnapshot",
    "case_from_trace",
    "detect_bad_case",
    "load_traces",
]

#: 回放用例的固定标签（便于与手写用例区分并单独统计）
REPLAY_TAG = "replay"

#: 未通过人工确认的候选标签
NEEDS_REVIEW_TAG = "needs_review"

#: 允许进入门禁数据集的标注来源
HUMAN_LABEL_SOURCE = "human"

#: 答案中引用文档的形态（citation 由 rag/citation.py 产出，这里只做兜底解析）
_CITATION_DOC_RE = re.compile(r"doc[_:/]?([0-9a-fA-F][0-9a-fA-F-]{3,})")

#: 拒答信号词（与 eval/refusal_metrics 的口径保持独立：这里只用于挑选候选，
#: 不参与任何评分，因此允许更宽松）
_REFUSAL_MARKERS = (
    "无法回答",
    "没有找到",
    "未检索到",
    "抱歉",
    "cannot answer",
    "not found",
    "i can't",
)


# ======================================================================
# 轨迹快照
# ======================================================================


@dataclass
class TraceSnapshot:
    """一次线上执行的结构化快照（回放的最小输入）。

    Attributes:
        trace_id: 轨迹唯一标识（LangFuse trace id 或本地 run_id）。
        query: 用户原始问题。
        answer: 系统最终答案。
        retrieved_doc_ids: 实际检索到的文档 ID（按序）。
        cited_doc_ids: 答案中引用/据称出处的文档 ID。
        spans: 标准 Span 记录（dict 列表，可为空）。
        feedback: 用户反馈类型（complaint / bug / praise / None）。
        judge_score: 线上 LLM Judge 评分（0-1 或百分制，仅用于筛候选）。
        iterations: Agent Loop 实际迭代轮次。
        max_iterations_reached: 是否跑满迭代上限（兜圈信号）。
        error: 执行错误信息。
        session_id / user_id / tenant_id: 归因维度。
        created_at: 轨迹时间（ISO 字符串或 epoch）。
        prompt_variant / ab_arm / ab_experiment: 命中的在线实验分组（若有），
            带进 tags 后，回放集可以按臂复现 —— 这是「A/B 赢了之后还能
            离线守住」的关键一环。
        correlation_id: 与 ``ab.exposure`` / 下游结果事件共享的关联键，
            用于把「这条 bad case」和「这次决策的业务后果」对上。
    """

    trace_id: str = ""
    query: str = ""
    answer: str = ""
    retrieved_doc_ids: list[str] = field(default_factory=list)
    cited_doc_ids: list[str] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    feedback: str | None = None
    judge_score: float | None = None
    iterations: int | None = None
    max_iterations_reached: bool = False
    error: str | None = None
    session_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    created_at: str = ""
    prompt_variant: str = ""
    ab_arm: str = ""
    ab_experiment: str = ""
    correlation_id: str = ""

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceSnapshot:
        """从任意来源的 dict 构造（LangFuse 导出 / 本地落盘 / DB 行）。

        对字段名做宽松匹配：LangFuse 用 ``input``/``output``，本地 SpanRecord
        用 ``metadata``，两边都能落到同一份快照上。识别不了的字段忽略，
        不抛异常 —— 回放是旁路能力，不该因为一条脏 trace 中断。
        """
        spans_raw = data.get("spans") or data.get("observations") or []
        spans = [s for s in spans_raw if isinstance(s, dict)] if spans_raw else []
        root_meta = _root_metadata(spans)

        query = _first_str(data, "query", "input", "user_query")
        if not query:
            query = _query_from_spans(spans)

        answer = _first_str(data, "answer", "output", "final_answer")
        if not answer:
            answer = _last_output(spans, ("generate", "answer"))

        retrieved = _list_of_str(
            data.get("retrieved_doc_ids") or root_meta.get("retrieved_doc_ids")
        )
        if not retrieved:
            retrieved = _docs_from_spans(spans)

        judge_raw = data.get("judge_score", root_meta.get("judge_score"))
        if judge_raw is None:
            # engine 的在线自评分是嵌在根 span metadata 的 quality 子字典里
            # （finalize 传入），不在顶层 —— 不读它就漏掉「无报错但低分」的坏 case。
            quality = root_meta.get("quality")
            if isinstance(quality, dict):
                judge_raw = quality.get("total_score")
        judge_score = _as_float(judge_raw)

        iterations = _as_int(data.get("iterations", root_meta.get("iterations")))
        max_iter = _as_bool(
            data.get(
                "max_iterations_reached", root_meta.get("max_iterations_reached")
            )
        )
        error = _first_str(data, "error") or _error_from_spans(spans)

        ab = data.get("ab") if isinstance(data.get("ab"), dict) else {}
        ab = ab or root_meta
        return cls(
            trace_id=_first_str(data, "trace_id", "id", "run_id"),
            query=query.strip(),
            answer=answer.strip(),
            retrieved_doc_ids=retrieved,
            cited_doc_ids=_list_of_str(
                data.get("cited_doc_ids")
                or _cited_from_text(answer)
            ),
            spans=spans,
            feedback=_first_str(data, "feedback", "feedback_type") or None,
            judge_score=judge_score,
            iterations=iterations,
            max_iterations_reached=max_iter,
            error=error or None,
            session_id=_first_str(data, "session_id", "conversation_id"),
            user_id=_first_str(data, "user_id"),
            tenant_id=_first_str(data, "tenant_id"),
            created_at=_first_str(data, "created_at", "timestamp"),
            prompt_variant=_first_str(
                ab, "ab_prompt_variant", "prompt_variant"
            ),
            ab_arm=_first_str(ab, "ab_arm", "arm"),
            ab_experiment=_first_str(ab, "ab_experiment", "experiment"),
            correlation_id=_first_str(
                data, "correlation_id"
            ) or _first_str(ab, "ab_correlation_id", "correlation_id"),
        )

    def content_hash(self) -> str:
        """轨迹内容指纹 —— 同一 query 的不同执行不该互相覆盖。"""
        h = hashlib.sha1()
        h.update(_normalize_query(self.query).encode("utf-8"))
        h.update(b"\x00")
        h.update(self.answer.encode("utf-8"))
        h.update(b"\x00")
        h.update(",".join(self.retrieved_doc_ids).encode("utf-8"))
        return h.hexdigest()[:12]


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _list_of_str(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x) for x in raw if x not in (None, "")]


def _as_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _as_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return int(raw)


def _as_bool(raw: Any) -> bool:
    return raw is True or (isinstance(raw, str) and raw.lower() in ("1", "true"))


def _root_metadata(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """根 span（parent_span_id 为空）的 metadata —— engine 在这里汇总
    total_tokens / iterations / AB 分组。"""
    for s in spans:
        if s.get("parent_span_id") is None:
            meta = s.get("metadata")
            if isinstance(meta, dict):
                return meta
            break
    return {}


def _query_from_spans(spans: list[dict[str, Any]]) -> str:
    for s in spans:
        meta = s.get("metadata") or {}
        if isinstance(meta, dict) and isinstance(meta.get("query"), str):
            return meta["query"]
        if str(s.get("span_type", "")).startswith("think") and s.get("input_ref"):
            return str(s["input_ref"])
    return ""


def _last_output(spans: list[dict[str, Any]], name_prefixes: tuple[str, ...]) -> str:
    for s in reversed(spans):
        name = str(s.get("name", ""))
        if any(name.startswith(p) for p in name_prefixes) and s.get("output_ref"):
            return str(s["output_ref"])
    return ""


def _error_from_spans(spans: list[dict[str, Any]]) -> str:
    for s in spans:
        if s.get("status") not in (None, "ok") or s.get("error"):
            return str(s.get("error") or f"{s.get('name', 'span')} status={s.get('status')}")
    return ""


def _docs_from_spans(spans: list[dict[str, Any]]) -> list[str]:
    """从 retrieve 类 span 的 evidence_ref / metadata 提取文档 ID。"""
    docs: list[str] = []
    for s in spans:
        if "retrieve" not in str(s.get("span_type", "")) and "retrieve" not in str(
            s.get("name", "")
        ):
            continue
        meta = s.get("metadata") or {}
        if isinstance(meta, dict):
            docs.extend(_list_of_str(meta.get("doc_ids")))
        ev = s.get("evidence_ref")
        if isinstance(ev, str) and ev:
            docs.extend(part for part in re.split(r"[,\s]+", ev) if part)
    seen: set[str] = set()
    return [d for d in docs if not (d in seen or seen.add(d))]


def _cited_from_text(text: str) -> list[str]:
    return sorted(set(_CITATION_DOC_RE.findall(text or "")))


def _normalize_query(query: str) -> str:
    """query 归一化 —— 折叠空白与标点差异，让「同一句话的不同写法」能去重。"""
    collapsed = re.sub(r"\s+", " ", (query or "").strip().lower())
    return re.sub(r"[。，！？.,!?;；:：]+$", "", collapsed)


# ======================================================================
# Bad case 判定
# ======================================================================


def detect_bad_case(snapshot: TraceSnapshot, *, judge_floor: float = 0.6) -> list[str]:
    """判定一条轨迹是否值得进回放集，返回命中的原因列表（空表示不是 bad case）。

    信号选取原则：**只用线上拿得到的信号**（反馈、错误、跑满迭代、拒答、
    空召回、judge 低分），不引入需要人工预先打标的判据 —— 否则又回到
    人工写用例的老路。

    Args:
        snapshot: 轨迹快照。
        judge_floor: judge 分数下限（低于即视为差）；None 表示不看 judge。
            judge 分制兼容 0-1 与 0-100（按 >1 自动判定量纲）。
    """
    reasons: list[str] = []
    if not snapshot.query:
        return reasons

    if snapshot.error:
        reasons.append("error")

    feedback = (snapshot.feedback or "").lower()
    if feedback in ("complaint", "bug", "down", "thumbs_down", "negative"):
        reasons.append("negative_feedback")

    if snapshot.max_iterations_reached:
        reasons.append("max_iterations_reached")

    if not snapshot.retrieved_doc_ids:
        reasons.append("empty_retrieval")

    answer = (snapshot.answer or "").lower()
    if any(m.lower() in answer for m in _REFUSAL_MARKERS):
        reasons.append("refusal")

    if snapshot.judge_score is not None and judge_floor is not None:
        score = snapshot.judge_score
        normalized = score / 100.0 if score > 1.0 else score
        if normalized < judge_floor:
            reasons.append("low_judge_score")

    return reasons


# ======================================================================
# 用例构造
# ======================================================================


def case_from_trace(
    snapshot: TraceSnapshot,
    *,
    reasons: list[str] | None = None,
    use_citations_as_expected: bool = False,
) -> EvalCase:
    """把一条轨迹转成 EvalCase（候选态）。

    Args:
        snapshot: 轨迹快照。
        reasons: 已判定的 bad case 原因；None 时内部重新判定。
        use_citations_as_expected: 是否把答案引用的文档作为**弱** ground
            truth。开启时 label_source=auto_citation，这类用例只可用于趋势
            观察；门禁要求人工确认（见 :meth:`ReplayExporter.promote`）。

    Returns:
        EvalCase，case_id 形如 ``replay-<内容哈希>``，tags 携带完整出处
        （trace_id / 时间 / 命中的实验臂 / 标注来源）。
    """
    if reasons is None:
        reasons = detect_bad_case(snapshot)

    # 默认无 ground truth（unlabeled）。只有显式允许时才把答案引用的文档
    # 当弱标注用 —— 且标成 auto_citation，promote 阶段仍会拦在门外。
    expected: list[str] = []
    label_source = "unlabeled"
    if use_citations_as_expected and snapshot.cited_doc_ids:
        expected = list(snapshot.cited_doc_ids)
        label_source = "auto_citation"

    tags = [REPLAY_TAG, *reasons]
    if label_source != HUMAN_LABEL_SOURCE:
        tags.append(NEEDS_REVIEW_TAG)
    tags.append(f"label_source:{label_source}")
    if snapshot.trace_id:
        tags.append(f"trace:{snapshot.trace_id}")
    if snapshot.created_at:
        tags.append(f"at:{snapshot.created_at}")
    for dim, value in (
        ("arm", snapshot.ab_arm),
        ("variant", snapshot.prompt_variant),
        ("experiment", snapshot.ab_experiment),
    ):
        if value:
            tags.append(f"ab_{dim}:{value}")

    return EvalCase(
        query=snapshot.query.strip(),
        case_id=f"replay-{snapshot.content_hash()}",
        expected_doc_ids=expected,
        expected_answer=None,
        tags=tags,
        case_type="normal",
        context_expect={
            # 保留当时的检索现场，供「期望文档是否本来就可召回」的人工判断
            "replayed_doc_ids": list(snapshot.retrieved_doc_ids),
            "replayed_answer": snapshot.answer[:2000],
            "label_source": label_source,
            "bad_case_reasons": list(reasons),
            # 关联键：标注时可回查这次决策的下游业务结果
            "correlation_id": snapshot.correlation_id,
            "session_id": snapshot.session_id,
        },
    )


# ======================================================================
# 导出器
# ======================================================================


class ReplayExporter:
    """回放候选的导出与合入 — 负责去重、落盘、以及「只放标注过的进门禁集」。

    使用方式::

        exporter = ReplayExporter(existing_paths=["eval_datasets/"])
        stats = exporter.export(snapshots, out_path="eval_datasets/replay_candidates.jsonl")
        # 人工在 candidates 里补 expected_doc_ids 并改 label_source:human 后：
        merged = exporter.promote("eval_datasets/replay_candidates.jsonl",
                                  "eval_datasets/p6_replay.jsonl")
    """

    def __init__(self, existing_paths: list[str] | None = None) -> None:
        """
        Args:
            existing_paths: 已存在的评测集路径（目录或 JSONL 文件），用于
                去重 —— 避免把线上高频问题反复导成上千条同质用例。
        """
        self._queries: set[str] = set()
        self._case_ids: set[str] = set()
        for path in existing_paths or []:
            self._absorb(path)

    # ------------------------------------------------------------------

    def _absorb(self, path: str) -> None:
        """把已有数据集的 query / case_id 载入去重索引。"""
        if not path or not os.path.exists(path):
            log.warning("replay.existing_path_missing", path=path)
            return
        files = (
            [
                os.path.join(path, f)
                for f in sorted(os.listdir(path))
                if f.endswith(".jsonl")
            ]
            if os.path.isdir(path)
            else [path]
        )
        for full in files:
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        q = _normalize_query(str(obj.get("query", "")))
                        if q:
                            self._queries.add(q)
                        cid = str(obj.get("case_id", "") or "")
                        if cid:
                            self._case_ids.add(cid)
            except OSError as exc:
                log.warning("replay.existing_read_failed", path=full, error=str(exc))

    # ------------------------------------------------------------------

    def export(
        self,
        snapshots: list[TraceSnapshot],
        out_path: str,
        *,
        only_bad: bool = True,
        judge_floor: float = 0.6,
        use_citations_as_expected: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """导出回放候选到 JSONL，返回统计（供 CI 打印与人工核对）。

        Args:
            snapshots: 轨迹快照列表。
            out_path: 输出 JSONL 路径（追加写，保留历史候选）。
            only_bad: 只导出命中 bad case 信号的轨迹。
            judge_floor: 传给 :func:`detect_bad_case` 的 judge 下限。
            use_citations_as_expected: 是否允许引用文档充当弱标注。
            limit: 本次最多导出条数（None 不限）。

        Returns:
            ``{"written", "skipped_no_query", "skipped_duplicate",
            "skipped_not_bad", "total_seen"}``。
        """
        stats = {
            "written": 0,
            "skipped_no_query": 0,
            "skipped_duplicate": 0,
            "skipped_not_bad": 0,
            "total_seen": len(snapshots),
        }
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with open(out_path, "a", encoding="utf-8") as fh:
            for snap in snapshots:
                if limit is not None and stats["written"] >= limit:
                    break
                if not snap.query.strip():
                    stats["skipped_no_query"] += 1
                    continue
                key = _normalize_query(snap.query)
                case_id = f"replay-{snap.content_hash()}"
                if key in self._queries or case_id in self._case_ids:
                    stats["skipped_duplicate"] += 1
                    continue
                reasons = detect_bad_case(snap, judge_floor=judge_floor)
                if only_bad and not reasons:
                    stats["skipped_not_bad"] += 1
                    continue
                case = case_from_trace(
                    snap,
                    reasons=reasons,
                    use_citations_as_expected=use_citations_as_expected,
                )
                fh.write(json.dumps(case.to_dict(), ensure_ascii=False) + "\n")
                self._queries.add(key)
                self._case_ids.add(case.case_id)
                stats["written"] += 1

        log.info("replay.exported", path=out_path, **stats)
        return stats

    # ------------------------------------------------------------------

    @staticmethod
    def promote(candidates_path: str, target_path: str) -> dict[str, Any]:
        """把**已人工标注**的候选合入正式评测集，未标注的一律拒绝。

        这是本模块存在的意义：自动导出的用例如果直接进评测集，
        ground truth 就等于「系统当时检索到的东西」，评测会退化成
        自我确认。因此合入前强制校验标注来源。

        合入标准：``label_source == human`` 且 ``expected_doc_ids`` 非空。
        已在目标集中存在的 case_id / query 不重复写入（幂等，可反复执行）。

        Returns:
            ``{"promoted", "rejected_unlabeled", "already_present", "total"}``。
        """
        existing_queries, existing_ids = _read_index(target_path)
        stats = {
            "promoted": 0,
            "rejected_unlabeled": 0,
            "already_present": 0,
            "total": 0,
        }
        kept_lines: list[str] = []
        if os.path.isfile(target_path):
            with open(target_path, "r", encoding="utf-8") as fh:
                kept_lines = [ln.rstrip("\n") for ln in fh if ln.strip()]

        with open(candidates_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                stats["total"] += 1
                case = EvalCase.from_dict(obj)
                source = str(case.context_expect.get("label_source", ""))
                if source != HUMAN_LABEL_SOURCE or not case.expected_doc_ids:
                    stats["rejected_unlabeled"] += 1
                    continue
                key = _normalize_query(case.query)
                if key in existing_queries or case.case_id in existing_ids:
                    stats["already_present"] += 1
                    continue
                # 合入后不再是候选：清掉 needs_review，保留 replay 出处。
                # label_source 标签要与 context_expect 同步 —— 门禁集里留着
                # "label_source:unlabeled" 会让读 tags 的人以为混进了未标注用例。
                case.tags = [
                    t
                    for t in case.tags
                    if t != NEEDS_REVIEW_TAG and not t.startswith("label_source:")
                ]
                case.tags.append(f"label_source:{HUMAN_LABEL_SOURCE}")
                case.context_expect["label_source"] = HUMAN_LABEL_SOURCE
                kept_lines.append(json.dumps(case.to_dict(), ensure_ascii=False))
                existing_queries.add(key)
                existing_ids.add(case.case_id)
                stats["promoted"] += 1

        out_dir = os.path.dirname(target_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as fh:
            for ln in kept_lines:
                fh.write(ln + "\n")

        log.info("replay.promoted", path=target_path, **stats)
        return stats


def _read_index(path: str) -> tuple[set[str], set[str]]:
    """读目标数据集的 (归一化 query 集合, case_id 集合)。"""
    queries: set[str] = set()
    ids: set[str] = set()
    if not os.path.isfile(path):
        return queries, ids
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            q = _normalize_query(str(obj.get("query", "")))
            if q:
                queries.add(q)
            cid = str(obj.get("case_id", "") or "")
            if cid:
                ids.add(cid)
    return queries, ids


def load_traces(path: str) -> list[TraceSnapshot]:
    """从文件读轨迹（JSONL 逐行，或 LangFuse 导出的 JSON 数组）。

    单行解析失败只跳过该行 —— 回放是旁路能力，坏数据不该阻断整批。
    """
    if not path or not os.path.isfile(path):
        log.warning("replay.trace_file_missing", path=path)
        return []
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    items: list[Any] = []
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
            items = loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError as exc:
            log.warning("replay.trace_json_invalid", path=path, error=str(exc))
    else:
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("replay.trace_line_skipped", path=path, line=line_no)

    snapshots = [
        TraceSnapshot.from_dict(o) for o in items if isinstance(o, dict)
    ]
    log.info("replay.traces_loaded", path=path, count=len(snapshots))
    return snapshots
