"""开源数据集导入脚本（backend/scripts/finetune/import_open_dataset.py）离线单元测试。

验证目标：
1. py_compile 通过 + 模块可 import（datasets / huggingface_hub 延迟导入，无网络可单测）；
2. 4 个转换函数（convert_t2ranking_record / convert_dureader_record /
   convert_coig_cqia_record / convert_dpo_record）对模拟 raw dict 的正确性；
3. 端到端：mock datasets.load_dataset 返回固定样本，验证 main 写出正确 jsonl
  （条数、字段结构、ensure_ascii=False 中文落盘、--limit 截断、下载失败容错）。
"""

from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
import types
from pathlib import Path

import pytest

FINETUNE_DIR = Path(__file__).parent.parent / "scripts" / "finetune"
SCRIPT_PATH = FINETUNE_DIR / "import_open_dataset.py"

#: 顶层禁止出现的重依赖（必须延迟导入到函数内）
HEAVY_DEPS = {"datasets", "huggingface_hub"}


def _load_module():
    """按文件路径加载脚本模块（scripts/finetune 非 Python 包，不经过 sys.path）。"""
    spec = importlib.util.spec_from_file_location("import_open_dataset", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. 语法有效性 + 模块 import（验证重依赖延迟导入）
# ---------------------------------------------------------------------------

class TestPyCompile:
    def test_py_compile(self):
        py_compile.compile(str(SCRIPT_PATH), doraise=True)


class TestImportWithoutHeavyDeps:
    def test_import_and_lazy_deps(self):
        before = set(sys.modules)
        module = _load_module()
        assert module is not None
        # import 模块后不得新增加载任何重依赖
        newly_loaded = (set(sys.modules) - before) & HEAVY_DEPS
        assert not newly_loaded, f"顶层引入了重依赖: {newly_loaded}"

    def test_expected_functions_exist(self):
        mod = _load_module()
        assert callable(mod.convert_t2ranking_record)
        assert callable(mod.convert_dureader_record)
        assert callable(mod.convert_coig_cqia_record)
        assert callable(mod.convert_dpo_record)
        assert callable(mod.load_and_convert)
        assert callable(mod.write_jsonl)
        assert callable(mod.main)
        assert "t2ranking" in mod.DATASET_CONFIGS
        assert "dureader" in mod.DATASET_CONFIGS
        assert "coig_cqia" in mod.DATASET_CONFIGS
        assert "dpo_en_zh" in mod.DATASET_CONFIGS


# ---------------------------------------------------------------------------
# 2. 转换函数纯函数测试（不依赖网络）
# ---------------------------------------------------------------------------

class TestConvertT2Ranking:
    def test_valid_single(self):
        mod = _load_module()
        raw = {"query": "如何重置密码", "positive": ["点击忘记密码"], "negative": ["天气不错"]}
        rec = mod.convert_t2ranking_record(raw)
        assert rec is not None
        assert rec["query"] == "如何重置密码"
        assert rec["pos"] == "点击忘记密码"
        assert rec["neg"] == "天气不错"
        assert rec["meta"]["source"] == "t2ranking"
        assert rec["meta"]["neg_type"] == "random"

    def test_valid_multiple_pos_neg(self):
        """T2Reranking 每条含多个正例/负例，取第一个非空。"""
        mod = _load_module()
        raw = {"query": "q", "positive": ["", "第二个正例"], "negative": ["n1", "n2"]}
        rec = mod.convert_t2ranking_record(raw)
        assert rec is not None
        assert rec["pos"] == "第二个正例"
        assert rec["neg"] == "n1"

    def test_missing_query(self):
        mod = _load_module()
        assert mod.convert_t2ranking_record({"positive": ["p"], "negative": ["n"]}) is None

    def test_empty_query(self):
        mod = _load_module()
        assert mod.convert_t2ranking_record({"query": "  ", "positive": ["p"], "negative": ["n"]}) is None

    def test_empty_positive_list(self):
        mod = _load_module()
        assert mod.convert_t2ranking_record({"query": "q", "positive": [], "negative": ["n"]}) is None

    def test_empty_negative_list(self):
        mod = _load_module()
        assert mod.convert_t2ranking_record({"query": "q", "positive": ["p"], "negative": []}) is None

    def test_missing_positive(self):
        mod = _load_module()
        assert mod.convert_t2ranking_record({"query": "q", "negative": ["n"]}) is None


class TestConvertDuReader:
    def test_valid_list_of_dict(self):
        """HongzheBi/DuReader2.0 格式：answers = list[{text, answer_start}]。"""
        mod = _load_module()
        raw = {"question": "年假几天", "answers": [{"text": "15 天", "answer_start": 0}]}
        rec = mod.convert_dureader_record(raw)
        assert rec is not None
        assert rec["messages"][0]["role"] == "user"
        assert rec["messages"][0]["content"] == "年假几天"
        assert rec["messages"][1]["role"] == "assistant"
        assert rec["messages"][1]["content"] == "15 天"
        assert rec["meta"]["source"] == "dureader"

    def test_answers_as_dict(self):
        """luozhouyang/dureader 格式：answers = {text: [str...], answer_start: [int...]}。"""
        mod = _load_module()
        raw = {"question": "q", "answers": {"text": ["答案一", "答案二"], "answer_start": [0, 5]}}
        rec = mod.convert_dureader_record(raw)
        assert rec is not None
        assert rec["messages"][1]["content"] == "答案一"

    def test_answers_as_str_list(self):
        """部分版本：answers = list[str]。"""
        mod = _load_module()
        raw = {"question": "q", "answers": ["直接字符串答案"]}
        rec = mod.convert_dureader_record(raw)
        assert rec is not None
        assert rec["messages"][1]["content"] == "直接字符串答案"

    def test_missing_question(self):
        mod = _load_module()
        assert mod.convert_dureader_record({"answers": [{"text": "a"}]}) is None

    def test_empty_question(self):
        mod = _load_module()
        assert mod.convert_dureader_record({"question": "", "answers": [{"text": "a"}]}) is None

    def test_empty_answers(self):
        mod = _load_module()
        assert mod.convert_dureader_record({"question": "q", "answers": []}) is None

    def test_missing_answers(self):
        mod = _load_module()
        assert mod.convert_dureader_record({"question": "q"}) is None

    def test_answers_with_empty_text(self):
        """answers 列表存在但所有 text 为空 → None。"""
        mod = _load_module()
        raw = {"question": "q", "answers": [{"text": "", "answer_start": 0}, {"text": "  "}]}
        assert mod.convert_dureader_record(raw) is None


class TestConvertCOIGCQIA:
    def test_valid_without_input(self):
        mod = _load_module()
        raw = {"instruction": "解释 RAG", "input": "", "output": "检索增强生成"}
        rec = mod.convert_coig_cqia_record(raw)
        assert rec is not None
        assert rec["messages"][0]["role"] == "user"
        assert rec["messages"][0]["content"] == "解释 RAG"
        assert rec["messages"][1]["role"] == "assistant"
        assert rec["messages"][1]["content"] == "检索增强生成"
        assert rec["meta"]["source"] == "coig_cqia"
        assert rec["meta"]["subset"] == "wiki"

    def test_valid_with_input(self):
        """input 非空时拼接到 instruction 后。"""
        mod = _load_module()
        raw = {"instruction": "总结以下内容", "input": "一段待总结文本", "output": "总结结果"}
        rec = mod.convert_coig_cqia_record(raw)
        assert rec is not None
        content = rec["messages"][0]["content"]
        assert "总结以下内容" in content
        assert "一段待总结文本" in content
        assert "\n\n" in content  # instruction 和 input 之间有换行分隔

    def test_missing_instruction(self):
        mod = _load_module()
        assert mod.convert_coig_cqia_record({"output": "答案"}) is None

    def test_empty_instruction(self):
        mod = _load_module()
        assert mod.convert_coig_cqia_record({"instruction": "  ", "output": "答案"}) is None

    def test_missing_output(self):
        mod = _load_module()
        assert mod.convert_coig_cqia_record({"instruction": "问题"}) is None

    def test_empty_output(self):
        mod = _load_module()
        assert mod.convert_coig_cqia_record({"instruction": "问题", "output": ""}) is None


class TestConvertDPO:
    def test_valid(self):
        mod = _load_module()
        raw = {"question": "写一首诗", "response_chosen": "好诗", "response_rejected": "差诗"}
        rec = mod.convert_dpo_record(raw)
        assert rec is not None
        assert rec["prompt"] == "写一首诗"
        assert rec["chosen"] == "好诗"
        assert rec["rejected"] == "差诗"
        assert rec["meta"]["source"] == "dpo_en_zh"

    def test_chosen_equals_rejected(self):
        """chosen 与 rejected 相同则无偏好信号 → None。"""
        mod = _load_module()
        raw = {"question": "q", "response_chosen": "相同", "response_rejected": "相同"}
        assert mod.convert_dpo_record(raw) is None

    def test_chosen_equals_rejected_after_strip(self):
        """strip 后相同也算相同。"""
        mod = _load_module()
        raw = {"question": "q", "response_chosen": "答案 ", "response_rejected": " 答案"}
        assert mod.convert_dpo_record(raw) is None

    def test_missing_question(self):
        mod = _load_module()
        assert mod.convert_dpo_record({"response_chosen": "c", "response_rejected": "r"}) is None

    def test_missing_chosen(self):
        mod = _load_module()
        assert mod.convert_dpo_record({"question": "q", "response_rejected": "r"}) is None

    def test_missing_rejected(self):
        mod = _load_module()
        assert mod.convert_dpo_record({"question": "q", "response_chosen": "c"}) is None

    def test_empty_field(self):
        mod = _load_module()
        raw = {"question": "", "response_chosen": "c", "response_rejected": "r"}
        assert mod.convert_dpo_record(raw) is None


# ---------------------------------------------------------------------------
# 3. write_jsonl 测试
# ---------------------------------------------------------------------------

class TestWriteJsonl:
    def test_write_and_ensure_ascii_false(self, tmp_path: Path):
        """ensure_ascii=False：中文原样落盘（非 \\uXXXX 转义）。"""
        mod = _load_module()
        records = [{"query": "查询", "pos": "正例", "neg": "负例", "meta": {"source": "test"}}]
        path = tmp_path / "sub" / "out.jsonl"
        n = mod.write_jsonl(records, path)
        assert n == 1
        text = path.read_text(encoding="utf-8")
        assert "查询" in text  # 若 ensure_ascii=True 则会是 \u67e5\u8be2
        assert "正例" in text
        assert "负例" in text
        assert text.count("\n") == 1

    def test_write_multiple(self, tmp_path: Path):
        mod = _load_module()
        records = [
            {"messages": [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}],
             "meta": {}}
            for i in range(5)
        ]
        path = tmp_path / "out.jsonl"
        n = mod.write_jsonl(records, path)
        assert n == 5
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 5


# ---------------------------------------------------------------------------
# 4. 端到端测试（mock datasets.load_dataset）
# ---------------------------------------------------------------------------

class _FakeDataset:
    """模拟 datasets.Dataset 的可迭代对象。"""

    def __init__(self, rows: list):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


def _make_fake_datasets(monkeypatch, samples: dict[str, list]):
    """在 sys.modules 注入 fake datasets 模块，load_dataset 按 name 返回固定样本。"""
    fake = types.ModuleType("datasets")

    def fake_load_dataset(name, config=None, split=None, **kwargs):
        return _FakeDataset(samples.get(name, []))

    fake.load_dataset = fake_load_dataset
    monkeypatch.setitem(sys.modules, "datasets", fake)
    return fake


# 4 个数据集的 mock 样本（含合法 + 不合法记录以验证跳过逻辑）
MOCK_SAMPLES = {
    "C-MTEB/T2Reranking": [
        {"query": "如何重置密码", "positive": ["点击忘记密码"], "negative": ["今天天气不错"]},
        {"query": "  ", "positive": ["p"], "negative": ["n"]},  # 空 query → 跳过
        {"query": "q2", "positive": [], "negative": ["n2"]},     # 空 positive → 跳过
    ],
    "HongzheBi/DuReader2.0": [
        {"question": "年假多少天", "answers": [{"text": "正式员工 15 天", "answer_start": 0}]},
        {"question": "无答案", "answers": []},  # 空 answers → 跳过
    ],
    "m-a-p/COIG-CQIA": [
        {"instruction": "解释 RAG", "input": "", "output": "检索增强生成"},
        {"instruction": "总结", "input": "上下文", "output": "总结结果"},
        {"instruction": "缺输出", "input": "", "output": ""},  # 空 output → 跳过
    ],
    "shibing624/DPO-En-Zh-20k-Preference": [
        {"question": "写诗", "response_chosen": "好诗", "response_rejected": "差诗"},
        {"question": "q", "response_chosen": "相同", "response_rejected": "相同"},  # 相同 → 跳过
    ],
}


class TestEndToEnd:
    def test_main_all_with_mock(self, monkeypatch, tmp_path: Path):
        """mock datasets.load_dataset，验证 --dataset all 写出正确 jsonl。"""
        _make_fake_datasets(monkeypatch, MOCK_SAMPLES)
        mod = _load_module()
        out_dir = tmp_path / "imported"
        rc = mod.main(["--dataset", "all", "--output_dir", str(out_dir), "--limit", "10"])
        assert rc == 0

        # ---- embedding.jsonl：1 条（2 条不合法被跳过）----
        emb_path = out_dir / "embedding.jsonl"
        assert emb_path.is_file()
        emb_lines = [l for l in emb_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(emb_lines) == 1
        emb_obj = json.loads(emb_lines[0])
        assert emb_obj["query"] == "如何重置密码"
        assert emb_obj["pos"] == "点击忘记密码"
        assert emb_obj["neg"] == "今天天气不错"
        assert emb_obj["meta"]["source"] == "t2ranking"
        assert emb_obj["meta"]["neg_type"] == "random"
        # ensure_ascii=False：中文原样落盘（非 \uXXXX 转义）
        assert "如何重置密码" in emb_lines[0]

        # ---- sft.jsonl：3 条（dureader 1 + coig 2；各 1 条不合法被跳过）----
        sft_path = out_dir / "sft.jsonl"
        assert sft_path.is_file()
        sft_lines = [l for l in sft_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(sft_lines) == 3
        # 第 1 条来自 dureader
        sft_obj = json.loads(sft_lines[0])
        assert sft_obj["messages"][0]["role"] == "user"
        assert sft_obj["messages"][1]["role"] == "assistant"
        assert sft_obj["messages"][0]["content"] == "年假多少天"
        assert sft_obj["messages"][1]["content"] == "正式员工 15 天"
        assert sft_obj["meta"]["source"] == "dureader"
        # ensure_ascii=False
        assert "正式员工 15 天" in sft_lines[0]
        # 第 2-3 条来自 coig_cqia
        coig_obj = json.loads(sft_lines[1])
        assert coig_obj["meta"]["source"] == "coig_cqia"
        assert coig_obj["meta"]["subset"] == "wiki"

        # ---- dpo.jsonl：1 条（1 条 chosen==rejected 被跳过）----
        dpo_path = out_dir / "dpo.jsonl"
        assert dpo_path.is_file()
        dpo_lines = [l for l in dpo_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(dpo_lines) == 1
        dpo_obj = json.loads(dpo_lines[0])
        assert dpo_obj["prompt"] == "写诗"
        assert dpo_obj["chosen"] == "好诗"
        assert dpo_obj["rejected"] == "差诗"
        assert dpo_obj["meta"]["source"] == "dpo_en_zh"
        # ensure_ascii=False
        assert "好诗" in dpo_lines[0]

    def test_main_single_dataset(self, monkeypatch, tmp_path: Path):
        """仅导入单个数据集（t2ranking）。"""
        samples = {
            "C-MTEB/T2Reranking": [
                {"query": "查询词", "positive": ["正例段落"], "negative": ["负例段落"]},
            ]
        }
        _make_fake_datasets(monkeypatch, samples)
        mod = _load_module()
        out_dir = tmp_path / "out"
        rc = mod.main(["--dataset", "t2ranking", "--output_dir", str(out_dir), "--limit", "5"])
        assert rc == 0

        emb = (out_dir / "embedding.jsonl").read_text(encoding="utf-8")
        assert "查询词" in emb
        assert "正例段落" in emb
        assert "负例段落" in emb
        # 不应生成 sft.jsonl / dpo.jsonl
        assert not (out_dir / "sft.jsonl").is_file()
        assert not (out_dir / "dpo.jsonl").is_file()

    def test_main_limit_respected(self, monkeypatch, tmp_path: Path):
        """--limit 限制每类最大转换条数。"""
        rows = [{"query": f"q{i}", "positive": [f"p{i}"], "negative": [f"n{i}"]} for i in range(20)]
        _make_fake_datasets(monkeypatch, {"C-MTEB/T2Reranking": rows})
        mod = _load_module()
        out_dir = tmp_path / "out"
        rc = mod.main(["--dataset", "t2ranking", "--output_dir", str(out_dir), "--limit", "3"])
        assert rc == 0
        lines = [l for l in (out_dir / "embedding.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_main_download_failure_returns_error(self, monkeypatch, tmp_path: Path):
        """数据集下载失败（网络异常等）时 main 返回 1。"""
        fake = types.ModuleType("datasets")

        def failing_load_dataset(name, config=None, split=None, **kwargs):
            raise ConnectionError("网络不可用")

        fake.load_dataset = failing_load_dataset
        monkeypatch.setitem(sys.modules, "datasets", fake)

        mod = _load_module()
        rc = mod.main(["--dataset", "t2ranking", "--output_dir", str(tmp_path / "out")])
        assert rc == 1  # 全部失败 → 返回 1

    def test_main_partial_failure_returns_zero(self, monkeypatch, tmp_path: Path):
        """部分数据集成功、部分失败时 main 返回 0（部分成功）。"""
        fake = types.ModuleType("datasets")
        call_count = {"n": 0}

        def mixed_load_dataset(name, config=None, split=None, **kwargs):
            call_count["n"] += 1
            if name == "C-MTEB/T2Reranking":
                return _FakeDataset([{"query": "q", "positive": ["p"], "negative": ["n"]}])
            raise ConnectionError("模拟下载失败")

        fake.load_dataset = mixed_load_dataset
        monkeypatch.setitem(sys.modules, "datasets", fake)

        mod = _load_module()
        rc = mod.main(["--dataset", "all", "--output_dir", str(tmp_path / "out"), "--limit", "5"])
        assert rc == 0  # t2ranking 成功 → 非全部失败
        assert (tmp_path / "out" / "embedding.jsonl").is_file()

    def test_split_override(self, monkeypatch, tmp_path: Path):
        """--split 覆盖默认 split。"""
        received_splits = []
        _make_fake_datasets(monkeypatch, {
            "C-MTEB/T2Reranking": [{"query": "q", "positive": ["p"], "negative": ["n"]}],
        })
        # 包装 fake_load_dataset 以记录 split 参数
        fake = sys.modules["datasets"]
        original_load = fake.load_dataset

        def tracking_load(name, config=None, split=None, **kwargs):
            received_splits.append(split)
            return original_load(name, config, split=split, **kwargs)

        fake.load_dataset = tracking_load

        mod = _load_module()
        rc = mod.main(["--dataset", "t2ranking", "--output_dir", str(tmp_path / "out"),
                       "--split", "test"])
        assert rc == 0
        assert received_splits == ["test"]  # 覆盖了默认的 "dev"

    def test_default_split_used_when_not_overridden(self, monkeypatch, tmp_path: Path):
        """未指定 --split 时使用 DATASET_CONFIGS 预设值。"""
        received_splits = []
        _make_fake_datasets(monkeypatch, {
            "C-MTEB/T2Reranking": [{"query": "q", "positive": ["p"], "negative": ["n"]}],
        })
        fake = sys.modules["datasets"]
        original_load = fake.load_dataset

        def tracking_load(name, config=None, split=None, **kwargs):
            received_splits.append(split)
            return original_load(name, config, split=split, **kwargs)

        fake.load_dataset = tracking_load

        mod = _load_module()
        rc = mod.main(["--dataset", "t2ranking", "--output_dir", str(tmp_path / "out")])
        assert rc == 0
        assert received_splits == ["dev"]  # T2Reranking 预设 split
