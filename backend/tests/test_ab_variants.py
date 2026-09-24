"""
在线实验变体测试 — ab_split 多因子分流 / ab_context 传递 / prompt 变体注入。

覆盖：
    - 实验配置新增 control/treatment 变体块，旧配置（只有 model 字段）行为不变
    - assign_ab_arm 对 control 也返回分组（归因需要两侧都有标记）
    - 非法变体块（max_iterations 非数字）整条实验被跳过而非污染主链路
    - ab_context：作用域绑定、迭代上限夹紧、未命中时元数据为空
    - prompt 变体：只替换指引区、红线继承、缺失文件回落、目录穿越被拒
"""

import json
from types import SimpleNamespace

import pytest

from app.core import ab_split
from app.core.ab_context import (
    ABAssignment,
    ab_scope,
    active_prompt_variant,
    effective_max_iterations,
    get_ab_assignment,
    peek_ab_metadata,
)
from app.core.ab_split import assign_ab_arm, reset_experiments_cache

CONTROL = "qwen-dpo-v3-7b"
TREATMENT = "qwen-dpo-v4-7b"


def _set_experiments(monkeypatch, experiments: list[dict]) -> None:
    monkeypatch.setattr(
        ab_split,
        "get_settings",
        lambda: SimpleNamespace(
            AB_EXPERIMENTS=json.dumps(experiments, ensure_ascii=False)
        ),
    )
    reset_experiments_cache()


@pytest.fixture(autouse=True)
def _restore_cache():
    yield
    reset_experiments_cache()


class TestLegacyConfigUnchanged:
    """旧配置（只写 control_model/treatment_model）行为必须与扩展前一致。"""

    def test_model_only_experiment_still_splits(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "m1",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 100,
                }
            ],
        )
        assert assign_ab_arm(CONTROL, user_id="u1").model == TREATMENT

    def test_zero_traffic_keeps_control(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "m1",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 0,
                }
            ],
        )
        assignment = assign_ab_arm(CONTROL, user_id="u1")
        assert assignment.model == CONTROL
        assert not assignment.hit

    def test_user_selected_model_not_hijacked(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "m1",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 100,
                }
            ],
        )
        assert assign_ab_arm("claude-haiku-4", user_id="u1").model == "claude-haiku-4"


class TestVariantDimensions:
    def test_prompt_variant_and_iterations_flow_through(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "prompt_ab",
                    "control_model": CONTROL,
                    # 纯 prompt 实验：两臂同模型，只换变体
                    "treatment_model": CONTROL,
                    "traffic_pct": 100,
                    "control": {"prompt_variant": ""},
                    "treatment": {
                        "prompt_variant": "answer_first",
                        "max_iterations": 3,
                    },
                }
            ],
        )
        assignment = assign_ab_arm(CONTROL, user_id="u1")
        assert assignment.arm == "treatment"
        assert assignment.model == CONTROL
        assert assignment.prompt_variant == "answer_first"
        assert assignment.max_iterations == 3

    def test_control_arm_also_gets_assignment(self, monkeypatch):
        """归因要两侧都有标记 —— 否则只能看到 treatment 一半的数据。"""
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "half",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 1,  # 绝大多数用户落 control
                }
            ],
        )
        controls = [
            assign_ab_arm(CONTROL, user_id=f"u{i}") for i in range(50)
        ]
        assert any(c.arm == "control" for c in controls)
        assert all(c.hit for c in controls if c.arm)

    def test_allowlist_hits_treatment_with_variant(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "vip",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 0,
                    "tenant_allowlist": ["tenant-big"],
                    "treatment": {"prompt_variant": "answer_first"},
                }
            ],
        )
        assignment = assign_ab_arm(
            CONTROL, tenant_id="tenant-big", user_id="u1"
        )
        assert assignment.arm == "treatment"
        assert assignment.via == "allowlist"
        assert assignment.prompt_variant == "answer_first"
        # 白名单不参与随机分桶
        assert assignment.bucket == -1

    def test_bad_variant_block_skips_experiment(self, monkeypatch):
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "broken",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 100,
                    "treatment": {"max_iterations": "many"},
                }
            ],
        )
        # 配置非法 → 整条实验跳过，主链路照常返回（失败安全）
        assert assign_ab_arm(CONTROL, user_id="u1").model == CONTROL

    def test_correlation_id_reaches_exposure_log(self, monkeypatch):
        """曝光日志必须带关联键，否则无法与下游结果 join 做归因。"""
        _set_experiments(
            monkeypatch,
            [
                {
                    "name": "m1",
                    "control_model": CONTROL,
                    "treatment_model": TREATMENT,
                    "traffic_pct": 100,
                }
            ],
        )
        events = []

        class _Recorder:
            def info(self, event, **kw):
                events.append((event, kw))

        monkeypatch.setattr(ab_split, "log", _Recorder())
        assignment = assign_ab_arm(CONTROL, user_id="u1", correlation_id="conv-42")
        exposure = [kw for name, kw in events if name == "ab.exposure"]
        assert exposure and exposure[0]["correlation_id"] == "conv-42"
        # 同一个键还要进 trace metadata（归因时两边才连得起来）
        assert assignment.correlation_id == "conv-42"
        with ab_scope(assignment):
            assert peek_ab_metadata()["ab_correlation_id"] == "conv-42"


class TestAbContext:
    def test_unbound_context_is_inert(self):
        with ab_scope(None):
            assert get_ab_assignment() is None
            assert active_prompt_variant() == ""
            assert peek_ab_metadata() == {}
            assert effective_max_iterations(5) == 5

    def test_scope_sets_and_restores(self):
        assignment = ABAssignment(
            experiment="e", arm="treatment", bucket=3, prompt_variant="answer_first"
        )
        assert get_ab_assignment() is None
        with ab_scope(assignment):
            assert active_prompt_variant() == "answer_first"
            meta = peek_ab_metadata()
            assert meta["ab_experiment"] == "e"
            assert meta["ab_arm"] == "treatment"
        assert get_ab_assignment() is None

    def test_control_arm_also_emits_metadata(self):
        with ab_scope(ABAssignment(experiment="e", arm="control", bucket=9)):
            assert peek_ab_metadata()["ab_arm"] == "control"

    def test_max_iterations_clamped_to_ceiling(self):
        """迭代上限直接决定单次请求的 LLM 调用次数与费用，必须夹紧。"""
        with ab_scope(ABAssignment(experiment="e", arm="treatment", max_iterations=999)):
            assert effective_max_iterations(5) == 5
        with ab_scope(ABAssignment(experiment="e", arm="treatment", max_iterations=0)):
            assert effective_max_iterations(5) == 5
        with ab_scope(ABAssignment(experiment="e", arm="treatment", max_iterations=2)):
            assert effective_max_iterations(5) == 2

    def test_non_hit_assignment_yields_no_variant(self):
        with ab_scope(ABAssignment(model=CONTROL)):
            assert active_prompt_variant() == ""


class TestPromptVariants:
    def test_shipped_variant_is_loadable(self):
        from app.rag.prompt_variants import variant_exists, variant_guidance

        assert variant_exists("answer_first")
        lines = variant_guidance("answer_first")
        assert lines and all(isinstance(x, str) for x in lines)

    def test_missing_variant_returns_none(self):
        from app.rag.prompt_variants import variant_guidance

        assert variant_guidance("no_such_variant_xyz") is None

    @pytest.mark.parametrize(
        "bad", ["../secrets", "a/b", "", "   ", "x" * 100, ".hidden"]
    )
    def test_unsafe_names_rejected(self, bad):
        from app.rag.prompt_variants import variant_guidance

        assert variant_guidance(bad) is None

    def test_redline_inherited_and_variant_replaces_only_guidance(self, monkeypatch, tmp_path):
        """红线区必须来自基线文件 —— 变体不得偷偷放宽安全约束。"""
        from app.rag import prompt_variants
        from app.rag.generator import Generator

        monkeypatch.setattr(
            prompt_variants,
            "_VARIANTS_DIR",
            tmp_path,
        )
        (tmp_path / "risky.md").write_text(
            "## 指引\n变体指引内容\n\n## 红线\n变体自带的红线必须被忽略\n",
            encoding="utf-8",
        )
        generator = Generator(llm=SimpleNamespace())
        with ab_scope(
            ABAssignment(experiment="e", arm="treatment", prompt_variant="risky")
        ):
            prompt = generator._build_base_instruction()
        assert "变体指引内容" in prompt
        assert "禁止编造未在上下文中出现的事实" in prompt  # 基线红线
        assert "变体自带的红线必须被忽略" not in prompt

    def test_no_variant_matches_baseline(self):
        from app.rag.generator import Generator

        generator = Generator(llm=SimpleNamespace())
        with ab_scope(None):
            baseline = generator._build_base_instruction()
        with ab_scope(ABAssignment(experiment="e", arm="control")):
            same = generator._build_base_instruction()
        assert baseline == same

    def test_unknown_variant_falls_back_to_baseline(self):
        """实验配置写错变体名，不能让线上 prompt 变成空串。"""
        from app.rag.generator import Generator

        generator = Generator(llm=SimpleNamespace())
        with ab_scope(None):
            baseline = generator._build_base_instruction()
        with ab_scope(
            ABAssignment(experiment="e", arm="treatment", prompt_variant="nope_xyz")
        ):
            fallback = generator._build_base_instruction()
        assert fallback == baseline

    def test_evolution_override_beats_variant(self):
        """进化循环的候选覆盖优先级最高（离线 rollout 不该被在线实验干扰）。"""
        from app.rag.generator import Generator

        generator = Generator(llm=SimpleNamespace(), base_guidance="候选全文")
        with ab_scope(
            ABAssignment(experiment="e", arm="treatment", prompt_variant="answer_first")
        ):
            assert generator._build_base_instruction() == "候选全文"
