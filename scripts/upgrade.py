#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把最新版覆盖安装到已装的中国医械法规库技能目录。升级 = 再装一次，默认覆盖。

新版从哪来，两条路都支持（AI 助手看手里有什么选一条）：

  一、从 GitHub 拉（默认）
      python upgrade.py --target "<已装目录>"
      python upgrade.py --target "<已装目录>" --proxy http://127.0.0.1:7890

  二、用别人发来的压缩包（那台电脑上不了 github 时走这条）
      python upgrade.py --target "<已装目录>" --from-zip "<中国医械法规库_V1.0.1.zip>"

  附带：用已经解压好的新版目录
      python upgrade.py --target "<已装目录>" --from-dir "<解压后的 china-medical-device-regs>"

  不带 --target 时，默认把「本脚本所在的技能目录」当成目标（已装好、在目录里跑的情况）。

铁律（代码保证，不靠嘱咐使用者）：
    · 只覆盖「代码 + 说明 + 配置 + 图片」白名单里的文件
    · logs/（抓来的通报命中表）、已生成的 *.html、__pycache__ 一律不删不改
    · data/cn_regs.jsonl 不整体覆盖，只把新版多出来的条目并进去（按 url 去重）
    · 覆盖前先把旧文件备份到 _backup/upgrade_<旧版本>_<时间>/，只留最近 3 份，出错可回退
    · 版本号相同但内容不同 → 照样覆盖（只比版本号会漏掉同名修订）
    · 下载/解压/校验任何一步失败，磁盘上一个字节都不动

版本号唯一真源 = SKILL.md frontmatter 的 version。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

DEFAULT_REPO = "https://github.com/Alynxiao/china-medical-device-regs"
ZIP_URL = DEFAULT_REPO + "/archive/refs/heads/main.zip"

# 覆盖白名单：只有这些会被新版替换（目录按整个目录同步）
COPY_DIRS = ["scripts", "templates", "configs", "references", "assets"]
COPY_FILES = [
    "SKILL.md", "README.md", "LICENSE", "run.py", "启动.bat",
    "使用说明.txt", "怎么装给AI助手.txt", "notice_watch.py", ".gitignore",
]
# 明确不动的东西（写在注释里当判据，代码里靠白名单天然实现）
#   logs/  data/  中国医械法规库_*.html  __pycache__  _backup/
KEEP_BACKUPS = 3          # _backup 下最多留几份
DATA_REL = os.path.join("data", "cn_regs.jsonl")


def log(m=""):
    print(m, flush=True)


def step(title):
    print("\n" + "=" * 58)
    print(" " + title)
    print("=" * 58, flush=True)


def read_version(skill_dir):
    """从 SKILL.md frontmatter 读 version；读不到返回 None。"""
    p = os.path.join(skill_dir, "SKILL.md")
    try:
        with open(p, encoding="utf-8-sig") as f:
            head = f.read(2000)
    except OSError:
        return None
    m = re.search(r"^version:\s*([0-9][0-9A-Za-z.\-]*)", head, re.M)
    return m.group(1) if m else None


def whitelisted_files(root):
    """白名单里实际存在的文件：相对路径 -> 绝对路径。"""
    out = {}
    for rel in COPY_FILES:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            out[rel] = p
    for d in COPY_DIRS:
        dd = os.path.join(root, d)
        if not os.path.isdir(dd):
            continue
        for name in os.listdir(dd):
            p = os.path.join(dd, name)
            if os.path.isfile(p):
                out[os.path.join(d, name)] = p
    return out


def signature(root):
    """白名单文件的整体指纹（留作日志/调试用）。"""
    h = hashlib.sha256()
    for rel, p in sorted(whitelisted_files(root).items()):
        h.update(rel.encode("utf-8"))
        h.update(open(p, "rb").read())
    return h.hexdigest()


def cmp_whitelist(src_root, dst_root):
    """按新版为准逐个比对：新版有的文件，本地是否都有且一致。

    返回 (缺失清单, 内容不同清单)。本地多出来的文件不算错——
    用户的包里可能少几张图、少一份 README，不能因此判失败。
    """
    missing, differ = [], []
    for rel, sp in sorted(whitelisted_files(src_root).items()):
        dp = os.path.join(dst_root, rel)
        if not os.path.isfile(dp):
            missing.append(rel)
            continue
        if hashlib.sha256(open(sp, "rb").read()).digest() != \
           hashlib.sha256(open(dp, "rb").read()).digest():
            differ.append(rel)
    return missing, differ


def find_skill_root(base, max_depth=3):
    """在解压结果里找真正含 SKILL.md + scripts/build.py 的那一层。

    兼容两种打包：GitHub 的 china-medical-device-regs-main/，
    以及分发包的中国医械法规库技能包/china-medical-device-regs/。
    """
    base = os.path.abspath(base)
    hits = []
    for root, dirs, files in os.walk(base):
        depth = 0 if root == base else os.path.relpath(root, base).count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        if "SKILL.md" in files and os.path.isfile(os.path.join(root, "scripts", "build.py")):
            hits.append(root)
    if not hits:
        return None
    hits.sort(key=lambda p: p.count(os.sep))
    return hits[0]


def extract_zip(zip_path, dest):
    """解压，并认 Windows 自带压缩/好压打出来的 GBK 中文文件名。

    没有 UTF-8 标志位的条目，zipfile 会按 cp437 解码成乱码，
    中文名文件（启动.bat / 使用说明.txt / 怎么装给AI助手.txt）就找不到了。
    """
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not (info.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("gbk")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            target = os.path.join(dest, name.replace("/", os.sep))
            target = os.path.abspath(target)
            if not target.startswith(os.path.abspath(dest) + os.sep):
                continue                      # 防 ../ 跳出目录
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def download(url, dest, proxy=None, tries=3, timeout=180):
    """下载到 dest。走系统代理变量；--proxy 可覆盖。失败重试 tries 次。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "china-medical-device-regs-upgrader")]
    last = None
    for i in range(1, tries + 1):
        try:
            log(f"  下载中（第 {i} 次）：{url}")
            with opener.open(url, timeout=timeout) as r, open(dest, "wb") as f:
                total = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            if total < 10000:
                raise IOError(f"只收到 {total} 字节，不像完整压缩包")
            log(f"  下载完成：{total:,} 字节")
            return True
        except Exception as e:                     # noqa: BLE001
            last = e
            log(f"  ✗ 失败：{type(e).__name__}: {e}")
            if i < tries:
                time.sleep(2 * i)
    log("")
    log("  ⚠ 拿不到新版。两条路任选：")
    log("     1) 走代理再试：加 --proxy http://127.0.0.1:7890（或先设 http_proxy/https_proxy）")
    log("     2) 改用压缩包：--from-zip \"<别人发来的那个 .zip>\"（不走网络）")
    log(f"  最后一次错误：{last}")
    return False


def backup(skill_dir, old_ver):
    """把将被覆盖的旧文件拷到 _backup/upgrade_<旧版本>_<时间>/，并只保留最近几份。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(skill_dir, "_backup", f"upgrade_{old_ver or 'unknown'}_{stamp}")
    n = 0
    for rel, src in whitelisted_files(skill_dir).items():
        dst = os.path.join(bdir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    dsrc = os.path.join(skill_dir, DATA_REL)
    if os.path.isfile(dsrc):
        os.makedirs(os.path.join(bdir, "data"), exist_ok=True)
        shutil.copy2(dsrc, os.path.join(bdir, DATA_REL))
        n += 1
    root = os.path.join(skill_dir, "_backup")
    if os.path.isdir(root):
        subs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
        for old in subs[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    log(f"  已备份 {n} 个文件到：{os.path.relpath(bdir, skill_dir)}")
    return bdir


def apply_files(src_root, skill_dir):
    """按白名单覆盖。返回 (覆盖数, 新增数, 出错清单)。

    单个文件被占用（比如页面开着、run.py 还在跑）不该让整轮崩在半路：
    记下来继续，后面复核会报出来，旧文件在 _backup/ 里。
    """
    replaced = added = 0
    failed = []
    for rel, src in whitelisted_files(src_root).items():
        dst = os.path.join(skill_dir, rel)
        existed = os.path.isfile(dst)
        try:
            os.makedirs(os.path.dirname(dst) or skill_dir, exist_ok=True)
            shutil.copy2(src, dst)
            replaced += existed
            added += (not existed)
        except OSError as e:
            failed.append(f"{rel}（{type(e).__name__}: {e.strerror or e}）")
    return replaced, added, failed


def merge_data(src_root, skill_dir):
    """data/cn_regs.jsonl 合并：保留本地全部行，只把远端多出来的 url 追加进去。"""
    s = os.path.join(src_root, DATA_REL)
    d = os.path.join(skill_dir, DATA_REL)
    if not os.path.isfile(s):
        return None
    os.makedirs(os.path.dirname(d), exist_ok=True)
    if not os.path.isfile(d):
        shutil.copy2(s, d)
        n = sum(1 for _ in open(d, encoding="utf-8"))
        return (0, n, n, "本地无数据，直接装入远端库")

    def load(p):
        rows, seen = [], set()
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = o.get("url") or o.get("u") or ""
                if u and u in seen:
                    continue
                if u:
                    seen.add(u)
                rows.append(o)
        return rows, seen

    mine, mine_urls = load(d)
    theirs, _ = load(s)
    extra = [o for o in theirs if (o.get("url") or o.get("u") or "") not in mine_urls]
    if not extra:
        return (len(mine), len(mine), 0, "远端没有比本地更全的条目")
    # 先备份本地数据
    shutil.copy2(d, d + ".bak_before_upgrade")
    with open(d, "a", encoding="utf-8") as f:
        for o in extra:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return (len(mine), len(mine) + len(extra), len(extra), "已追加远端新条目")


def main():
    ap = argparse.ArgumentParser(description="把最新版覆盖安装到已装的中国医械法规库技能")
    ap.add_argument("--target", help="已安装技能的根目录（含 SKILL.md）；不给就用本脚本所在技能目录")
    ap.add_argument("--from-zip", help="别人发来的压缩包 .zip；给了就不上网")
    ap.add_argument("--from-dir", help="已经解压好的新版目录；给了就不上网")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="仓库地址（默认 GitHub）")
    ap.add_argument("--proxy", help="代理地址，如 http://127.0.0.1:7890；不填就用系统 http_proxy/https_proxy")
    ap.add_argument("--dry", action="store_true", help="只检查版本和内容，不覆盖")
    ap.add_argument("--force", action="store_true", help="内容完全一致也强制覆盖")
    a = ap.parse_args()

    # ---- 1. 定位目标目录 ----
    if a.target:
        skill_dir = os.path.abspath(a.target)
    else:
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    step("第 1 步 / 5：找到已装的技能")
    log(f"  目录：{skill_dir}")
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        log("  ✗ 这个目录里没有 SKILL.md，不像已经装好的技能。")
        log("    请用 --target 指到那个文件夹（里面有 SKILL.md 和 scripts 子目录）。")
        return 2
    old_ver = read_version(skill_dir)
    log(f"  当前版本：{old_ver or '(读不到版本号)'}")

    # ---- 2. 取新版：本地包 / 本地目录 / GitHub ----
    tmp = tempfile.mkdtemp(prefix="mdr_upgrade_")
    try:
        if a.from_zip or a.from_dir:
            step("第 2 步 / 5：从本地取新版（不上网）")
            if a.from_zip:
                zp = os.path.abspath(a.from_zip)
                if not os.path.isfile(zp):
                    log(f"  ✗ 找不到压缩包：{zp}")
                    return 2
                log(f"  压缩包：{zp}（{os.path.getsize(zp):,} 字节）")
                try:
                    extract_zip(zp, tmp)
                except Exception as e:                 # noqa: BLE001
                    log(f"  ✗ 解压失败：{type(e).__name__}: {e}")
                    return 2
                base = tmp
            else:
                base = os.path.abspath(a.from_dir)
                log(f"  目录：{base}")
                if not os.path.isdir(base):
                    log("  ✗ 这个目录不存在")
                    return 2
            src_root = find_skill_root(base)
            if not src_root:
                log("  ✗ 没在里面找到含 SKILL.md + scripts/build.py 的目录，不像是这个技能。")
                log("    本机文件一个都没动。")
                return 2
            rel = os.path.relpath(src_root, base)
            log(f"  新版位置：{'(包根目录)' if src_root == base else rel}")
        else:
            step("第 2 步 / 5：从 GitHub 拉最新版")
            zip_path = os.path.join(tmp, "main.zip")
            url = a.repo.rstrip("/") + "/archive/refs/heads/main.zip"
            if not download(url, zip_path, proxy=a.proxy):
                log("\n没拿到新版，本机文件一个都没动。")
                return 2
            try:
                extract_zip(zip_path, os.path.join(tmp, "x"))
            except Exception as e:                     # noqa: BLE001
                log(f"  ✗ 解压失败：{type(e).__name__}: {e}\n本机文件一个都没动。")
                return 2
            src_root = find_skill_root(os.path.join(tmp, "x"))
            if not src_root:
                log("  ✗ 下载下来的包里没找到技能目录，不动本机文件。")
                return 2

        # ---- 3. 比版本 + 比内容 ----
        step("第 3 步 / 5：核对版本与内容")
        new_ver = read_version(src_root)
        log(f"  新版版本：{new_ver or '(读不到)'}")
        same_ver = bool(old_ver and new_ver and old_ver == new_ver)
        miss, diff = cmp_whitelist(src_root, skill_dir)
        if not miss and not diff:
            log("  本地文件与新版逐一对得上。")
            if not a.force:
                log("\n已经是最新，不用再覆盖。要强行覆盖加 --force。")
                return 0
        else:
            log(f"  版本{'相同' if same_ver else '不同'}：缺 {len(miss)} 个、内容不同 {len(diff)} 个 → 照常覆盖")
        if a.dry:
            log("\n(--dry 只检查) 没有覆盖任何文件。")
            return 0

        # ---- 4. 备份 + 覆盖 ----
        step("第 4 步 / 5：备份旧文件 → 覆盖代码和说明")
        backup(skill_dir, old_ver)
        replaced, added, failed = apply_files(src_root, skill_dir)
        log(f"  覆盖 {replaced} 个、新增 {added} 个")
        if failed:
            log(f"  ⚠ 有 {len(failed)} 个文件写不进去（可能程序正在用）：")
            for f in failed[:5]:
                log(f"    · {f}")
            log("    关掉正在用的窗口/程序后重新跑一次即可。")
        log("  未触动：logs/、已生成的网页、__pycache__")

        merged = merge_data(src_root, skill_dir)
        if merged:
            before, after, extra, why = merged
            log(f"  数据：{before} → {after} 行（{why}，追加 {extra} 条）")

        # ---- 5. 复核 ----
        step("第 5 步 / 5：复核")
        now_ver = read_version(skill_dir)
        m2, d2 = cmp_whitelist(src_root, skill_dir)
        ok = (now_ver == new_ver) and not m2 and not d2
        log(f"  技能版本：{old_ver or '?'} → {now_ver or '?'}  {'✓' if now_ver == new_ver else '✗ 不一致'}")
        log(f"  与新版逐一对得上：{'✓' if not m2 and not d2 else '✗ 缺 ' + str(len(m2)) + ' 个、不同 ' + str(len(d2)) + ' 个'}")
        for bad in (m2 + d2)[:6]:
            log(f"    · {bad}")
        for must in ("scripts/build.py", "templates/page.html", "run.py"):
            log(f"  {'✓' if os.path.isfile(os.path.join(skill_dir, must)) else '✗'} {must}")
        n_logs = len(os.listdir(os.path.join(skill_dir, "logs"))) if os.path.isdir(os.path.join(skill_dir, "logs")) else 0
        log(f"  你的 logs 文件：{n_logs} 个（原样保留）")

        log("")
        log("=" * 58)
        if ok:
            log(f"覆盖安装完成：{old_ver or '?'} → {now_ver}")
            log("下一步生成页面（会顺带抓最新法规）：")
            log(f'  python "{os.path.join(skill_dir, "run.py")}"')
        else:
            log("⚠ 覆盖没完全成功，可能被占用或权限不足。旧文件在 _backup/ 下，可回退。")
        log("=" * 58)
        return 0 if ok else 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
