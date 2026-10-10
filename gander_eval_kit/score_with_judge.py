#!/usr/bin/env python
"""用判官模型（LLM-as-a-judge）重跑官方打分，规则抽不到答案时交给模型兜底，失败项自动重试。

与 score_official.py 的关系：同一套官方计分口径（release 自带的 omniedu_bench），
只多两件事——
  1. 把 extract 的兜底抽取接到 LiteLLM 网关的判官模型上（judge_litellm.llm_extract，带指数退避）；
  2. 整批跑完后，对「仍然没抽到答案」的样本再跑一轮（按样本过滤，重跑该样本的所有轮次），
     取更优结果合并，直到没有改进或达到 --passes 上限。

用法：
  python score_with_judge.py --release release_gaokao_mm_v1.1 --tag fullstack --model gpt-5.6-luna
  python score_with_judge.py --tag natural --passes 3 --limit 20      # 小规模试跑
输出 results/<release>.<tag>.judge.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import judge_litellm  # noqa: E402
from score_official import load_responses  # noqa: E402
from test_video_bench import read_rows, resolve_release  # noqa: E402

CALLS = Counter()


def _log(msg: str) -> None:
    CALLS["log"] += 1
    if CALLS["log"] % 50 == 1:
        print(msg, flush=True)


def patch_extract(model: str) -> None:
    """把官方 scorer 内部绑定的兜底抽取换成带重试的判官实现。"""
    def extract(text, question_type, n_sub, _model):
        CALLS["extract"] += 1
        return judge_litellm.llm_extract(text, question_type, n_sub, model, log=_log)

    from omniedu_bench.scorers import gaokao_mm
    gaokao_mm.llm_extract_fn = extract
    try:
        from omniedu_bench import extract as official_extract
        official_extract.llm_extract = extract
    except Exception:  # noqa: BLE001
        pass
    print(f"[judge] 兜底抽取已接到 {model}", flush=True)


def origin_by_slot(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:
        for s in row.get("scored") or []:
            out[s["slot_id"]] = s["origin"]
    return out


def aggregate(per_slot: list[dict], origins: dict[str, dict]) -> dict:
    """按官方口径重新分桶聚合（与 correctness.run 的分桶一致）。"""
    from omniedu_bench.scorers import get
    from omniedu_bench.scorers.base import Result

    buckets: dict[tuple, list] = defaultdict(list)
    seen: dict[tuple, set] = defaultdict(set)
    for s in per_slot:
        origin = origins.get(s["slot_id"])
        if origin is None:
            continue
        mode, bench, scorer_id = s["mode"], s["bench"], s["scorer"]
        source = (origin.get("figure") or {}).get("source") or "none"
        seen[(mode, bench)].add(origin.get("source_id") or s["slot_id"])
        keys = [s["cell"], "*", f"位置{min(s['position'], 3)}", f"figure:{source}"]
        for stage in ("first", "final"):
            res = Result(**s[stage])
            for key in keys:
                buckets[(mode, bench, scorer_id, stage, key)].append((origin, res))

    report: dict = {}
    for (mode, bench, scorer_id, stage, key), items in sorted(buckets.items()):
        official = get(scorer_id).aggregate(items)
        answered = sum(r.answered for _, r in items)
        node = report.setdefault(mode, {}).setdefault(bench, {}).setdefault(scorer_id, {}).setdefault(stage, {})
        node[key] = {"n": len(items), "answered_rate": round(answered / len(items), 3),
                     "scoring_rate": official.get("scoring_rate"),
                     **({"official": official} if key == "*" else {})}
    reconcile = {f"{m}/{b}": {"scored_items": len(v)} for (m, b), v in sorted(seen.items())}
    return {"report": report, "reconcile": reconcile}


def reasons(per_slot: list[dict]) -> Counter:
    c: Counter = Counter()
    for s in per_slot:
        for stage in ("first", "final"):
            d = (s[stage].get("detail") or {}).get("extract") or {}
            if not s[stage].get("answered"):
                c[d.get("reason") or ("llm:" + str(d.get("method")) if d.get("method") else "none")] += 1
    return c


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default="release_gaokao_mm_v1.1")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--model", default=judge_litellm.DEFAULT_MODEL)
    ap.add_argument("--passes", type=int, default=2, help="最多跑几轮（后续轮只重跑仍未抽到答案的样本）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个样本，调试用")
    ap.add_argument("--results-dir", default=str(HERE / "results"))
    args = ap.parse_args()

    release_dir, jsonl = resolve_release(args.release)
    sys.path.insert(0, str(release_dir / "code"))
    from omniedu_bench.scoring import correctness

    patch_extract(args.model)
    judge = judge_litellm.make_judge(args.model, log=_log)

    rows = read_rows(jsonl)
    responses, errors, paths = load_responses(Path(args.results_dir), release_dir.name, args.tag)
    if args.limit:
        keep = {r["item_id"] for r in responses[: args.limit]}
        responses = [r for r in responses if r["item_id"] in keep]
        rows = [r for r in rows if r["item_id"] in keep]
    origins = origin_by_slot(rows)
    print(f"[judge] {release_dir.name} tag={args.tag} 样本 {len(responses)}，计分槽位 {len(origins)}", flush=True)

    merged: dict[tuple[str, str], dict] = {}
    stats = []
    pending = responses
    pending_rows = rows
    for p in range(1, args.passes + 1):
        out = correctness.run(pending_rows, pending, judge=judge, llm_extract=args.model)
        per_slot = out["slots"]          # correctness.run 返回的键是 slots
        for s in per_slot:
            key = (s["item_id"], s["slot_id"])
            old = merged.get(key)
            if old is None or (s["first"]["answered"] and not old["first"]["answered"]):
                merged[key] = s
        answered = sum(1 for s in merged.values() if s["first"]["answered"])
        print(f"[judge] 第 {p} 轮：本轮 {len(per_slot)} 槽位，累计已作答 {answered}/{len(merged)}，"
              f"判官调用 {CALLS['extract']} 次", flush=True)
        print(f"[judge]   未作答原因分布：{dict(reasons(per_slot).most_common(6))}", flush=True)
        stats.append({"pass": p, "slots": len(merged), "answered": answered, "extract_calls": CALLS["extract"]})
        if not any(str((s["first"].get("detail") or {}).get("extract", {}).get("reason", "")).startswith("llm_error")
                   or (not s["first"]["answered"] and
                       "llm:" in str((s["first"].get("detail") or {}).get("extract", {}).get("method", "")))
                   for s in per_slot):
            break                     # 没有因判官失败而未作答的样本，提前结束
        failed = {s["item_id"] for s in per_slot if not s["first"]["answered"]}
        if not failed:
            break
        pending = [r for r in pending if r["item_id"] in failed]
        pending_rows = [r for r in pending_rows if r["item_id"] in failed]
        if not pending:
            break

    per_slot = list(merged.values())
    agg = aggregate(per_slot, origins)
    out_path = Path(args.results_dir) / f"{release_dir.name}.{args.tag}.judge.json"
    out_path.write_text(json.dumps({
        "meta": {"release": release_dir.name, "tag": args.tag, "judge_model": args.model,
                 "files": paths, "rows_scored": len(responses), "rows_errored": len(errors),
                 "slots": len(per_slot), "extract_calls": CALLS["extract"], "passes": stats,
                 "errors": errors[:20]},
        **agg,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[judge] 报告 → {out_path}", flush=True)
    for mode, benches in agg["report"].items():
        for bench, scorers in benches.items():
            for sid, stages in scorers.items():
                for stage in ("first", "final"):
                    node = stages.get(stage, {}).get("*")
                    if not node:
                        continue
                    off = node.get("official") or {}
                    print(f"[{mode}/{bench}/{sid}] {stage:5s} n={node['n']:4d} "
                          f"作答率={node['answered_rate']} 官方 accuracy={off.get('accuracy')}", flush=True)


if __name__ == "__main__":
    main()
