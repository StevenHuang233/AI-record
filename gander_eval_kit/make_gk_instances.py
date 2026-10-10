#!/usr/bin/env python
"""为 GAOKAO-MM 前后台评测生成独立的 Gander 服务实例 g0/g1（基于已有 FDB 实例 i0 的 serve.yaml 改写）。

实例 g<i>：端口 8010+i；Thinker=GPU 3i，Talker=GPU 3i+1，ASR=GPU 3i+2（服务要求 ASR 不与 Gander 同卡，
评测时不调用 ASR，turn.final 直接用标注原话）。后台 Codex 不挂 FDB 业务工具，只有高考答题的任务约定。
key 不写入任何文件：Codex 通过环境变量 LITELLM_API_KEY 读取。
"""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    "# GAOKAO-MM task contract\n\n"
    "You are the execution back-end of a voice study assistant. The student shows an exam page through the camera "
    "and asks about one question by voice. Look at the most recent camera frames, find the question the student "
    "means, solve it, and reply with the final choice first (for example `答案：B`; multiple answers like `答案：AC`), "
    "then at most two short sentences of reasoning. Do not run shell commands, do not edit files, do not ask "
    "clarifying questions.\n"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--reasoning", default="medium")
    args = ap.parse_args()
    out = ROOT / "bench" / "instances"
    src = (out / "i0" / "serve.yaml").read_text(encoding="utf-8")
    base_cfg = (out / "i0" / "codex_home" / "config.toml").read_text(encoding="utf-8")
    base_cfg = base_cfg.split("[mcp_servers.fdb_tools]")[0].rstrip() + "\n"
    base_cfg = re.sub(r'(?m)^model = ".*"$', f'model = "{args.model}"', base_cfg)
    base_cfg = re.sub(r'(?m)^model_reasoning_effort = ".*"$', f'model_reasoning_effort = "{args.reasoning}"', base_cfg)
    for i in range(args.n):
        d = out / f"g{i}"
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        (d / "var").mkdir(exist_ok=True)
        (d / "workspace" / "AGENTS.md").write_text(CONTRACT, encoding="utf-8")
        home = d / "codex_home"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(base_cfg, encoding="utf-8")
        (home / "config.toml").chmod(0o600)
        s = src.replace("/instances/i0", f"/instances/g{i}")
        s = s.replace("/tmp/gander_i0/", f"/tmp/gander_g{i}/")
        s = re.sub(r"(?m)^  port: 8000$", f"  port: {8010 + i}", s)
        s = re.sub(r"(?m)^  port: 8995$", f"  port: {9005 + i}", s)
        s = s.replace('cuda_visible_devices: "0,1"', f'cuda_visible_devices: "{3 * i},{3 * i + 1}"')
        s = s.replace('cuda_visible_devices: "2"', f'cuda_visible_devices: "{3 * i + 2}"')
        (d / "serve.yaml").write_text(s, encoding="utf-8")
    print(f"生成 {args.n} 个 GAOKAO 实例 -> {out}/g*")


if __name__ == "__main__":
    main()
