"""微调脚本共享工具 — 边界信号词常量 + 防变体泄漏的分组切分 + MPS 回调工厂。

三个训练/评测入口（train_grpo / generate_rlaif_data / eval_boundary_200）
曾各自维护一份边界/拒答关键词且口径不一致（评审 #1/#9）：
- BOUNDARY_KEYWORDS 含"怎么办/怎么做/出差/预订/劳动"等泛词，实测 36% 工作
  问题被误判为边界问题，GRPO rule reward 反向训练（实质回答被给 -1）；
- 训练侧 REFUSAL_SIGNALS 含"欢迎随时提问"等礼貌结尾弱信号，把工作问题的
  正常回答误判为误拒答；评测侧 REFUSAL_KW 为严格强信号，两侧口径脱节。
本模块收敛为单一常量来源。

分组切分（评审 #2/#8）：数据生成侧 _query_variants 为每个基问题产 8-10 个
同义变体，行级随机切分使同一问题的变体同时落入 train/eval（实测 95.5%
验证集样本在训练集中有同问题变体），eval 指标全面虚高。本模块提供
question_group_key（归一化到基问题）+ grouped_split_indices（同组整体
进 train 或 test）。
"""

from __future__ import annotations

import random

# ------------------------------------------------------------------
# 边界问题触发词（强信号版 v2，2026-08-11 评审修正）
# 旧版含 怎么办/怎么做/出差/预订/告/劳动/心情/生病 等泛词：
#   "VPN 连接不上怎么办""出差申请怎么提交""会议室怎么预订""第三方组件安全
#   扫描怎么做"等工作问题全部命中 → 误判为边界 → 实质回答被惩罚。
# 本版只保留"几乎不可能出现在工作问题中"的强边界词。
# ------------------------------------------------------------------
BOUNDARY_KEYWORDS: tuple[str, ...] = (
    # 创意写作
    "写诗", "写一首", "写首", "打油诗", "藏头诗", "现代诗", "情诗",
    "写个歌词", "写首词", "写个对联", "顺口溜", "写个剧本", "写封情书",
    "写个小故事", "朋友圈文案",
    # 金融/天气
    "股票", "股市", "股价", "基金行情", "天气",
    # 出行预订（移除"出差/预订"——OA 出差申请/会议室预订是工作问题）
    "机票", "航班", "订酒店",
    # 翻译/代码（保留"写"前缀限定，避免误伤"错误代码"类 IT 问题）
    "翻译", "写代码", "写一段代码",
    # 餐饮/生活/娱乐
    "餐厅", "好吃", "电影", "一首歌", "笑话", "唱歌", "减肥计划",
    # 玄学
    "八字", "命理", "算命", "星座", "塔罗", "手相",
    # 医疗（移除"生病/怎么办"——病假/考勤是工作问题）
    "头痛", "失眠", "吃什么药",
    # 法律（移除"告/劳动"——劳动合同咨询是 HR 工作问题）
    "裁员", "劳动仲裁",
    # 学术/心理（移除"心情"）
    "论文", "学术", "抑郁", "心理测试", "梦境", "起名字", "起个名字",
    # 游戏/购物/菜谱（移除"怎么做"——"安全扫描怎么做"是工作问题）
    "原神", "游戏", "比价", "值得买", "红烧肉", "菜谱", "食谱",
)

#: 拒答信号词（严格强信号，训练 reward 与 eval 评测共用同一常量）。
#: 只含"明确拒答语句"；礼貌结尾（欢迎随时提问等）与"建议"类词不得入内：
#: 工作问题正常回答常含这些弱信号，会触发误拒答误判。
REFUSAL_KEYWORDS: tuple[str, ...] = (
    "无法提供", "不能提供", "不提供",
    "不在服务范围", "不在我的服务范围", "不在知识库",
    "超出", "不属于",
    "无法查询", "无法直接", "无法实时", "无法回答",
)

#: 引导信号词（拒答后给出替代方案/联系入口）。
#: 注意：仅用于边界问题分支的"拒答"认定与质量分档，不得用于工作问题分支
#: （工作回答本来就该给引导入口）。
GUIDANCE_SIGNALS: tuple[str, ...] = (
    "建议您使用", "建议使用", "建议咨询", "请咨询",
    "请联系", "可联系", "请使用", "可拨打", "拨打",
)

#: 企业系统入口关键词（工作问题回答含这些=更高质量，对齐 generate_dpo_data 的企业系统名）
ENTERPRISE_SYSTEMS: tuple[str, ...] = (
    "example.com", "企业邮箱", "企业统一身份", "IT 服务管理", "IT服务管理",
    "差旅平台", "内网软件", "考勤系统", "费用报销", "报销系统",
    "文件服务器", "OA 系统", "OA系统", "IT 服务台", "IT服务台", "HR 部门", "HR部门",
    "idp", "itsm", "tripmgmt",
)

#: 判定"实质回答"的最小长度（字符），低于此视为空话
SUBSTANTIVE_MIN_LEN = 20


# ------------------------------------------------------------------
# 问题文本提取与归一化（防变体泄漏分组用）
# ------------------------------------------------------------------

#: RAG prompt 中问题段标记（generate_dpo_data / generate_sft_data 的 RAG 模板）
_QUESTION_MARK = "【问题】"

#: _query_variants 添加的口语化前缀（归一化时剥离）
_VARIANT_PREFIXES = ("请问", "我想知道", "麻烦问下")

#: 结尾标点/语气词（_query_variants 的增删对象）
_TRAILING_CHARS = "？?。.！!~～呢 "


def extract_question_text(user_text: str) -> str:
    """从 user 消息提取纯问题文本。

    RAG prompt 形如 "根据以下文档回答问题。\\n\\n{context}\\n\\n【问题】{variant}"，
    context 中的企业答案含"出差/报销"等词，参与边界关键词匹配会放大误判
    （评审 #1）；本函数只取【问题】之后的真实问题。非 RAG 文本原样返回。
    """
    if _QUESTION_MARK in user_text:
        return user_text.split(_QUESTION_MARK, 1)[1]
    return user_text


def normalize_question(question: str) -> str:
    """将 query 变体归一化到基问题（逆转 generate_embedding_data._query_variants 的改写）。

    覆盖的变体规则：剥"请问/我想知道/麻烦问下"前缀、剥结尾标点与"呢"、
    句首"如何/咋"还原为"怎么"、补回被去掉的"企业"修饰词（统一删除）。
    不同基问题归一化后仍不同，同基问题的变体归一化后相同。
    """
    s = question.strip()
    for prefix in _VARIANT_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip(_TRAILING_CHARS)
    if s.startswith("如何"):
        s = "怎么" + s[2:]
    elif s.startswith("咋"):
        s = "怎么" + s[1:]
    s = s.replace("企业", "")
    return s


def question_group_key(user_text: str) -> str:
    """user 消息（或 query）→ 基问题分组键。"""
    return normalize_question(extract_question_text(user_text))


def last_user_content(messages: list) -> str:
    """从 conversational messages 提取最后一条 user content。"""
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return msg.get("content", "")
    return ""


def grouped_split_indices(
    keys: list[str],
    test_size: int,
    seed: int,
) -> tuple[list[int], list[int]]:
    """按组（基问题）切分 train/test 索引 — 同组整体进同一侧，防变体跨集合泄漏。

    Args:
        keys: 每条样本的分组键（question_group_key 结果）。
        test_size: 期望的 test 样本数（按组近似满足：整组划入直到达到数量）。
        seed: 随机种子（可复现）。

    Returns:
        (train_indices, test_indices)。组数 < 2 时回退行级随机切分，
        保证两侧均非空（如全部样本同属一个基问题的极端情况）。
    """
    rng = random.Random(seed)
    groups: dict[str, list[int]] = {}
    for i, key in enumerate(keys):
        groups.setdefault(key, []).append(i)

    if len(groups) < 2:
        idx = list(range(len(keys)))
        rng.shuffle(idx)
        test_size = max(1, min(test_size, len(idx) - 1)) if len(idx) > 1 else len(idx)
        return idx[test_size:], idx[:test_size]

    shuffled_groups = list(groups.values())
    rng.shuffle(shuffled_groups)
    test_idx: list[int] = []
    train_idx: list[int] = []
    for group in shuffled_groups:
        if len(test_idx) < test_size:
            test_idx.extend(group)
        else:
            train_idx.extend(group)
    if not train_idx:
        # 首个组即超 test_size（test 侧过大）：把最后一组挪回 train，保证 train 非空
        train_idx = shuffled_groups[-1]
        test_idx = [i for g in shuffled_groups[:-1] for i in g]
    return train_idx, test_idx


# ------------------------------------------------------------------
# MPS 缓存清理回调工厂（train_dpo / train_simpo / train_orpo 共用）
# ------------------------------------------------------------------


def make_mps_cache_cleanup_callback(every_n_steps: int = 5):
    """构造 MPS 缓存清理回调（torch/transformers 延迟导入，顶层无重依赖）。

    PyTorch MPS 后端的 Metal buffer 不像 CUDA 积极回收临时张量内存。DPO 系
    训练每步产生大量临时 logits，Python 侧释放后 GPU 侧仍缓存，碎片化累积
    导致系统用 swap 兜底→恶性循环。每 N 步 torch.mps.empty_cache() 强制回收。
    """
    import torch
    from transformers import TrainerCallback

    class MPSCacheCleanupCallback(TrainerCallback):
        """每隔 N 步清空 MPS Metal 缓存，防止碎片化累积。"""

        def on_step_end(self, args, state, control, **kwargs):
            if state.global_step % every_n_steps == 0:
                torch.mps.empty_cache()

    return MPSCacheCleanupCallback()
