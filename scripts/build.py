#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
中国医械法规库 · 生成页面

用法:
    python build.py                                  # 用 ./data/cn_regs.jsonl 生成 法规库.html
    python build.py --data X.jsonl --out 页面.html
    python build.py --tpl ../templates/page.html

做了什么:
    1. 读 jsonl，剔除标准类公告（保护《标准管理办法》等管理规章）
    2. 政策解读里剔掉药品/化妆品主题
    3. 判效力层级（法律/行政法规/部门规章/规范性文件/指导原则）
    4. 打 12 个主题标签（注册备案/生产质量/经营流通/…）
    5. 按标题判状态（现行/已废止/已修订）
    6. 同标题+同文号去重，优先药监局详情页
    7. 套模板出单文件 HTML（数据内嵌，断网可用）
"""
import argparse
import csv
import glob
import json
import os
import re
import time
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "..", "data", "cn_regs.jsonl")
DEFAULT_TPL = os.path.join(HERE, "..", "templates", "page.html")
# 技能版本的唯一真源：本文件同级目录里的 SKILL.md frontmatter `version`。
# 页面模板写 V__VERSION__，由 build() 渲染时填入；任何别处都不要手写版本号。
SKILL_MD = os.path.join(HERE, "..", "SKILL.md")


def read_skill_version():
    """从 SKILL.md frontmatter 读 version —— 技能版本只有这一个真源。"""
    try:
        with open(SKILL_MD, encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r"^version:\s*([0-9][0-9A-Za-z.\-]*)", head, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"
# 默认输出名带数据版本，由 build() 里按最新日期拼；这里只留目录
OUT_DIR = os.path.abspath(os.path.join(HERE, ".."))
BASE_NAME = "中国医械法规库"
# 监管动态命中表：优先读包内 logs/（notice_watch.py 就写在这里），
# 找不到才回退到同级 notice-watch 技能。写死包外路径会让独立用户静默丢两类通报。
_LOCAL_LOGS = os.path.abspath(os.path.join(HERE, "..", "logs"))
_EXTERNAL_LOGS = os.path.abspath(os.path.join(HERE, "..", "..", "notice-watch", "logs"))
NOTICE_DIR = _LOCAL_LOGS if os.path.isdir(_LOCAL_LOGS) else _EXTERNAL_LOGS
# 监管动态只留最近 12 个月
NOTICE_CUTOFF = "2025-11-21"


def clean(t):
    return re.sub(r"[【\[（(]\s*(废止|已废止|失效|已修改|暂行|试行)\s*[】\]）)]", "", t).strip()


def status_of(t):
    if re.search(r"【废止】|【已废止】|（废止）|【失效】", t):
        return "已废止"
    if re.search(r"【已修改】|（已修改）", t):
        return "已修订"
    return "现行"


# 栏目 → 发布机构（仅用于卡片上标注，不作筛选维度）
ORG = {
    "nmpa_fgwj": "国家药监局", "cmde_fg": "国务院", "cmde_guizh": "国家药监局",
    "cmde_fgwj": "国家药监局", "cmde_zdyz": "器审中心", "cmde_zqyj": "器审中心",
    "cmde_gztg": "器审中心", "nmpa_zcjd": "国家药监局",
    "gd_gzwj": "广东省药监局", "gd_fgjd": "广东省药监局", "nidc_fljd": "中检院",
    # 省级药监局（只收省局自己发的实施细则/工作程序，转发的国家局文件已在抓取时剔除）
    "js_jsfg": "江苏省药监局", "js_gfxwj": "江苏省药监局", "js_flgz": "江苏省药监局",
    "js_gzwj": "江苏省药监局", "js_ggtg": "江苏省药监局",
    "hn_fgwj": "河南省药监局", "hn_ggtg": "河南省药监局",
    "ah_gsgg": "安徽省药监局",
    "bj_zcwj": "北京市药监局",
    "zj_gzwj": "浙江省药监局",
    "sh_gfxwj": "上海市药监局",
    "sh_qttzgg": "上海市药监局",
    "sh_qtgzwj": "上海市药监局",
    "zj2_1229215895": "浙江省药监局",
    "zj2_1229562213": "浙江省药监局",
    "zj2_1229215831": "浙江省药监局",
    "zj2_1229893251": "浙江省药监局",
    "zj2_1228989345": "浙江省药监局",
    "zj3_1229136205": "浙江省药监局",
    "zj3_1228989285": "浙江省药监局",
}

# 省级药监局的栏目：国家局规定在本省的落地细则，主要给本省企业看
PROVINCE_COLS = {
    "gd_gzwj", "gd_fgjd",
    "js_jsfg", "js_gfxwj", "js_flgz", "js_gzwj", "js_ggtg",
    "hn_fgwj", "hn_ggtg", "ah_gsgg",
    "bj_zcwj",
    "zj_gzwj", "sh_gfxwj", "sh_qttzgg", "sh_qtgzwj",
}


def source_of(r):
    c = r.get("col") or ""
    return "省级" if (c in PROVINCE_COLS or c.startswith("zj2_") or c.startswith("zj3_")) else "国家"


def province_of(r):
    """省级文件归到具体省：江苏省药监局 → 江苏；北京直辖市也归北京"""
    if r["source"] != "省级":
        return ""
    o = r["org"]
    for suf in ("药品监督管理局", "药监局", "监督管理局", "管理局"):
        o = o.replace(suf, "")
    return re.sub(r"(省|市|自治区|特别行政区)$", "", o)

LEVEL_ORDER = ["法律", "行政法规", "部门规章", "规范性文件", "指导原则"]


def org_of(r):
    o = ORG.get(r.get("col", ""), "国家药监局")
    b = clean(r["title"])
    if re.search(r"国务院令|^《?[^》]{0,20}条例》?$", b) or "主席令" in b:
        return "国务院"
    return o


def dtype_of(r):
    b = clean(r["title"])
    c = r.get("col", "")
    if c == "nmpa_zcjd":
        return "政策解读"
    if re.search(r"主席令", b) or re.match(r"^《?中华人民共和国[^》]{1,20}法》?$", b):
        return "法律"
    if c == "nidc_fljd":
        return "分类界定"
    if "征求意见" in b or c == "cmde_zqyj":
        return "征求意见稿"
    if re.search(r"指导原则|审查指南|检查指南|审评要点|技术审查要点", b) and not re.search(r"通知$|公告$|通告$", b):
        return "指导原则"
    if c == "cmde_zdyz":
        return "指导原则"
    if re.search(r"国务院令|^《?[^》]{0,20}条例》?$", b) or "主席令" in b:
        return "行政法规"
    if re.search(r"(局令|总局令|卫生部令|令)\s*第\s*\d+\s*号", b) and not re.search(r"通知$|公告$|通告$|复函$", b):
        return "部门规章"
    if re.match(r"^《?[^》]{0,26}(办法|规定|规则|规范)》?$", b) and not re.search(r"通知|公告|通告", b):
        return "部门规章"
    if re.search(r"公告", b):
        return "公告"
    if re.search(r"通告", b):
        return "通告"
    return "通知"


def level_of(r):
    if r.get("col") in PROVINCE_COLS:
        return "规范性文件"   # 省局出的只能是本省实施细则/工作程序，不是部门规章
    y = r["dtype"]
    if y in ("法律", "行政法规", "部门规章", "指导原则"):
        return y
    return "规范性文件"


# 12 个主题：这是唯一的工作量分类，按使用者要办的事打标签
SCENE = OrderedDict([
    ("注册备案", r"注册|备案|申报|受理|审评审批|延续注册|变更注册|注册证"),
    ("生产质量", r"生产|GMP|质量管理体系|生产企业|受托生产|洁净|工艺|验证"),
    ("经营流通", r"经营|销售|流通|网络销售|贮存|运输|第三方物流|广告"),
    ("临床评价", r"临床|试验|GCP|临床评价|真实世界"),
    # 注意：不能写裸"监测"，否则"远程监测系统注册审查指导原则"会被打成不良事件
    ("不良事件", r"不良事件|召回|再评价|警戒|飞行检查|抽检|质量公告"),
    ("分类命名", r"分类|目录|命名|属性界定|类别"),
    ("UDI唯一标识", r"唯一标识|UDI|追溯"),
    ("体外诊断", r"体外诊断|试剂|校准品|质控品|IVD|病原|核酸"),
    ("注册检验", r"检验|检测|自检|型式检验"),
    ("说明书标签", r"说明书|标签|包装|标识"),
    ("进出口", r"进口|出口|境外|海关"),
    ("机构与程序", r"机构|职责|程序|流程|工作规则|政务服务"),
])


def topics_of(t):
    return [k for k, p in SCENE.items() if re.search(p, t)]


# 标准类公告剔除，但保留标准管理规章
STD = re.compile(r"(等\s*\d+\s*项|发布|废止|修改单|立项|制修订计划|征求意见).{0,40}(医疗器械)?(行业标准|国家标准)"
                 r"|(YY/T|YY|GB/T|GB)\s*\d|医疗器械行业标准|行业标准.{0,10}转化为|标准.{0,6}转化为推荐性")
KEEP_STD_LAW = re.compile(r"标准管理办法|标准化技术|标准制修订工作管理规范|标准化技术归口")
# 批准注册 N 个产品 = 办理结果，不是规定（早期公告写作"产品公告"，无"的"字）
RESULT_ONLY = re.compile(r"关于批准注册\s*\d+\s*个医疗器械产品")
# 抽检结果通告属于「通报」不是「规定」，归监管动态板块
TEST_NOTICE = re.compile(r"监督抽检结果|抽检结果的通告")
# 非法规内容：协会人事、录用公示、座谈会/培训班通知、表彰名单、竞赛征文
# 注意不能一刀切"采购/招标"——「集中带量采购中选产品监管通知」是真监管文件
NON_REG = re.compile(
    r"协会.{0,12}(人选|换届|负责人|理事会)|学会.{0,12}(换届|人选)|理事会.{0,12}(人选|换届|负责人)"
    r"|录用公示|招录|招聘|选调|任前公示|拟任|任职前公示"
    r"|召开.{0,24}(座谈会|研讨会|论坛|年会|交流会)|报名参加.{0,20}座谈会"
    r"|举办.{0,24}培训班|培训班.{0,6}通知"
    r"|获奖名单|先进工作者|先进集体|先进个人|表彰"
    r"|知识竞赛|征文|摄影比赛|书法|书画"
    r"|脱贫攻坚|定点扶贫|对口支援"
)

# 注销/撤销注册证、许可证 = 办事结果通报，不是法规（用户明确说不关心，只作工作通告）
CANCEL_NOTICE = re.compile(r"(注销|撤销|缴销).{0,24}(注册证|注册证书|许可证|备案凭证)")
# 年度工作报告/总结 = 年度回顾性质，不是"该怎么做"的规定
# 但「政策解读/图解」里提到年度报告的，是解读文件，保留
REPORT_ONLY = re.compile(r"工作报告|年度报告|工作总结|年度总结|白皮书|蓝皮书|工作年报")

# 名单公示类：谁取得了证、谁考核合格 = 办事结果，不是"该怎么做"的规定
# 注意不能剔「关于开展…检查的通知」那类监管要求
LIST_NOTICE = re.compile(
    r"公布.{0,40}(名单|企业目录)|合格名单|名单公示|人员名单|企业名单|"
    r"评定结果|评审结果|考核结果|认定名单")
# 会议/活动类
MEETING_NOTICE = re.compile(r"召开.{0,24}会议|报名参加.{0,24}会议|工作会议的通知|电视电话会议")

# 事务性日程安排（咨询排班表、值班表）= 过期作废，没有留存价值，直接剔除
# 注意不误伤「关于全面开展…前置咨询服务的通告」这类真政策
SCHEDULE_ONLY = re.compile(
    r"咨询工作安排|咨询安排的通告|受理前技术问题咨询工作|值班表|排班|日程安排|"
    r"\d+月.{0,6}\d+月.{0,12}咨询|受理窗口安排")

# 宣传材料（图解海报/视频解读/一图读懂）= 不是文件本身，一份文件能拆成十几张图
# 图解/海报类：纯宣传（数说、新闻发布会、活动推广）删；对法规的解读要留
PROMO_ONLY = re.compile(r"图解|海报|一图读懂|视频解读|政策速览|图读")
PROMO_KEEP = re.compile(r"解读|办法|规定|规范|条例|指导原则|要求|目录|标准")
# 机构批复（同意筹建/调整标准化技术归口单位、委员会换届）= 机构事务，不是法规
ORG_REPLY = re.compile(
    r"同意(筹建|调整|新增).{0,24}(标准化技术归口|技术委员会|归口单位|分技术委员会)|"
    r"换届及组成方案|标准化技术委员会.{0,12}换届|归口单位.{0,12}调整")

# 审查/审批结果公示 = 进度通报（谁进了通道），不是"该怎么做"的规定
RESULT_PUBLIC = re.compile(
    r"审查结果公示|审批结果公示|注册申请审查结果|审查结果的通知|优先审批申请审核结果")

# 系统/平台启用通知 = 操作提示（告诉你现在要去哪办），不是"该怎么做"的规定
PLATFORM_ONLY = re.compile(
    r"启用.{0,26}(平台|系统|功能|模块|专栏)|平台启用|系统上线|正式上线|开通运行|上线运行|"
    r"新增功能|功能上线")

# 企业召回公告（企业行为通报）≠ 法规；但《医疗器械召回管理办法》及解读是真法规，要留
RECALL_ACTION = re.compile(r"公司.{0,40}召回|对.{0,30}召回|召回公告|召回通告|责令召回")
RECALL_KEEP = re.compile(r"办法|规定|解读|图解|政策速览|实施细则")

# 监管处置类：暂停生产/责令停产/停产整改/解除风控/飞检结果 → 监管动态，不是规范性文件
# 但排除「关于印发…办法/规范/程序」这类真法规
ENFORCE_NOTICE = re.compile(
    r"暂停生产|责令停产|停产整改|暂停进口|暂停经营|暂停使用|责令暂停|责令改正|"
    r"限期整改|解除风险控制|风险控制措施|飞行检查情况|飞行检查结果|飞行检查通报")

DRUG = re.compile(r"化妆品|药品(?!、)|化学药|中药|生物制品|疫苗|麻醉药品|精神药品")

# 省局公告里混着大量"办理结果名单"，不是规定，对查法规的人没用
# 先认规定类词，再判结果类，避免把"…工作程序…的通知"误杀
RULE_WORDS = re.compile(r"实施细则|工作程序|操作规程|管理办法|管理规定|规范|指南|要点|"
                        r"程序|方案|制度|要求|标准|办法|规定|规则")
RESULT_NOTICE = re.compile(r"批准注册\s*\d+|注销.{0,20}注册证|注册产品公告|产品公告|"
                           r"备案情况|不予注册|领取|送达|注销.{0,10}许可证的公告")


def is_result_notice(title):
    """是"审批结果名单"而不是"规定"吗"""
    if RULE_WORDS.search(title):
        return False
    return bool(RESULT_NOTICE.search(title))


def is_device_notice(r):
    """省局的处罚栏目里混着药品、化妆品，只留器械相关"""
    t = r.get("Title", "")
    if re.search(r"妆罚|化妆品|牙膏|保健食品|食品", t):
        return False
    if re.search(r"药罚|药品行政处罚|药品经营|药品生产", t):
        return False
    if re.search(r"器械|医用|体外诊断|UDI|植入|敷料|诊断试剂", t):
        return True
    # 国家局那几个源本身就是器械专栏
    return r.get("Region", "").startswith("国家局")


def load_notice():
    """从 notice-watch 的命中表里读监管动态，只留四类、只要器械、只要 12 个月内"""
    if not os.path.isdir(NOTICE_DIR):
        print(f"  ⚠ 监管动态：找不到命中表目录 {NOTICE_DIR}")
        print(f"     页面会缺飞行检查/行政处罚等通报 —— 请先跑 notice_watch.py")
        return []
    seen = {}
    for f in sorted(glob.glob(os.path.join(NOTICE_DIR, "*hits*.csv")), key=os.path.getmtime):
        try:
            for r in csv.DictReader(open(f, encoding="utf-8-sig")):
                key = r.get("DetailUrl") or r.get("ItemId")
                if key:
                    seen[key] = r
        except Exception:
            continue

    out = []
    for r in seen.values():
        hit = (r.get("HitType") or "").strip()
        if hit == "不良事件":          # 用户明确不要这一类
            continue
        if hit not in ("飞检", "行政处罚", "监督抽检", "注册证注销"):
            continue
        if not is_device_notice(r):
            continue
        d = (r.get("Date") or "").strip()
        if d and d < NOTICE_CUTOFF:
            continue
        # 清掉抓取时混进来的 HTML 片段（如 '（2026年 第95号）" target="_blank">'）
        raw_title = r.get("Title", "").strip()
        if '">' in raw_title:
            raw_title = raw_title.split('">', 1)[-1]
        raw_title = re.sub(r"<[^>]+>", "", raw_title)
        raw_title = re.sub(r"^[·•\-–—\s]+", "", raw_title).strip()
        region = (r.get("Region") or "").replace("国家局 NMPA", "国家局").strip()
        prov = re.sub(r"(省|市)$", "", region) if region not in ("国家局",) else ""
        label = "飞行检查" if hit == "飞检" else ("停产整改 · 注销" if hit == "注册证注销" else hit)
        out.append({
            "title": raw_title,
            "date": d,
            "docno": "",
            "url": r.get("DetailUrl", "").strip(),
            "col": "notice_" + hit,
            "col_name": "监管动态-" + label,
            "level": label,
            "utitle": re.sub(r"\s+", " ", raw_title),
            "status": "现行",
            "org": region,
            "dtype": label,
            "topics": [],
            "lv": label,
            "source": "省级" if prov else "国家",
            "prov": prov,
            "grp": "notice",
        })
    # 四类里少任何一类都要喊出来：静默丢数据是这套流程最危险的故障
    got = Counter(x["dtype"] for x in out)
    missing = [t for t in ("飞行检查", "行政处罚", "监督抽检", "停产整改 · 注销") if got.get(t, 0) == 0]
    if out and missing:
        print(f"  ⚠ 监管动态：命中表里{'/'.join(missing)}为 0，可能没抓到或被拦，页面会缺这一类")
    return out


def date_from_url(u):
    """NMPA 早期文件列表页没给日期，但文件名前 8 位就是 YYYYMMDD"""
    m = re.search(r"/((?:19|20)\d{2})(\d{2})(\d{2})\d*\.html?$", u or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    try:
        if 1990 <= int(y) <= 2030 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    except ValueError:
        pass
    return ""


def is_device_like(r):
    if r.get("col") != "nmpa_zcjd":
        return True
    if "/zhcjdylqx/" in r.get("url", ""):
        return True
    t = r["title"]
    return bool(re.search(r"医疗器械|器械|UDI|唯一标识|体外诊断|医用", t))


def load(src):
    rows = []
    for line in open(src, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if not is_device_like(r):
            continue
        if not r.get("date"):
            r["date"] = date_from_url(r.get("url", ""))
        r["utitle"] = clean(r["title"])
        r["status"] = status_of(r["title"])
        r["org"] = org_of(r)
        r["dtype"] = dtype_of(r)
        r["topics"] = topics_of(r["title"])
        r["lv"] = level_of(r)
        r["grp"] = "draft" if r["dtype"] == "征求意见稿" else "law"
        # 抽检结果通告归「监管动态」，不占法规板块
        if TEST_NOTICE.search(r["utitle"]):
            r["grp"] = "notice"
            r["dtype"] = "监督抽检"
            r["lv"] = "监督抽检"
        # 视频类一律剔（不可搜不可引），哪怕标题里有"解读"
        if re.search(r"视频解读|微视频|长图|H5", r["utitle"]):
            continue
        # 图解/海报：纯宣传才剔，对法规的解读保留
        if PROMO_ONLY.search(r["utitle"]) and not PROMO_KEEP.search(r["utitle"]):
            continue
        # 机构批复（筹建/换届/归口单位）= 不是法规，剔
        if ORG_REPLY.search(r["utitle"]):
            continue
        # 审查/审批结果公示 = 名单公示，不是法规也不是监管动态，直接剔
        elif RESULT_PUBLIC.search(r["utitle"]):
            continue
        # 系统/平台启用 → 监管动态·工作通告
        elif PLATFORM_ONLY.search(r["utitle"]):
            r["grp"] = "notice"
            r["dtype"] = "工作通告"
            r["lv"] = "工作通告"
        # 企业主动召回/责令召回 → 监管动态·召回（不是规范性文件）
        elif RECALL_ACTION.search(r["utitle"]) and not RECALL_KEEP.search(r["utitle"]):
            r["grp"] = "notice"
            r["dtype"] = "召回"
            r["lv"] = "召回"
        # 监管处置（暂停生产/责令停产/停产整改/解除风控）→ 监管动态
        elif ENFORCE_NOTICE.search(r["utitle"]) and not re.search(
                r"印发|制定|办法|规范|程序|方案|指南", r["utitle"]):
            r["grp"] = "notice"
            r["dtype"] = "停产整改 · 注销"
            r["lv"] = "停产整改 · 注销"
        # 注销/撤销注册证、许可证 → 工作通告（监管动态），不进法规板块
        elif CANCEL_NOTICE.search(r["utitle"]):
            r["grp"] = "notice"
            r["dtype"] = "停产整改 · 注销"
            r["lv"] = "停产整改 · 注销"
        r["source"] = source_of(r)
        r["prov"] = province_of(r)
        if STD.search(r["utitle"]) and not KEEP_STD_LAW.search(r["utitle"]):
            continue
        # 批准注册 N 个产品 = 办理结果名单，不是规定（任何来源都剔）
        if RESULT_ONLY.search(r["utitle"]):
            continue
        # 协会人事/录用公示/会议培训通知/表彰名单 = 不是法规
        if NON_REG.search(r["utitle"]):
            continue
        # 咨询排班/值班日程等事务性安排 = 过期作废，剔
        if SCHEDULE_ONLY.search(r["utitle"]):
            continue
        # 年度工作报告/总结 = 年度回顾，不是规定
        if REPORT_ONLY.search(r["utitle"]) and not re.search(r"政策解读|图解|解读", r["utitle"]):
            continue
        # 公布名单/考核结果 = 办事结果，不是规定
        if LIST_NOTICE.search(r["utitle"]):
            continue
        # 召开会议/报名参会 = 会议通知
        if MEETING_NOTICE.search(r["utitle"]):
            continue
        # 省局栏目：剔掉“批准注册X个产品/注销注册证书”这类办理结果名单
        if r["col"] in PROVINCE_COLS and is_result_notice(r["utitle"]):
            continue
        rows.append(r)

    # 跨栏目去重：同一份文件（标题+日期完全相同）只留一条，保留优先级最高的
    _dedup = {}
    for r in rows:
        k = (r["utitle"], r["date"])
        cur = _dedup.get(k)
        if cur is None:
            _dedup[k] = r
            continue
        # 同一份文件被多个栏目收录时，保留国家局原文、现行版本
        better = ((r["source"] == "国家" and cur["source"] != "国家")
                  or (r["source"] == cur["source"] and r["status"] == "现行" and cur["status"] != "现行"))
        if better:
            _dedup[k] = r
    _uniq = list(_dedup.values())
    _dropped = len(rows) - len(_uniq)
    if _dropped:
        print(f"跨栏目重复：{len(rows)} → {len(_uniq)}（剔除 {_dropped} 条同一文件的重复记录，保留国家局原文）")
    rows = _uniq

    # 同一份指导原则/文件：既有正文标题、又有「关于发布…的通告」时，只留正文
    def _core(t):
        x = re.sub(r"^.{0,40}?关于(?:公开)?(?:发布|印发|批准发布)", "", t)
        x = re.sub(r"的通告.*$|的公告.*$", "", x)
        x = re.sub(r"（\d{4}年第\d+号）|\(\d{4}年第\d+号\)", "", x)
        return re.sub(r"[\s《》【】]", "", x)

    # 「关于发布《X》等N项…的通告」：若 X 已作为正文单列，通告本身不必重复收
    _titles = {re.sub(r"[\s《》【】]", "", x["utitle"]) for x in rows}
    _drop_tg = 0
    _rows2 = []
    for x in rows:
        m = re.match(r".{0,30}?关于(?:公开)?发布《(.+?)》等\s*\d+\s*项", x["utitle"])
        if m:
            core = re.sub(r"[\s《》【】]", "", m.group(1))
            _self = re.sub(r"[\s《》【】]", "", x["utitle"])
            # 正文标题通常带文号后缀（如「（2026年第28号）」），用前缀匹配
            if core and core != _self and any(t.startswith(core) for t in _titles):
                _drop_tg += 1
                continue
        _rows2.append(x)
    if _drop_tg:
        print(f"「发布N项」通告合并：剔掉 {_drop_tg} 条（各项正文已单列收录）")
    rows = _rows2

    _by_core = {}
    for r in rows:
        _by_core.setdefault(_core(r["utitle"]), []).append(r)
    _keep, _merged = [], 0
    for k, grp in _by_core.items():
        if len(grp) == 1:
            _keep.append(grp[0])
            continue
        _notices = [r for r in grp if re.match(r"^.{0,40}?关于(?:公开)?(?:发布|印发|批准发布)", r["utitle"])]
        _others = [r for r in grp if r not in _notices]
        if _notices and _others:
            _keep.extend(_others)   # 有正文，通告不重复收
            _merged += len(_notices)
        else:
            _keep.extend(grp)
    if _merged:
        print(f"正文/发布通告合并：剔掉 {_merged} 条『关于发布…的通告』（正文已收录）")
    rows = _keep

    def prio(r):
        s = 0
        if "/xxgk/fgwj/" in r["url"]:
            s += 3
        if r.get("col") == "cmde_guizh":
            s += 2
        if r["status"] == "现行":
            s += 1
        return -s

    best = {}
    for r in rows:
        k = (re.sub(r"[\s《》（）()【】\[\]、,，。:：;；\-—_]", "", r["utitle"]),
             re.sub(r"\s", "", r["docno"]), r["status"])
        if k not in best or prio(r) < prio(best[k]):
            best[k] = r
    return sorted(best.values(), key=lambda x: (x["date"] or "0000"), reverse=True)


def qr_data_uri():
    """公众号二维码：读 assets/qrcode.jpg 转 base64 内嵌，保证单文件离线可用。
    图不存在就返回空字符串（页面上那格自然空着，不报错）。"""
    import base64
    p = os.path.join(HERE, "..", "assets", "qrcode.jpg")
    if not os.path.exists(p):
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()


def build(src, out, tpl_path):
    rows = load(src)
    notices = load_notice()
    # 同一条公告可能既在省局法规栏目里、又被 notice-watch 抓到，按标题去重（法规那份保留）
    _norm = lambda t: re.sub(r"[\s《》（）()【】\[\]、,，。:：;；\-—_\"'“”]", "", t or "")
    law_titles = {_norm(r["utitle"]) for r in rows}
    before = len(notices)
    notices = [n for n in notices if _norm(n["utitle"]) not in law_titles]
    # 多个省局源可能指向同一份处罚公告，按 标题+日期 再去一次
    _nseen = {}
    for n in notices:
        k = (_norm(n["utitle"]), n.get("date") or "")
        if k not in _nseen:
            _nseen[k] = n
    if len(_nseen) != len(notices):
        print(f"监管动态内部重复：{len(notices)} → {len(_nseen)}")
        notices = list(_nseen.values())
    if before != len(notices):
        print(f"监管动态去重：{before} → {len(notices)}（与法规库重复的已剔除）")
    rows = sorted(rows + notices, key=lambda x: (x.get("date") or "0000"), reverse=True)
    if not rows:
        print("✗ 没有可用数据，先跑 update.py 抓取")
        return 2

    tp = Counter(t for r in rows for t in r["topics"])
    n_law = sum(1 for r in rows if r.get("grp") == "law")
    n_draft = sum(1 for r in rows if r.get("grp") == "draft")
    n_notice = sum(1 for r in rows if r.get("grp") == "notice")
    lvc = Counter(r["lv"] for r in rows if r.get("grp") == "law")
    st = Counter(r["status"] for r in rows)
    years = sorted({r["date"][:4] for r in rows if r["date"]}, reverse=True)
    updated = time.strftime("%Y-%m-%d")
    latest = max((r["date"] for r in rows if r["date"]), default=updated)

    # 文件名 = 页面里的名字，两者一致：中国医疗器械法规全集_2026-11-19.html
    # 那个日期只是「这批数据更新到哪天」，不是版本号；版本号唯一真源是 SKILL.md frontmatter
    if not out or os.path.isdir(out or ""):
        _dir = out if out else OUT_DIR
        out = os.path.join(_dir, f"{BASE_NAME}_{latest}.html")

    payload = [{"t": r["utitle"], "u": r["url"], "d": r["date"], "n": r["docno"],
                "o": r["org"], "y": r["dtype"], "st": r["status"], "lv": r["lv"],
                "src": r["source"], "pv": r["prov"], "sc": r["topics"], "pr": [],
                "grp": r.get("grp", "law")} for r in rows]
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    levels = "".join(
        f'<button class="nav-item" data-lv="{l}">{l}<span class="cnt">{lvc[l]}</span></button>'
        for l in LEVEL_ORDER if lvc.get(l))
    # 来源：国家局 + 各省分开列，按条数降序
    src_prov = Counter(r["prov"] for r in rows if r["prov"])
    n_nat = sum(1 for r in rows if r["source"] == "国家")
    srcs = '<button class="chip srcchip on" data-src="">全部 <span class="cnt"></span></button>'
    srcs += '<button class="chip srcchip" data-src="国家">国家局 <span class="cnt"></span></button>'
    for pv, c in src_prov.most_common():
        srcs += f'<button class="chip srcchip" data-src="{pv}">{pv} <span class="cnt"></span></button>'

    # 监管动态的通报类型（用户只留这四类）
    NT_ORDER = ["飞行检查", "行政处罚", "监督抽检", "停产整改 · 注销"]
    ntc = Counter(r["lv"] for r in rows if r.get("grp") == "notice")
    # 四类固定在前，其余类目（召回/工作通告/审查结果公示）按数量排后面
    _names = [k for k in NT_ORDER if ntc.get(k)]
    _names += [k for k, _ in ntc.most_common() if k not in _names]
    ntypes = "".join(
        f'<button class="nav-item" data-nt="{k}">{k}<span class="cnt">{ntc[k]}</span></button>'
        for k in _names)

    scenes = "".join(
        f'<button class="scene" data-topic="{k}">{k}<span class="cnt"></span></button>'
        for k in SCENE)
    year_opts = "".join(f'<option value="{y}">{y}</option>' for y in years)

    tpl = open(tpl_path, encoding="utf-8").read()
    html = (tpl.replace("__DATA__", data_json)
               .replace("__LEVELS__", levels)
               .replace("__SCENES__", scenes)
               .replace("__NTYPES__", ntypes)
               .replace("__LAW_N__", str(n_law))
               .replace("__DRAFT_N__", str(n_draft))
               .replace("__NOTICE_N__", str(n_notice))
               .replace("__SRCS__", srcs)
               .replace("__PRODS__", "")
               .replace("__YEARS__", year_opts)
               .replace("__TOTAL__", str(len(rows)))
               .replace("__UPDATED__", latest)
               .replace("__VERSION__", read_skill_version())
               .replace("__GENTIME__", time.strftime("%Y-%m-%d %H:%M"))
               .replace("__QRCODE__", qr_data_uri())
               .replace("__ABOLISHED__", str(st.get("已废止", 0))))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)

    print(f"已生成: {out}  {os.path.getsize(out)/1024:.0f} KB")
    print(f"总条数: {len(rows)}  最新发布日期: {latest}")
    print("效力层级:", dict(lvc))
    print("状态:", dict(st))
    print("主题:", dict(tp))
    print("板块:", {"法规": n_law, "征求意见稿": n_draft, "监管动态": n_notice})
    print("来源:", dict(Counter(r["source"] for r in rows)))
    print("各省:", dict(Counter(r["prov"] for r in rows if r["prov"])))
    return 0


def main():
    ap = argparse.ArgumentParser(description="中国医械法规库 · 生成页面")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--out", default=None, help="输出路径；不传则自动命名为 中国医械法规库_<数据日期>.html")
    ap.add_argument("--tpl", default=DEFAULT_TPL)
    a = ap.parse_args()
    # --out 不传时是 None，要原样交给 build() 让它按数据日期自动命名。
    # 别在这里对 None 调 os.path.abspath —— 会直接 TypeError 把第 4 步搞崩。
    out = os.path.abspath(a.out) if a.out else None
    tpl = os.path.abspath(a.tpl) if a.tpl else DEFAULT_TPL
    return build(os.path.abspath(a.data), out, tpl)


if __name__ == "__main__":
    raise SystemExit(main())
