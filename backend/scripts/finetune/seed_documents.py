#!/usr/bin/env python
"""样本文档灌库脚本 — 往知识库 Document 表批量灌入内置样本文档。

为微调数据合成（synthesize_qa.py）提供真实可用的语料底座：脚本内置 40 篇
覆盖企业常见场景的样本文档文本，按 5 大场景分布，密级分布约为
public 30% / internal 50% / confidential 20%，便于后续验证密级过滤、
场景召回与能力边界识别。

设计要点：
    1. 文档内容真实、有信息量（非 lorem ipsum），每篇 200-500 字，贴近真实企业文档；
    2. 复用项目 Document 模型 + 异步 DB session（照 finetune_tasks.py 的 task_db_session 写法）；
    3. 幂等：相同 title+tenant 已存在（未软删）则跳过，除非 --clear 先清空该租户的 seed 文档；
    4. 重依赖（sqlalchemy / app 模块）延迟导入到函数内，模块顶层仅标准库，
       保证无 DB / 无 sqlalchemy 环境可 import 做单测；
    5. 内置文档数据抽成纯函数 get_seed_documents() -> list[dict]，可独立单测。

运行示例：

    # 灌入全部样本文档（需提供 tenant_id）
    cd backend && .venv/bin/python scripts/finetune/seed_documents.py --tenant_id <uuid>

    # 清空该租户已有 seed 文档后重新灌入（防重复）
    cd backend && .venv/bin/python scripts/finetune/seed_documents.py --tenant_id <uuid> --clear

    # 仅灌入前 20 篇
    cd backend && .venv/bin/python scripts/finetune/seed_documents.py --tenant_id <uuid> --count 20

    # 指定所属知识库与所有者（否则自动取该租户下首个知识库 / 首个用户兜底）
    cd backend && .venv/bin/python scripts/finetune/seed_documents.py --tenant_id <uuid> \\
        --kb_id <uuid> --owner_id <uuid>
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from typing import Any

logger = logging.getLogger("seed_documents")

# ------------------------------------------------------------------
# 场景常量（同时写入 Document.category，供召回/统计区分场景）
# ------------------------------------------------------------------
SCENARIO_IT = "IT运维"
SCENARIO_HR = "HR人事"
SCENARIO_OA = "OA审批"
SCENARIO_PRODUCT = "产品业务"
SCENARIO_BOUNDARY = "边界说明"

SCENARIOS = (
    SCENARIO_IT,
    SCENARIO_HR,
    SCENARIO_OA,
    SCENARIO_PRODUCT,
    SCENARIO_BOUNDARY,
)

#: 合法密级（secret 不在样本文档中使用，避免被密级过滤全部剔除）
VALID_CLASSIFICATIONS = ("public", "internal", "confidential")

#: seed 来源标记（写入 content_json，--clear 据此精确清理 seed 文档，不影响人工录入）
SEED_SOURCE = "seed"


# ------------------------------------------------------------------
# 内置样本文档数据（纯函数，可单测）
# ------------------------------------------------------------------
# 每条：(title, content, classification, scenario)
# 密级分布（共 40 篇）：public 12 (30%) / internal 20 (50%) / confidential 8 (20%)
_SEED_DOCS: list[tuple[str, str, str, str]] = [
    # === IT 运维类（10 篇：public 4 / internal 5 / confidential 1）===
    (
        "密码重置流程",
        "忘记登录密码的同事可通过以下方式自助重置：\n"
        "1. 打开统一身份认证平台 sso.example.com，点击登录页「忘记密码」；\n"
        "2. 输入工号或企业邮箱，选择「手机验证码」或「邮箱验证码」完成身份校验；\n"
        "3. 设置新密码（需 8 位以上，包含大小写字母与数字，90 天强制更换一次）；\n"
        "4. 重置成功后 5 分钟内生效，如仍无法登录请清除浏览器缓存或重启客户端。\n"
        "如手机号已变更无法接收验证码，请携带工卡至 IT 服务台前台办理人工重置。",
        "public",
        SCENARIO_IT,
    ),
    (
        "VPN 远程接入配置指南",
        "远程办公需通过 VPN 接入内网，配置步骤如下：\n"
        "1. 在软件中心下载并安装 EasyConnect 客户端（Windows/macOS 通用）；\n"
        "2. 打开客户端，服务器地址填入 vpn.example.com，端口 443；\n"
        "3. 使用工号 + 域账号密码登录，首次登录需绑定 MFA 动态口令；\n"
        "4. 连接成功后系统托盘图标显示绿色，即可访问 OA、知识库、代码仓库等内网资源；\n"
        "5. VPN 会话闲置 30 分钟自动断开，如需长时间连接请在客户端勾选「保持在线」。\n"
        "禁止在公共网络下使用 VPN，避免会话被劫持。",
        "internal",
        SCENARIO_IT,
    ),
    (
        "企业邮箱客户端设置说明",
        "企业邮箱支持 Outlook / Foxmail / 网页邮箱多种客户端，推荐使用网页邮箱收发邮件。\n"
        "IMAP 服务器：imap.example.com，端口 993，SSL 加密；\n"
        "SMTP 服务器：smtp.example.com，端口 465，SSL 加密；\n"
        "账号格式：工号@example.com，密码与域账号一致。\n"
        "Outlook 自动发现配置：在「添加账户」输入邮箱地址即可自动完成配置。\n"
        "邮箱容量 50GB，归档邮件不计入容量，可在「设置-归档」中配置自动归档规则。\n"
        "如遇发信被退回，请检查是否触发了外发频率限制（每分钟 30 封）。",
        "public",
        SCENARIO_IT,
    ),
    (
        "办公打印机连接与驱动安装",
        "公司各楼层均配置网络打印机，连接方式如下：\n"
        "1. Windows：控制面板 → 设备和打印机 → 添加打印机 → 选择「我需要的打印机不在列表中」→ 按 TCP/IP 地址添加，地址见打印机机身标签；\n"
        "2. macOS：系统设置 → 打印机与扫描仪 → 添加打印机 → IP 栏输入打印机地址，协议选 LPD，队列名填 lp；\n"
        "3. 驱动在软件中心「打印机驱动包」一键安装，包含主流型号；\n"
        "4. 默认双面黑白打印，彩色打印需在打印属性中手动切换并计入部门成本。\n"
        "如打印任务卡住，可在打印服务器 print.example.com 的 Web 面板清除任务队列。",
        "public",
        SCENARIO_IT,
    ),
    (
        "多因素认证(MFA)开启指引",
        "为提升账号安全，所有域账号需开启多因素认证（MFA）：\n"
        "1. 登录统一身份认证平台，进入「安全设置 - 多因素认证」；\n"
        "2. 推荐使用「企业令牌 App」扫码绑定（App 在软件中心下载）；\n"
        "3. 也可使用第三方验证器（Google Authenticator / 微软身份验证器）扫描二维码；\n"
        "4. 绑定后登录 VPN、邮箱、OA 等系统时需输入 6 位动态口令，每 30 秒刷新；\n"
        "5. 请妥善保存备份恢复码（10 个），手机丢失时可用恢复码登录后重新绑定。\n"
        "未开启 MFA 的账号将在 7 天后被限制登录 VPN 与外网邮箱。",
        "internal",
        SCENARIO_IT,
    ),
    (
        "办公电脑标准化软件清单",
        "公司办公电脑预装标准化软件清单，禁止自行安装未授权软件：\n"
        "- 办公套件：WPS Office 或 Microsoft 365（按授权分配）；\n"
        "- 浏览器：Edge（默认）/ Chrome（开发岗）；\n"
        "- 通讯协作：企业微信 / 飞书、腾讯会议；\n"
        "- 安全软件：终端 EDR（强制安装，不可卸载）、VPN 客户端；\n"
        "- 开发工具：VS Code、IntelliJ IDEA、Git、Docker Desktop（仅开发岗）；\n"
        "- 压缩解压：Bandizip / The Unarchiver。\n"
        "其他软件请在软件中心提交申请，经 IT 审批后由管理员推送安装。"
        "私自安装破解软件将被 EDR 告警并通报信息安全部。",
        "internal",
        SCENARIO_IT,
    ),
    (
        "VPN 账号申请与权限审批流程",
        "VPN 账号按最小权限原则分配，申请流程如下：\n"
        "1. 申请人在 OA 系统「VPN 权限申请」流程中填写：姓名、工号、部门、申请事由、所需访问的内网网段；\n"
        "2. 直属主管审批 → 部门信息安全员复核 → IT 运维组开通；\n"
        "3. 权限有效期默认 90 天，到期需重新申请续期；\n"
        "4. 离职 / 转岗时权限自动回收，由 HR 系统触发联动。\n"
        "申请访问研发核心网段、生产数据库网段需额外 CTO 审批，且仅限研发核心岗位。\n"
        "严禁将 VPN 账号借给他人使用，违规者将按信息安全红线处理。",
        "confidential",
        SCENARIO_IT,
    ),
    (
        "邮箱归档与容量清理操作",
        "邮箱容量接近上限时（达 80% 预警），建议进行归档清理：\n"
        "1. 网页邮箱「设置 - 归档规则」可按时间/发件人/关键词自动归档，归档邮件移入「在线归档」不计入容量；\n"
        "2. 手动归档：选中邮件 → 右键「归档」，或拖入左侧「在线归档」文件夹；\n"
        "3. 大附件清理：在「设置 - 存储分析」查看占用最大的邮件，可一键清理超过 1 年的大附件；\n"
        "4. 清空「已删除」与「垃圾邮件」文件夹可立即释放空间。\n"
        "归档邮件保留 5 年，仍可通过搜索检索，但不再占用主邮箱容量。",
        "internal",
        SCENARIO_IT,
    ),
    (
        "IT 服务台工单提交规范",
        "遇到 IT 问题请通过工单系统提交，便于跟踪与统计：\n"
        "1. 访问 helpdesk.example.com 或企业微信「IT 服务台」应用；\n"
        "2. 选择问题分类（账号 / 网络 / 设备 / 软件 / 邮箱 / 其他），填写标题、描述、截图；\n"
        "3. 紧急问题（如系统宕机、无法登录）可勾选「加急」，SLA 为 15 分钟响应；\n"
        "4. 普通工单 SLA 为 4 小时内响应、1 个工作日内解决；\n"
        "5. 工单进度会通过企业微信推送，可在「我的工单」查看与评价。\n"
        "电话热线 8000 仅受理紧急故障，常规问题请走工单以便留痕。",
        "public",
        SCENARIO_IT,
    ),
    (
        "无线网络 WiFi 接入指南",
        "公司办公区提供两个无线网络：\n"
        "- EKB-Staff：员工办公网络，使用域账号 + 密码认证，自动连接；\n"
        "- EKB-Guest：访客网络，通过短信验证码登录，仅可访问互联网，无法访问内网资源。\n"
        "首次连接 EKB-Staff：在 WiFi 列表选择后输入工号与域密码，信任证书后即可上网。\n"
        "macOS 用户如提示证书不受信任，请在「钥匙串访问」中将根证书设为「始终信任」。\n"
        "禁止私自搭建无线路由器或热点，避免干扰办公网络与造成安全风险。",
        "internal",
        SCENARIO_IT,
    ),
    # === HR 人事类（10 篇：public 2 / internal 6 / confidential 2）===
    (
        "年假政策与休假申请",
        "公司年假政策如下：\n"
        "- 入职满 1 年享有 5 天年假，满 3 年 10 天，满 5 年 15 天，满 10 年及以上 20 天；\n"
        "- 年假按自然年计算，当年未休完可结转至次年第一季度，逾期清零；\n"
        "- 休假需提前在 OA 系统「请假申请」提交，1 天以内由直属主管审批，3 天以上需部门负责人审批；\n"
        "- 连续休假 5 天以上需提前 7 个工作日申请并做好工作交接；\n"
        "- 法定节假日按国家规定执行，调休安排以公司公告为准。\n"
        "病假需提供三甲医院病假证明，婚假、产假、陪产假按当地法规执行。",
        "public",
        SCENARIO_HR,
    ),
    (
        "员工费用报销流程",
        "费用报销统一通过 OA「费用报销」流程线上提交：\n"
        "1. 准备发票（电子发票需打印或上传 PDF）、支付凭证、消费明细；\n"
        "2. 在报销单中选择费用类型（差旅 / 办公 / 招待 / 培训 / 其他），填写金额与事由；\n"
        "3. 关联出差申请单（差旅类必填）或项目编号（项目分摊类必填）；\n"
        "4. 提交后按金额分级审批：5000 元以下主管审批，5000-20000 元部门负责人审批，20000 元以上需分管副总审批；\n"
        "5. 审批通过后财务在 5 个工作日内打款至工资卡。\n"
        "报销需在费用发生后 30 天内提交，跨年报销截止次年 1 月 15 日。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "考勤打卡与异常处理规则",
        "考勤管理规则如下：\n"
        "- 工作时间 9:00-18:00，午休 12:00-13:00，实行弹性打卡（9:00 前打卡计为正常，最晚 9:30 到岗）；\n"
        "- 打卡方式：办公区门禁刷卡、企业 WiFi 自动打卡、企业微信「考勤打卡」（仅限外勤）；\n"
        "- 漏打卡需在 2 个工作日内提交「补卡申请」，附同事证明，每月限 3 次；\n"
        "- 迟到 / 早退 30 分钟内扣 0.5 小时假，超过 30 分钟计半天事假；\n"
        "- 旷工 1 天扣 3 天工资，连续旷工 3 天或全年累计 5 天按严重违纪处理。\n"
        "考勤异常申诉请在次月 3 日前在 OA 提交，逾期不再受理。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "新员工入职手续办理指南",
        "新员工入职需按以下流程办理：\n"
        "1. 入职前一天携带身份证原件、学历学位证原件、离职证明、体检报告、银行卡至 HR 柜台报到；\n"
        "2. 签订劳动合同（一式两份）、保密协议、知识产权归属协议；\n"
        "3. 领取工卡、门禁卡，由 IT 开通域账号、邮箱、企业微信、OA 系统权限；\n"
        "4. 配置办公电脑与工位，领取办公用品（在 OA「办公用品申领」提交）；\n"
        "5. 参加为期 3 天的新员工培训（公司文化、规章制度、信息安全、产品介绍）；\n"
        "6. 分配至用人部门，由直属主管安排导师进行为期 3 个月的试用期辅导。\n"
        "试用期为 3 个月，期满考核合格后转正。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "员工离职交接流程",
        "员工离职需提前 30 天（试用期 3 天）提交书面辞职申请，交接流程如下：\n"
        "1. OA「离职申请」提交，直属主管与部门负责人审批；\n"
        "2. 审批通过后领取《离职交接清单》，逐项交接：工作内容、文档资料、项目进度、客户联系；\n"
        "3. 归还工卡、门禁卡、办公电脑、外发设备、钥匙等公司财产；\n"
        "4. IT 注销账号权限（邮箱设为转发 30 天后关闭）、HR 结算工资与年假折算；\n"
        "5. 签署《离职保密承诺书》，离职后 2 年内承担竞业限制义务（限核心岗位）。\n"
        "最后工作日由 HR 出具《离职证明》，社保公积金次月停缴并转移。\n"
        "离职交接清单须经主管、IT、行政、财务四方签字确认后方可办理最终结算。",
        "confidential",
        SCENARIO_HR,
    ),
    (
        "加班管理与调休政策",
        "加班管理遵循「调休优先、补贴为辅」原则：\n"
        "- 工作日加班需提前在 OA「加班申请」提交，经主管审批后生效；\n"
        "- 工作日加班满 1 小时起算，可按 1:1 调休或领取加班补贴（按小时工资 1.5 倍）；\n"
        "- 周末加班可调休（1:1）或领取补贴（2 倍工资），法定节假日加班不可调休，按 3 倍工资发放；\n"
        "- 调休需在加班发生后 3 个月内使用，逾期清零；\n"
        "- 每月加班时长原则上不超过 36 小时，特殊情况需部门负责人与 HR 联合审批。\n"
        "调休申请在 OA「请假申请」中选择「调休」类型，关联原加班记录。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "社保公积金缴纳说明",
        "公司依法为正式员工缴纳五险一金，缴纳规则如下：\n"
        "- 缴纳基数：按员工上年度月平均工资核定，新员工按约定薪资核定，每年 7 月统一调整；\n"
        "- 养老保险：单位 16% + 个人 8%；医疗保险：单位 8% + 个人 2%；\n"
        "- 失业保险：单位 0.5% + 个人 0.5%；工伤保险：单位 0.2%（个人不缴）；生育保险：单位 0.8%（个人不缴）；\n"
        "- 住房公积金：单位 12% + 个人 12%，可在 5%-12% 区间申请调整个人比例；\n"
        "- 补充住房公积金：核心岗位可享单位额外 5% 缴纳。\n"
        "社保公积金每月 15 日扣缴，异地缴纳需在入职时登记参保地。"
        "员工可通过当地社保 App 或公积金中心查询个人账户明细。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "员工培训与发展路径",
        "公司提供多通道职业发展路径与培训资源：\n"
        "- 双通道发展：管理通道（M1-M5）与专业通道（P1-P8），员工可结合自身特长选择；\n"
        "- 内部培训：每月发布课程日历，涵盖技术、管理、通用技能，可在「学习平台」报名；\n"
        "- 外部培训：年度预算每人 5000 元，用于行业大会、认证考试、外部课程；\n"
        "- 导师制度：入职配备 3 个月导师，晋升后可申请成为导师带教新人；\n"
        "- 学习平台：接入极客时间、Coursera 等外部资源，账号在 IT 服务台申请。\n"
        "晋升评审每年 4 月、10 月各一次，需提交述职报告并由晋升委员会评议。",
        "public",
        SCENARIO_HR,
    ),
    (
        "绩效考核周期与流程",
        "绩效考核采用季度 + 年度双周期：\n"
        "- 季度回顾：每季度末由员工填写「季度总结」，主管进行 1 对 1 面谈，确认下季度目标；\n"
        "- 年度考核：每年 1 月进行上年度考核，综合 4 个季度表现，产出年度绩效等级（S/A/B/C/D）；\n"
        "- 考核维度：业绩达成 60% + 价值观 20% + 协作贡献 20%；\n"
        "- 等级分布：S（10%）/ A（20%）/ B（55%）/ C（10%）/ D（5%），D 级进入改进计划；\n"
        "- 绩效结果与年终奖、调薪、晋升、期权授予直接挂钩。\n"
        "员工对考核结果有异议可在结果公布后 5 个工作日内发起申诉，由 HRBP 介入复核。",
        "internal",
        SCENARIO_HR,
    ),
    (
        "薪酬保密与薪资调整规则",
        "公司实行薪酬保密制度，相关规定如下：\n"
        "- 员工薪资属个人保密信息，不得向同事打听、透露或讨论，违者按严重违纪处理；\n"
        "- 年度调薪：每年 4 月根据年度绩效与市场水平统一调整，调幅参考绩效等级（S 15% / A 10% / B 5% / C 0% / D 降薪）；\n"
        "- 晋升调薪：通过晋升评审后随职级调整薪资，管理通道涨幅 15%-30%；\n"
        "- 特殊调薪：因岗位变动、市场稀缺人才可由部门负责人发起特殊调薪申请，需 CTO/CFO 审批；\n"
        "- 薪资结构与发放：基本工资 70% + 绩效工资 30%，每月 10 日发放，遇节假日提前。\n"
        "薪资条在「员工自助平台」查询，如有疑问请在发薪后 3 个工作日内联系薪酬专员。",
        "confidential",
        SCENARIO_HR,
    ),
    # === OA 审批类（8 篇：public 2 / internal 4 / confidential 2）===
    (
        "采购申请审批流程",
        "采购申请统一在 OA「采购申请」流程提交，审批节点如下：\n"
        "1. 申请人填写采购清单（品名、规格、数量、预算单价、供应商、用途）；\n"
        "2. 单笔 1 万元以下：直属主管审批 → 采购部询价比价 → 下单；\n"
        "3. 单笔 1 万-10 万元：部门负责人审批 → 采购部招标（3 家以上比价）→ 财务复核 → 下单；\n"
        "4. 单笔 10 万元以上：分管副总审批 → 公开招标 → 法务审核合同 → 财务付款；\n"
        "5. 固定资产采购需额外填写「资产登记卡」，到货后由行政验收贴标入库。\n"
        "采购周期：标准品 5 个工作日，定制/招标类 20-30 个工作日。"
        "严禁拆单规避审批，违规采购不予报销并通报。",
        "internal",
        SCENARIO_OA,
    ),
    (
        "用印申请与印章管理规范",
        "公司印章（公章、合同章、财务章、法人章）由行政部专人保管，使用需审批：\n"
        "1. 用印申请在 OA「用印申请」提交，填写用印事由、文件名称、份数、印章类型；\n"
        "2. 普通用印（公章）：直属主管审批 → 行政部审核 → 现场监印；\n"
        "3. 合同用印：需先完成合同审批流程，凭审批通过记录申请用印；\n"
        "4. 携带印章外出或异地用印需部门负责人 + 行政总监双重审批，并由两人同行监印；\n"
        "5. 用印登记本（电子 + 纸质）逐次记录，留存用印文件复印件归档。\n"
        "禁止在空白文件、未审批文件上用印，禁止印章外借。"
        "印章遗失需立即上报并登报声明作废。",
        "confidential",
        SCENARIO_OA,
    ),
    (
        "差旅预订与报销审批",
        "差旅预订与报销规范如下：\n"
        "1. 出差前在 OA「出差申请」提交，填写目的地、时间、事由、预算，经主管审批；\n"
        "2. 交通：通过公司差旅平台（携程企业版）预订，机票经济舱、高铁二等座为标准；\n"
        "3. 住宿：按城市等级限额（一线 500 元/晚，二线 400 元/晚），超标需提前申请；\n"
        "4. 餐补：按出差地标准发放（50-100 元/天），凭发票报销或计入出差补贴；\n"
        "5. 出差返回后 5 个工作日内提交报销单，关联出差申请单，附行程单与住宿发票；\n"
        "6. 审批：5000 元以下主管审批，5000 元以上部门负责人审批。\n"
        "管理人员可乘商务舱 / 一等座，需在出差申请中注明职级。",
        "internal",
        SCENARIO_OA,
    ),
    (
        "合同审批与归档流程",
        "合同审批流程如下：\n"
        "1. 经办人在 OA「合同审批」上传合同文本（必须是法务标准模板或经法务审核的版本）；\n"
        "2. 填写对方主体、合同金额、期限、关键条款、风险点说明；\n"
        "3. 审批节点：直属主管 → 法务（合规与条款审核）→ 财务（付款条款与税务）→ 部门负责人 → 分管副总（金额超阈值）；\n"
        "4. 审批通过后申请用印，原件一式四份（公司两份、对方两份）；\n"
        "5. 合同原件交行政部归档，扫描件上传至合同管理系统，按编号检索；\n"
        "6. 合同履行过程由经办人跟进，到期前 30 天系统预警续签或终止。\n"
        "金额 50 万元以上或涉及知识产权、对外担保的合同需法务总监与总经理联签。",
        "confidential",
        SCENARIO_OA,
    ),
    (
        "请假审批节点与权限",
        "请假类型与审批节点如下：\n"
        "- 事假：1 天以内直属主管审批；1-3 天主管 + 部门负责人审批；3 天以上加 HR 审批；\n"
        "- 病假：1 天以内主管审批（需病假证明可后补）；3 天以上需三甲医院证明 + HR 审批；\n"
        "- 年假：1 天以内主管审批；连续 5 天以上需提前 7 天申请并交接工作；\n"
        "- 婚假：3 天，凭结婚证申请，主管审批；\n"
        "- 产假：98 天 + 地方延长假，需提前 1 个月申请，HR + 部门负责人审批；\n"
        "- 陪产假：15 天，凭出生证明申请。\n"
        "请假需在 OA「请假申请」提交，审批通过后系统自动通知考勤与同事。"
        "紧急情况可先口头请假，返岗当日补提申请。",
        "public",
        SCENARIO_OA,
    ),
    (
        "固定资产申领审批",
        "固定资产（电脑、显示器、办公家具等）申领流程：\n"
        "1. 在 OA「资产申领」提交申请，选择资产类型、规格、用途；\n"
        "2. 标准配置（如开发岗标配笔记本电脑 + 27 寸显示器）直属主管审批即可；\n"
        "3. 非标配置或新增资产需部门负责人 + 行政部审批，超出预算需财务复核；\n"
        "4. 审批通过后由行政部调拨库存或采购，到货后领取并签署《资产领用确认单》；\n"
        "5. 资产贴标登记至个人名下，离职或调岗时需归还或办理资产转移。\n"
        "资产损坏 / 遗失需在 2 个工作日内报修或报损，非正常损耗按折旧赔偿。",
        "internal",
        SCENARIO_OA,
    ),
    (
        "会议室预定与使用规范",
        "公司会议室通过「会议室预定系统」线上预定：\n"
        "1. 在企业微信「会议室」或 OA 预定系统选择时间、人数、所需设备（投影/视频会议/白板）；\n"
        "2. 单次预定不超过 2 小时，全天会议需提前 1 天申请并经行政确认；\n"
        "3. 预定后 15 分钟未签到系统自动释放，连续 3 次爽约将暂停预定权限 1 周；\n"
        "4. 使用完毕请清理桌面、关闭设备电源、擦净白板；\n"
        "5. 涉外会议或客户接待需提前通知行政安排茶水与门禁。\n"
        "外部访客进入办公区需在前台登记并佩戴访客牌。",
        "public",
        SCENARIO_OA,
    ),
    (
        "OA 系统移动端审批操作",
        "OA 移动端审批操作指引：\n"
        "1. 在企业微信「工作台」打开 OA 应用，进入「待办审批」；\n"
        "2. 查看审批详情：申请人信息、申请内容、附件、历史审批意见；\n"
        "3. 审批操作：同意 / 退回 / 转交。同意可填写意见，退回需说明原因，转交需选择转交人；\n"
        "4. 批量审批：长按多条待办可批量同意（仅限同类型）；\n"
        "5. 审批记录在「我已审批」中查询，可随时查看进度；\n"
        "6. 离线时审批请求会缓存，联网后自动提交。\n"
        "建议审批人每日处理待办，超过 24 小时未处理系统将催办并抄送上级。",
        "internal",
        SCENARIO_OA,
    ),
    # === 产品业务类（8 篇：public 3 / internal 4 / confidential 1）===
    (
        "企业知识库产品使用手册(检索篇)",
        "企业知识库提供智能检索能力，使用方法如下：\n"
        "1. 在首页搜索框输入关键词或自然语言问题（如「年假有几天」），支持中英文混合；\n"
        "2. 系统基于语义检索 + 关键词召回返回最相关的文档片段，按相关度排序；\n"
        "3. 结果卡片显示文档标题、片段摘要、密级标识与来源，点击可跳转原文；\n"
        "4. 支持过滤：按知识库、文档分类、密级、时间范围筛选；\n"
        "5. 高级语法：双引号精确匹配（\"VPN 配置\"）、减号排除（报销 -差旅）、site: 限定知识库；\n"
        "6. 检索历史保存在「我的搜索」，可收藏常用查询。\n"
        "若检索不到结果，可切换为「智能问答」模式由 AI 综合作答并标注引用来源。",
        "public",
        SCENARIO_PRODUCT,
    ),
    (
        "知识库文档上传与编辑指南",
        "文档上传与编辑流程：\n"
        "1. 进入目标知识库，点击「新建文档」可在线编辑（支持 Markdown / 富文本），或「上传文件」导入 Word/PDF/Markdown；\n"
        "2. 上传后系统自动解析、抽取正文、分块向量化并建立索引，约 1-2 分钟可检索；\n"
        "3. 在线编辑支持协同：多人同时编辑同一文档，光标与变更实时同步（基于 Yjs）；\n"
        "4. 文档属性：设置标题、分类、密级、标签、生效时间窗口；\n"
        "5. 版本管理：每次保存自动生成版本快照，可对比差异或回滚至任意历史版本；\n"
        "6. 评论与 @ 提及：可在文档内选中文字评论，@ 同事会收到通知。\n"
        "草稿状态文档仅作者可见，发布后按知识库可见性规则开放。",
        "internal",
        SCENARIO_PRODUCT,
    ),
    (
        "智能问答功能使用说明",
        "智能问答基于检索增强生成（RAG），为用户提供准确可溯源的答案：\n"
        "1. 在对话框输入问题，系统先检索相关文档片段，再由大模型综合生成答案；\n"
        "2. 答案下方展示「引用来源」：点击可跳转原文片段，便于核对；\n"
        "3. 多轮对话：系统保留上下文，可追问（如「那病假呢」指代上文年假）；\n"
        "4. 反馈机制：答案下方可点赞 / 点踩，点踩需填写原因，用于优化模型；\n"
        "5. 安全边界：超出知识库范围或涉及密级超限的问题，系统将提示无法回答并建议联系相关部门；\n"
        "6. 历史会话保存在「我的对话」，可重命名、收藏或导出。\n"
        "问答结果仅基于授权可见的文档生成，确保密级隔离。",
        "public",
        SCENARIO_PRODUCT,
    ),
    (
        "知识库权限与分享设置",
        "知识库权限管理支持精细化控制：\n"
        "1. 可见性层级：公开（全员可见）/ 部门可见（指定部门）/ 私有（仅成员）；\n"
        "2. 成员角色：所有者（全部权限）/ 管理员（管理文档与成员）/ 编辑者（增改文档）/ 查看者（只读）；\n"
        "3. 文档级密级：public / internal / confidential / secret，检索时按用户密级权限过滤；\n"
        "4. 分享：生成分享链接可设置有效期与访问密码，外部用户需登录后访问；\n"
        "5. 操作审计：所有文档的查看、编辑、删除均记录审计日志，管理员可在「审计中心」查询；\n"
        "6. 多租户隔离：SaaS 模式下各租户数据完全隔离，私有部署默认单租户。\n"
        "权限变更立即生效，已分享的链接如对方失去权限将无法访问。",
        "internal",
        SCENARIO_PRODUCT,
    ),
    (
        "常见问题 FAQ 汇总",
        "企业知识库常见问题解答：\n"
        "Q：忘记密码怎么办？\n"
        "A：访问统一身份认证平台点击「忘记密码」，通过手机验证码重置。\n"
        "Q：上传的文档为什么搜不到？\n"
        "A：解析索引需要 1-2 分钟；若仍搜不到请检查文档密级是否超出你的权限，或文档是否处于草稿状态。\n"
        "Q：智能问答答案不准确怎么办？\n"
        "A：点击「点踩」反馈，或补充更具体的问题描述；也可直接检索原文核对。\n"
        "Q：能否导出整个知识库？\n"
        "A：管理员可在「设置 - 数据导出」导出 Markdown 或 JSON 全量备份。\n"
        "Q：移动端能否使用？\n"
        "A：支持企业微信工作台与独立 App，功能与网页端一致。\n"
        "Q：如何申请知识库权限？\n"
        "A：在 OA「知识库权限申请」提交，由知识库所有者审批。",
        "internal",
        SCENARIO_PRODUCT,
    ),
    (
        "API 接口调用与鉴权说明",
        "知识库开放 API 供系统集成，调用规范如下：\n"
        "1. 鉴权：在「个人中心 - API 密钥」创建 API Key（格式 ek-xxx），请求头携带 Authorization: Bearer <key>；\n"
        "2. 速率限制：默认每分钟 60 次，企业版可提升至 600 次，超限返回 429；\n"
        "3. 主要接口：\n"
        "   - POST /api/v1/search 语义检索（参数 query, kb_id, top_k, classification）\n"
        "   - POST /api/v1/chat 智能问答（流式 SSE 返回）\n"
        "   - GET /api/v1/documents/{id} 获取文档详情\n"
        "   - POST /api/v1/documents 上传文档\n"
        "4. 返回格式：统一 JSON（code/data/message），错误码见开发者文档；\n"
        "5. 回调：长任务（如批量索引）支持 webhook 回调通知。\n"
        "API Key 切勿泄露，如泄露请立即吊销重建。仅企业版支持 API 调用。",
        "confidential",
        SCENARIO_PRODUCT,
    ),
    (
        "数据导出与备份操作",
        "数据导出与备份操作说明：\n"
        "1. 单文档导出：在文档页右上角选择导出格式（Markdown / PDF / Word），立即下载；\n"
        "2. 知识库全量导出：管理员在「设置 - 数据导出」选择知识库与格式（Markdown 压缩包 / JSON），后台打包后邮件通知下载链接，链接 24 小时有效；\n"
        "3. 导出范围：可按分类、密级、时间范围筛选；密级受调用者权限限制，超权文档自动跳过；\n"
        "4. 定时备份：管理员可配置每日自动备份至对象存储（OSS/S3），保留 30 天；\n"
        "5. 数据恢复：联系管理员从备份恢复指定时间点的知识库快照，覆盖当前数据。\n"
        "导出文件包含敏感信息，请妥善保管，禁止上传至外部公共存储。",
        "internal",
        SCENARIO_PRODUCT,
    ),
    (
        "移动端 App 功能与下载",
        "企业知识库移动端 App 提供随时随地访问能力：\n"
        "1. 下载：iOS 在 App Store 搜索「企业知识库」，Android 在应用商店或公司软件中心下载；\n"
        "2. 登录：使用域账号登录，首次登录需开启 MFA；\n"
        "3. 核心功能：检索、智能问答、文档浏览与编辑、收藏、消息通知、待办审批；\n"
        "4. 离线阅读：可将文档下载至本地离线查看，联网后同步更新与阅读进度；\n"
        "5. 扫码：扫描文档二维码快速定位，扫描文档图片可 OCR 提取文字并检索；\n"
        "6. 推送通知：文档评论、@ 提及、审批待办通过系统推送，可在设置中分类开关。\n"
        "移动端与网页端数据实时同步，权限规则一致。",
        "public",
        SCENARIO_PRODUCT,
    ),
    # === 边界 case（4 篇：public 1 / internal 1 / confidential 2）===
    (
        "知识库能力边界与不支持场景说明",
        "本企业知识库的能力边界说明，请在使用前了解：\n"
        "1. 知识库基于已录入的文档进行检索与问答，无法回答未录入领域的问题；\n"
        "2. 不支持的场景包括但不限于：实时财务报表数据查询、员工个人薪资明细查询、"
        "客户合同商业条款谈判建议、医疗健康诊断、法律法规专业意见、股价与市场预测；\n"
        "3. 对于超出能力边界的问题，系统将明确告知「无法回答」并建议联系对应部门：\n"
        "   - 财务数据 → 财务部\n   - 个人薪资 → HR 薪酬专员\n   - 法务问题 → 法务部\n   - 医疗 → 就医\n"
        "4. 知识库不存储也不会生成涉及商业机密、个人隐私的敏感推断；\n"
        "5. 答案仅供参考，重大决策请以正式文件或相关部门确认结论为准。\n"
        "如发现知识库内容错误或过期，请通过文档评论反馈或联系知识库管理员更新。",
        "public",
        SCENARIO_BOUNDARY,
    ),
    (
        "财务报表数据查询指引(转接财务部)",
        "本知识库不涵盖实时财务报表数据，相关查询请转接财务部：\n"
        "1. 本知识库仅收录政策类、流程类、操作类文档，不存储任何财务报表、账目明细、预算执行数据；\n"
        "2. 需查询财务数据的同事请联系财务部：\n"
        "   - 日常报销进度：财务共享中心（OA「财务咨询」工单）\n"
        "   - 预算执行与部门费用：各部门财务 BP\n   - 公司财报与审计数据：财务部报表组（需授权）\n"
        "3. 财务系统（ERP / 财务共享平台）需单独权限，申请路径见《ERP 权限申请流程》；\n"
        "4. 涉及税务、发票的问题可咨询税务专员，税务政策类文档可在「财务知识库」查阅（需单独授权）。\n"
        "请勿在通用知识库中检索或询问具体金额数据，系统无法提供。",
        "internal",
        SCENARIO_BOUNDARY,
    ),
    (
        "涉密信息处理红线与合规要求",
        "涉密信息处理红线，所有员工必须遵守：\n"
        "1. 涉密信息（secret 级）包括：核心源代码、未公开财务数据、客户敏感信息、战略规划、核心技术配方等，仅授权人员可接触；\n"
        "2. 知识库中 secret 级文档不进入通用检索，仅在专用涉密知识库中流转，且需物理隔离环境访问；\n"
        "3. 禁止将涉密信息上传至通用知识库、公共聊天群、个人邮箱、外部网盘或 AI 工具；\n"
        "4. 涉密文档打印需在专用打印机并登记，废页即时粉碎，不得带离涉密区域；\n"
        "5. 发现涉密信息泄露迹象应立即上报信息安全部（security@example.com / 内线 9000），并保留现场；\n"
        "6. 违反涉密红线将依据《信息安全管理办法》追责，情节严重者移送司法机关。\n"
        "本知识库问答不会输出任何 secret 级内容，相关问题将拒绝作答。",
        "confidential",
        SCENARIO_BOUNDARY,
    ),
    (
        "个人敏感信息查询限制说明",
        "关于个人敏感信息查询的限制说明：\n"
        "1. 本知识库不提供任何员工或客户的个人敏感信息查询能力，包括但不限于：身份证号、银行卡号、"
        "手机号、家庭住址、健康记录、薪资明细、绩效具体分数；\n"
        "2. 员工本人查询个人薪资、考勤、社保等信息，请登录「员工自助平台」（ESS），使用域账号验证身份后查看；\n"
        "3. HR / 管理者查询下属信息需通过 HR 系统（eHR）并受角色权限与最小必要原则约束，操作留审计日志；\n"
        "4. 客户个人信息查询仅限授权业务岗位在 CRM 系统中操作，禁止导出整库数据；\n"
        "5. 如有业务确需使用个人敏感信息，须经数据合规专员评估并签署数据处理协议（DPA）；\n"
        "6. 任何在知识库中检索、询问他人敏感信息的行为将被记录并触发安全告警。\n"
        "保护个人信息是法律义务，违规查询 / 泄露将依据《个人信息保护法》追责。",
        "confidential",
        SCENARIO_BOUNDARY,
    ),
]


def get_seed_documents() -> list[dict]:
    """返回内置样本文档列表（纯函数，无副作用，可单测）。

    每篇文档为 dict，字段：
        - title: 文档标题
        - content: 纯文本正文（200-500 字）
        - classification: 密级（public / internal / confidential）
        - category: 场景分类（IT运维 / HR人事 / OA审批 / 产品业务 / 边界说明）
        - source: 来源标记，固定为 "seed"

    Returns:
        样本文档 dict 列表，顺序与内置顺序一致。
    """
    docs: list[dict] = []
    for title, content, classification, scenario in _SEED_DOCS:
        docs.append(
            {
                "title": title,
                "content": content,
                "classification": classification,
                "category": scenario,
                "source": SEED_SOURCE,
            }
        )
    return docs


# ------------------------------------------------------------------
# 灌库实现（重依赖延迟导入）
# ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="往知识库 Document 表批量灌入内置样本文档（供微调数据合成）"
    )
    parser.add_argument(
        "--tenant_id",
        required=True,
        help="目标租户 ID（UUID 字符串）",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="灌入文档数量（默认全部，按内置顺序取前 N 篇）",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="灌库前清空该租户已有的 seed 文档（按 content_json.source=seed 精确清理）",
    )
    parser.add_argument(
        "--kb_id",
        default=None,
        help="所属知识库 ID（UUID）。不传则自动取该租户下首个知识库",
    )
    parser.add_argument(
        "--owner_id",
        default=None,
        help="文档所有者 ID（UUID）。不传则自动取该租户下首个用户",
    )
    return parser.parse_args(argv)


async def _resolve_kb_and_owner(
    session: Any,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """自动解析 kb_id / owner_id（仅在未显式传入时查询）。延迟导入 ORM 模型。"""
    from sqlalchemy import select

    from app.models.knowledge import KnowledgeBase
    from app.models.user import User

    if kb_id is None:
        kb = (
            await session.execute(
                select(KnowledgeBase.id)
                .where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if kb is None:
            raise ValueError(
                f"租户 {tenant_id} 下未找到可用知识库，请通过 --kb_id 显式指定"
            )
        kb_id = kb

    if owner_id is None:
        owner = (
            await session.execute(
                select(User.id).where(User.tenant_id == tenant_id).limit(1)
            )
        ).scalar_one_or_none()
        if owner is None:
            raise ValueError(
                f"租户 {tenant_id} 下未找到可用用户，请通过 --owner_id 显式指定"
            )
        owner_id = owner

    return kb_id, owner_id


async def run_seed(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    count: int | None = None,
    clear: bool = False,
    kb_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """执行灌库，返回统计 dict。

    重依赖（sqlalchemy / app.models）在此延迟导入，保证模块顶层仅标准库。

    Args:
        session: AsyncSession（或兼容 mock）。
        tenant_id: 目标租户 ID。
        count: 灌入数量上限，None 表示全部。
        clear: 是否先清理该租户的 seed 文档。
        kb_id: 所属知识库 ID，None 时自动解析。
        owner_id: 文档所有者 ID，None 时自动解析。

    Returns:
        统计 dict：inserted / skipped / cleared / scenario_dist / classification_dist。
    """
    from sqlalchemy import delete, select

    from app.models.knowledge import Document

    # 1. （可选）清理该租户已有的 seed 文档
    cleared = 0
    if clear:
        result = await session.execute(
            delete(Document).where(
                Document.tenant_id == tenant_id,
                Document.content_json["source"].as_string() == SEED_SOURCE,
            )
        )
        cleared = getattr(result, "rowcount", 0) or 0
        await session.commit()
        logger.info("已清理租户 %s 的 seed 文档 %d 篇", tenant_id, cleared)

    # 2. 解析 kb_id / owner_id（自动兜底）
    kb_id, owner_id = await _resolve_kb_and_owner(
        session, tenant_id, kb_id, owner_id
    )

    # 3. 查询已存在（未软删）的标题，用于幂等跳过
    existing_titles: set[str] = set(
        (
            await session.execute(
                select(Document.title).where(
                    Document.tenant_id == tenant_id,
                    Document.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )

    # 4. 选出待灌文档
    seed_docs = get_seed_documents()
    if count is not None:
        seed_docs = seed_docs[:count]

    # 5. 逐篇构建并跳过已存在
    inserted = 0
    skipped = 0
    scenario_dist: dict[str, int] = {}
    classification_dist: dict[str, int] = {}
    for doc in seed_docs:
        title = doc["title"]
        if title in existing_titles:
            skipped += 1
            logger.debug("跳过已存在文档：%s", title)
            continue
        content = doc["content"]
        scenario = doc["category"]
        classification = doc["classification"]
        document = Document(
            id=uuid.uuid4(),
            kb_id=kb_id,
            owner_id=owner_id,
            title=title,
            content_text=content,
            content_html=content,
            content_json={"source": SEED_SOURCE, "scenario": scenario},
            doc_type="md",
            status="published",
            classification=classification,
            category=scenario,
            char_count=len(content),
            page_count=1,
            tenant_id=tenant_id,
        )
        session.add(document)
        inserted += 1
        scenario_dist[scenario] = scenario_dist.get(scenario, 0) + 1
        classification_dist[classification] = (
            classification_dist.get(classification, 0) + 1
        )

    await session.commit()

    return {
        "inserted": inserted,
        "skipped": skipped,
        "cleared": cleared,
        "scenario_dist": scenario_dist,
        "classification_dist": classification_dist,
    }


def _print_report(result: dict[str, Any]) -> None:
    """打印灌库结果统计。"""
    logger.info("=" * 60)
    logger.info("灌库完成")
    logger.info("  新增文档: %d 篇", result["inserted"])
    logger.info("  跳过(已存在): %d 篇", result["skipped"])
    if result.get("cleared"):
        logger.info("  清理旧 seed 文档: %d 篇", result["cleared"])
    logger.info("  按场景分布:")
    for scenario in SCENARIOS:
        logger.info("    %-6s: %d 篇", scenario, result["scenario_dist"].get(scenario, 0))
    logger.info("  按密级分布:")
    for cls in VALID_CLASSIFICATIONS:
        logger.info("    %-14s: %d 篇", cls, result["classification_dist"].get(cls, 0))
    logger.info("=" * 60)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # 重依赖延迟导入
    import asyncio

    from app.database import task_db_session

    tenant_id = uuid.UUID(args.tenant_id)
    kb_id = uuid.UUID(args.kb_id) if args.kb_id else None
    owner_id = uuid.UUID(args.owner_id) if args.owner_id else None

    async def _run() -> dict[str, Any]:
        async with task_db_session() as session:
            return await run_seed(
                session,
                tenant_id=tenant_id,
                count=args.count,
                clear=args.clear,
                kb_id=kb_id,
                owner_id=owner_id,
            )

    result = asyncio.run(_run())
    _print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
