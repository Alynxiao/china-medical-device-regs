#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
中国医械法规库 · 抓取入口

用法:
    python update.py                 # 自动判断：没数据就全量，有数据就增量
    python update.py --full          # 强制全量（把所有栏目翻到最后一页，约 25 分钟）
    python update.py --data X.jsonl  # 指定数据文件（默认 ./cn_regs.jsonl）
    python update.py --only cmde_zdyz,nmpa_fgwj   # 只抓指定栏目

判断规则: 数据文件不存在或少于 200 条 → 全量；否则增量（只扫到"整页都是旧的"就停）。

依赖: pip install "scrapling[fetchers]"  然后  scrapling install
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "..", "data", "cn_regs.jsonl")

# ---------------------------------------------------------------- 栏目定义
# kind: scrapling = 有瑞数反爬，必须用无头浏览器；urllib = 可直接连
COLUMNS = [
    dict(key="cmde_zdyz", name="器审中心-指导原则文本库", level="指导原则", kind="scrapling",
         url="https://www.cmde.org.cn/flfg/zdyz/zdyzwbk/index.html", expect=741),
    dict(key="cmde_zqyj", name="器审中心-征求意见稿", level="征求意见", kind="scrapling",
         url="https://www.cmde.org.cn/flfg/zdyz/zqyjg/index.html", expect=454),
    dict(key="cmde_gztg", name="器审中心-工作通告", level="工作通告", kind="scrapling",
         url="https://www.cmde.org.cn/xwdt/shpgzgg/gztg/index.html", expect=284),
    dict(key="cmde_fg", name="器审中心-法规", level="法律/行政法规", kind="scrapling",
         url="https://www.cmde.org.cn/flfg/flfgnew/index.html", expect=None),
    dict(key="cmde_guizh", name="器审中心-部门规章", level="部门规章", kind="scrapling",
         url="https://www.cmde.org.cn/flfg/guizh/index.html", expect=16),
    dict(key="cmde_fgwj", name="器审中心-规范性文件", level="规范性文件", kind="scrapling",
         url="https://www.cmde.org.cn/flfg/fgwj/index.html", expect=256),
    dict(key="nmpa_fgwj", name="国家药监局-医疗器械法规文件", level="法规/规范性文件", kind="scrapling",
         url="https://www.nmpa.gov.cn/ylqx/ylqxfgwj/index.html", expect=883),
    dict(key="nmpa_ggtg", name="国家药监局-医疗器械公告通告", level="公告通告", kind="scrapling",
         url="https://www.nmpa.gov.cn/ylqx/ylqxggtg/index.html", expect=1276, drop_result=True),

    # 官方页写着「共 1020 条」，但实测翻到第 50 页就没了，合计 994 条（去重 967）。
    # 早期条目页已下架，这个 1020 永远抓不到，不设 expect 免得永远误报"漏抓"。
    # 另外这是全站政策解读（药/妆/械混排），药品类在 build.py 阶段按 DRUG 规则剔除。
    dict(key="nmpa_zcjd", name="国家药监局-政策解读", level="政策解读", kind="scrapling",
         url="https://www.nmpa.gov.cn/xxgk/zhcjd/index.html", expect=None),
    # 国家局「征求意见」栏目（1.0.1 新增）：全站混排（药品/化妆品/综合都会进来），
    # device_only 过滤后约 177 条器械相关，含 2026-09-29《体外诊断试剂检查指导原则
    # （征求意见稿）》这类只在综合司栏目发、CMDE 征求意见稿库里没有的文件。
    # 注意别开 exclude_national：这里每条标题都以「国家药监局综合司」开头，会被剔光。
    # 翻到 index_28（2002-10）为止：index_29 起是 2001-2002 的旧表格页，逐页看过只剩
    # 药品条目（药品国家标准、非处方药说明书、年鉴征稿），没有器械的。
    dict(key="nmpa_zqyj", name="国家药监局-征求意见", level="征求意见", kind="scrapling",
         url="https://www.nmpa.gov.cn/xxgk/zhqyj/index.html", expect=None,
         pages=29, device_only=True),
    dict(key="gd_gzwj", name="广东省药监局-工作文件", level="通知", kind="urllib",
         url="https://mpa.gd.gov.cn/zwgk/gzwj/index.html", expect=None,
         parser="gd", device_only=True, exclude_national=True),

    # ---- 江苏（省药监局，大汉 jpage 接口）----
    dict(key="js_jsfg", name="江苏省药监局-政策法规", level="通知", kind="js_jiangsu",
         url="https://da.jiangsu.gov.cn/col/col84618/index.html", expect=None,
         cid=84618, uid=369470, webid=18, device_only=True, exclude_national=True),
    dict(key="js_gfxwj", name="江苏省药监局-规范性文件", level="规范性文件", kind="js_jiangsu",
         url="https://da.jiangsu.gov.cn/col/col84620/index.html", expect=None,
         cid=84620, uid=369470, webid=18, device_only=True, exclude_national=True),
    dict(key="js_flgz", name="江苏省药监局-法律法规规章", level="行政法规", kind="js_jiangsu",
         url="https://da.jiangsu.gov.cn/col/col84619/index.html", expect=None,
         cid=84619, uid=369470, webid=18, device_only=True, exclude_national=True),
    dict(key="js_gzwj", name="江苏省药监局-工作文件", level="通知", kind="js_jiangsu",
         url="https://da.jiangsu.gov.cn/col/col84617/index.html", expect=None,
         cid=84617, uid=369619, webid=18, device_only=True, exclude_national=True),
    dict(key="js_ggtg", name="江苏省药监局-公告通告", level="通告", kind="js_jiangsu",
         url="https://da.jiangsu.gov.cn/col/col84627/index.html", expect=None,
         cid=84627, uid=374104, webid=18, device_only=True, exclude_national=True),

    # ---- 河南（器械专栏，省局自己发的）----
    dict(key="hn_fgwj", name="河南省药监局-器械省局文件", level="通知", kind="urllib",
         url="https://yjj.henan.gov.cn/ylqx/fgwj/", expect=None,
         device_only=True, exclude_national=True, pages=14),
    dict(key="hn_ggtg", name="河南省药监局-器械公告通告", level="通告", kind="urllib",
         url="https://yjj.henan.gov.cn/ylqx/ggtg/", expect=None,
         device_only=True, exclude_national=True, pages=14),

    # ---- 北京（easysite 模板，静态可抓；"政策文件"全 112 条里按器械过滤）----
    dict(key="bj_zcwj", name="北京市药监局-政策文件", level="通知", kind="beijing",
         url="https://yjj.beijing.gov.cn/yjj/zwgk20/zcwj91/index.html", expect=112,
         device_only=True, exclude_national=True),

    # ---- 浙江（政务云 jpaas 接口）----
    dict(key="zj_gzwj", name="浙江省药监局-工作文件", level="通知", kind="zj_jpaas",
         url="https://mpa.zj.gov.cn/col/col1228989345/index.html", expect=None,
         pid="1228989345", tpl="PpFTeEhA5DVkxKPoRDIzx", webid="3396", tag="列表通用list",
         device_only=True, exclude_national=True),

    # ---- 上海（静态 HTML，日期是 2026.11.20 点分隔）----
    dict(key="sh_gfxwj", name="上海市药监局-规范性文件", level="规范性文件", kind="urllib",
         url="https://yjj.sh.gov.cn/gfxwj/index.html", expect=None,
         parser="sh", device_only=True, exclude_national=True),

    # ---- 安徽（只要公示公告；"文件转发"栏目全 15 条都是国家局药品/化妆品，已核实 0 器械，不接）----
    dict(key="ah_gsgg", name="安徽省药监局-公示公告", level="通告", kind="urllib",
         url="https://mpa.ah.gov.cn/zwgz/gsgg/index.html", expect=None,
         device_only=True, exclude_national=True),
]

# 已知抓不到的，写在这里免得每次都以为是程序坏了
KNOWN_BLOCKED = {
    "gd_fgjd": "广东省局-法规解读：列表由 JS 渲染，静态抓取取不到（0 条）",
    "nidc_fljd": "中检院分类界定：www.nifdc.org.cn 公共 DNS 返回 NXDOMAIN，域名已不存在",
}

# 这些栏目的差额是「故意剔除」造成的，不是漏抓。跑 --full 也追不回来，别劝人去全量。
KNOWN_FILTERED = {
    "nmpa_ggtg": "故意剔除批准注册名单、标准类公告、复审结果（不是法规）",
}

# 省局栏目混着药品/化妆品/药典，只留器械相关
DEVICE_RE = re.compile(r"器械|医用|体外诊断|UDI|唯一标识|义齿|植入|敷料|诊断试剂")

# 省局栏目里"转发国家局"的，不收（库里已有国家局原文，收重了没意义）
NATIONAL_RE = re.compile(r"^(国家药监局|国家食品药品|国家市场监督管理总局|市场监管总局|国务院|"
                         r"器审中心|国家局|中检院|国家卫生健康委|关于《|关于印发《国家)")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 解析
def page_url(home, idx):
    """第 N 页地址。注意两种结尾：xxx/index.html → xxx/index_N.html；xxx/ → xxx/index_N.html
    （河南等站 index_1.html 就代表第 2 页，与 idx 正好对上）"""
    if idx == 0:
        return home
    if "index.html" in home:
        return home.replace("index.html", f"index_{idx}.html")
    if home.endswith("/"):
        return home + f"index_{idx}.html"
    return home


def parse_li(html, base):
    """通用 <li><a> 列表解析（CMDE / NMPA）"""
    out = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", html, re.S):
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        if not m:
            continue
        href, raw = m.group(1), m.group(2)
        title = re.sub(r"<[^>]+>", "", raw)
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 8:
            continue
        if href.startswith(("javascript:", "#")):
            continue
        # 排除栏目索引页（如 /zcwj91/index.html），但保留详情页（如 /zcwj91/744126913/index.html）
        _parts = href.rstrip("/").split("/")
        if _parts[-1] == "index.html" and not re.search(r"\d{5,}", _parts[-2] if len(_parts) > 1 else ""):
            continue
        d = re.search(r"(20\d{2}-\d{2}-\d{2})", li)
        mm = re.search(r"（((?:20\d{2}年)?第?\s*\d+\s*号)）|(第\s*\d+\s*号)", title)
        docno = next((g for g in (mm.groups() if mm else []) if g), "")
        out.append({"title": title, "date": d.group(1) if d else "",
                    "docno": docno, "url": urljoin(base, href)})
    return out


def parse_gd(html, base):
    """广东省药监局：<li><h4><a title=...>...</a><span class=time>[日期]</span>"""
    out = []
    for m in re.finditer(
        r'<li>\s*<h4>\s*<a href="([^"]+)"[^>]*title="([^"]*)"[^>]*>.*?</a>\s*'
        r'<span class="time">\[\s*(\d{4}-\d{2}-\d{2})\s*\]</span>', html, re.S):
        href, title, date = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"<br\s*/?>", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        out.append({"title": title, "date": date, "docno": "", "url": urljoin(base, href)})
    return out


def parse_sh(html, base):
    """上海：<li class="list-ban"><a href title>…</a><small class="listTime">2026.11.20</small>"""
    out = []
    for li in re.findall(r'<li class="list-ban[^"]*">(.*?)</li>', html, re.S):
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]*)"', li)
        if not m:
            continue
        href, title = m.group(1), m.group(2).strip()
        if len(title) < 8:
            continue
        d = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", li)
        date = ""
        if d:
            y, mo, dd = d.group(1), int(d.group(2)), int(d.group(3))
            if 1 <= mo <= 12 and 1 <= dd <= 31:
                date = f"{y}-{mo:02d}-{dd:02d}"
        out.append({"title": re.sub(r"\s+", " ", title), "date": date, "docno": "",
                    "url": urljoin(base, href)})
    return out


def crawl_zj(col, known, mode):
    """浙江：政务云 jpaas 发布接口，返回 JSON 里带 HTML 片段"""
    from urllib.parse import quote
    import json as _json
    u = ("https://mpa.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?"
         f"parseType=bulidstatic&webId={col['webid']}&tplSetId={col['tpl']}&pageType=column"
         f"&tagId={quote(col['tag'])}&editType=null&pageId={col['pid']}")
    raw = fetch_urllib(u)
    if not raw:
        return [], 0, 0, "接口打不开"
    try:
        html = (_json.loads(raw).get("data") or {}).get("html") or ""
    except Exception:
        return [], 0, 0, "接口返回不是 JSON"
    rows, seen = [], set()
    for li in re.findall(r"<li[^>]*>(.*?)</li>", html, re.S):
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*?title="([^"]*)"', li)
        if not m:
            continue
        href, title = m.group(1), m.group(2).strip()
        if len(title) < 8:
            continue
        url = urljoin("https://mpa.zj.gov.cn", href)
        if url in known or url in seen:
            continue
        seen.add(url)
        d = re.search(r"(\d{4})-(\d{2})-(\d{2})", li)
        rows.append({"title": re.sub(r"\s+", " ", title), "date": d.group(0) if d else "",
                     "docno": "", "url": url,
                     "col": col["key"], "col_name": col["name"], "level": col["level"]})
    return rows, len(rows), 1, ""


def total_pages(html):
    m = re.search(r"共\s*([\d,]+)\s*页", html)
    return int(m.group(1).replace(",", "")) if m else 1


def maxpage_links(html):
    """广东省局不分「共N页」，分页靠 index_1.html … index_N.html 的链接数"""
    nums = [int(x) for x in re.findall(r"index_(\d+)\.html", html)]
    return max(nums) + 1 if nums else 1


def declared(html):
    m = re.search(r"共\s*([\d,]+)\s*条", html)
    return int(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------- 抓取
def fetch_scrapling(sess, url, tries=3):
    for i in range(tries):
        try:
            p = sess.fetch(url, timeout=45000)
            h = p.html_content if hasattr(p, "html_content") else str(p.body)
            if h and len(h) > 3000:
                return h
        except Exception as e:
            if i == tries - 1:
                log(f"   ✗ {url} {type(e).__name__}: {str(e)[:60]}")
        time.sleep(1.5)
    return ""


def fetch_urllib(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            if i == tries - 1:
                log(f"   ✗ {url} {type(e).__name__}: {str(e)[:60]}")
        time.sleep(1)
    return ""


JSU_WEBNAME = "%E6%B1%9F%E8%8B%8F%E7%9C%81%E8%8D%AF%E5%93%81%E7%9B%91%E7%9D%A3%E7%AE%A1%E7%90%86%E5%B1%80"  # 江苏省药品监督管理局


def crawl_jiangsu(col, known, mode):
    """江苏用的是大汉 jpage 接口：一次返回约 60 条，按实际返回条数步进翻页"""
    head = "https://da.jiangsu.gov.cn/module/web/jpage/dataproxy.jsp?"
    tail = (f"&col=1&appid=1&webid={col['webid']}&path=/&columnid={col['cid']}"
            f"&unitid={col['uid']}&webname={JSU_WEBNAME}&permissiontype=0")
    base = "https://da.jiangsu.gov.cn"

    fresh_all, seen, total, start, rounds, failed = [], set(), 0, 1, 0, []
    while rounds < 40:
        url = head + f"startrecord={start}&endrecord={start + 59}&perpage=60" + tail
        html = fetch_urllib(url)
        if not html:
            failed.append(rounds + 1)
            break
        m = re.search(r"<totalrecord>(\d+)</totalrecord>", html)
        if m:
            total = int(m.group(1))
        batch = []
        for rec in re.findall(r"<record><!\[CDATA\[(.*?)\]\]></record>", html, re.S):
            mm = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', rec, re.S)
            if not mm:
                continue
            t = re.sub(r"<[^>]+>", "", mm.group(2))
            t = re.sub(r"\s+", " ", t).replace("·", " ").strip()
            if len(t) < 6:
                continue
            d = re.search(r"(20\d{2}-\d{2}-\d{2})", rec)
            mm2 = re.search(r"（((?:20\d{2}年)?第?\s*\d+\s*号)）", t)
            batch.append({"title": t, "date": d.group(1) if d else "",
                          "docno": mm2.group(1) if mm2 else "",
                          "url": urljoin(base, mm.group(1)),
                          "col": col["key"], "col_name": col["name"], "level": col["level"]})
        new = [r for r in batch if r["url"] not in known and r["url"] not in seen]
        for r in new:
            seen.add(r["url"])
        fresh_all += new
        rounds += 1
        if not batch:
            break
        if mode == "incr" and not new:
            break
        start += len(batch)
        if total and start > total:
            break
        time.sleep(0.2)
    err = f"第 {'/'.join(map(str, failed))} 批抓取失败" if failed else ""
    return fresh_all, total, rounds, err


def parse_beijing(html, base, col):
    """北京是 easysite 模板：<li class="easysite-article-li"> 里 a 的 title 属性才是完整标题"""
    out = []
    for li in re.findall(r'<li class="easysite-article-li[^"]*">(.*?)</li>', html, re.S):
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*?\stitle="([^"]*)"', li)
        if not m:
            continue
        href, title = m.group(1), m.group(2).strip()
        if not title:
            continue
        d = re.search(r"(\d{4}-\d{2}-\d{2})", li)
        out.append({"title": re.sub(r"\s+", " ", title), "date": d.group(1) if d else "",
                    "docno": "", "url": urljoin(base, href),
                    "col": col["key"], "col_name": col["name"], "level": col["level"]})
    return out


def crawl_beijing(col, known, mode):
    """北京：静态 HTML 能抓到，分页是 <前缀>-N.html"""
    url = col["url"]
    html = fetch_urllib(url)
    if not html:
        return [], 0, 0, "首页打不开"

    m = re.search(r"<b>(\d+)条</b>", html)
    total = int(m.group(1)) if m else 0
    pm = re.search(r"/([0-9a-f]{6,})-\d+\.html", html)
    prefix = pm.group(1) if pm else ""

    def page_url(i):
        if i == 1:
            return url
        if not prefix:
            return ""
        return url.replace("index.html", f"{prefix}-{i}.html")

    pages = max(1, (total + 9) // 10) if total else 1
    fresh_all, seen, failed = [], set(), []
    i = 1
    while i <= pages and i <= 40:
        u = page_url(i)
        if not u:
            break
        h = html if i == 1 else fetch_urllib(u)
        if not h:
            failed.append(i)
            i += 1
            continue
        items = parse_beijing(h, u, col)
        fresh = [r for r in items if r["url"] not in known and r["url"] not in seen]
        for r in fresh:
            seen.add(r["url"])
        fresh_all += fresh
        if mode == "incr" and not fresh:
            break
        i += 1
        time.sleep(0.3)
    err = f"第 {'/'.join(map(str, failed))} 页抓取失败" if failed else ""
    return fresh_all, total, pages, err


# 公告通告栏目：批准注册结果是办理结果（不是规定），标准类用户明确不要
RESULT_ONLY_RE = re.compile(r"关于批准注册\s*\d+\s*个医疗器械产品")
# 早期公告还写作"批准注册N个医疗器械产品公告（无『的』字）"
STANDARD_TITLE_RE = re.compile(
    r"发布.{0,40}(?:YY|行业标准|国家标准)|标准修改单|标准立项|标准废止|废止.{0,20}标准"
    r"|标准复审|标准化技术|归口单位|信息化标准|标准制修订")


def apply_filters(col, rows):
    """省局栏目：剔掉转发的国家局文件 + 只留器械相关"""
    if col.get("exclude_national"):
        rows = [r for r in rows if not NATIONAL_RE.match(r["title"])]
    if col.get("device_only"):
        rows = [r for r in rows if DEVICE_RE.search(r["title"])]
    if col.get("drop_result"):
        rows = [r for r in rows if not RESULT_ONLY_RE.search(r["title"])
                and not STANDARD_TITLE_RE.search(r["title"])]
    return rows


def crawl(col, known, mode, sess):
    """返回 (新条目列表, 官方声明条数, 页数, 错误说明)"""
    if col["kind"] == "js_jiangsu":
        return crawl_jiangsu(col, known, mode)
    if col["kind"] == "beijing":
        return crawl_beijing(col, known, mode)
    if col["kind"] == "zj_jpaas":
        return crawl_zj(col, known, mode)

    get = (lambda u: fetch_scrapling(sess, u)) if col["kind"] == "scrapling" else fetch_urllib
    # 广东的列表结构是 <li><h4><a title=…>，其他站是普通 <li><a>
    parse = {"gd": parse_gd, "sh": parse_sh}.get(col.get("parser"), parse_li)

    home = col["url"]
    html = get(home)
    if not html:
        return [], None, 0, "首页打不开（被拦或网络不通）"
    # NMPA 反爬会回 412 空壳页（约 5KB，长度判据拦不住），解析出来 0 条 →
    # 页数算成 0 → 整栏静默 0 条、退出码还正常。解析 0 条就重抓两次，两次都空就明说。
    for _ in range(2):
        if parse(html, home):
            break
        html = get(home)
    if not parse(html, home):
        return [], None, 0, "首页解析 0 条（疑似反爬空壳页）"

    pages = total_pages(html) if col["kind"] == "scrapling" else maxpage_links(html)
    # 有些站的分页链接是 JS 生成的、源码里没有；栏目配置里写死页数兜底
    if col.get("pages") and pages <= 1:
        pages = col["pages"]
    dec = declared(html)
    fresh_all, seen, failed = [], set(), []

    i = 0
    while i < pages:
        h = html if i == 0 else get(page_url(home, i))
        if not h:
            failed.append(i + 1)
            i += 1
            continue
        items = parse(h, page_url(home, i))
        if col.get("device_only") or col.get("exclude_national") or col.get("drop_result"):
            items = apply_filters(col, items)
        fresh = []
        for it in items:
            if it["url"] in known or it["url"] in seen:
                continue
            seen.add(it["url"])
            it.update(col=col["key"], col_name=col["name"], level=col["level"])
            fresh.append(it)
        fresh_all += fresh

        # 增量模式：整页没有新东西 → 后面的都是旧的，停
        if mode == "incr" and not fresh:
            break
        i += 1
        if i % 5 == 0:
            log(f"   {col['name']} {i}/{pages} 页，本栏新增 {len(fresh_all)}")
        time.sleep(0.3)

    err = f"第 {'/'.join(map(str, failed))} 页抓取失败" if failed else ""
    return fresh_all, dec, pages, err


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="中国医械法规库 · 抓取")
    ap.add_argument("--data", default=DEFAULT_DATA, help="数据文件 jsonl 路径")
    ap.add_argument("--full", action="store_true", help="强制全量重抓")
    ap.add_argument("--only", default="", help="只抓这些栏目 key（逗号分隔）")
    args = ap.parse_args()

    data = os.path.abspath(args.data)
    os.makedirs(os.path.dirname(data), exist_ok=True)

    known = set()
    if os.path.exists(data):
        with open(data, encoding="utf-8") as f:
            for line in f:
                try:
                    known.add(json.loads(line)["url"])
                except Exception:
                    pass

    mode = "full" if (args.full or len(known) < 200) else "incr"
    log(f"数据文件 {data}")
    log(f"已有 {len(known)} 条 → 本次走【{'全量' if mode == 'full' else '增量'}】")
    if mode == "full":
        log("全量会翻完每个栏目的所有页，通常 20-30 分钟，请耐心等或用后台跑")

    cols = COLUMNS
    if args.only:
        want = {k.strip() for k in args.only.split(",") if k.strip()}
        cols = [c for c in COLUMNS if c["key"] in want]
        if not cols:
            log(f"没有匹配的栏目：{args.only}")
            return 2

    sess = None
    if any(c["kind"] == "scrapling" for c in cols):
        try:
            from scrapling.fetchers import StealthySession
            sess = StealthySession(headless=True)
            sess.__enter__()
        except ImportError:
            log("✗ 缺少 scrapling：先跑  pip install \"scrapling[fetchers]\"  然后  scrapling install")
            return 2
        except Exception as e:
            log(f"✗ 启动无头浏览器失败：{type(e).__name__}: {e}")
            return 2

    report, total_new = [], 0
    try:
        for col in cols:
            log(f"→ {col['name']}")
            new, dec, pages, err = crawl(col, known, mode, sess)
            raw_n = len(new)
            new = apply_filters(col, new)
            if col.get("device_only"):
                log(f"   抓到 {raw_n} 条 → 器械相关 {len(new)} 条")
            total_new += len(new)
            if new:
                with open(data, "a", encoding="utf-8") as f:
                    for it in new:
                        f.write(json.dumps(it, ensure_ascii=False) + "\n")
                        known.add(it["url"])
            report.append(dict(name=col["name"], key=col["key"], new=len(new),
                               dec=dec, pages=pages, err=err,
                               filtered=bool(col.get("device_only")), raw=raw_n))
            flag = "" if not err else f"  ⚠ {err}"
            log(f"   新增 {len(new)} 条｜官方声明 {dec if dec is not None else '未标注'}｜共 {pages} 页{flag}")
    finally:
        if sess is not None:
            try:
                sess.__exit__(None, None, None)
            except Exception:
                pass

    # ---------------- 汇总与对账
    lib = {}
    with open(data, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                lib[r.get("col", "")] = lib.get(r.get("col", ""), 0) + 1
            except Exception:
                pass

    print("\n" + "=" * 60)
    log(f"本次新增 {total_new} 条，库内总计 {len(known)} 条")
    bad = []
    for r in report:
        n = lib.get(r["key"], 0)
        dec = r["dec"]
        if r.get("filtered"):
            verdict = f"—（栏目共抓到 {r['raw']} 条，器械相关 {r['new']} 条）"
        elif dec is None:
            verdict = "—"
        elif n == dec:
            verdict = "✓ 与官方一致"
        elif n < dec:
            if r["key"] in KNOWN_FILTERED:
                verdict = f"✓ 比官方少 {dec - n} 条 —— {KNOWN_FILTERED[r['key']]}"
            else:
                verdict = f"⚠ 比官方少 {dec - n} 条（建议 --full 复核）"
                bad.append(r["name"])
        else:
            verdict = f"✓ 比官方多 {n - dec} 条（正常，含历史/去重前条目）"
        print(f"  {r['name']:<26} 库内 {n:>5}  官方 {dec if dec is not None else '—':>6}  {verdict}"
              + (f"  [{r['err']}]" if r["err"] else ""))
    for k, why in KNOWN_BLOCKED.items():
        print(f"  （已知抓不到）{why}")
    if bad:
        print("\n⚠ 以下栏目库内条数少于官方声明，说明有漏：" + "、".join(bad))
        print("  先跑 --full 复核；仍少就查是否官方补发了旧条目")
    print("=" * 60)
    log("抓取完成，接着跑 build.py 生成页面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
