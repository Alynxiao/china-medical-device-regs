#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
环境自检 —— 给 AI 助手用的第一步

AI 拿到这个技能包后，先跑这一句：
    python scripts/check_env.py

输出会明确告诉你：环境齐不齐、缺哪一样、装它的确切命令。
加 --install 就自动装（缺什么装什么，已经有的跳过）。

退出码：0 = 全都齐；2 = 有缺项（看清要装什么）
"""
import argparse
import glob
import os
import subprocess
import sys

MIN_PY = (3, 9)

PY_INSTALL_HINT = (
    "当前 Python 太老或没装。要 3.9 以上。\n"
    "  · 有包管理器：winget install Python.Python.3.11\n"
    "  · 或下载官方安装包（安装时勾 Add python.exe to PATH）：https://www.python.org/downloads/\n"
    "  · 完全不想装：直接双击技能包根目录的 启动.bat，它会自动下便携版"
)

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
SCRAPLING_CMD = [sys.executable, "-m", "pip", "install", "scrapling[fetchers]", "-i", MIRROR]
SCRAPLING_CMD_OFFICIAL = [sys.executable, "-m", "pip", "install", "scrapling[fetchers]"]
BROWSER_CMD = [sys.executable, "-m", "playwright", "install", "chromium"]


def has_scrapling():
    try:
        import scrapling  # noqa: F401
        return True
    except Exception:
        return False


def has_browser():
    """playwright 把 Chromium 放在 %LOCALAPPDATA%\\ms-playwright\\chromium-*"""
    base = os.environ.get("LOCALAPPDATA", "")
    if base and glob.glob(os.path.join(base, "ms-playwright", "chromium-*")):
        return True
    # 有些环境装在 PLAYWRIGHT_BROWSERS_PATH
    alt = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if alt and glob.glob(os.path.join(alt, "chromium-*")):
        return True
    return False


def has_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def run_install(cmd, what, fallback=None):
    print(f"\n>>> 正在安装：{what}")
    print("    命令:", " ".join(cmd))
    try:
        r = subprocess.run(cmd, timeout=1800)
    except Exception as e:
        print(f"    ✗ 执行失败：{type(e).__name__}: {e}")
        return False
    if r.returncode != 0 and fallback:
        print("    镜像源不通，改试官方源…")
        print("    命令:", " ".join(fallback))
        try:
            r = subprocess.run(fallback, timeout=1800)
        except Exception as e:
            print(f"    ✗ 执行失败：{type(e).__name__}: {e}")
            return False
    if r.returncode != 0:
        print(f"    ✗ 安装失败（退出码 {r.returncode}）。常见原因：网络被墙/公司代理，"
              f"可先设代理再试：set HTTPS_PROXY=http://127.0.0.1:7890")
        return False
    print(f"    ✓ {what} 装好了")
    return True


def main():
    ap = argparse.ArgumentParser(description="中国医械法规库 · 环境自检")
    ap.add_argument("--install", action="store_true", help="缺什么就自动装什么")
    a = ap.parse_args()

    print("=" * 56)
    print(" 环境自检：中国医械法规库")
    print("=" * 56)
    print(f" Python : {sys.version.split()[0]}  ({sys.executable})")

    problems = []

    if sys.version_info < MIN_PY:
        problems.append(("python", "Python 版本太低", None))

    ok_scrapling = has_scrapling()
    print(f" scrapling : {'✓ 已装' if ok_scrapling else '✗ 没装'}")
    if not ok_scrapling:
        problems.append(("scrapling", 'pip install "scrapling[fetchers]"', SCRAPLING_CMD))

    # scrapling[fetchers] 自带 playwright；只有装了一半（有 scrapling 没 playwright）才要补
    if ok_scrapling and not has_playwright():
        problems.append(("playwright", "playwright 缺失（scrapling 装了一半），重装即可", SCRAPLING_CMD))

    ok_br = has_browser()
    print(f" 浏览器内核 : {'✓ 已装' if ok_br else '✗ 没装'}")
    if not ok_br:
        problems.append(("browser", "python -m playwright install chromium", BROWSER_CMD))

    # 数据文件（有就是能走增量，没有就是首次全量）
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.abspath(os.path.join(here, "..", "data", "cn_regs.jsonl"))
    n = 0
    if os.path.exists(data):
        with open(data, encoding="utf-8") as f:
            n = sum(1 for _ in f)
    print(f" 历史数据 : {'✓ ' + str(n) + ' 条（走增量）' if n >= 200 else '✗ 没有（首次会全量，约 25 分钟）'}")

    if not problems:
        print("\n 结论：环境齐了，可以直接跑  python scripts/update.py")
        print("=" * 56)
        return 0

    print("\n 缺这些：")
    for key, desc, cmd in problems:
        print(f"   · {desc}")

    if not a.install:
        print("\n 处理办法（二选一）：")
        print("   A) 让 AI 跑：python scripts/check_env.py --install")
        print("   B) 自己双击技能包根目录的 启动.bat（自动装）")
        if any(p[0] == "python" for p in problems):
            print("\n " + PY_INSTALL_HINT)
        print("=" * 56)
        return 2

    # 自动装
    if any(p[0] == "python" for p in problems):
        print("\n ✗ Python 本身不合格，没法自动装 Python。")
        print(" " + PY_INSTALL_HINT)
        return 2

    for key, desc, cmd in problems:
        fb = SCRAPLING_CMD_OFFICIAL if key in ("scrapling", "playwright") else None
        if not run_install(cmd, desc, fb):
            return 2

    # 复查
    print("\n 复查：")
    ok1, ok2 = has_scrapling(), has_browser()
    print(f"   scrapling {'✓' if ok1 else '✗'}   浏览器内核 {'✓' if ok2 else '✗'}")
    if ok1 and ok2:
        print("\n 结论：装好了，可以跑  python scripts/update.py")
        print("=" * 56)
        return 0
    print("\n 还有没装上的，按上面的报错处理")
    print("=" * 56)
    return 2


if __name__ == "__main__":
    sys.exit(main())
