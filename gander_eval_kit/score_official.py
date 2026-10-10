#!/usr/bin/env python
"""用 release 自带的官方 scorer（omniedu_bench.scoring.correctness）给 test_video_bench.py 的回答出分。

只做"正确性轴"，不联网、不调判官模型，所以只适用于不需要判官的计分器（目前 release 里只实现了
gaokao.obj / gaokao.sub / gaokao_mm.obj，其中 gaokao_mm.obj 不需要判官）。

用法：
  /opt/conda/envs/gander/bin/python score_official.py --release release_gaokao_mm_v1.1 --tag natural
会读取 results/<release>.<tag>.jsonl 和 results/<release>.<tag>.shard*of*.jsonl，写出
results/<release>.<tag>.report.json，并打印汇总。

口径（见 omniedu_bench/scoring/correctness.py）：
  first  这道题第一次被问到时的回答 —— 与原版严格可比，作主指标
  final  这道题最后一次给出可计分答案的回答（追问没有原话、没被喂给模型，所以与 first 基本相同）
未作答（抽不到可计分答案）按 0 分计入，并单独给出 answered_rate。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_video_bench import read_rows, resolve_release  # noqa: E402


def load_responses(results_dir: Path, release: str, tag: str) -> tuple[list[dict], list[dict], list[str]]:
    paths = sorted(
        {
            *glob.glob(str(results_dir / f"{release}.{tag}.jsonl")),
            *glob.glob(str(results_dir / f"{release}.{tag}.shard*of*.jsonl")),
        }
    )
    if not paths:
        raise SystemExit(f"{results_dir} 下没有 {release}.{tag}*.jsonl")
    responses, errors = [], []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("error"):
                    errors.append({"item_id": rec.get("item_id"), "error": rec["error"]})
                    continue
                n_turns = max(int(a["turn"]) for a in rec["asks"])
                turns = [""] * n_turns
                for a in rec["asks"]:
                    turns[int(a["turn"]) - 1] = a["answer"]
                responses.append({"item_id": rec["item_id"], "mode": rec.get("mode", "natural"), "turns": turns})
    return responses, errors, paths


def fmt(x) -> str:
    return "-" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--results-dir", default=str(HERE / "results"))
    args = ap.parse_args()

    release_dir, jsonl = resolve_release(args.release)
    results_dir = Path(args.results_dir)
    responses, errors, paths = load_responses(results_dir, release_dir.name, args.tag)

    code = str(release_dir / "code")
    sys.path.insert(0, code)
    from omniedu_bench.scoring import correctness

    rows = read_rows(jsonl)
    done = {r["item_id"] for r in responses}
    out = correctness.run([r for r in rows if r["item_id"] in done], responses)
    report = out["report"]

    n_rows_total = len(rows)
    meta = {
        "release": release_dir.name,
        "tag": args.tag,
        "files": paths,
        "rows_in_release": n_rows_total,
        "rows_scored": len(done),
        "rows_errored": len(errors),
        "errors": errors[:20],
    }
    path = results_dir / f"{release_dir.name}.{args.tag}.report.json"
    path.write_text(
        json.dumps({"meta": meta, "report": report, "reconcile": out["reconcile"], "slots": out["slots"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[score] {release_dir.name} tag={args.tag}: rows {len(done)}/{n_rows_total}, errored {len(errors)}")
    print(f"[score] 报告 → {path}")
    for mode, benches in report.items():
        for bench, scorers in benches.items():
            for scorer_id, stages in scorers.items():
                for stage in ("first", "final"):
                    node = stages.get(stage, {}).get("*")
                    if not node:
                        continue
                    official = node.get("official", {})
                    print(
                        f"[{mode}/{bench}/{scorer_id}] {stage:5s} n={node['n']:4d} "
                        f"answered_rate={fmt(node['answered_rate'])} "
                        f"官方 accuracy={fmt(official.get('accuracy'))} "
                        f"(得分 {fmt(official.get('correct_question_num'))} / 题数 {official.get('question_num')})"
                    )
                node = stages.get("first", {})
                cells = {k: v for k, v in node.items() if k != "*" and not k.startswith(("位置", "figure:"))}
                for key, v in sorted(cells.items()):
                    print(f"    first {key:12s} n={v['n']:4d} answered={fmt(v['answered_rate'])} rate={fmt(v['scoring_rate'])}")
                subjects = stages.get("first", {}).get("*", {}).get("official", {}).get("subject", {})
                for name, s in sorted(subjects.items()):
                    print(f"    first 学科 {name:10s} n={s['question_num']:4d} acc={fmt(s.get('accuracy'))}")


if __name__ == "__main__":
    main()
