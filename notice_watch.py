#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice_watch —— 违规通报监测（Hermes 复刻版）

定位
----
监测国家药监局及重点省市药监/市场监管公开网站中的【违规通报类】监管信息变化：
飞检通告、暂停生产/经营、停产整改、行政处罚、风险控制、召回、曝光台/黑名单。

明确不是医疗器械「不良事件监测」系统（那是 ADR 上报体系，另一套数据源）。

设计要点（与 OpenClaw 版 run.ps1 v0.2.6 的差异，均为修复性质）
------------------------------------------------------------------
1. 抓取改用 Python 标准库 urllib + OpenSSL。原版 PowerShell 5.1 走 .NET
   Framework 的 TLS 栈，个别政务站（广西 yjj.gxzf.gov.cn）握手直接失败；
   Python 侧还能给出「到底是证书问题还是站点离线」的可读错误。
2. 输出目录统一到本技能自己的 logs/，不再散落到两个互不一致的位置。
3. 命中口径 = 飞检 / 不良事件 / 行政处罚 三类，与原版 v0.2.1 一致；
   但「不良事件」关键词单独标注，避免与 ADR 监测混淆。
4. 所有状态（已见条目、上次页面哈希）按「目标」分目录存，支持增量。

用法
----
    python notice_watch.py doctor                    # 诊断配置与环境
    python notice_watch.py watch                     # 单次监测
    python notice_watch.py watch --days 7            # 只看最近 7 天
    python notice_watch.py watch --no-proxy          # 不走系统代理
    python notice_watch.py watch --region 广东,上海    # 只跑指定地区
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import socket
import ssl
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------
# 路径约定：技能根 = 本文件所在目录
# --------------------------------------------------------------------------
SKILL_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_ROOT / "configs" / "notice_targets.csv"
LOGS_DIR = SKILL_ROOT / "logs"
STATE_DIR = SKILL_ROOT / "state"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# --------------------------------------------------------------------------
# 命中口径 —— 三类。这是本技能的核心判据，改这里等于改业务口径。
# --------------------------------------------------------------------------
HIT_RULES: list[tuple[str, str]] = [
    # (命中类型, 正则)
    ("飞检", r"飞行检查|飞检|检查情况通告|检查结果通告|医疗器械.{0,10}检查结果"),
    ("不良事件", r"医疗器械不良事件|不良事件监测|严重不良事件|不良事件报告|安全警戒|警戒快讯|风险信号"),
    ("行政处罚",
     r"行政处罚|处罚决定|处罚决定书|行政处罚决定书|处罚案件信息公开|罚〔|药监.{0,6}罚|械罚|没收违法所得|罚款"),
    # 以下两类是从 NMPA 实际公报里发现的漏项：
    # 「监督抽检结果通告」= 查出不合格产品，监管属性比飞检还强，但原本完全没被捕获。
    ("监督抽检", r"监督抽检|抽检结果|质量抽检|抽验结果|检验不合格|抽检不合格"),
    # 「注销注册证书公告」= 产品退市，同类还有召回、撤销、停产整改
    # 注意排除「执业药师注册证」——那是人员资格证，不是医疗器械产品证，是另一码事
    ("注册证注销", r"(?!.*执业药师)注销.{0,12}注册证书|(?!.*执业药师)撤销.{0,10}注册证|注销医疗器械|停产整改|暂停生产|责令停产|恢复生产"),
]

# 标题噪声：导航、页眉页脚、模板占位符
NOISE_EXACT = {
    "首页", "政务公开", "网上办事", "公众参与", "智慧监管", "邮箱", "手机版", "电脑版",
    "返回首页", "更多", "更多>>", "详情", "查看", "上一页", "下一页", "尾页",
    "机构概况", "新闻动态", "信息公开", "服务大厅", "互动交流", "专题专栏",
    "政府信息公开", "领导信息", "政策法规", "政策解读", "通知公告", "公告通告",
}

# 误报过滤器 —— 这些是「提到监管词的采购/招标/人事/培训类公告」，不是违规通报。
# 实例：福建「体外诊断试剂不良事件报告辅助收集服务询价比选通知书」
#       含「不良事件」三字，但本质是买服务的采购公告。
FALSE_POSITIVE_PATTERNS = [
    r"询价|比选|招标|中标|成交公告|采购公告|采购项目|竞争性磋商|单一来源",
    r"招聘|招录|面试|笔试|录用公示",
    r"培训通知|培训班|研修班",
    r"征求意见稿|意见的函|公开征求意见",
    r"预算|决算|绩效评价",
    r"中标结果|合同公告|验收公告",
    # 备案 / 版权等页脚信息，常被当成"标题"抓进来（浙江站曾因此骗过空页判断）
    r"ICP备|公网安备|版权所有|网站地图|举报热线|无障碍",
    # 栏目名 / 导航入口：NMPA 侧边栏会以无日期长标题混进列表
    r"^政府信息公开专栏$|^医疗器械召回$|^化妆品抽检通告$|^药品抽检通告$",
]

DATE_PATTERNS = [
    re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"),
    re.compile(r"20\d{6}"),
]

MOJIBAKE_MARKS = ("é", "æ", "ç", "ï¼", "Ã", "â€")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Target:
    region: str
    category: str
    name: str
    url: str
    keywords: list[str]
    enabled: bool = True

    @property
    def key(self) -> str:
        raw = f"{self.region}_{self.category}_{self.name}"
        return re.sub(r'[\\/:*?"<>|\s]+', "_", raw)[:120]


@dataclass
class Item:
    title: str
    detail_url: str
    date: str = ""
    hit_types: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    item_id: str = ""

    def __post_init__(self):
        if not self.item_id:
            basis = self.detail_url or self.title
            self.item_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass
class TargetResult:
    target: Target
    status: str = "OK"            # OK / BLOCKED / ERROR
    error: str = ""
    page_changed: bool = False
    link_count: int = 0
    items: list[Item] = field(default_factory=list)
    new_items: list[Item] = field(default_factory=list)
    proxy_used: str = ""
    elapsed: float = 0.0
    rendered: str = ""                      # "" / follow-redirect / js
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def html_to_text(html: str) -> str:
    """粗粒度去标签 + 去多余空白，够用于标题清洗。"""
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\s+", " ", s).strip()


META_CHARSET = re.compile(rb"""(?i)<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""")
DECL_CHARSET = re.compile(rb"""(?i)encoding\s*=\s*["']?\s*([\w\-]+)""")

CHARSET_ALIAS = {
    "gb2312": "gb18030", "gbk": "gb18030", "gb_2312-80": "gb18030",
    "x-gbk": "gb18030", "utf8": "utf-8",
}


def mojibake_score(text: str) -> int:
    """乱码评分：分越高越像乱码。用于在候选编码里挑最干净的那个。"""
    if not text:
        return 9999
    bad = sum(text.count(m) for m in MOJIBAKE_MARKS)
    bad += text.count("\ufffd")
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    # 有正常汉字且乱码符号少 → 分数低
    return bad * 10 - min(cjk, 200)


def decode_bytes(raw: bytes, content_type: str = "") -> str:
    """
    解码网页字节。

    陷阱：不少政务站声明的 charset 是错的（广东 mpa.gd.gov.cn 声明 utf-8，
    实际内容是 GBK）。所以不能盲信声明——所有候选都试一遍，
    谁解出来的乱码少、汉字多就用谁。
    """
    candidates: list[str] = []

    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))

    head = raw[:4096]
    for rx in (META_CHARSET, DECL_CHARSET):
        mm = rx.search(head)
        if mm:
            candidates.append(mm.group(1).decode("ascii", "ignore"))

    # 兜底候选一定要有 gb18030（兼容 GBK/GB2312）
    candidates += ["utf-8", "gb18030"]

    seen: set[str] = set()
    results: list[tuple[int, int, str]] = []
    for idx, c in enumerate(candidates):
        c = CHARSET_ALIAS.get(c.lower().strip(), c.lower().strip())
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            txt = raw.decode(c, "replace")
        except LookupError:
            continue
        # 排序键：(乱码分, 候选顺序) —— 分数相同时优先声明的编码
        results.append((mojibake_score(txt), idx, txt))

    if not results:
        return raw.decode("utf-8", "replace")
    results.sort(key=lambda x: (x[0], x[1]))
    return results[0][2]


def abs_url(href: str, base: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.lower().startswith(("javascript:", "mailto:", "#")):
        return ""
    return urllib.parse.urljoin(base, href).split("#")[0]


# --------------------------------------------------------------------------
# 日期处理
# --------------------------------------------------------------------------
def parse_date(text: str) -> str:
    """从一段文本里抠出第一个合法日期，统一成 yyyy-MM-dd。"""
    if not text:
        return ""
    for rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            raw = m.group(0)
            s = raw.replace("年", "-").replace("月", "-").replace("日", "")
            s = s.replace("/", "-").replace(".", "-").strip("-")
            parts = s.split("-")
            try:
                if len(parts) == 1 and len(parts[0]) == 8:
                    y, mo, d = parts[0][:4], parts[0][4:6], parts[0][6:]
                elif len(parts) == 3:
                    y, mo, d = parts
                else:
                    continue
                dt = datetime(int(y), int(mo), int(d))
                if 2000 <= dt.year <= 2100:
                    return dt.strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                continue
    return ""


# --------------------------------------------------------------------------
# 命中判定
# --------------------------------------------------------------------------
def hit_types(text: str) -> list[str]:
    """
    按 HIT_RULES 判定命中类型，返回去重后的列表。

    先过误报过滤：采购/招标/招聘/培训类公告即使含监管词也不算违规通报。
    """
    s = text or ""
    for pat in FALSE_POSITIVE_PATTERNS:
        if re.search(pat, s):
            return []
    out: list[str] = []
    for name, pattern in HIT_RULES:
        if re.search(pattern, s) and name not in out:
            out.append(name)
    return out


def matched_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    s = text or ""
    return [k for k in keywords if k and k in s]


def is_title_candidate(title: str, url: str) -> bool:
    """判断一个 <a> 文本是否像真正的公告标题。"""
    t = (title or "").strip()
    if len(t) < 6:
        return False
    if "{{" in t or "}}" in t or "#if" in t or "showName" in t:
        return False
    if t.lower() == "title" and len(t) < 20:
        return False
    if t in NOISE_EXACT:
        return False
    # 页脚备案号之类：标题里没有公告词、却是纯备案信息，直接丢
    if re.search(r"ICP备|公网安备|版权所有", t):
        return False
    if any(m in t for m in MOJIBAKE_MARKS):
        return False

    has_date = bool(DATE_PATTERNS[0].search(t))
    has_word = re.search(
        r"公告|通告|通知|公示|处罚|飞行检查|检查|整改|暂停|停产|召回|风险控制|恢复|黑名单|曝光|案件|信息公开",
        t,
    )
    has_article_url = bool(re.search(r"\.html?$|content|art|col|info", url or "", re.I))
    return bool(has_date or has_word or (len(t) >= 14 and has_article_url))


# --------------------------------------------------------------------------
# 链接提取
# --------------------------------------------------------------------------
LINK_RX = re.compile(
    r"""(?is)<a\b[^>]*?href\s*=\s*["'](?P<href>[^"']+)["'][^>]*>(?P<text>.*?)</a>"""
)


def extract_items(html: str, base_url: str, keywords: list[str]) -> list[Item]:
    """
    从列表页 HTML 抽公告条目。

    日期来源按可靠性排序：
      1) 链接自身的 title 属性（gov 站点常带完整日期）
      2) <a> 之后紧跟的一小段文本（列表页常见结构）
      3) 标题文本本身
    """
    items: list[Item] = []
    seen_ids: set[str] = set()

    for m in LINK_RX.finditer(html):
        href = m.group("href")
        raw_inner = m.group("text")

        # title 属性（gov 站点常用 <a href=... title="2026-08-18"）
        full_tag = m.group(0)
        tm = re.search(r"""title\s*=\s*["']([^"']+)["']""", full_tag, re.I)
        attr_title = tm.group(1) if tm else ""

        title = html_to_text(raw_inner) or html_to_text(attr_title)
        url = abs_url(href, base_url)
        if not url or not is_title_candidate(title, url):
            continue

        # 日期候选
        date = ""
        if attr_title:
            date = parse_date(attr_title)
        if not date:
            tail = html[m.end(): m.end() + 300]
            date = parse_date(html_to_text(tail))
        if not date:
            date = parse_date(title)

        item = Item(title=title, detail_url=url, date=date)
        if item.item_id in seen_ids:
            continue
        seen_ids.add(item.item_id)

        item.matched_keywords = matched_keywords(title + " " + url, keywords)
        item.hit_types = hit_types(title + " " + url)
        items.append(item)

    # 兜底：原文里没抓到日期时，用整个条目的上下文补一次
    return items


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
class FetchError(Exception):
    pass


def detect_system_proxy() -> str:
    """读系统/环境代理，返回 http://host:port 或空串。"""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v:
            return v
    # Windows 注册表
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
                if server and "=" not in server:
                    return f"http://{server}"
        except OSError:
            pass
    return ""


def fetch(url: str, timeout: int = 30, proxy: str = "") -> tuple[str, str]:
    """
    抓一个页面，返回 (html, 最终URL)。

    错误分类：
      BLOCKED —— 明确的反爬拦截（403/429/503 + 挑战页）
      ERROR   —— 其它失败，保留可读原因
    """
    handlers: list[urllib.request.BaseHandler] = []
    proxy_used = ""
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        proxy_used = proxy
    else:
        # 显式禁用，避免环境变量残留影响
        handlers.append(urllib.request.ProxyHandler({}))

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # 部分政务站只开 TLS1.2；个别老站点需要 legacy 重协商
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    try:
        # 江西等老政务站会触发 unsafe legacy renegotiation，否则握手直接失败。
        # Python 3.11 的 ssl 模块没有 OP_LEGACY_SERVER_CONNECT 常量，用 OpenSSL 数值 0x4。
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    except (AttributeError, ValueError):
        pass
    handlers.append(urllib.request.HTTPSHandler(context=ctx))

    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=HEADERS, method="GET")

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            encoding = (resp.headers.get("Content-Encoding", "") or "").lower()
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (403, 412, 429, 503):
            raise FetchError(f"BLOCKED {e.code} {e.reason}") from e
        raise FetchError(f"HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise FetchError(f"TIMEOUT {timeout}s") from e
        raise FetchError(f"NET {type(reason).__name__}: {reason}") from e
    except socket.timeout as e:
        raise FetchError(f"TIMEOUT {timeout}s") from e
    except Exception as e:  # noqa: BLE001 —— 兜底，保留类型名便于排查
        raise FetchError(f"{type(e).__name__}: {e}") from e

    # urllib 不会自动解压：必须自己处理 gzip/deflate，
    # 否则拿到的是压缩二进制，后续所有解析都是空的。
    raw = decompress(raw, encoding, ctype)

    html = decode_bytes(raw, ctype)
    return html, final_url


def decompress(raw: bytes, content_encoding: str, content_type: str) -> bytes:
    """按 Content-Encoding 解压；未声明时按魔数兜底嗅探。"""
    if not raw:
        return raw
    try:
        if "gzip" in content_encoding or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if "deflate" in content_encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "br" in content_encoding:
            try:
                import brotli  # type: ignore
                return brotli.decompress(raw)
            except ImportError:
                pass
    except (OSError, zlib.error):
        pass
    return raw


# --------------------------------------------------------------------------
# JS 渲染兜底
# --------------------------------------------------------------------------
def _find_scrapling():
    """找 scrapling 命令行：先看当前解释器同目录（venv/Scripts），再查 PATH。
    以前这里写死了一个绝对路径，别人机器上没有就永远用不了 JS 渲染兜底。"""
    cands = []
    py_dir = Path(sys.executable).parent
    for name in ("scrapling.exe", "scrapling"):
        cands.append(py_dir / name)
    for name in ("scrapling.exe", "scrapling"):
        p = shutil.which(name)
        if p:
            cands.append(Path(p))
    for c in cands:
        if c.exists():
            return c
    return None


SCRAPLING = _find_scrapling()


def grab_js_redirect_target(html: str, base_url: str) -> str:
    """广东等站点用脚本跳转，从 thisSrc / window.location 里抠出真实列表页。"""
    for rx in (
        r"""thisSrc\s*=\s*["']([^"']+)["']""",
        r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        r"""location\.replace\(\s*["']([^"']+)["']""",
    ):
        m = re.search(rx, html)
        if m:
            target = abs_url(m.group(1), base_url)
            if target:
                return target
    return ""


def fetch_rendered(url: str, timeout: int = 60, use_proxy: bool = True) -> str:
    """
    用 Scrapling 的无头浏览器渲染 JS 页面，返回渲染后的 HTML。

    只在静态抓取解析不出条目时调用——启动浏览器约 3-8 秒，不能无脑用。
    """
    if SCRAPLING is None or not SCRAPLING.exists():
        raise FetchError("JS_RENDER_UNAVAILABLE: 没找到 scrapling 命令行（pip install \"scrapling[fetchers]\" 后会有）")

    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "page.html"
        cmd = [str(SCRAPLING), "extract", "fetch", url, str(out),
               "--network-idle", "--disable-resources", "--ai-targeted"]
        env = dict(os.environ)
        if use_proxy:
            px = detect_system_proxy()
            if px:
                cmd += ["--proxy", px]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, env=env, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            raise FetchError(f"JS_RENDER_TIMEOUT {timeout}s") from e

        if not out.exists() or out.stat().st_size == 0:
            msg = (r.stderr or r.stdout or "").strip()[:300]
            raise FetchError(f"JS_RENDER_EMPTY: {msg}")
        return out.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_targets(path: Path) -> list[Target]:
    if not path.exists():
        raise SystemExit(f"配置文件不存在：{path}")
    targets: list[Target] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            enabled_raw = (row.get("Enabled") or "true").strip().lower()
            enabled = enabled_raw not in ("false", "0", "no", "n", "否")
            kws = [k.strip() for k in re.split(r"[,;，；|]", row.get("Keywords") or "") if k.strip()]
            targets.append(
                Target(
                    region=(row.get("Region") or "").strip(),
                    category=(row.get("Category") or "").strip(),
                    name=(row.get("Name") or "").strip(),
                    url=(row.get("Url") or "").strip(),
                    keywords=kws,
                    enabled=enabled,
                )
            )
    return targets


# --------------------------------------------------------------------------
# 状态：已见条目
# --------------------------------------------------------------------------
def state_file(target: Target) -> Path:
    return STATE_DIR / target.key / "seen_items.txt"


def load_seen(target: Target) -> set[str]:
    p = state_file(target)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}


def save_seen(target: Target, seen: set[str]) -> None:
    p = state_file(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")


def page_hash_file(target: Target) -> Path:
    return STATE_DIR / target.key / "last_page_hash.txt"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def watch(targets: list[Target], days: int, timeout: int, use_proxy: bool,
          include_unknown: bool, render_js: bool = True) -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (datetime.now() - timedelta(days=days)).date() if days > 0 else None

    proxy = detect_system_proxy() if use_proxy else ""

    results: list[TargetResult] = []
    all_new_hits: list[tuple[Target, Item]] = []
    all_hits: list[tuple[Target, Item]] = []

    for idx, t in enumerate(targets, 1):
        if not t.enabled:
            continue
        r = TargetResult(target=t)
        started = time.time()
        print(f"[{idx}/{len(targets)}] {t.region} / {t.category} / {t.name}", end=" ... ", flush=True)

        try:
            html, final_url = fetch(t.url, timeout=timeout, proxy=proxy)
            r.proxy_used = proxy
        except FetchError as e:
            msg = str(e)
            # 反爬拦截（412/403/503）不是"抓不到"，只是 urllib 特征被识别。
            # 切 Scrapling 的隐身浏览器——它有自己的 TLS 指纹和完整浏览器环境，
            # 实测 NMPA 的 412 在它这里返回 200。这是本技能最重要的兜底。
            if msg.startswith("BLOCKED") and render_js:
                try:
                    html = fetch_rendered(t.url, use_proxy=use_proxy)
                    final_url = t.url
                    r.rendered = "scrapling-anti-bot"
                    r.notes.append(f"urllib 被拦（{msg}），已用 Scrapling 绕过")
                except FetchError as e2:
                    r.status = "BLOCKED"
                    r.error = f"{msg}；Scrapling 兜底也失败：{e2}"
                    r.elapsed = time.time() - started
                    print(f"BLOCKED: {msg}（兜底失败）")
                    results.append(r)
                    continue
            else:
                r.status = "BLOCKED" if msg.startswith("BLOCKED") else "ERROR"
                r.error = msg
                r.elapsed = time.time() - started
                print(f"{r.status}: {msg}")
                results.append(r)
                continue

        # 站点已下线/未部署的判断：nginx/apache 默认页说明服务端没起来
        if re.search(r"Welcome to nginx|Welcome to Apache|It works!", html[:2000]):
            r.status = "DEAD"
            r.error = "SITE_NOT_DEPLOYED: 返回 Web 服务器默认页"
            r.elapsed = time.time() - started
            print(f"DEAD: 服务器默认页（站点未部署）")
            results.append(r)
            continue

        page_hash = sha256_text(html)
        hf = page_hash_file(t)
        old_hash = hf.read_text(encoding="utf-8").strip() if hf.exists() else ""
        r.page_changed = old_hash != page_hash

        items = extract_items(html, final_url or t.url, t.keywords)
        r.link_count = len(items)

        # 判断页面是否真的解析出了"公告列表"
        # 不能只看 items 是否为空——浙江站的页脚备案号会骗过空页判断，
        # 所以还要看有没有带日期、或带公告词的像样条目。
        def looks_like_listing(its: list[Item]) -> bool:
            if not its:
                return False
            dated = sum(1 for i in its if i.date)
            noticed = sum(
                1 for i in its
                if re.search(r"公告|通告|通知|公示|检查|处罚|整改|召回|案件", i.title)
            )
            return dated >= 2 or noticed >= 2

        # 静态解析不像列表页 → 两级兜底
        if not looks_like_listing(items):
            # ① 脚本跳转页：抠出真实列表页再抓一次
            redirect = grab_js_redirect_target(html, final_url or t.url)
            if redirect and redirect != (final_url or t.url):
                try:
                    html2, final2 = fetch(redirect, timeout=timeout, proxy=proxy)
                    items2 = extract_items(html2, final2 or redirect, t.keywords)
                    if looks_like_listing(items2):
                        html, final_url, items = html2, final2, items2
                        r.link_count = len(items)
                        r.rendered = "follow-redirect"
                except FetchError:
                    pass

            # ② 仍是空 → JS 渲染
            if not looks_like_listing(items) and render_js:
                try:
                    html3 = fetch_rendered(t.url, use_proxy=use_proxy)
                    items3 = extract_items(html3, t.url, t.keywords)
                    if looks_like_listing(items3):
                        html, items = html3, items3
                        r.link_count = len(items)
                        r.rendered = "js"
                    else:
                        r.notes.append(f"js_render 后仍无有效列表（{len(items3)} 条）")
                except FetchError as e:
                    r.notes.append(f"js_render: {e}")

        r.items = items

        seen = load_seen(t)
        for it in items:
            if it.item_id not in seen:
                r.new_items.append(it)

        # 命中项（只看回溯窗口内）
        def in_window(it: Item) -> bool:
            if cutoff is None:
                return True
            if not it.date:
                return include_unknown
            try:
                return datetime.strptime(it.date, "%Y-%m-%d").date() >= cutoff
            except ValueError:
                return include_unknown

        r.items = [it for it in items if in_window(it)]
        all_hits.extend((t, it) for it in r.items if it.hit_types)
        all_new_hits.extend(
            (t, it) for it in r.new_items if it.hit_types and in_window(it)
        )

        save_seen(t, seen | {it.item_id for it in items})
        if r.page_changed:
            hf.parent.mkdir(parents=True, exist_ok=True)
            hf.write_text(page_hash, encoding="utf-8")

        r.elapsed = time.time() - started
        print(f"OK links={r.link_count} hits={sum(1 for i in r.items if i.hit_types)} "
              f"new={len(r.new_items)} {r.elapsed:.1f}s")
        results.append(r)

    # ---- 输出 ----
    hits_csv = LOGS_DIR / f"notice_watch_hits_{stamp}.csv"
    items_csv = LOGS_DIR / f"notice_watch_items_{stamp}.csv"
    report_txt = LOGS_DIR / f"notice_watch_report_{stamp}.txt"
    latest_json = LOGS_DIR / "latest_result.json"

    with hits_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["RunTime", "Region", "Category", "SourceName", "HitType",
                    "Title", "Date", "DetailUrl", "MatchedKeywords", "ItemId"])
        for t, it in all_hits:
            w.writerow([run_time, t.region, t.category, t.name,
                        "|".join(it.hit_types), it.title, it.date,
                        it.detail_url, "|".join(it.matched_keywords), it.item_id])

    with items_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["RunTime", "Region", "Category", "SourceName", "Title",
                    "Date", "HitType", "DetailUrl", "ItemId"])
        for r in results:
            for it in r.items:
                w.writerow([run_time, r.target.region, r.target.category,
                            r.target.name, it.title, it.date,
                            "|".join(it.hit_types), it.detail_url, it.item_id])

    ok = sum(1 for r in results if r.status == "OK")
    blocked = sum(1 for r in results if r.status == "BLOCKED")
    errors = sum(1 for r in results if r.status == "ERROR")
    dead = sum(1 for r in results if r.status == "DEAD")
    changed = sum(1 for r in results if r.page_changed)

    # 站点下线/反爬属于"没法控制"，不算运行失败，只降级成 WARN
    hard_fail = errors + dead
    if hard_fail == 0 and blocked == 0:
        final = "PASS" if not all_new_hits else "CHECK"
    elif ok > 0:
        final = "WARN"
    else:
        final = "FAIL"

    lines = [f"notice_watch run @ {run_time}", f"LOOKBACK={days}d CUTOFF={cutoff or 'DISABLED'}",
             "", f"TARGETS={len(results)} OK={ok} BLOCKED={blocked} ERROR={errors} DEAD={dead}",
             f"PAGE_CHANGED={changed}",
             f"HIT_ITEMS={len(all_hits)} NEW_HITS={len(all_new_hits)}", ""]
    for r in results:
        lines += [
            f"[TARGET] {r.target.region} / {r.target.category} / {r.target.name}",
            f"STATUS={r.status}",
            f"RENDERED={r.rendered or 'static'}",
            f"PROXY_USED={r.proxy_used or 'none'}",
            f"PAGE_CHANGED={r.page_changed}",
            f"LINK_COUNT={r.link_count}",
            f"HIT_COUNT={sum(1 for i in r.items if i.hit_types)}",
            f"NEW_COUNT={len(r.new_items)}",
        ]
        if r.error:
            lines.append(f"ERROR={r.error}")
        for n in r.notes:
            lines.append(f"NOTE={n}")
        lines.append("")
    lines.append(f"FINAL: {final}")
    report_txt.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "run_time": run_time,
        "lookback_days": days,
        "cutoff": str(cutoff) if cutoff else "DISABLED",
        "summary": {
            "targets": len(results), "ok": ok, "blocked": blocked,
            "errors": errors, "dead": dead, "page_changed": changed,
            "hit_items": len(all_hits), "new_hits": len(all_new_hits),
        },
        "final": final,
        "outputs": {
            "hits_csv": str(hits_csv),
            "items_csv": str(items_csv),
            "report": str(report_txt),
        },
        "targets": [
            {
                "region": r.target.region, "category": r.target.category,
                "name": r.target.name, "url": r.target.url,
                "status": r.status, "error": r.error,
                "rendered": r.rendered, "notes": r.notes,
                "page_changed": r.page_changed, "link_count": r.link_count,
                "hit_count": sum(1 for i in r.items if i.hit_types),
                "new_count": len(r.new_items),
                "elapsed": round(r.elapsed, 2),
            }
            for r in results
        ],
        "new_hits": [
            {
                "region": t.region, "category": t.category, "source": t.name,
                "hit_types": it.hit_types, "title": it.title, "date": it.date,
                "url": it.detail_url,
            }
            for t, it in all_new_hits
        ],
    }
    latest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return payload


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def doctor() -> int:
    print("=== notice_watch doctor ===")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"技能根      : {SKILL_ROOT}")
    print(f"配置文件    : {CONFIG_PATH}  {'存在' if CONFIG_PATH.exists() else '缺失'}")
    print(f"输出目录    : {LOGS_DIR}")
    print(f"状态目录    : {STATE_DIR}")
    print(f"系统代理    : {detect_system_proxy() or '未检测到'}")

    if not CONFIG_PATH.exists():
        print("\n[FAIL] 配置文件缺失，后续测试跳过")
        return 1

    targets = load_targets(CONFIG_PATH)
    enabled = [t for t in targets if t.enabled]
    print(f"监测目标    : 共 {len(targets)} 个，启用 {len(enabled)} 个")
    by_cat: dict[str, int] = {}
    for t in enabled:
        by_cat[t.category] = by_cat.get(t.category, 0) + 1
    for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {k:<8} {v}")

    print("\n连通性抽测（每个地区第一源）:")
    tested: set[str] = set()
    for t in enabled:
        if t.region in tested:
            continue
        tested.add(t.region)
        try:
            html, _ = fetch(t.url, timeout=15, proxy=detect_system_proxy())
            n = len(extract_items(html, t.url, t.keywords))
            print(f"  [OK]      {t.region:<10} links={n}")
        except FetchError as e:
            print(f"  [FAIL]    {t.region:<10} {str(e)[:80]}")
    print("\nFINAL: DONE")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="违规通报监测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="诊断配置与环境")

    w = sub.add_parser("watch", help="单次监测")
    w.add_argument("--config", default=str(CONFIG_PATH))
    w.add_argument("--days", type=int, default=14, help="回溯天数，0=不限")
    w.add_argument("--timeout", type=int, default=30)
    w.add_argument("--no-proxy", action="store_true")
    w.add_argument("--include-unknown-date", action="store_true")
    w.add_argument("--no-js", action="store_true", help="关闭 JS 渲染兜底，只用静态抓取")
    w.add_argument("--region", default="", help="只跑指定地区，逗号分隔")

    args = ap.parse_args()

    if args.cmd == "doctor":
        return doctor()

    targets = load_targets(Path(args.config))
    if args.region:
        want = {x.strip() for x in args.region.split(",") if x.strip()}
        targets = [t for t in targets if any(w in t.region for w in want)]
        if not targets:
            print(f"没有匹配的地区：{args.region}")
            return 2

    payload = watch(
        targets,
        days=args.days,
        timeout=args.timeout,
        use_proxy=not args.no_proxy,
        include_unknown=args.include_unknown_date,
        render_js=not args.no_js,
    )

    s = payload["summary"]
    print("\n" + "=" * 56)
    print(f"目标 {s['targets']} | OK {s['ok']} | BLOCKED {s['blocked']} | ERROR {s['errors']}")
    print(f"命中项 {s['hit_items']} | 新增命中 {s['new_hits']} | 页面变化 {s['page_changed']}")
    print(f"命中表: {payload['outputs']['hits_csv']}")
    print(f"报告  : {payload['outputs']['report']}")
    print(f"FINAL: {payload['final']}")
    return 0 if payload["final"] in ("PASS", "CHECK") else 1


if __name__ == "__main__":
    sys.exit(main())
