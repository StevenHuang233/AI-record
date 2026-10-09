#!/usr/bin/env python3
"""范例播放器：把范例写进一个「临时状态目录」并模拟运行，生成可回放的时间线。

绝不碰真实的 state/：默认写到 examples/.play/<名字>/，要写别处必须是空目录或不存在的目录。

    python3 examples/play.py                    # 播放全部范例到同一块看板（examples/.play/all）
    python3 examples/play.py etl-fanin          # 只播放某一个
    python3 examples/play.py --list             # 列出范例
    python3 server.py --state-dir examples/.play/all --port 38417    # 看效果

范例是「形状」的示意，不是模板：内容都是虚构的，别照抄。读法见 examples/README.md。
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES = ["ml-training", "etl-fanin", "media-curation", "release-checklist"]


def load_scenario(name):
    spec = importlib.util.spec_from_file_location("scenario_" + name.replace("-", "_"), os.path.join(HERE, name, "scenario.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description="播放 status_board 范例（写入临时状态目录）")
    ap.add_argument("name", nargs="?", default="all", help="范例名或 all（默认）")
    ap.add_argument("--state-dir", help="目标状态目录（须为空或不存在；默认 examples/.play/<名字>）")
    ap.add_argument("--speed", type=float, default=1.0, help="播放速度，越大越快（默认 1）")
    ap.add_argument("--list", action="store_true", help="列出范例并退出")
    a = ap.parse_args()

    if a.list:
        for n in EXAMPLES:
            print(n, "-", sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, n, "*.json"))))
        return 0
    names = EXAMPLES if a.name == "all" else [a.name]
    for n in names:
        if n not in EXAMPLES:
            print(f"没有这个范例: {n}。可选: {', '.join(EXAMPLES)} 或 all", file=sys.stderr)
            return 1

    dest = os.path.abspath(a.state_dir or os.path.join(HERE, ".play", a.name))
    real = os.path.join(ROOT, "state")
    if os.path.realpath(dest) == os.path.realpath(real):
        print("拒绝写入真实的 state/：范例只能播放到临时目录。", file=sys.stderr)
        return 1
    if a.state_dir and os.path.exists(dest) and os.listdir(dest):
        print(f"目标目录非空: {dest}（为避免覆盖你的数据，请换一个空目录）", file=sys.stderr)
        return 1
    if os.path.isdir(dest) and not a.state_dir:
        shutil.rmtree(dest)                               # 只会清理 examples/.play/ 下自己生成的目录
    os.makedirs(dest, exist_ok=True)

    os.environ["SB_STATE_DIR"] = dest
    os.environ.setdefault("SB_EVENT_GAP", "0.4")          # 模拟时事件密一点，回放更好看
    sys.path.insert(0, ROOT)
    import sb  # noqa: E402

    pause = lambda sec: time.sleep(max(0.0, sec) / max(a.speed, 0.05))  # noqa: E731
    board = os.path.join(real, "board.json")
    if os.path.exists(board):
        shutil.copy(board, os.path.join(dest, "board.json"))

    for n in names:
        d = os.path.join(HERE, n)
        print(f"== {n}", flush=True)
        for f in sorted(glob.glob(os.path.join(d, "*.json"))):
            coll, gid, _ = os.path.basename(f).split(".", 2)
            sb.define(coll, gid, f)
        assets = os.path.join(d, "assets")
        if os.path.isdir(assets):
            os.makedirs(os.path.join(dest, "assets"), exist_ok=True)
            for fn in os.listdir(assets):
                shutil.copy(os.path.join(assets, fn), os.path.join(dest, "assets", fn))
        load_scenario(n).run(sb, pause)

    print(f"\n已写入: {dest}")
    print(f"查看:   python3 server.py --state-dir {os.path.relpath(dest, os.getcwd())} --port <端口>")
    print("体检:   SB_STATE_DIR=%s python3 sb.py validate --ignore-example" % os.path.relpath(dest, os.getcwd()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
