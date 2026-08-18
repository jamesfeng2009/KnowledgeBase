"""记忆遗忘机制消融评测 — 开关 A/B，产出可写入简历的量化数字（扩量版）。

样本规模：
  实验一  冲突场景 50/组（10 领域 × 5 变体），对照组 vs 实验组配对比较
  实验二  记忆库 80 条（老冷 40 / 老热 20 / 长期 20），查询 1:1 配对
  实验三  复活窗口边界用例
  实验四  互补（非冲突）记忆对 21 对，测误杀率

统计口径：
  比例指标附 Wilson 95% 置信区间；实验一加 McNemar 配对检验（同场景
  跨组对照，控制场景间方差）。裁判盲评（不知分组）。

运行：cd backend && python -m evals.forgetting_ablation
数据卫生：种子行 fact_key 前缀 eval:forgetting:，启动时仅清理该前缀。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
EVAL_KEY_PREFIX = "eval:forgetting:"

# ======================================================================
# 实验一数据：50 个冲突场景 = 10 领域 × 5 变体
# 约束：dep_kw 只出现在旧事实（新事实不得包含，避免误判采信）
# ======================================================================

# (名称, 旧事实, 旧类别, 新事实, 新类别, 问句, 标准答案, 作废关键词, 新事实关键词)
_CONFLICT_RAW = [
    # D1 号码类
    ("手机换号", "用户的手机号是13812345678", "fact", "用户已更换手机号，新号码是15987654321", "fact",
     "验证码应该发送到哪个手机号？", "15987654321。", "13812345678", "15987654321"),
    ("分机变更", "用户的办公分机号是8021", "fact", "用户调换了工位，新分机号是8066", "fact",
     "打用户座机分机拨多少？", "8066。", "8021", "8066"),
    ("传真迁移", "部门传真号是0571-88886666", "fact", "部门传真迁移后新号是0571-88887777", "fact",
     "给部门发传真用什么号码？", "0571-88887777。", "88886666", "88887777"),
    ("热线变更", "IT报修热线是8001", "fact", "IT报修热线已变更为8002", "fact",
     "电脑坏了打哪个热线？", "8002。", "8001", "8002"),
    ("客服更换", "供应商客服电话是400-123-4567", "fact", "供应商客服电话已更换为400-765-4321", "fact",
     "找供应商客服打什么电话？", "400-765-4321。", "400-123-4567", "400-765-4321"),
    # D2 地点类
    ("办公搬迁", "用户办公地点在浦东软件园3号楼5层", "fact", "用户办公室已搬迁至张江高科技园区B座12层", "fact",
     "现在去哪里找用户办公？", "张江高科技园区B座12层。", "浦东软件园", "张江"),
    ("工位调整", "用户工位在A区12号", "fact", "用户工位已调整到C区08号", "fact",
     "用户的工位在哪？", "C区08号。", "A区12号", "C区08号"),
    ("会议室变更", "部门周例会固定在301会议室", "fact", "周例会已改到502会议室", "fact",
     "周例会去哪个会议室？", "502会议室。", "301", "502"),
    ("档案室搬迁", "合同档案室在地下1层", "fact", "档案室已搬到地上3层东侧", "fact",
     "去哪层取合同档案？", "地上3层东侧。", "地下1层", "地上3层"),
    ("仓库迁移", "物料仓库在昆山园区", "fact", "物料仓库已迁至太仓园区", "fact",
     "领物料去哪个仓库？", "太仓园区。", "昆山", "太仓"),
    # D3 人员类
    ("上级变更", "用户的直属上级是张总", "fact", "组织架构调整后，用户的直属上级变为陈总", "fact",
     "用户现在向谁汇报？", "陈总。", "张总", "陈总"),
    ("审批人变更", "用户在财务部报销审批找王经理", "fact", "用户调岗后报销审批改为找李总监", "fact",
     "用户的报销单应该找谁审批？", "李总监。", "王经理", "李总监"),
    ("HR对接变更", "入职手续对接HR小周", "fact", "HR对接人已换为小吴", "fact",
     "入职材料找哪位HR？", "小吴。", "小周", "小吴"),
    ("IT对接变更", "电脑设备找IT的老赵", "fact", "IT设备对接人换成了小孙", "fact",
     "领电脑找谁？", "小孙。", "老赵", "小孙"),
    ("接口人变更", "财务接口人是老钱", "fact", "财务接口人已变更为小何", "fact",
     "报销问题找谁对接？", "小何。", "老钱", "小何"),
    # D4 平台/工具类
    ("代码仓库迁移", "项目代码仓库在SVN上，地址是svn://old-server/proj", "fact",
     "项目代码已迁移至GitLab，新地址是gitlab.internal/proj", "fact",
     "提交代码应该用哪个仓库？", "GitLab（gitlab.internal/proj）。", "SVN", "GitLab"),
    ("任务管理迁移", "项目任务管理用Jira", "fact", "团队已迁移到飞书项目管理", "fact",
     "建任务去哪个工具？", "飞书项目管理。", "Jira", "飞书"),
    ("文档平台迁移", "技术文档存在Confluence", "fact", "文档已统一迁移到语雀", "fact",
     "查技术文档去哪里？", "语雀。", "Confluence", "语雀"),
    ("监控平台切换", "服务监控用Zabbix", "fact", "监控平台已切换为Prometheus", "fact",
     "看服务告警上什么平台？", "Prometheus。", "Zabbix", "Prometheus"),
    ("CI系统迁移", "构建发布用Jenkins", "fact", "CI已迁移到GitLab Runner", "fact",
     "发版用哪个构建系统？", "GitLab Runner。", "Jenkins", "GitLab Runner"),
    # D5 供应商类
    ("云供应商更换", "公司云服务供应商是AWS", "fact", "公司已将云服务整体迁移到阿里云", "fact",
     "公司现在的云服务供应商是谁？", "阿里云。", "AWS", "阿里云"),
    ("短信通道切换", "短信服务供应商是云片", "fact", "短信通道已切换到阿里云短信", "fact",
     "发验证码短信走哪家？", "阿里云短信。", "云片", "阿里云短信"),
    ("快递合作更换", "公司合作快递是顺丰月结", "fact", "快递合作方已换为京东物流", "fact",
     "寄合同用哪家快递？", "京东物流。", "顺丰", "京东物流"),
    ("耗材供应商更换", "办公用品采购走得力", "fact", "办公耗材供应商已换为晨光", "fact",
     "领办公耗材找哪家供应商？", "晨光。", "得力", "晨光"),
    ("差旅平台更换", "差旅订票通过携程商旅", "fact", "差旅预订已统一到同程商旅", "fact",
     "出差订机票用哪个平台？", "同程商旅。", "携程", "同程"),
    # D6 时间/制度类
    ("周会改期", "部门周会定在每周一上午10点", "fact", "部门周会已调整为每周三下午2点", "fact",
     "这周的部门周会是什么时候？", "每周三下午2点。", "周一", "周三"),
    ("上班时间调整", "用户弹性上班时间9:00", "fact", "用户上班时间已调整为9:30", "fact",
     "用户几点上班？", "9:30。", "9:00", "9:30"),
    ("报销截止变更", "报销单每月25日前提交", "fact", "报销截止日已改为每月20日", "fact",
     "这个月报销最晚哪天交？", "每月20日。", "25日", "20日"),
    ("值班调整", "用户周四值班", "fact", "值班排班调整后用户改周五值班", "fact",
     "用户这周哪天值班？", "周五。", "周四", "周五"),
    ("团建改期", "团建活动定在10月15日", "fact", "团建日期已改到10月22日", "fact",
     "团建是哪天？", "10月22日。", "10月15日", "10月22日"),
    # D7 偏好反转类
    ("偏好详细到简洁", "用户偏好详细的回答，包含完整背景", "preference",
     "用户近期偏好简洁直接的回答", "preference",
     "回答问题应该详细还是简洁？", "简洁直接。", "详细", "简洁"),
    ("语言切换", "用户习惯使用英语交流", "preference", "用户当前的工作语言已切换为中文", "preference",
     "应该用什么语言回复用户？", "中文。", "英语", "中文"),
    ("沟通方式变更", "用户习惯邮件沟通", "preference", "用户现在希望用即时消息沟通", "preference",
     "找用户沟通用什么方式？", "即时消息。", "邮件", "即时消息"),
    ("语气偏好变更", "用户喜欢正式书面语", "preference", "用户现在偏好轻松口语化的交流", "preference",
     "回复的语气风格应该怎样？", "轻松口语化。", "正式", "口语化"),
    ("通知方式变更", "用户希望重要事项打电话通知", "preference", "用户现在要求重要事项发文字消息", "preference",
     "有重要事项怎么通知用户？", "发文字消息。", "打电话", "文字消息"),
    # D8 套餐/权益类
    ("套餐降级", "用户是VIP会员，享受机场贵宾厅和免费代驾权益", "preference",
     "用户已将会员套餐降级为基础版", "fact",
     "用户还能使用机场贵宾厅吗？", "不能。", "机场贵宾厅", "基础版"),
    ("车位制度变更", "用户有B1层的专属车位", "fact", "车位管理已改为每月抽签分配", "fact",
     "用户现在有专属车位吗？", "没有，改为抽签。", "专属车位", "抽签"),
    ("私教福利取消", "用户享有公司健身房免费私教课", "preference",
     "健身房福利已缩减为仅场地使用", "fact",
     "还能约私教课吗？", "不能。", "私教课", "缩减"),
    ("餐补下调", "用户每月有300元餐补", "fact", "餐补标准已下调为200元", "fact",
     "用户每月餐补多少？", "200元。", "300", "200"),
    ("授权降级", "用户订阅的是专业版IDE授权", "fact", "用户IDE授权已降为社区版", "fact",
     "用户现在用什么版本的IDE？", "社区版。", "专业版", "社区版"),
    # D9 地址/入口类
    ("wiki迁移", "内部wiki地址是wiki.oldcorp.com", "fact", "wiki已迁移到kb.newcorp.com", "fact",
     "查制度文档访问哪个网址？", "kb.newcorp.com。", "oldcorp", "newcorp"),
    ("OA升级", "OA系统入口是oa.internal:8080", "fact", "OA已升级，新入口是oa2.internal", "fact",
     "提交审批去哪个入口？", "oa2.internal。", "8080", "oa2"),
    ("VPN迁移", "VPN服务器是vpn.oldcorp.com", "fact", "VPN已迁移至ssl.newcorp.com", "fact",
     "远程办公连哪个VPN地址？", "ssl.newcorp.com。", "oldcorp", "newcorp"),
    ("测试环境合并", "测试环境地址是test1.internal", "fact", "测试环境已合并到test2.internal", "fact",
     "联调部署到哪个环境？", "test2.internal。", "test1", "test2"),
    ("报表迁移", "经营报表在共享盘reports目录", "fact", "报表已迁移到云盘drive.newcorp.com", "fact",
     "下载月度经营报表去哪？", "云盘drive.newcorp.com。", "共享盘", "云盘"),
    # D10 职责范围类
    ("区域调整", "用户只负责华东区", "fact", "用户的工作区域已调整为华中区", "fact",
     "用户现在负责哪个区域？", "华中区。", "华东", "华中"),
    ("产品线转岗", "用户负责支付产品线", "fact", "用户已转岗负责风控产品线", "fact",
     "用户现在负责什么产品？", "风控产品线。", "支付", "风控"),
    ("客户群调整", "用户对接大客户", "fact", "用户客户群已调整为中小客户", "fact",
     "用户对接哪类客户？", "中小客户。", "大客户", "中小客户"),
    ("项目组调动", "用户在猎户座项目组", "fact", "用户已调入天权项目组", "fact",
     "用户现在在哪个项目组？", "天权项目组。", "猎户座", "天权"),
    ("团队重组", "用户属于平台一组", "fact", "组织调整后用户划入平台三组", "fact",
     "用户现在在哪个组？", "平台三组。", "一组", "三组"),
]

CONFLICT_SCENARIOS = [
    {"name": n, "old": o, "old_cat": oc, "new": nw, "new_cat": nc,
     "q": q, "gold": g, "dep_kw": d, "new_kw": k}
    for n, o, oc, nw, nc, q, g, d, k in _CONFLICT_RAW
]


def _validate_scenarios() -> None:
    """种子自检：作废词必须在旧事实中且不得出现在新事实里（防误判采信）。"""
    for sc in CONFLICT_SCENARIOS:
        assert sc["dep_kw"] in sc["old"], f"dep_kw 不在旧事实: {sc['name']}"
        assert sc["dep_kw"] not in sc["new"], f"dep_kw 泄漏到新事实: {sc['name']}"
        assert sc["new_kw"] in sc["new"], f"new_kw 不在新事实: {sc['name']}"


# ======================================================================
# 实验二数据：记忆库 80 条（老冷 40 / 老热 20 / 长期 20），(事实, 改述查询)
# ======================================================================

COLD_PAIRS = [
    # IT（10）
    ("打印服务器地址是print.internal", "打印机连不上先看哪台服务器"),
    ("内网DNS是10.0.0.2", "域名解析用哪个DNS"),
    ("公司邮箱后缀是oldcorp.com", "员工邮箱是什么后缀"),
    ("远程接入用OpenVPN客户端", "远程办公装什么客户端"),
    ("代码评审在GitLab的MR里做", "提交代码后在哪里评审"),
    ("会议室大屏投屏用HDMI线", "大屏怎么投屏"),
    ("网络报修平台是itsm.internal", "断网了去哪报修"),
    ("正版软件申请走IT工单", "装正版软件怎么申请"),
    ("访客WiFi密码是guest2024", "访客连WiFi用什么密码"),
    ("数据库备份保留30天", "误删数据最多能恢复几天"),
    # 行政（10）
    ("公司前台电话分机是8000", "找前台拨哪个分机"),
    ("快递收发室在1楼东侧", "取包裹去几楼"),
    ("办公用品每月5号集中发放", "什么时候领办公用品"),
    ("名片印刷找行政部小林", "印名片找谁"),
    ("公司班车早班7:30发车", "最早一班班车几点"),
    ("节假日值班表贴在公告栏", "节日值班安排在哪里看"),
    ("会议室预订最长4小时", "会议室最多能订多久"),
    ("公司食堂午饭供应到13:30", "午饭最晚几点去食堂"),
    ("办公室空调报修找物业前台", "空调坏了找谁"),
    ("访客登记用前台平板", "来访客人怎么登记"),
    # HR（10）
    ("年假入职满1年有5天", "新员工第一年有几天年假"),
    ("试用期统一6个月", "试用期多长"),
    ("入职体检指定三甲医院", "体检去什么级别的医院"),
    ("离职交接清单在HR共享盘", "离职流程清单去哪下载"),
    ("调薪评审每年4月一次", "一年几次调薪窗口"),
    ("员工活动经费每人每季100元", "部门团建费用标准是多少"),
    ("加班调休有效期6个月", "加班调休多久内要用掉"),
    ("婚假按国家规定3天", "请婚假有几天"),
    ("病假需二级以上医院证明", "请病假需要什么证明"),
    ("员工卡补办收工本费20元", "员工卡丢了补办多少钱"),
    # 财务（10）
    ("差旅住宿标准一线城市500元", "去北京出差住宿报多少"),
    ("经济舱需提前3天预订", "订机票要提前几天"),
    ("出差餐补每天100元", "出差每天吃饭补贴多少"),
    ("发票抬头是上海云启科技有限公司", "报销发票抬头写什么"),
    ("单笔报销超过5000元需总监审批", "多大金额要总监签字"),
    ("报销周期每月一次", "报销多久打款一次"),
    ("打车报销限22点后", "晚上加班打车能报吗"),
    ("培训费用需提前预算申请", "报课程费用怎么走流程"),
    ("供应商付款账期60天", "给供应商多久付款"),
    ("备用金上限2000元", "能借多少备用金"),
]

HOT_PAIRS = [
    ("用户的门禁权限覆盖B座全部楼层", "门禁卡能进哪些楼层"),
    ("用户负责的客户是华东三家连锁", "用户管哪些客户"),
    ("用户的项目代号是猎户座", "用户的项目叫什么"),
    ("用户常用测试账号是qa_user_02", "测试用什么账号"),
    ("用户工位在12层东侧靠窗", "用户的工位在哪"),
    ("用户每天9:30到公司", "用户几点到公司"),
    ("用户邮箱前缀是feng.yu", "用户邮箱是什么"),
    ("用户在写Q3技术方案", "用户最近在写什么"),
    ("用户的服务器跳板机是bastion-2", "登录服务器用哪台跳板"),
    ("用户的报销卡尾号是6688", "报销打款到哪张卡"),
    ("用户对接的运维是老郑", "服务器故障找谁"),
    ("用户的产品内部代号叫青鸟", "用户产品内部代号是什么"),
    ("用户周报要抄送部门总监", "周报抄送谁"),
    ("用户代码分支用feature前缀命名", "用户建分支怎么命名"),
    ("用户的会议默认定在401", "用户开会常用哪间会议室"),
    ("用户的紧急联系人是配偶", "用户紧急联系人是谁"),
    ("用户的工位椅子加了腰靠", "用户工位椅子什么配置"),
    ("用户用双27寸显示器", "用户用几个显示器"),
    ("用户午餐常去园区负一楼", "用户午饭常去哪"),
    ("用户通勤坐2号线", "用户坐几号线上班"),
]

EVERGREEN_PAIRS = [
    # preference（10）
    ("用户偏好中文回复", "preference", "回复语言有什么要求"),
    ("用户偏好简洁直接的回答", "preference", "回答风格是什么"),
    ("用户不喜欢被称呼全名", "preference", "怎么称呼用户"),
    ("用户午休时间不接会议", "preference", "什么时候别安排会议"),
    ("用户希望周一早上不被打扰", "preference", "周一早上能开会吗"),
    ("用户喜欢用表格总结信息", "preference", "信息汇总用什么形式"),
    ("用户反对工作群发通知", "preference", "重要通知怎么发给用户"),
    ("用户习惯下午2点后做深度工作", "preference", "什么时候安排复杂任务"),
    ("用户偏好回答中附参考链接", "preference", "回答里要带什么"),
    ("用户反感表情包", "preference", "能用表情包吗"),
    # fact（10）
    ("用户在知识库平台组", "fact", "用户在哪个组"),
    ("用户的技术栈以Python为主", "fact", "用户主要用什么语言"),
    ("用户负责RAG引擎模块", "fact", "用户负责什么模块"),
    ("用户的办公电脑是MacBook", "fact", "用户用什么电脑"),
    ("用户的英文名是Fisher", "fact", "用户英文名是什么"),
    ("用户的职级是P7", "fact", "用户什么职级"),
    ("用户入职于2023年6月", "fact", "用户什么时候入职"),
    ("用户的母校是同济大学", "fact", "用户哪里毕业"),
    ("用户的家乡是杭州", "fact", "用户老家在哪"),
    ("用户养了一只猫", "fact", "用户养什么宠物"),
]

# ======================================================================
# 实验四数据：21 对互补（相关但非冲突）记忆 — 理想行为是两条共存
# ======================================================================

COMPLEMENTARY_PAIRS = [
    # 加型（"也/还/同时"）
    ("双区域职责", "用户负责华东区的客户", "用户同时也在支持华南区的新客户"),
    ("偏好双维度", "用户偏好中文回复", "用户希望回答中附带代码示例"),
    ("混合办公", "用户每天9:30到公司", "用户周三和周五远程办公"),
    ("并行项目", "用户在写Q3技术方案", "用户还在负责Q2项目的收尾验收"),
    ("报销双材料", "报销必须提供电子发票", "差旅报销还需要附行程单"),
    ("技能拓展", "用户常用Python开发", "用户近期在学Rust"),
    ("门禁扩展", "用户的门禁权限覆盖B座", "用户还申请了A座的访客权限"),
    # 并列型（两个不同对象，互不否定）
    ("双代号项目", "用户的项目代号是猎户座", "用户参与的另一个项目代号是天枢"),
    ("双设备", "用户的办公电脑是MacBook", "用户家里有一台Windows台式机"),
    ("双客户群", "用户对接零售连锁客户", "用户也服务两家制造企业"),
    ("双汇报线", "用户的直属上级是陈总", "用户虚线汇报给技术委员会"),
    ("双会议场地", "用户的会议默认定在401", "客户来访改用一楼展厅"),
    ("双通勤方式", "用户通勤坐2号线", "用户出差常乘高铁"),
    ("双城市", "用户的团队在杭州", "用户的家在苏州"),
    # 细节补充型（同一对象的不同侧面）
    ("发票细节", "报销必须提供电子发票", "发票抬头需要写公司全称"),
    ("周会细节", "部门周会在每周三下午2点", "周会时长控制在45分钟内"),
    ("提交规范", "团队代码放在GitLab", "提交信息遵循约定式提交规范"),
    ("到岗习惯", "用户每天9:30到公司", "用户到岗后先查看告警面板"),
    ("偏好例外", "用户偏好简洁的回答", "技术方案类问题可以展开细节"),
    ("账号细节", "用户常用测试账号qa_user_02", "该测试账号密码每月轮换"),
    ("模块细节", "用户负责RAG引擎模块", "RAG引擎包含四路混合召回"),
]


# ======================================================================
# 统计工具
# ======================================================================

def wilson_ci(p_hat: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间 — 小样本比例比正态近似更稳。"""
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    margin = z * ((p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def mcnemar(b: int, c: int) -> float | None:
    """McNemar 配对卡方（带连续性校正）。b/c 为不一致配对数。"""
    if b + c == 0:
        return None
    return (abs(b - c) - 1) ** 2 / (b + c)


async def chat_once(provider, messages: list[dict], max_tokens: int = 400) -> str:
    chunks: list[str] = []
    async for chunk in provider.chat(messages, stream=False, max_tokens=max_tokens):
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "".join(chunks)


def parse_judge_json(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def set_switches(settings, *, activation: bool, consolidation: bool) -> None:
    settings.MEMORY_ACTIVATION_ENABLED = activation
    settings.MEMORY_CONSOLIDATION_ENABLED = consolidation
    settings.MEMORY_CONSOLIDATION_LLM_ENABLED = consolidation


async def cleanup_eval_rows(session) -> int:
    from sqlalchemy import text

    result = await session.execute(
        text("DELETE FROM memory_facts WHERE fact_key LIKE :p").bindparams(
            p=EVAL_KEY_PREFIX + "%"
        )
    )
    await session.commit()
    return result.rowcount or 0


# ======================================================================
# 实验一：冲突干扰率（N=50/组，两阶段：DB 召回顺序执行 + LLM 并发盲评）
# ======================================================================

async def run_experiment1(session, settings, provider) -> dict:
    from app.memory.memory_manager import MemoryManager

    _validate_scenarios()
    n = len(CONFLICT_SCENARIOS)
    contexts = []  # (group, idx, scenario, ctx_texts, dep_in_ctx, new_in_ctx)

    # --- 阶段 A：种子 + 召回（DB 顺序执行，session 不支持并发） ---
    for group, activation, consolidation in (
        ("control", False, False),
        ("treatment", True, True),
    ):
        set_switches(settings, activation=activation, consolidation=consolidation)
        manager = MemoryManager(session)
        for i, sc in enumerate(CONFLICT_SCENARIOS):
            user_id = uuid.uuid4()
            await manager.mem0.add_fact(
                user_id=user_id, fact_text=sc["old"], category=sc["old_cat"],
                fact_key=f"{EVAL_KEY_PREFIX}e1-{group}-{i}-old",
            )
            if group == "control":
                await manager.mem0.add_fact(
                    user_id=user_id, fact_text=sc["new"], category=sc["new_cat"],
                    fact_key=f"{EVAL_KEY_PREFIX}e1-{group}-{i}-new",
                )
            else:
                await manager._consolidated_add(user_id, sc["new"], sc["new_cat"])
            await session.commit()

            recalled = await manager.mem0.search_facts(
                user_id=user_id, query=sc["q"], limit=5, update_access_stats=False
            )
            ctx_texts = [f.fact_text for f in recalled]
            contexts.append((
                group, i, sc, ctx_texts,
                any(sc["dep_kw"] in t for t in ctx_texts),
                any(sc["new_kw"] in t for t in ctx_texts),
            ))
        print(f"  [{group}] 种子+召回完成 {n} 场景")

    # --- 阶段 B：答案生成 + 盲评（LLM 并发，信号量限 4） ---
    sem = asyncio.Semaphore(4)

    async def qa(group, sc, ctx_texts):
        memory_block = "\n".join(f"- {t}" for t in ctx_texts) or "（无记忆）"
        async with sem:
            answer = await chat_once(
                provider,
                [
                    {"role": "system", "content": (
                        "你是企业智能助手。仅依据下方<记忆>中的信息回答用户问题，"
                        "不要使用记忆之外的知识。记忆不足时直说不知道。\n"
                        f"<记忆>\n{memory_block}\n</记忆>")},
                    {"role": "user", "content": sc["q"]},
                ],
            )
            verdict_raw = await chat_once(
                provider,
                [
                    {"role": "system", "content": (
                        "你是评测裁判。依据<标准答案>判断<助手回答>是否正确，"
                        "并判断回答是否采信了<已作废信息>。"
                        '只输出一行JSON：{"correct": true/false, '
                        '"cites_deprecated": true/false}')},
                    {"role": "user", "content": (
                        f"问题：{sc['q']}\n标准答案：{sc['gold']}\n"
                        f"已作废信息（采信此信息即算 true）：{sc['dep_kw']}\n"
                        f"助手回答：{answer}")},
                ],
                max_tokens=120,
            )
        verdict = parse_judge_json(verdict_raw) or {}
        return {
            "answer": answer,
            "answer_correct": bool(verdict.get("correct", False)),
            "cites_deprecated": bool(verdict.get("cites_deprecated", False)),
        }

    tasks = [qa(group, sc, ctx) for group, _, sc, ctx, _, _ in contexts]
    qa_results = await asyncio.gather(*tasks)

    rows = {"control": [None] * n, "treatment": [None] * n}
    for (group, i, sc, ctx, dep_in_ctx, new_in_ctx), r in zip(contexts, qa_results):
        bad = (not r["answer_correct"]) or r["cites_deprecated"]
        rows[group][i] = {
            "scenario": sc["name"],
            "dep_in_context": dep_in_ctx,
            "new_in_context": new_in_ctx,
            "answer_correct": r["answer_correct"],
            "cites_deprecated": r["cites_deprecated"],
            "bad": bad,
        }
        mark = "OK " if not bad else "BAD"
        print(f"  [{group:9s}] {mark} {sc['name']:8s} 作废注入={dep_in_ctx} "
              f"答案对={r['answer_correct']} 引用作废={r['cites_deprecated']}")

    def summarize(group_rows):
        m = len(group_rows)
        dep_rate = sum(r["dep_in_context"] for r in group_rows) / m
        bad_rate = sum(r["bad"] for r in group_rows) / m
        ok_rate = sum(r["answer_correct"] for r in group_rows) / m
        return {
            "n": m,
            "dep_inject_rate": dep_rate, "dep_inject_ci": wilson_ci(dep_rate, m),
            "bad_rate": bad_rate, "bad_rate_ci": wilson_ci(bad_rate, m),
            "answer_correct_rate": ok_rate,
        }

    # McNemar：b = 对照坏→实验好，c = 对照好→实验坏
    b = sum(1 for ct, tt in zip(rows["control"], rows["treatment"])
            if ct["bad"] and not tt["bad"])
    c = sum(1 for ct, tt in zip(rows["control"], rows["treatment"])
            if not ct["bad"] and tt["bad"])
    chi2 = mcnemar(b, c)

    return {
        "control": summarize(rows["control"]),
        "treatment": summarize(rows["treatment"]),
        "mcnemar": {"b_control_bad_treatment_good": b,
                    "c_control_good_treatment_bad": c,
                    "chi2": chi2, "significant_at_0.05": chi2 is not None and chi2 > 3.84},
        "rows": rows,
    }


# ======================================================================
# 实验二：上下文压缩 + 保持率（记忆库 80 条：冷 40 / 热 20 / 长期 20）
# ======================================================================

async def run_experiment2(session, settings) -> dict:
    from app.memory.mem0_manager import Mem0Manager
    from app.models.memory import MemoryFact
    from sqlalchemy import select

    user_id = uuid.uuid4()
    now = datetime.utcnow()
    manager = Mem0Manager(session)

    async def seed(text, category, created_at, access_count=0, last_accessed=None):
        await manager.add_fact(
            user_id=user_id, fact_text=text, category=category,
            fact_key=f"{EVAL_KEY_PREFIX}e2-{uuid.uuid4().hex[:8]}",
        )
        stmt = select(MemoryFact).where(
            MemoryFact.user_id == user_id, MemoryFact.fact_text == text
        )
        fact = (await session.execute(stmt)).scalars().first()
        fact.created_at = created_at
        fact.access_count = access_count
        fact.last_accessed_at = last_accessed
        await session.flush()

    for text, _ in COLD_PAIRS:
        await seed(text, "working", now - timedelta(days=90))
    for text, _ in HOT_PAIRS:
        await seed(text, "working", now - timedelta(days=90),
                   access_count=12, last_accessed=now - timedelta(days=2))
    for text, cat, _ in EVERGREEN_PAIRS:
        await seed(text, cat, now - timedelta(days=90))
    await session.commit()
    print(f"  记忆库就绪：冷{len(COLD_PAIRS)} 热{len(HOT_PAIRS)} 长期{len(EVERGREEN_PAIRS)}")

    result = {}
    for group, activation in (("control", False), ("treatment", True)):
        settings.MEMORY_ACTIVATION_ENABLED = activation
        m = Mem0Manager(session)

        async def measure(pairs):
            injected_chars, hits = [], 0
            for q, target in pairs:
                recalled = await m.search_facts(
                    user_id=user_id, query=q, limit=10, update_access_stats=False
                )
                texts = [f.fact_text for f in recalled]
                injected_chars.append(sum(len(t) for t in texts))
                hits += 1 if target in texts else 0
            return {"avg_injected_chars": sum(injected_chars) / len(pairs),
                    "hit_rate": hits / len(pairs)}

        cold = await measure([(q, t) for t, q in COLD_PAIRS])
        hot = await measure([(q, t) for t, q in HOT_PAIRS])
        ever = await measure([(q, t) for t, _, q in EVERGREEN_PAIRS])
        result[group] = {"cold": cold, "hot": hot, "evergreen": ever}
        print(f"  [{group:9s}] 老冷注入={cold['avg_injected_chars']:.0f}字符/查询 "
              f"老冷命中={cold['hit_rate']:.0%} 老热保持={hot['hit_rate']:.0%} "
              f"长期保持={ever['hit_rate']:.0%}")

    ctrl, treat = result["control"], result["treatment"]
    compression = 1 - treat["cold"]["avg_injected_chars"] / ctrl["cold"]["avg_injected_chars"]
    return {
        "cold_avg_chars": {"control": ctrl["cold"]["avg_injected_chars"],
                           "treatment": treat["cold"]["avg_injected_chars"]},
        "cold_compression": compression,
        "cold_hit_rate": {"control": ctrl["cold"]["hit_rate"],
                          "treatment": treat["cold"]["hit_rate"]},
        "hot_retention": {"control": ctrl["hot"]["hit_rate"],
                          "treatment": treat["hot"]["hit_rate"]},
        "evergreen_retention": {"control": ctrl["evergreen"]["hit_rate"],
                                "treatment": treat["evergreen"]["hit_rate"]},
        "sample_sizes": {"cold": len(COLD_PAIRS), "hot": len(HOT_PAIRS),
                         "evergreen": len(EVERGREEN_PAIRS)},
    }


# ======================================================================
# 实验三：复活窗口（边界用例）
# ======================================================================

async def run_experiment3(session, settings) -> dict:
    from app.memory.mem0_manager import Mem0Manager
    from app.models.memory import MemoryFact as MF
    from sqlalchemy import select

    user_id = uuid.uuid4()
    manager = Mem0Manager(session)
    fact_text = "报销必须提供电子发票，发票抬头需为公司全称"
    fact = await manager.add_fact(
        user_id=user_id, fact_text=fact_text, category="fact",
        fact_key=f"{EVAL_KEY_PREFIX}e3-revival",
    )
    await manager.mark_superseded([fact.id], uuid.uuid4())
    await session.commit()

    settings.MEMORY_ACTIVATION_ENABLED = True
    m = Mem0Manager(session)

    strong_recalled = await m.search_facts(
        user_id=user_id, query=fact_text, limit=3, update_access_stats=False
    )
    strong_hit = any(f.fact_text == fact_text for f in strong_recalled)
    print(f"  强命中（原文查询）：复活={'是' if strong_hit else '否'}")

    row = (await session.execute(
        select(MF).where(MF.user_id == user_id))).scalars().first()
    row.is_active = False
    await session.flush()

    weak_recalled = await m.search_facts(
        user_id=user_id, query="财务报销有什么要求", limit=3, update_access_stats=False
    )
    weak_hit = any(f.fact_text == fact_text for f in weak_recalled)
    print(f"  弱命中（改述查询）：保持退场={'是' if not weak_hit else '否'}")

    return {"strong_hit_revived": strong_hit, "weak_hit_stays_out": not weak_hit}


# ======================================================================
# 实验四：互补记忆误杀率（N=21，理想 0）
# ======================================================================

async def run_experiment4(session, settings) -> dict:
    from app.memory.conflict_arbiter import ACTION_DISCARD
    from app.memory.memory_manager import MemoryManager
    from app.models.memory import MemoryFact

    settings.MEMORY_CONSOLIDATION_ENABLED = True
    settings.MEMORY_CONSOLIDATION_LLM_ENABLED = True
    settings.MEMORY_ACTIVATION_ENABLED = True

    rows = []
    for i, (name, old_text, new_text) in enumerate(COMPLEMENTARY_PAIRS):
        user_id = uuid.uuid4()
        manager = MemoryManager(session)
        old_fact = await manager.mem0.add_fact(
            user_id=user_id, fact_text=old_text, category="fact",
            fact_key=f"{EVAL_KEY_PREFIX}e4-{i}-old",
        )
        await session.commit()

        verdict = await manager.arbiter.consolidate(user_id, new_text, "fact")
        if verdict.action != ACTION_DISCARD:
            new_fact = await manager.mem0.add_fact(
                user_id=user_id, fact_text=new_text, category="fact",
                fact_key=f"{EVAL_KEY_PREFIX}e4-{i}-new",
            )
            if verdict.superseded_ids:
                await manager.mem0.mark_superseded(verdict.superseded_ids, new_fact.id)
        await session.commit()

        db_old = await session.get(MemoryFact, old_fact.id)
        killed = db_old.is_active is False
        rows.append({"name": name, "reason": verdict.reason, "killed": killed})
        print(f"  [互补对 {i + 1}/{len(COMPLEMENTARY_PAIRS)}] "
              f"{'KILLED' if killed else 'kept  '} {name:6s} 裁决路径={verdict.reason}")

    killed_count = sum(r["killed"] for r in rows)
    rate = killed_count / len(rows)
    return {"pairs": rows, "wrongful_supersede_rate": rate,
            "rate_ci": wilson_ci(rate, len(rows))}


# ======================================================================
# 维度适配：ORM Vector(1536) vs text-embedding-v3（最高 1024）
# ======================================================================

class PaddedEmbedder:
    """零填充 Embedder 包装 — 对事实与查询向量统一零填充到 1536 维。

    点积与范数均不变，余弦相似度严格相等 — 语义排序/闸门行为与原生一致。
    """

    def __init__(self, inner, target_dim: int = 1536):
        self.inner = inner
        self.target_dim = target_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = await self.inner.embed(texts)
        return [v + [0.0] * (self.target_dim - len(v)) for v in vecs]


def install_padded_embedder() -> None:
    import app.llm.factory as llm_factory

    original = llm_factory.get_embedder

    def wrapped():
        return PaddedEmbedder(original())

    llm_factory.get_embedder = wrapped


async def main() -> None:
    from app.config import get_settings
    from app.database import async_session_factory
    from app.llm.factory import get_llm_provider

    install_padded_embedder()
    settings = get_settings()
    provider = get_llm_provider()
    print("=" * 72)
    print("记忆遗忘机制消融评测（扩量版：50 冲突场景/组 + 80 条记忆库 + 21 互补对）")
    print("=" * 72)

    async with async_session_factory() as session:
        cleaned = await cleanup_eval_rows(session)
        print(f"[setup] 清理历史评测种子 {cleaned} 行")

        print("\n[实验一] 冲突干扰率（对照组=共存 vs 实验组=冲突整合，N=50/组）")
        exp1 = await run_experiment1(session, settings, provider)

        print("\n[实验二] 上下文压缩 + 长期记忆保持（记忆库 80 条）")
        exp2 = await run_experiment2(session, settings)

        print("\n[实验三] 复活窗口（误判恢复）")
        exp3 = await run_experiment3(session, settings)

        print("\n[实验四] 互补记忆误杀率（N=21，边界用例）")
        exp4 = await run_experiment4(session, settings)

    report = {
        "experiment1": {k: v for k, v in exp1.items() if k != "rows"},
        "experiment1_rows": exp1["rows"],
        "experiment2": exp2, "experiment3": exp3, "experiment4": exp4,
        "meta": {
            "conflict_scenarios": len(CONFLICT_SCENARIOS),
            "memory_bank": len(COLD_PAIRS) + len(HOT_PAIRS) + len(EVERGREEN_PAIRS),
            "complementary_pairs": len(COMPLEMENTARY_PAIRS),
            "finished_at": datetime.utcnow().isoformat(),
        },
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, "forgetting_ablation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    c, t = exp1["control"], exp1["treatment"]
    mc = exp1["mcnemar"]
    print("\n" + "=" * 72)
    print(f"实验一 N={c['n']}/组")
    print(f"  作废记忆注入率 {c['dep_inject_rate']:.0%} "
          f"[{c['dep_inject_ci'][0]:.0%},{c['dep_inject_ci'][1]:.0%}] → "
          f"{t['dep_inject_rate']:.0%} [{t['dep_inject_ci'][0]:.0%},{t['dep_inject_ci'][1]:.0%}]")
    print(f"  答案错误/引用作废 {c['bad_rate']:.0%} "
          f"[{c['bad_rate_ci'][0]:.0%},{c['bad_rate_ci'][1]:.0%}] → "
          f"{t['bad_rate']:.0%} [{t['bad_rate_ci'][0]:.0%},{t['bad_rate_ci'][1]:.0%}]")
    sig = "是" if mc["significant_at_0.05"] else "否"
    print(f"  McNemar χ²={mc['chi2']:.2f}（b={mc['b_control_bad_treatment_good']}, "
          f"c={mc['c_control_good_treatment_bad']}，p<0.05: {sig}）")
    print(f"实验二 老冷注入 {exp2['cold_avg_chars']['control']:.0f} → "
          f"{exp2['cold_avg_chars']['treatment']:.0f} 字符/查询"
          f"（压缩 {exp2['cold_compression']:.0%}，N={exp2['sample_sizes']['cold']}）")
    print(f"  老冷命中 {exp2['cold_hit_rate']['control']:.0%} → "
          f"{exp2['cold_hit_rate']['treatment']:.0%}；"
          f"老热保持 {exp2['hot_retention']['treatment']:.0%}（N={exp2['sample_sizes']['hot']}）、"
          f"长期偏好保持 {exp2['evergreen_retention']['treatment']:.0%}"
          f"（N={exp2['sample_sizes']['evergreen']}）")
    print(f"实验三 强命中复活={exp3['strong_hit_revived']}，弱命中保持退场={exp3['weak_hit_stays_out']}")
    e4n = len(exp4["pairs"])
    print(f"实验四 互补记忆误杀率 {exp4['wrongful_supersede_rate']:.0%}"
          f"（{sum(r['killed'] for r in exp4['pairs'])}/{e4n}，"
          f"CI [{exp4['rate_ci'][0]:.0%},{exp4['rate_ci'][1]:.0%}]）")
    print(f"\n完整结果已写入 {out_path}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    asyncio.run(main())
