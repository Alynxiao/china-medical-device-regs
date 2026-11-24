#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一条命令跑完全套：自检 → 装缺的依赖 → 抓取 → 生成页面

    python run.py               # 正常更新（首次全量，之后增量）
    python run.py --full        # 强制全量重抓
    python run.py --dry         # 只做环境自检，不抓
    python run.py --no-install  # 缺依赖只报告，不自动装

给 AI 助手看的最短路径就是：跑这个，看输出。
"""
import argparse
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
DATA = os.path.join(HERE, "data", "cn_regs.jsonl")
# 页面文件名带数据版本（中国医疗器械法规全集_V2026-11-19.html），跑完按最新时间挑
def latest_html():
    cands = glob.glob(os.path.join(HERE, "中国医械法规库_*.html"))
    if not cands:
        cands = glob.glob(os.path.join(HERE, "中国医械法规库.html"))
    return max(cands, key=os.path.getmtime) if cands else os.path.join(HERE, "(未生成)")
PY = sys.executable


def log(m):
    print(m, flush=True)


def step(title):
    print("\n" + "=" * 58)
    print(" " + title)
    print("=" * 58, flush=True)


def call(args, allow_fail=False, cwd=None):
    """跑子脚本，实时把输出透出去"""
    r = subprocess.run([PY] + args, cwd=cwd or HERE)
    if r.returncode != 0 and not allow_fail:
        return False
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="中国医械法规库 · 一键更新")
    ap.add_argument("--full", action="store_true", help="强制全量重抓（约 25 分钟）")
    ap.add_argument("--dry", action="store_true", help="只自检，不抓取")
    ap.add_argument("--no-install", action="store_true", help="缺依赖只报告，不自动装")
    a = ap.parse_args()

    # ---------- 1. 自检 ----------
    step("第 1 步 / 4：检查环境")
    ok = call([os.path.join(SCRIPTS, "check_env.py")], allow_fail=True)

    if not ok:
        if a.no_install:
            log("\n环境不齐（按上面提示装好后重跑本脚本）。加了 --no-install，不自动装。")
            return 2
        log("\n环境不齐，开始自动安装缺少的组件…")
        step("第 1 步补充：自动安装依赖")
        if not call([os.path.join(SCRIPTS, "check_env.py"), "--install"]):
            log("\n安装没成功。把上面的报错发给维护人。")
            return 2

    if a.dry:
        log("\n(--dry 只自检) 环境没问题，去掉 --dry 就会开始抓取。")
        return 0

    # ---------- 2. 抓取 ----------
    step("第 2 步 / 4：抓取官方最新法规")
    if not os.path.exists(DATA):
        log("没有历史数据 → 本次是首次全量，约 25 分钟，别中断。")
    else:
        log("已有历史数据 → 本次走增量，通常 1 分钟内。")

    fetch_args = [os.path.join(SCRIPTS, "update.py"), "--data", DATA]
    if a.full:
        fetch_args.append("--full")
    if not call(fetch_args):
        log("\n抓取失败。如果某个栏目被拦，上面的报告里会写明是哪一栏。")
        return 2

    # ---------- 3. 监管动态 ----------
    step("第 3 步 / 4：抓监管动态（飞检、行政处罚、监督抽检、停产整改）")
    # 包内自带 notice_watch.py；找不到才退回外部 notice-watch 技能
    notice_py = os.path.join(HERE, "notice_watch.py")
    notice_cwd = HERE
    if not os.path.exists(notice_py):
        alt = os.path.abspath(os.path.join(HERE, "..", "notice-watch"))
        if os.path.exists(os.path.join(alt, "notice_watch.py")):
            notice_py, notice_cwd = os.path.join(alt, "notice_watch.py"), alt
    if os.path.exists(notice_py):
        log("扫 30 个源查飞检/处罚/抽检/停产整改，约 1 分钟…")
        if not call([notice_py, "watch", "--days", "365"], allow_fail=True, cwd=notice_cwd):
            log("  ⚠ 有部分源没抓到（常见原因：站点证书问题、超时），已抓到的照常进页面")
    else:
        log("  ⚠ 包内缺少 notice_watch.py，本次不出监管动态板块")

    # ---------- 4. 生成页面 ----------
    step("第 4 步 / 4：生成页面")
    if not call([os.path.join(SCRIPTS, "build.py"), "--data", DATA]):
        log("\n生成页面失败。")
        return 2

    print("\n" + "=" * 58)
    log("全部完成")
    log(f"  页面：{latest_html()}")
    log(f"  数据：{DATA}")
    log("  双击页面就能看，断网也能用。")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
