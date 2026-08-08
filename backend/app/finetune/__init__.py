"""
微调数据飞轮 — 从业务数据构建 SFT / DPO / Embedding / Golden 四类训练数据集。

模块划分（单一职责）：
- data_cleaner：PII 脱敏、哈希去重、长度过滤（纯函数，易测试）；
- dataset_builder：四类数据集的构建逻辑（查询 + 清洗 + 过滤统计）；
- exporter：JSONL 落盘与版本目录管理。
"""
