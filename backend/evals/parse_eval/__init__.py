"""中文复杂版式解析评测包 — Docling vs MinerU 消融的评测集 schema、四维指标与合成语料。

P1 交付物：
    schema.py          真值标注模型 + manifest.jsonl 序列化
    parse_metrics.py   四维指标（表格还原 / 公式 LaTeX / 文本抽取 P·R / 扫描件 CER）
    smoke_corpus.py    内置合成 smoke 语料（离线可跑、CI 可回归）
    parse_ablation.py  消融编排入口（在 evals/parse_ablation.py 复用）
"""