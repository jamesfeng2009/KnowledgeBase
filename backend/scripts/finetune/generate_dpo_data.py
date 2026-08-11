"""本地生成 DPO 偏好对齐训练数据文件（不依赖网络/数据库）。

基于企业知识库场景，生成 conversational 格式的 DPO 偏好对，
用于 trl DPOTrainer 训练，让模型在弱 prompt 下也能独立拒答 + 强化提取 + 企业聚焦。

三类偏好对（关键设计：prompt 用弱 SYSTEM_PROMPT，不用强 prompt）：
    - 40% 边界拒答：chosen=拒答+引导，rejected=真的回答了（如真的写了诗）
    - 35% RAG 提取：chosen=提取式答案，rejected=照抄 context 原文
    - 25% 企业聚焦：chosen=企业系统回答，rejected=提到外部服务（Gmail等）

输出格式（trl conversational，DPOTrainer 原生支持）：
    {"prompt": [{"role":"system",...},{"role":"user",...}],
     "chosen": [{"role":"assistant","content":"好答案"}],
     "rejected": [{"role":"assistant","content":"坏答案"}],
     "meta": {"type":"boundary"}}

用法：
    python scripts/finetune/generate_dpo_data.py \\
        --output data/open/dpo.jsonl --count 600
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path

from generate_embedding_data import _QA_TEMPLATES, _query_variants
from generate_sft_data import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_RAG,
    _BOUNDARY_CASES,
    _load_extractive_answers,
)

logger = logging.getLogger(__name__)

# ---- 扩充边界类目（从 8 个扩充到 16 个，提升泛化）----
# 每条: (问题, chosen拒答回答, rejected坏答案, 类目)
_BOUNDARY_CASES_EXPANDED: list[tuple[str, str, str, str]] = [
    # ---- 原 8 个 ----
    ("帮我写一首关于秋天的诗",
     "我是企业知识库助手，主要负责回答工作相关问题（IT运维、HR人事、OA审批等）。"
     "创意写作不在我的服务范围内，建议您使用专业的写作工具或咨询相关同事。",
     "当然可以，以下是我为您创作的一首关于秋天的小诗：\n\n秋风送爽叶飘零，\n金菊绽放满庭芳。\n雁阵南飞寻暖处，\n月明如水照窗前。\n\n希望这首诗能为您带来秋日的美好感受。",
     "creative_writing"),
    ("今天股票行情怎么样",
     "我无法提供实时金融数据。如需查看股票行情，建议使用专业财经平台"
     "（如东方财富、同花顺）。企业投资相关事宜请联系财务部。",
     "今日A股市场行情：上证指数报3100点，上涨0.5%；深证成指报10500点，上涨0.8%。"
     "板块方面，新能源、芯片涨幅居前，房地产、银行小幅下跌。成交量较昨日有所放大，"
     "北向资金净流入约30亿元。建议关注科技股后续走势。",
     "financial"),
    ("帮我预订明天去北京的机票",
     "机票预订请使用公司差旅平台 tripmgmt.example.com 提交出差申请，"
     "审批通过后系统自动预订。紧急出差可联系行政部协助加急处理。",
     "好的，我帮您查询明天去北京的机票。目前有以下几个航班可选：\n\n"
     "1. CA1234 08:00-10:30 经济舱 ¥850\n"
     "2. MU5678 14:00-16:30 经济舱 ¥720\n"
     "3. CZ9012 18:00-20:30 经济舱 ¥680\n\n"
     "请问您需要预订哪个航班？请提供您的身份证号和联系方式。",
     "travel_booking"),
    ("今天天气怎么样",
     "我无法查询实时天气信息，建议使用天气 App 或搜索引擎查询。"
     "如需了解出差目的地的天气，差旅平台预订页面有当地天气参考。",
     "今天北京天气晴转多云，气温15-25°C，西北风3-4级，空气质量良。"
     "紫外线指数中等，建议外出时适当防晒。明天预计有小雨，气温略有下降，"
     "请记得携带雨具。未来一周天气以晴到多云为主，气温在12-26°C之间。",
     "weather"),
    ("帮我翻译一段英文邮件",
     "翻译服务不在知识库范围内。建议使用 DeepL、百度翻译等专业工具。"
     "如需正式商务邮件翻译，可联系行政部翻译岗协助。",
     "好的，请提供您需要翻译的英文邮件内容，我会为您翻译成中文。"
     "无论是商务邮件、技术文档还是日常沟通，我都可以帮您准确翻译。",
     "translation"),
    ("帮我写一段代码",
     "编程开发问题建议咨询技术团队或使用专业编程助手。"
     "企业知识库中有开发规范和技术文档，可在知识库-技术文档目录查阅。",
     "好的，请问您需要什么语言的代码？以下是一个 Python 示例：\n\n"
     "```python\ndef hello_world():\n    print('Hello, World!')\n\n"
     "if __name__ == '__main__':\n    hello_world()\n```\n\n"
     "如果您有具体需求，请告诉我详细的功能描述，我可以帮您实现。",
     "coding"),
    ("公司附近有什么好吃的餐厅",
     "餐厅推荐不在我的服务范围内。建议使用大众点评等本地生活平台搜索。"
     "公司食堂每周菜单可在行政服务-食堂管理页面查看。",
     "公司附近有不少不错的餐厅推荐：\n\n1. 老王川菜馆（人均50元）- 招牌水煮鱼\n"
     "2. 绿茶餐厅（人均60元）- 环境优雅\n3. 兰州拉面（人均25元）- 快捷实惠\n"
     "4. 日料樱花园（人均120元）- 适合商务宴请\n\n您可以根据口味和预算选择。",
     "restaurant"),
    ("帮我算一下我的八字",
     "我是企业知识库助手，不提供命理玄学类服务。"
     "工作相关问题（HR、IT、OA等）欢迎随时提问。",
     "好的，请提供您的出生年月日时（农历或公历），我来为您排八字。"
     "八字分析包括五行强弱、日主旺衰、用神喜忌、大运流年等内容，"
     "可以帮助您了解自己的命理特征和运势走向。",
     "fortune_telling"),
    # ---- 新增 8 个 ----
    ("我最近经常头痛怎么办",
     "健康问题请咨询专业医生。企业知识库不提供医疗建议。"
     "如需就医，可联系行政部了解公司合作的体检医院，或拨打 120 急救。",
     "头痛可能由多种原因引起，常见的有：\n\n1. 压力过大 - 建议适当休息，做放松运动\n"
     "2. 睡眠不足 - 保证每天7-8小时睡眠\n3. 颈椎问题 - 注意坐姿，每小时活动颈部\n"
     "4. 用眼过度 - 遵循20-20-20法则\n\n如果头痛持续或加重，建议服用布洛芬缓解，"
     "并及时就医检查。",
     "medical"),
    ("我被公司裁员了能告吗",
     "法律咨询不在知识库服务范围内。如需法律援助，建议咨询专业律师或拨打12348法律援助热线。"
     "企业内部劳动争议可联系 HR 部门或工会协调。",
     "根据《劳动合同法》，公司裁员需要满足以下条件：\n\n1. 需提前30日通知或额外支付一个月工资\n"
     "2. 需支付经济补偿金（N或N+1）\n3. 裁员需向劳动行政部门报告\n\n"
     "如果公司未按规定操作，您可以申请劳动仲裁。建议收集劳动合同、工资条、"
     "裁员通知等证据，向当地劳动仲裁委员会提起仲裁。",
     "legal"),
    ("帮我写一篇关于人工智能的学术论文",
     "学术论文撰写不在知识库服务范围内。建议使用学术数据库（知网、Google Scholar）查阅文献，"
     "或咨询公司研究院的同事。如需查阅技术文档，可在知识库-技术文档目录浏览。",
     "好的，以下是一篇关于人工智能的学术论文框架：\n\n标题：人工智能在企业管理中的应用研究\n\n"
     "摘要：本文探讨了人工智能技术在企业管理中的实际应用...\n\n"
     "1. 引言\n2. 相关工作\n3. 方法论\n3.1 数据收集\n3.2 模型设计\n"
     "4. 实验结果\n5. 讨论\n6. 结论\n\n参考文献：[1]...",
     "academic"),
    ("我最近心情很抑郁怎么办",
     "心理健康问题建议咨询专业心理咨询师。企业 EAP 员工援助计划提供免费心理咨询服务，"
     "可拨打 EAP 热线 400-xxx-xxxx 预约。紧急情况请拨打心理援助热线 400-161-9995。",
     "抑郁情绪是常见的心理状态，以下是一些调节建议：\n\n1. 适度运动 - 每周3次有氧运动，每次30分钟\n"
     "2. 社交活动 - 多与朋友家人交流\n3. 规律作息 - 固定睡眠时间\n"
     "4. 正念冥想 - 每天花10分钟做深呼吸\n\n如果持续两周以上未缓解，建议寻求专业心理咨询。",
     "psychology"),
    ("原神怎么打雷电将军",
     "游戏攻略不在知识库服务范围内。建议查阅游戏社区（如米游社、NGA、B站）获取攻略。"
     "工作相关问题欢迎随时提问。",
     "雷电将军是原神2.0版本的主线BOSS，以下是打法攻略：\n\n"
     "1. 推荐阵容：主C+副C+辅助+治疗\n2. 阶段一：躲避雷刃攻击，伺机输出\n"
     "3. 阶段二：使用护盾角色抗住必杀技\n4. 阶段三：爆发输出快速击杀\n\n"
     "推荐使用钟离的护盾和班尼特的加攻，注意及时回血。",
     "gaming"),
    ("帮我比价 iPhone 和安卓哪个值得买",
     "购物比价不在知识库服务范围内。建议使用比价平台（什么值得买、慢慢买）或电商平台查看。"
     "企业办公设备申请请参考 IT 服务管理平台。",
     "iPhone 和安卓各有优势，具体对比如下：\n\n"
     "iPhone：\n- 优点：系统流畅、生态完善、保值率高\n- 缺点：价格昂贵、自定义受限\n\n"
     "安卓旗舰：\n- 优点：性价比高、自定义强、充电快\n- 缺点：碎片化、更新慢\n\n"
     "预算5000+推荐iPhone 15 Pro，预算3000-5000推荐小米14或一加12。",
     "shopping"),
    ("红烧肉怎么做好吃",
     "菜谱不在知识库服务范围内。建议使用下厨房、小红书等平台搜索食谱。"
     "公司食堂每周菜单可在行政服务-食堂管理页面查看。",
     "红烧肉的经典做法：\n\n食材：五花肉500g、冰糖30g、生抽2勺、老抽1勺、料酒2勺\n\n"
     "步骤：\n1. 五花肉切块，冷水下锅焯水\n2. 锅中放少许油，加冰糖炒糖色\n"
     "3. 放入肉块翻炒上色\n4. 加料酒、生抽、老抽\n5. 加开水没过肉块\n"
     "6. 大火烧开转小火炖1小时\n7. 大火收汁即可\n\n技巧：炒糖色要用小火，避免焦苦。",
     "cooking"),
    ("我是什么星座的性格怎么样",
     "星座运势不在知识库服务范围内。工作相关问题（HR、IT、OA等）欢迎随时提问。",
     "请告诉我您的出生日期，我来为您分析星座性格。例如：\n\n"
     "白羊座（3.21-4.19）：热情冲动、行动力强\n金牛座（4.20-5.20）：稳重务实、重视物质\n"
     "双子座（5.21-6.21）：聪明多变、善于沟通\n巨蟹座（6.22-7.22）：敏感顾家、重感情\n\n"
     "请提供您的出生月日，我可以给出更详细的分析。",
     "astrology"),
]

# ---- 外部服务替换映射（企业聚焦 rejected 构造）----
# 将企业系统名替换为外部服务，构造"坏答案"
_EXTERNAL_SERVICE_MAP: dict[str, str] = {
    "idp.example.com": "Gmail 忘记密码页面",
    "itsm.example.com": "第三方软件下载站",
    "tripmgmt.example.com": "携程/去哪儿网",
    "fs.example.com": "百度网盘个人版",
    "mail.example.com": "QQ 邮箱服务器",
    "企业邮箱": "个人 Gmail",
    "企业统一身份认证平台": "Google 账户",
    "IT 服务管理平台": "360 软件管家",
    "公司差旅平台": "携程旅行 App",
    "内网软件中心": "腾讯软件中心",
    "考勤系统": "钉钉个人版打卡",
    "费用报销系统": "支付宝转账",
    "文件服务器": "百度网盘",
    "OA 系统": "微信工作群",
    "IT 服务台": "电脑维修店",
    "HR 部门": "外面的人事代理公司",
}


def _replace_enterprise_with_external(text: str, rng: random.Random) -> str:
    """将企业系统名替换为外部服务，构造企业聚焦的 rejected 答案。

    评审 #6：此前仅随机替换前 3 个命中键，rejected 中残留其余企业系统名
    （外部+企业混合信号 = 偏好标签噪声）；且迭代顺序随机，短键可能嵌套
    破坏长键。现按键长降序替换全部命中键，rejected 为纯外部服务版本。
    rng 参数保留以兼容调用签名（替换已改为确定性）。
    """
    del rng
    result = text
    for key in sorted(_EXTERNAL_SERVICE_MAP, key=len, reverse=True):
        if key in result:
            result = result.replace(key, _EXTERNAL_SERVICE_MAP[key])
    return result


def generate_boundary_pairs(rng: random.Random) -> list[dict]:
    """生成边界拒答偏好对。

    chosen = 拒答+引导（从 _BOUNDARY_CASES_EXPANDED）
    rejected = 真的回答了（如真写诗、真给股票建议）
    prompt 用弱 SYSTEM_PROMPT（非强 prompt），让 DPO 学到弱 prompt 下也能拒答。
    """
    pairs: list[dict] = []
    for question, chosen_answer, rejected_answer, category in _BOUNDARY_CASES_EXPANDED:
        # 为每个边界 case 生成 query 变体，增加数据量
        for variant in _query_variants(question):
            pairs.append({
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": variant},
                ],
                "chosen": [{"role": "assistant", "content": chosen_answer}],
                "rejected": [{"role": "assistant", "content": rejected_answer}],
                "meta": {"type": "boundary", "category": category},
            })
    return pairs


def generate_rag_extraction_pairs(rng: random.Random) -> list[dict]:
    """生成 RAG 提取偏好对。

    chosen = extractive_answers.json 的提取式答案（基座模型生成，精简直接）
    rejected = QA 模板原文（照抄 context，冗长复述）
    """
    extractive_map = _load_extractive_answers()
    pairs: list[dict] = []

    for qa_idx, qa in enumerate(_QA_TEMPLATES):
        scene, question, answer = qa

        # chosen：优先用基座模型提取式答案，fallback 到规则化重组
        if extractive_map and str(qa_idx) in extractive_map:
            chosen_answer = extractive_map[str(qa_idx)]
        else:
            # fallback：简单提取
            chosen_answer = answer.split("。")[0] + "。" if "。" in answer else answer

        # rejected：QA 模板原文（照抄 context 的行为）
        rejected_answer = answer

        # 跳过 chosen 与 rejected 相同的（无偏好信号）
        if chosen_answer.strip() == rejected_answer.strip():
            continue

        # 构造 context（含干扰文档，复用 SFT 数据的构造逻辑）
        context_parts = [f"【文档1】（来源：{scene}知识库）\n{answer}"]
        distractors = [q for q in _QA_TEMPLATES if q[2] != answer and q[0] != scene]
        distractor_answer: str | None = None
        if distractors:
            d = rng.choice(distractors)
            distractor_answer = d[2]
            context_parts.append(f"【文档2】（来源：{d[0]}知识库）\n{d[2]}")
        context = "\n\n".join(context_parts)

        for variant_idx, variant in enumerate(_query_variants(question)):
            user_msg = f"根据以下文档回答问题。\n\n{context}\n\n【问题】{variant}"
            # 评审 #6：rejected 覆盖两类失败模式——偶数变体=照抄正确文档原文
            # （冗长复述），奇数变体=照抄干扰文档（张冠李戴，答非所问）。
            # 此前只有前者，模型学不到"答非所问"是更严重的错误。
            if distractor_answer is not None and variant_idx % 2 == 1:
                rejected_text = distractor_answer
                rejected_kind = "wrong_document"
            else:
                rejected_text = rejected_answer
                rejected_kind = "verbatim_copy"
            # 变体级跳过：chosen 与该 rejected 相同则无偏好信号
            if chosen_answer.strip() == rejected_text.strip():
                continue
            pairs.append({
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT_RAG},
                    {"role": "user", "content": user_msg},
                ],
                "chosen": [{"role": "assistant", "content": chosen_answer}],
                "rejected": [{"role": "assistant", "content": rejected_text}],
                "meta": {"type": "rag_extraction", "scene": scene,
                         "rejected_kind": rejected_kind},
            })
    return pairs


def generate_enterprise_focus_pairs(rng: random.Random) -> list[dict]:
    """生成企业聚焦偏好对。

    chosen = QA 模板企业答案（提到 idp.example.com 等企业系统）
    rejected = 外部服务替换版（提到 Gmail/Outlook 等外部服务）
    只选择包含企业系统入口的 QA 模板。
    """
    pairs: list[dict] = []

    # 筛选包含企业系统名的 QA 模板
    enterprise_keywords = list(_EXTERNAL_SERVICE_MAP.keys())
    for qa in _QA_TEMPLATES:
        scene, question, answer = qa

        # 检查答案是否包含企业系统名
        has_enterprise_ref = any(kw in answer for kw in enterprise_keywords)
        if not has_enterprise_ref:
            continue

        chosen_answer = answer
        rejected_answer = _replace_enterprise_with_external(answer, rng)

        # 跳过无变化的（替换没生效）
        if chosen_answer.strip() == rejected_answer.strip():
            continue

        for variant in _query_variants(question):
            pairs.append({
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": variant},
                ],
                "chosen": [{"role": "assistant", "content": chosen_answer}],
                "rejected": [{"role": "assistant", "content": rejected_answer}],
                "meta": {"type": "enterprise_focus", "scene": scene},
            })
    return pairs


def generate_dpo_pairs(count: int = 600, seed: int = 42) -> list[dict]:
    """生成 DPO 偏好对列表（三类混合 + shuffle）。

    样本配比：40% 边界拒答 + 35% RAG 提取 + 25% 企业聚焦。
    """
    rng = random.Random(seed)

    # 生成各类偏好对
    boundary_pairs = generate_boundary_pairs(rng)
    rag_pairs = generate_rag_extraction_pairs(rng)
    enterprise_pairs = generate_enterprise_focus_pairs(rng)

    logger.info("生成偏好对: boundary=%d, rag=%d, enterprise=%d",
                len(boundary_pairs), len(rag_pairs), len(enterprise_pairs))

    # 按配比截取
    boundary_count = int(count * 0.40)
    rag_count = int(count * 0.35)
    enterprise_count = count - boundary_count - rag_count

    # 如果某类不够，从其他类补
    if len(boundary_pairs) < boundary_count:
        boundary_count = len(boundary_pairs)
        rag_count = count - boundary_count - min(enterprise_count, len(enterprise_pairs))
    if len(rag_pairs) < rag_count:
        rag_count = len(rag_pairs)
    if len(enterprise_pairs) < enterprise_count:
        enterprise_count = len(enterprise_pairs)

    rng.shuffle(boundary_pairs)
    rng.shuffle(rag_pairs)
    rng.shuffle(enterprise_pairs)

    pairs = (
        boundary_pairs[:boundary_count]
        + rag_pairs[:rag_count]
        + enterprise_pairs[:enterprise_count]
    )
    rng.shuffle(pairs)
    return pairs[:count]


def write_dpo_jsonl(pairs: list[dict], path: str | Path) -> int:
    """写 dpo.jsonl（conversational 格式，保留 meta 字段）。

    评审 #6：此前丢弃 meta，训练/审计侧无法追溯 type/category/scene/
    rejected_kind 分布。train_dpo.load_dpo_jsonl 本就读取 meta（缺省 {}），
    写入无副作用。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for p in pairs:
            out = {
                "prompt": p["prompt"],
                "chosen": p["chosen"],
                "rejected": p["rejected"],
                "meta": p.get("meta", {}),
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    return len(pairs)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地生成 DPO 偏好对齐训练数据（conversational 格式，不依赖网络/数据库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default="data/open/dpo.jsonl", help="输出 jsonl 路径")
    parser.add_argument("--count", type=int, default=600, help="生成条数上限")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（可复现）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    pairs = generate_dpo_pairs(count=args.count, seed=args.seed)
    n = write_dpo_jsonl(pairs, args.output)

    # 统计
    types: dict[str, int] = {}
    for p in pairs:
        t = p["meta"]["type"]
        types[t] = types.get(t, 0) + 1

    print(f"\n===== DPO 数据生成完成 =====")
    print(f"文件: {args.output}")
    print(f"总数: {n} 对")
    print(f"类型分布:")
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c} 对 ({c/n*100:.0f}%)")

    # 抽检样本
    print(f"\n===== 抽检样本（前 3 对）=====")
    for i, p in enumerate(pairs[:3]):
        print(f"\n--- 样本 {i+1} [{p['meta']['type']}] ---")
        print(f"Q: {p['prompt'][-1]['content'][:80]}...")
        print(f"chosen:    {p['chosen'][0]['content'][:80]}...")
        print(f"rejected:  {p['rejected'][0]['content'][:80]}...")

    print(f"\n下一步:")
    print(f"  1. 训练: python scripts/finetune/train_dpo.py --data {args.output} \\")
    print(f"         --base_model models/Qwen2.5-1.5B-Instruct --sft_adapter outputs/sft-v3-distill \\")
    print(f"         --output_dir outputs/dpo-v1-1.5b --beta 0.1 --lr 5e-5 --epochs 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
