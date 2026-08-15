"""
规则匹配器 — 单一职责：零 Token 意图识别。

通过正则表达式匹配用户输入中的常见意图模式，覆盖中英文双语。
规则保守匹配（高置信度阈值），未命中时交由 LLM 兜底。

遵循开闭原则：新增意图规则只需在 _RULES 追加条目。
"""

from __future__ import annotations

import re

from app.intent.router import (
    IntentResult,
    IntentType,
    _SHORTCUT_INTENTS,
    _TERMINAL_INTENTS,
)


class RuleMatcher:
    """规则匹配器 — 零 Token 意图识别。

    使用预编译正则表达式匹配用户输入，覆盖 4 种常见意图：
        - LIST_DOCUMENTS: "列出文档" / "list documents"
        - RAG_SEARCH: "搜索XX" / "XX是什么" / "search XX"
        - GET_DOCUMENT: "查看XX" / "view XX"
        - CREATE_DOCUMENT: "创建文档" / "upload document"
    """

    # 规则定义：(意图类型, [正则模式列表], 置信度)
    # 规则按优先级排序：终态出口（拒识）优先于检索意图，
    # 确保"越界/闲聊"查询先被拦截，不被 RAG_SEARCH 兜住。
    _RULES: list[tuple[IntentType, list[re.Pattern[str]], float]] = [
        # --- UNSUPPORTED: 超出知识库服务范围 → 拒识出口（优先级最高）---
        # 仅覆盖明确非知识库的领域（订票/天气/行情/闲聊/法律咨询），保守匹配，
        # 避免误伤企业范围内的工作流/业务查询。
        (
            IntentType.UNSUPPORTED,
            [
                re.compile(
                    r"(订|买|抢|退).*(机票|火车票|高铁票|酒店|宾馆|外卖|门票|演唱会)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(今天|明天|后天|周末|下周).*(天气|气温|降雨|台风|空气质量)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(推荐|预测|帮我选|买哪只|买什么|该买).*(股票|基金|股价|行情)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(讲个笑话|讲个故事|写一首诗|写作文|编个故事|闲聊|聊聊)",
                    re.IGNORECASE,
                ),
                # --- 法律咨询拒答（方案3：规则兜底）---
                # 匹配法律领域关键词，拦截法律咨询类问题，引导至法务部/12348。
                # 设计原则：匹配法律术语本身（如"劳动仲裁""离婚财产"），
                # 这些词在企业知识库语境下几乎只出现在法律咨询场景。
                # 劳动法相关
                re.compile(
                    r"(劳动仲裁|劳动争议|劳动维权|欠薪|被辞退|被开除|"
                    r"工伤认定|伤残鉴定|竞业协议.*(效力|合法|补偿|违约|纠纷)|"
                    r"调岗降薪|离职补偿金|年假.*折算|产假.*工资|"
                    r"试用期.*社保|试用期.*辞退|社保补缴|"
                    r"劳务派遣|非全日制用工|"
                    r"劳动合同.*(到期|不续签|未签|索赔)|"
                    r"不发工资.*告|加班费.*追讨|辞退.*补偿|"
                    r"加班.*调休.*加班费|加班费.*还是.*调休)",
                    re.IGNORECASE,
                ),
                # 合同法相关
                re.compile(
                    r"(合同纠纷|合同违约|供应商.*违约|对方.*不履行|"
                    r"对方.*不交货|对方.*不还钱|合作协议.*毁约|"
                    r"合同.*争议|格式合同|格式条款|缔约过失|"
                    r"定金.*订金|违约金|电子合同.*效力|口头协议|"
                    r"合同诈骗|合同.*单方.*解除|合同.*撤销|"
                    r"租房.*纠纷|借款.*不还)",
                    re.IGNORECASE,
                ),
                # 交通事故相关
                re.compile(
                    r"(交通事故|车祸|被车撞|肇事|酒驾.*事故|"
                    r"交通事故.*私了|交通事故.*误工费|"
                    r"交通事故.*精神损害|交通事故.*伤残|"
                    r"交通事故.*(赔偿|理赔|索赔)|"
                    r"车祸.*(赔偿|全责|索赔)|肇事.*(赔偿|不赔))",
                    re.IGNORECASE,
                ),
                # 婚姻家庭/继承
                re.compile(
                    r"(离婚.*财产|离婚协议|抚养权|抚养费|探望权|"
                    r"婚前财产|夫妻.*共同债务|家暴|"
                    r"遗产继承|法定继承|遗嘱|代位继承|转继承|"
                    r"遗赠扶养|遗产.*分割|邻居.*噪音.*起诉)",
                    re.IGNORECASE,
                ),
                # 知识产权
                re.compile(
                    r"(专利.*侵权|商标.*侵权|著作权.*侵权|"
                    r"商标.*注册|专利.*申请|商业秘密.*保护|"
                    r"不正当竞争|域名.*抢注|软件著作权|"
                    r"商标被侵权|个人隐私.*泄露|被诽谤|名誉权.*侵权)",
                    re.IGNORECASE,
                ),
                # 公司法相关
                re.compile(
                    r"(股权转让|公司清算|股东.*权利|董事.*责任|"
                    r"关联交易|公司减资|公司分立)",
                    re.IGNORECASE,
                ),
                # 侵权责任
                re.compile(
                    r"(人身损害|产品.*缺陷.*赔偿|医疗事故|"
                    r"高空坠物|被.*狗咬|网络.*诽谤|"
                    r"精神损害.*赔偿|被人诽谤|网上.*诽谤)",
                    re.IGNORECASE,
                ),
                # 消费者权益
                re.compile(
                    r"(七天无理由|假冒伪劣|网购.*纠纷|"
                    r"虚假宣传|预付卡.*跑路|快递.*丢失.*索赔|"
                    r"食品安全.*维权|消费者.*维权|"
                    r"买到.*假冒|网购.*投诉)",
                    re.IGNORECASE,
                ),
                # 行政/刑事
                re.compile(
                    r"(行政复议|行政诉讼|国家赔偿|刑事.*报案|"
                    r"正当防卫|取保候审|刑事附带民事|紧急避险|"
                    r"报案.*立案|怎么起诉|怎么.*告.*公司|"
                    r"可以告|怎么.*维权|怎么.*索赔|怎么.*追讨)",
                    re.IGNORECASE,
                ),
            ],
            0.9,
        ),
        # --- LIST_DOCUMENTS: 列出/列表/有哪些 ---
        (
            IntentType.LIST_DOCUMENTS,
            [
                re.compile(
                    r"(列出|列表|有哪些|显示一下|看一下列表).*(文档|知识库|文件|资料)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(list|show|all)\s+(documents?|knowledge|files?)",
                    re.IGNORECASE,
                ),
                re.compile(r"(我的|所有).*(文档|知识库)", re.IGNORECASE),
            ],
            0.9,
        ),
        # --- RAG_SEARCH: 搜索/查找/是什么/怎么 ---
        (
            IntentType.RAG_SEARCH,
            [
                re.compile(
                    # 长词优先：避免 "搜索一下XX" 被 "搜索" 截断留下 "一下XX"
                    r"(搜索一下|搜一下|查找一下|查一下|找一下|帮我查|搜索|查找|查询|检索)(.+)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(search|find|query|look\s+for)\s+(.+)",
                    re.IGNORECASE,
                ),
                re.compile(r".*(是什么|怎么|如何|为什么|什么是|区别|对比|优缺点)"),
                re.compile(r".*(流程|步骤|方法|规范|要求).*(是|有)"),
            ],
            0.85,
        ),
        # --- GET_DOCUMENT: 查看/打开/详情 ---
        (
            IntentType.GET_DOCUMENT,
            [
                re.compile(
                    r"(查看|打开|看看|详情|阅读)(.+)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(view|open|detail|read)\s+(.+)",
                    re.IGNORECASE,
                ),
            ],
            0.85,
        ),
        # --- CREATE_DOCUMENT: 创建/上传/添加 ---
        (
            IntentType.CREATE_DOCUMENT,
            [
                re.compile(
                    r"(创建|上传|添加|新建|导入).*(文档|文件|知识|资料)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(create|upload|add|new|import)\s+(document|file|knowledge)",
                    re.IGNORECASE,
                ),
            ],
            0.9,
        ),
    ]

    def match(self, query: str) -> IntentResult | None:
        """匹配用户查询到意图。

        Args:
            query: 用户输入的自然语言查询。

        Returns:
            IntentResult | None: 匹配结果，未匹配返回 None。
        """
        query_stripped = query.strip()
        if not query_stripped:
            return None

        for intent, patterns, confidence in self._RULES:
            for pattern in patterns:
                match = pattern.search(query_stripped)
                if match:
                    # 提取参数（如搜索关键词）
                    parameters: dict[str, str] = {}
                    if intent == IntentType.RAG_SEARCH:
                        candidate = ""
                        if match.lastindex and match.lastindex >= 2:
                            candidate = match.group(match.lastindex).strip()
                        # 单捕获组模式（".*(是什么|怎么...)"）无组 2 可取；
                        # 双捕获组但组 2 为 "是"/"有" 等单字虚词时同样无意义。
                        # 两种情形都回退为整条查询，避免用虚词做检索词。
                        parameters["search_query"] = (
                            candidate if len(candidate) > 1 else query_stripped
                        )
                    elif intent == IntentType.GET_DOCUMENT and match.lastindex and match.lastindex >= 2:
                        parameters["document_ref"] = match.group(match.lastindex).strip()
                    elif intent == IntentType.UNSUPPORTED:
                        if self._is_legal_query(query_stripped):
                            parameters["refusal_type"] = "legal"

                    return IntentResult(
                        intent=intent,
                        confidence=confidence,
                        parameters=parameters,
                        use_shortcut=(
                            intent in _SHORTCUT_INTENTS
                            or intent in _TERMINAL_INTENTS
                        ),
                    )
        return None

    # 法律咨询检测关键词 — 用于在 UNSUPPORTED 匹配后判断是否为法律类，
    # 以便 shortcut_handler 返回法律专属拒答话术（引导到法务部/12348）。
    # 覆盖范围与 _RULES 中的法律 UNSUPPORTED 模式一致。
    _LEGAL_INDICATORS: re.Pattern[str] = re.compile(
        r"(劳动仲裁|劳动争议|劳动维权|欠薪|被辞退|被开除|"
        r"工伤认定|伤残鉴定|竞业协议.*(效力|合法|补偿|违约|纠纷)|"
        r"调岗降薪|离职补偿|年假.*折算|产假.*工资|试用期.*社保|"
        r"试用期.*辞退|社保补缴|劳务派遣|非全日制用工|"
        r"劳动合同.*(到期|不续签|未签|索赔)|"
        r"不发工资.*告|加班费.*追讨|辞退.*补偿|"
        r"加班.*调休.*加班费|加班费.*还是.*调休|"
        r"合同纠纷|合同违约|供应商.*违约|对方.*不履行|"
        r"对方.*不交货|对方.*不还钱|合作协议.*毁约|"
        r"合同.*争议|格式合同|格式条款|缔约过失|"
        r"定金.*订金|违约金|电子合同|口头协议|"
        r"合同诈骗|合同.*单方.*解除|合同.*撤销|"
        r"租房.*纠纷|借款.*不还|"
        r"交通事故|车祸|被车撞|肇事|酒驾.*事故|"
        r"交通事故.*私了|交通事故.*误工费|"
        r"交通事故.*精神损害|交通事故.*伤残|"
        r"车祸.*(赔偿|全责|索赔)|肇事.*(赔偿|不赔)|"
        r"离婚.*财产|离婚协议|抚养权|抚养费|探望权|"
        r"婚前财产|夫妻.*共同债务|家暴|"
        r"遗产继承|法定继承|遗嘱|代位继承|转继承|"
        r"遗赠扶养|遗产.*分割|邻居.*噪音.*起诉|"
        r"专利.*侵权|商标.*侵权|著作权.*侵权|"
        r"商标.*注册|专利.*申请|商业秘密.*保护|"
        r"不正当竞争|域名.*抢注|软件著作权|"
        r"商标被侵权|个人隐私.*泄露|被诽谤|名誉权.*侵权|"
        r"股权转让|公司清算|股东.*权利|董事.*责任|"
        r"关联交易|公司减资|公司分立|"
        r"人身损害|产品.*缺陷.*赔偿|医疗事故|"
        r"高空坠物|被.*狗咬|网络.*诽谤|"
        r"精神损害.*赔偿|被人诽谤|网上.*诽谤|"
        r"七天无理由|假冒伪劣|网购.*纠纷|"
        r"虚假宣传|预付卡.*跑路|快递.*丢失.*索赔|"
        r"食品安全.*维权|消费者.*维权|"
        r"买到.*假冒|网购.*投诉|"
        r"行政复议|行政诉讼|国家赔偿|刑事.*报案|"
        r"正当防卫|取保候审|刑事附带民事|紧急避险|"
        r"报案.*立案|怎么起诉|怎么.*告.*公司|"
        r"可以告|怎么.*维权|怎么.*索赔|怎么.*追讨)",
        re.IGNORECASE,
    )

    def _is_legal_query(self, query: str) -> bool:
        """检测查询是否为法律咨询类问题。

        用于在 UNSUPPORTED 匹配后区分法律类拒答（引导到法务部/12348）
        与通用越界拒答（引导到对应服务台）。
        """
        return bool(self._LEGAL_INDICATORS.search(query))
