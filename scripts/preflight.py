# -*- coding: utf-8 -*-
"""交付前预检 —— 三类致命问题一次扫完，零命中才准外发。

  1. 本机绝对路径：别人机器上没有，轻则找不到文件，重则静默降级丢数据
  2. 运行残留：logs/ state/ __pycache__ *.pyc 混进包，别人一跑就是“增量”假象
  3. zip 完整性：中文名 UTF-8 flag、CRC、可解码

用法:
    python scripts/preflight.py --zip ../../中国医械法规库_261121.zip
    python scripts/preflight.py --dir .
    python scripts/preflight.py --zip x.zip --deny D:\\My_Docs

退出码 0 = 全过；1 = 有问题（逐条列出，改完重跑）。
"""
import argparse
import os
import re
import sys
import zipfile

# 绝对路径 / 本机痕迹
PATTERNS = [
    (re.compile(r"[A-Za-z]:\\\\[^\s\"'<>|,;)]{2,}"), "Windows 绝对路径"),
    (re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+"), "Unix 家目录"),
    (re.compile(r"\\Users\\[A-Za-z0-9_.-]+"), "Windows 用户目录"),
]
# 常见的本机工作区名（不一定是绝对路径形态，但一样是私有痕迹）
DEFAULT_DENY = ["hermes_workspace", "My_Docs", "scrapling-venv", "persional"]
# 占位示例不算命中
ALLOW = ("xxx", "<", ">", "your_", "example")
TEXT_EXT = (".py", ".md", ".txt", ".bat", ".cmd", ".ps1", ".sh", ".html", ".htm",
            ".json", ".csv", ".yaml", ".yml", ".toml", ".ini")
JUNK = ("__pycache__/", ".pyc", "logs/", "state/", ".bak")


def bad(s):
    return not any(a in s for a in ALLOW)


def scan_text(text, deny):
    out = []
    for rx, label in PATTERNS:
        for m in rx.finditer(text):
            s = m.group(0)
            if bad(s):
                out.append((label, s[:80]))
    for d in deny:
        if d in text:
            out.append(("本机痕迹", d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip")
    ap.add_argument("--dir")
    ap.add_argument("--deny", action="append", default=[], help="额外要禁的词")
    a = ap.parse_args()
    if not a.zip and not a.dir:
        ap.error("给 --zip 或 --dir")
    deny = DEFAULT_DENY + a.deny

    problems, items = [], []
    if a.zip:
        with zipfile.ZipFile(a.zip) as z:
            broken = z.testzip()
            if broken is not None:
                problems.append(("zip CRC", f"损坏条目 {broken}"))
            for info in z.infolist():
                if not (info.flag_bits & 0x800) and any(ord(c) > 127 for c in info.filename):
                    problems.append(("zip 编码", f"中文名未置 UTF-8 flag: {info.filename}"))
                items.append((info.filename, z.read(info)))
    else:
        for root, dirs, fs in os.walk(a.dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "state", "logs", "runtime")]
            for f in fs:
                p = os.path.join(root, f)
                items.append((os.path.relpath(p, a.dir), open(p, "rb").read()))

    for name, _ in items:
        n = name.replace("\\", "/")
        if any(j in n for j in JUNK):
            problems.append(("运行残留", name))

    for name, raw in items:
        if not name.lower().endswith(TEXT_EXT):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(("编码", f"非 UTF-8，别人打开会乱码: {name}"))
            continue
        if "\ufffd" in text:
            problems.append(("编码", f"含替换字符 U+FFFD: {name}"))
        for label, frag in scan_text(text, deny):
            problems.append((label, f"{name} → {frag}"))

    print(f"扫描 {len(items)} 个文件")
    if problems:
        print(f"\n✗ {len(problems)} 处问题：")
        for label, detail in problems:
            print(f"  [{label}] {detail}")
        print("\n改完重跑本脚本，零命中才准外发。")
        return 1
    print("✅ 绝对路径 / 运行残留 / 编码 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
