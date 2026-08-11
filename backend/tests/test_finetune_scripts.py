"""微调脚本（backend/scripts/finetune/）离线单元测试。

验证目标：
1. 全部脚本 py_compile 通过（语法有效）；
2. 模块可直接 import —— torch/transformers/peft/trl/datasets/sentence-transformers
   等重依赖均为函数内延迟导入，无 GPU / 无 ML 依赖环境下 import 不得报错；
3. 数据加载函数（load_sft_jsonl / load_dpo_jsonl / load_triplets / load_golden）
   对临时 JSONL 解析正确（条数、字段、跳过坏行）；
4. data_stats 统计函数与 eval_compare 指标函数的正确性。
"""

from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
from pathlib import Path

import pytest

FINETUNE_DIR = Path(__file__).parent.parent / "scripts" / "finetune"

# 多个脚本顶层 from finetune_utils import ...（scripts/finetune 非 Python 包），
# importlib 按路径加载时需目录在 sys.path 上才能解析目录内相互 import。
if str(FINETUNE_DIR) not in sys.path:
    sys.path.insert(0, str(FINETUNE_DIR))

#: 无重依赖顶层引入、可离线 import 的脚本
PY_SCRIPTS = [
    "finetune_utils",
    "data_stats",
    "train_lora",
    "train_dpo",
    "train_orpo",
    "train_simpo",
    "train_grpo",
    "train_embedding",
    "train_reranker",
    "eval_compare",
    "prepare_mlx_data",
    "seed_documents",
    "import_open_dataset",
    "synthesize_qa",
    "generate_embedding_data",
    "generate_sft_data",
    "generate_dpo_data",
    "generate_rlaif_data",
    "generate_extractive_answers",
    "bench_memory",
]

#: 顶层 import torch/transformers/peft 的评测脚本：仅做语法校验，跳过 import 测试
COMPILE_ONLY_SCRIPTS = [
    "eval_boundary_refusal",
    "eval_dpo_7b",
]

#: 顶层禁止出现的重依赖（必须延迟导入到函数内）
HEAVY_DEPS = {"torch", "transformers", "peft", "trl", "datasets", "sentence_transformers"}


def _load_module(name: str):
    """按文件路径加载脚本模块（scripts/finetune 非 Python 包，不经过 sys.path）。"""
    path = FINETUNE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"finetune_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, lines: list) -> Path:
    """写入临时 JSONL；元素为 dict 则序列化，为 str 则原样写入（用于构造坏行）。"""
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item)
            f.write("\n")
    return path


# ---------------------------------------------------------------------------
# 1. 语法有效性
# ---------------------------------------------------------------------------

class TestPyCompile:
    @pytest.mark.parametrize("name", PY_SCRIPTS + COMPILE_ONLY_SCRIPTS)
    def test_py_compile(self, name: str):
        py_compile.compile(str(FINETUNE_DIR / f"{name}.py"), doraise=True)


# ---------------------------------------------------------------------------
# 2. 模块 import（验证重依赖延迟导入）
# ---------------------------------------------------------------------------

class TestImportWithoutHeavyDeps:
    @pytest.mark.parametrize("name", PY_SCRIPTS)
    def test_import_and_lazy_deps(self, name: str):
        before = set(sys.modules)
        module = _load_module(name)
        assert module is not None
        # import 模块后不得新增加载任何重依赖
        newly_loaded = (set(sys.modules) - before) & HEAVY_DEPS
        assert not newly_loaded, f"{name} 顶层引入了重依赖: {newly_loaded}"

    def test_expected_loader_functions_exist(self):
        assert callable(getattr(_load_module("train_lora"), "load_sft_jsonl"))
        assert callable(getattr(_load_module("train_dpo"), "load_dpo_jsonl"))
        assert callable(getattr(_load_module("train_embedding"), "load_triplets"))
        assert callable(getattr(_load_module("train_reranker"), "load_triplets"))
        assert callable(getattr(_load_module("eval_compare"), "load_golden"))
        assert callable(getattr(_load_module("data_stats"), "summarize_file"))


# ---------------------------------------------------------------------------
# 3. 数据加载函数解析正确性
# ---------------------------------------------------------------------------

class TestDataLoaders:
    def test_load_sft_jsonl(self, tmp_path: Path):
        good = {
            "messages": [
                {"role": "system", "content": "你是企业知识库助手"},
                {"role": "user", "content": "如何申请年假？"},
                {"role": "assistant", "content": "在 OA 系统提交申请。"},
            ],
            "meta": {"source": "oa_wiki", "doc_ids": ["d1"], "tenant_id": "t1"},
        }
        no_assistant = {"messages": [{"role": "user", "content": "只有 user"}]}
        path = _write_jsonl(tmp_path / "sft.jsonl", [
            good,
            "{bad json line",          # JSON 解析失败 → 跳过
            '["not", "a", "dict"]',   # 顶层非对象 → 跳过
            no_assistant,              # 缺 assistant → 跳过
            good,
        ])
        records = _load_module("train_lora").load_sft_jsonl(path)
        assert len(records) == 2
        assert records[0]["messages"][2]["role"] == "assistant"
        assert records[0]["meta"]["source"] == "oa_wiki"
        # meta 缺失时默认为空 dict
        path2 = _write_jsonl(tmp_path / "sft2.jsonl", [{"messages": good["messages"]}])
        assert _load_module("train_lora").load_sft_jsonl(path2)[0]["meta"] == {}

    def test_load_dpo_jsonl(self, tmp_path: Path):
        good = {
            "prompt": [{"role": "user", "content": "报销流程？"}],
            "chosen": [{"role": "assistant", "content": "好答案"}],
            "rejected": [{"role": "assistant", "content": "差答案"}],
            "meta": {"source": "feedback"},
        }
        path = _write_jsonl(tmp_path / "dpo.jsonl", [
            good,
            # 无偏好信号（chosen == rejected）→ 跳过
            {"prompt": [{"role": "user", "content": "q"}],
             "chosen": [{"role": "assistant", "content": "相同"}],
             "rejected": [{"role": "assistant", "content": "相同"}]},
            # 空 prompt → 跳过
            {"prompt": [], "chosen": [{"role": "assistant", "content": "a"}],
             "rejected": [{"role": "assistant", "content": "b"}]},
            # 缺字段 → 跳过
            {"prompt": [{"role": "user", "content": "q"}],
             "chosen": [{"role": "assistant", "content": "a"}]},
        ])
        records = _load_module("train_dpo").load_dpo_jsonl(path)
        assert len(records) == 1
        assert records[0]["chosen"][-1]["content"] == "好答案"
        assert records[0]["meta"]["source"] == "feedback"

    def test_load_triplets(self, tmp_path: Path):
        good = {"query": "年假天数", "pos": "正式员工每年 15 天年假", "neg": "病假需三甲医院证明"}
        path = _write_jsonl(tmp_path / "embedding.jsonl", [
            good,
            {"query": "q", "pos": "p"},                  # 缺 neg → 跳过
            {"query": "q", "pos": "p", "neg": "  "},     # 空白 neg → 跳过
            "not json at all",                           # 坏行 → 跳过
        ])
        for name in ("train_embedding", "train_reranker"):
            records = _load_module(name).load_triplets(path)
            assert len(records) == 1
            assert records[0]["query"] == "年假天数"
            assert records[0]["pos"] == "正式员工每年 15 天年假"
            assert records[0]["neg"] == "病假需三甲医院证明"

    def test_reranker_build_pairs(self):
        pairs, labels = _load_module("train_reranker").build_pairs(
            [{"query": "q", "pos": "p", "neg": "n"}]
        )
        assert pairs == [["q", "p"], ["q", "n"]]
        assert labels == [1.0, 0.0]

    def test_load_golden(self, tmp_path: Path):
        full = {"query": "如何重置密码？", "expected_answer": "点击登录页「忘记密码」。",
                "expected_doc_ids": ["doc_a", "doc_b"], "meta": {"source": "manual"}}
        minimal = {"query": "无文档标注的问题"}  # 缺 expected_* → 默认值
        path = _write_jsonl(tmp_path / "golden.jsonl", [
            full,
            minimal,
            {"expected_answer": "没有 query"},  # 缺 query → 跳过
            "broken line",
        ])
        records = _load_module("eval_compare").load_golden(path)
        assert len(records) == 2
        assert records[0]["expected_doc_ids"] == ["doc_a", "doc_b"]
        assert records[0]["meta"]["source"] == "manual"
        assert records[1]["expected_answer"] == ""
        assert records[1]["expected_doc_ids"] == []


# ---------------------------------------------------------------------------
# 4. data_stats 统计函数正确性
# ---------------------------------------------------------------------------

class TestDataStats:
    def test_compute_length_stats(self):
        ds = _load_module("data_stats")
        stats = ds.compute_length_stats([10, 20, 30, 40, 50])
        assert stats["count"] == 5
        assert stats["mean"] == 30.0
        assert stats["min"] == 10
        assert stats["max"] == 50
        assert stats["p50"] == 30
        assert stats["p90"] == 50
        assert stats["p99"] == 50

    def test_compute_length_stats_empty(self):
        stats = _load_module("data_stats").compute_length_stats([])
        assert stats == {"count": 0, "mean": 0.0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p99": 0}

    def test_length_histogram(self):
        hist = _load_module("data_stats").length_histogram([10, 100, 100, 5000], bins=(64, 128, 1024))
        assert hist["<=64"] == 1
        assert hist["65-128"] == 2
        assert hist[">1024"] == 1

    def test_source_distribution(self):
        dist = _load_module("data_stats").source_distribution([
            {"meta": {"source": "wiki"}},
            {"meta": {"source": "wiki"}},
            {"meta": {"source": "crm"}},
            {"meta": {}},
            {},
        ])
        assert dist == {"wiki": 2, "crm": 1, "<missing>": 2}

    def test_detect_format(self):
        ds = _load_module("data_stats")
        assert ds.detect_format({"messages": []}) == "sft"
        assert ds.detect_format({"prompt": "q", "chosen": "c", "rejected": "r"}) == "dpo"
        assert ds.detect_format({"query": "q", "pos": "p", "neg": "n"}) == "embedding"
        assert ds.detect_format({"query": "q", "expected_doc_ids": []}) == "golden"
        assert ds.detect_format({"foo": "bar"}) == "unknown"

    def test_summarize_file(self, tmp_path: Path):
        ds = _load_module("data_stats")
        path = _write_jsonl(tmp_path / "mix.jsonl", [
            {"messages": [{"role": "user", "content": "你好"},
                          {"role": "assistant", "content": "您好，有什么可以帮您？"}],
             "meta": {"source": "wiki"}},
            {"query": "q", "pos": "p", "neg": "n", "meta": {"source": "crm"}},
            "{bad json",
        ])
        summary = ds.summarize_file(path)
        assert summary["samples"] == 2
        assert summary["format_counts"] == {"sft": 1, "embedding": 1}
        assert summary["source_distribution"] == {"wiki": 1, "crm": 1}
        assert summary["length_stats"]["count"] == 5  # 2 条 message + query/pos/neg


# ---------------------------------------------------------------------------
# 5. eval_compare 纯指标函数正确性
# ---------------------------------------------------------------------------

class TestEvalMetrics:
    def test_recall_at_k(self):
        ec = _load_module("eval_compare")
        assert ec.recall_at_k(["a", "b", "c"], ["b"], 2) == 1.0
        assert ec.recall_at_k(["a", "b", "c"], ["c"], 2) == 0.0
        assert ec.recall_at_k(["a", "b", "c"], ["c"], 3) == 1.0
        assert ec.recall_at_k(["a"], [], 5) == 0.0

    def test_mrr(self):
        ec = _load_module("eval_compare")
        assert ec.mrr(["a", "b", "c"], ["a"]) == 1.0
        assert ec.mrr(["a", "b", "c"], ["c"]) == pytest.approx(1 / 3)
        assert ec.mrr(["a", "b"], ["x"]) == 0.0
        assert ec.mrr(["a"], []) == 0.0

    def test_bleu_score(self):
        ec = _load_module("eval_compare")
        assert ec.bleu_score("企业知识库支持多租户隔离", "企业知识库支持多租户隔离") == pytest.approx(1.0)
        assert ec.bleu_score("", "企业知识库") == 0.0
        assert ec.bleu_score("完全不同的内容", "企业知识库支持多租户隔离") < 0.5
        # 英文按词切分
        assert ec.bleu_score("the cat sat", "the cat sat") == pytest.approx(1.0)

    def test_rouge_l(self):
        ec = _load_module("eval_compare")
        assert ec.rouge_l("企业知识库", "企业知识库") == pytest.approx(1.0)
        # LCS=5（hyp 全长），precision=5/5=1，recall=5/7
        assert ec.rouge_l("企业知识库", "企业知识库平台") == pytest.approx(2 * 1.0 * (5 / 7) / (1.0 + 5 / 7))
        assert ec.rouge_l("", "企业知识库") == 0.0
        assert ec.rouge_l("甲乙丙", "丁戊己") == 0.0

    def test_keyword_overlap(self):
        ec = _load_module("eval_compare")
        assert ec.keyword_overlap("企业知识库", "企业知识库") == pytest.approx(1.0)
        assert ec.keyword_overlap("", "企业知识库") == 0.0
        assert ec.keyword_overlap("企业知识库", "") == 0.0
        assert 0.0 < ec.keyword_overlap("企业知识库平台", "企业知识库支持隔离") < 1.0

    def test_format_markdown_table(self):
        ec = _load_module("eval_compare")
        table = ec.format_markdown_table(["模型", "Recall@10"], [["base", "0.5"], ["ft", "0.8"]])
        lines = table.splitlines()
        assert lines[0] == "| 模型 | Recall@10 |"
        assert lines[1] == "| --- | --- |"
        assert lines[2] == "| base | 0.5 |"
        assert len(lines) == 4

    def test_build_pseudo_corpus(self):
        ec = _load_module("eval_compare")
        ids, texts = ec.build_pseudo_corpus([
            {"expected_answer": "答案甲", "expected_doc_ids": ["d1"]},
            {"expected_answer": "答案乙", "expected_doc_ids": []},
            {"expected_answer": "", "expected_doc_ids": ["d3"]},   # 空答案 → 跳过
            {"expected_answer": "答案甲", "expected_doc_ids": ["d1"]},  # doc_id 去重
        ])
        assert ids == ["d1", "golden_1"]
        assert texts == ["答案甲", "答案乙"]


# ---------------------------------------------------------------------------
# 6. prepare_mlx_data MLX 数据准备
# ---------------------------------------------------------------------------

class TestPrepareMlxData:
    def test_split_dataset_ratio(self):
        pm = _load_module("prepare_mlx_data")
        records = [{"messages": [{"role": "user", "content": f"q{i}"},
                                 {"role": "assistant", "content": f"a{i}"}]} for i in range(100)]
        train, valid = pm.split_dataset(records, valid_ratio=0.1, seed=42)
        assert len(train) == 90
        assert len(valid) == 10
        # 可复现：同种子两次结果一致
        train2, valid2 = pm.split_dataset(records, valid_ratio=0.1, seed=42)
        assert train == train2 and valid == valid2

    def test_split_dataset_min_valid(self):
        pm = _load_module("prepare_mlx_data")
        # 仅 2 条样本：valid 至少 1 条，train 至少 1 条
        records = [{"messages": [{"role": "user", "content": "q"},
                                 {"role": "assistant", "content": "a"}]}] * 2
        train, valid = pm.split_dataset(records, valid_ratio=0.1, seed=1)
        assert len(valid) >= 1
        assert len(train) >= 1

    def test_split_dataset_empty(self):
        pm = _load_module("prepare_mlx_data")
        assert pm.split_dataset([], valid_ratio=0.1) == ([], [])

    def test_write_jsonl_roundtrip(self, tmp_path: Path):
        pm = _load_module("prepare_mlx_data")
        rec = {"messages": [{"role": "user", "content": "如何重置密码"}],
               "meta": {"source": "qa_adopted", "tenant_id": "t1"}}
        path = tmp_path / "mlx" / "train.jsonl"
        n = pm.write_jsonl([rec, rec], path)
        assert n == 2
        # ensure_ascii=False：中文原样落盘
        text = path.read_text(encoding="utf-8")
        assert "如何重置密码" in text
        assert text.count("\n") == 2

    def test_main_end_to_end(self, tmp_path: Path):
        pm = _load_module("prepare_mlx_data")
        good = {"messages": [{"role": "system", "content": "你是助手"},
                             {"role": "user", "content": "年假几天？"},
                             {"role": "assistant", "content": "正式员工 10 天起。"}],
                "meta": {"source": "qa_adopted"}}
        src = _write_jsonl(tmp_path / "sft.jsonl", [good, good, good, good,
                                                    "{bad json", good])
        out_dir = tmp_path / "mlx_out"
        rc = pm.main(["--data", str(src), "--output_dir", str(out_dir),
                      "--valid_ratio", "0.2", "--seed", "7"])
        assert rc == 0
        train_path = out_dir / "train.jsonl"
        valid_path = out_dir / "valid.jsonl"
        assert train_path.is_file() and valid_path.is_file()
        # 5 条有效（1 条坏行被跳过），8:2 拆分 → train 4 / valid 1
        train_lines = [l for l in train_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        valid_lines = [l for l in valid_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(train_lines) + len(valid_lines) == 5
        assert len(valid_lines) == 1
        # 校验写出的仍是合法 messages 结构
        obj = json.loads(train_lines[0])
        assert obj["messages"][2]["role"] == "assistant"

    def test_main_empty_input_returns_error(self, tmp_path: Path):
        pm = _load_module("prepare_mlx_data")
        src = _write_jsonl(tmp_path / "empty.jsonl", ["{bad json only"])
        rc = pm.main(["--data", str(src), "--output_dir", str(tmp_path / "out")])
        assert rc == 1


# ---------------------------------------------------------------------------
# 7. LLaMA-Factory 配置模板结构校验（PyYAML 可用时）
# ---------------------------------------------------------------------------

class TestLlamaFactoryConfig:
    def test_yaml_template(self):
        yaml = pytest.importorskip("yaml")
        path = FINETUNE_DIR / "llama_factory_config.yaml"
        assert path.is_file()
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
        assert len(docs) == 2
        # 第 1 段：dataset_info 片段
        assert docs[0]["ekb_sft"]["formatting"] == "sharegpt"
        assert docs[0]["ekb_sft"]["columns"]["messages"] == "messages"
        # 第 2 段：训练参数
        assert docs[1]["stage"] == "sft"
        assert docs[1]["finetuning_type"] == "lora"
        assert docs[1]["dataset"] == "ekb_sft"
        assert docs[1]["lora_rank"] == 16
        assert docs[1]["val_size"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# 8. finetune_utils 共享工具（边界词表 / 问题归一化 / 分组切分）
# ---------------------------------------------------------------------------

class TestFinetuneUtils:
    # ---- 关键词表口径（评审 #1/#9）----

    def test_boundary_keywords_reject_work_questions(self):
        """工作问题不得命中边界词（旧版"怎么办/预订/出差"曾致 36% 误判）。"""
        fu = _load_module("finetune_utils")
        work_questions = [
            "VPN 连接不上怎么办",
            "出差申请怎么提交",
            "会议室怎么预订",
            "第三方组件安全扫描怎么做",
            "年假怎么计算",
            "企业邮箱怎么设置签名",
            "怎么重置密码",
            "报销流程是什么",
            "劳动合同什么时候续签",
            "最近生病了怎么请病假",
        ]
        for q in work_questions:
            assert not any(kw in q for kw in fu.BOUNDARY_KEYWORDS), f"误判为边界: {q}"

    def test_boundary_keywords_catch_real_boundary(self):
        """真实边界问题必须命中边界词。"""
        fu = _load_module("finetune_utils")
        boundary_questions = [
            "帮我写一首关于秋天的诗",
            "今天股票行情怎么样",
            "帮我预订明天去北京的机票",
            "今天天气怎么样",
            "帮我算一下我的八字",
            "红烧肉怎么做",
            "我最近总是失眠，该吃什么药",
        ]
        for q in boundary_questions:
            assert any(kw in q for kw in fu.BOUNDARY_KEYWORDS), f"漏判边界: {q}"

    def test_refusal_keywords_no_weak_signals(self):
        """拒答词表只含强信号：不得含"建议/欢迎/服务台"等弱信号及单独"无法"。"""
        fu = _load_module("finetune_utils")
        for weak in ("建议", "欢迎", "服务台", "请使用", "无法"):
            assert weak not in fu.REFUSAL_KEYWORDS, f"弱信号混入拒答词表: {weak}"
        # 工作问题的正常回答（含礼貌结尾）不得误判为拒答
        normal_answer = "年假按工龄计算：1-10 年 5 天。建议在 OA 系统提交申请，欢迎随时提问。"
        assert not any(kw in normal_answer for kw in fu.REFUSAL_KEYWORDS)

    def test_training_scripts_share_constants(self):
        """训练/生成脚本与 finetune_utils 词表一致（单一来源，importlib 加载
        产生独立模块实例，按值比较）。"""
        fu = _load_module("finetune_utils")
        grpo = _load_module("train_grpo")
        assert grpo.BOUNDARY_KEYWORDS == fu.BOUNDARY_KEYWORDS
        assert grpo.REFUSAL_KEYWORDS == fu.REFUSAL_KEYWORDS
        rlaif = _load_module("generate_rlaif_data")
        assert rlaif.BOUNDARY_KEYWORDS == fu.BOUNDARY_KEYWORDS
        assert rlaif.REFUSAL_KEYWORDS == fu.REFUSAL_KEYWORDS

    # ---- 问题文本提取与归一化（评审 #2）----

    def test_extract_question_text(self):
        fu = _load_module("finetune_utils")
        # RAG prompt：只取【问题】之后的真实问题，context 中的边界词不参与匹配
        rag = "根据以下文档回答问题。\n\n【文档1】股票期权制度\n\n【问题】年假怎么计算"
        assert fu.extract_question_text(rag) == "年假怎么计算"
        # 非 RAG 文本原样返回
        assert fu.extract_question_text("今天天气怎么样") == "今天天气怎么样"

    def test_normalize_question_collapses_variants(self):
        """_query_variants 的全部改写规则归一化后必须收敛到同一基问题。"""
        fu = _load_module("finetune_utils")
        base_key = "怎么重置密码"
        variants = [
            "怎么重置密码？", "怎么重置密码", "如何重置密码？", "咋重置密码？",
            "请问怎么重置密码？", "我想知道怎么重置密码", "麻烦问下怎么重置密码",
            "怎么重置密码呢",
        ]
        for v in variants:
            assert fu.normalize_question(v) == base_key, f"变体未收敛: {v}"

    def test_normalize_question_distinguishes_different_questions(self):
        fu = _load_module("finetune_utils")
        assert fu.normalize_question("怎么重置密码") != fu.normalize_question("怎么申请年假")

    def test_question_group_key_ignores_enterprise_modifier(self):
        """"企业邮箱怎么设置签名" 与 "邮箱怎么设置签名"（去企业修饰变体）同组。"""
        fu = _load_module("finetune_utils")
        assert fu.question_group_key("企业邮箱怎么设置签名？") == \
            fu.question_group_key("邮箱怎么设置签名")

    def test_question_group_key_rag_prompt(self):
        """RAG prompt 的分组键只取决于【问题】段，与 context 无关。"""
        fu = _load_module("finetune_utils")
        p1 = "根据以下文档回答问题。\n\n【文档1】内容甲\n\n【问题】年假怎么计算？"
        p2 = "根据以下文档回答问题。\n\n【文档1】内容乙\n\n【文档2】内容丙\n\n【问题】请问年假怎么计算"
        assert fu.question_group_key(p1) == fu.question_group_key(p2)

    # ---- 分组切分（评审 #2：变体不得跨 train/test）----

    def test_grouped_split_group_integrity(self):
        """同组样本必须整体进同一侧。"""
        fu = _load_module("finetune_utils")
        # 10 个基问题 × 2 个变体
        keys = [f"问题{i // 2}" for i in range(20)]
        train_idx, test_idx = fu.grouped_split_indices(keys, test_size=4, seed=42)
        assert len(train_idx) + len(test_idx) == 20
        test_set, train_set = set(test_idx), set(train_idx)
        assert not (test_set & train_set)
        for g in range(10):
            pair = {2 * g, 2 * g + 1}
            assert pair <= test_set or pair <= train_set, f"组 {g} 被拆分"

    def test_grouped_split_reproducible(self):
        fu = _load_module("finetune_utils")
        keys = [f"问题{i}" for i in range(30)]
        a = fu.grouped_split_indices(keys, test_size=6, seed=7)
        b = fu.grouped_split_indices(keys, test_size=6, seed=7)
        assert a == b

    def test_grouped_split_test_size_approx(self):
        fu = _load_module("finetune_utils")
        keys = [f"问题{i}" for i in range(100)]  # 每组 1 条 → 精确满足
        train_idx, test_idx = fu.grouped_split_indices(keys, test_size=10, seed=1)
        assert len(test_idx) == 10
        assert len(train_idx) == 90

    def test_grouped_split_single_group_fallback(self):
        """全部样本同组时回退行级切分，保证两侧均非空。"""
        fu = _load_module("finetune_utils")
        train_idx, test_idx = fu.grouped_split_indices(["同组"] * 4, test_size=1, seed=3)
        assert len(test_idx) == 1
        assert len(train_idx) == 3

    def test_grouped_split_last_user_content(self):
        fu = _load_module("finetune_utils")
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "最后一句"}]
        assert fu.last_user_content(msgs) == "最后一句"
        assert fu.last_user_content([]) == ""
        assert fu.last_user_content("not a list") == ""
