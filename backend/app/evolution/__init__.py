"""技能自进化包（P1 · SkillOptLite）— 生成层基础指引的自动优化闭环。

模块分工：
- editor.py    有界编辑器：apply_edits 纯函数 + 冻结区守卫
- optimizer.py 反思器：LLM 诊断 → JSON 编辑提案（含已拒清单回喂）
- gate.py      三态门控：红线否决 + 严格更优 + 死区
- loop.py      编排：rollout → 诊断 → 提案 → 应用 → 门控 → 快照

进化对象：app/rag/prompts/generate_base.md 的「## 指引」区。
红线区由代码持有，编辑器结构上不可触达。
"""

from app.evolution.editor import EditOp, EditOutcome, apply_edits
from app.evolution.gate import GateDecision, MetricsSnapshot, evaluate_gate

__all__ = [
    "EditOp",
    "EditOutcome",
    "apply_edits",
    "GateDecision",
    "MetricsSnapshot",
    "evaluate_gate",
]
