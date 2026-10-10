#!/usr/bin/env python
"""用 Gander (Thinker) 跑 OmniEdu 视频测试集（handheld_v4 release_*），产出回答；打分见 score_official.py。

输入形态与训练/部署契约一致（见 Omni-Interaction-Agent 的 serialize_duplex.py）：
  - 每 1 秒为一个 unit：1 帧画面（按源时间取帧）+ 1 秒静音音频 + 可选用户文本；
  - 视频无音轨，提问是文本 `instruction`，在其出现时刻 t 所在的 unit（floor(t)）作为用户文本注入，
    并让 pending logits 落在文本之后（与 OnlineRunner.prefill_tool_response 同一做法）；
  - 视频结束后保持最后一帧 + 静音，直到模型说完（end_of_turn）或达到 --tail 上限。

两种作答模式（与官方 omniedu_bench 的 mode 同名）：
  natural   只说学生的口头提问；
  protocol  在口头提问后附上官方作答格式要求（scorer.protocol_prompt，如 GAOKAO-MM 的【解析】…<eoe>【答案】…<eoa>）。

用法（需在 gander conda 环境里跑）：
  CUDA_VISIBLE_DEVICES=0 /opt/conda/envs/gander/bin/python test_video_bench.py \
      --release release_gaokao_mm_v1.1 --limit 5
  # 多卡分片：每个进程一张卡，--shard i/N，最后 score_official.py 会自动合并
  CUDA_VISIBLE_DEVICES=3 ... --shard 3/8

风格 SP（语音解题风格，被测 SP v5，取自 release 自带 code/omniedu_bench/paiti.py）：
  --style-sp spoken  把风格 SP 追加到 Gander 的 system prompt 之后（与 --mode 正交），用于语音风格轴；
                     此时默认 --tail 40（讲解比一句话答案长得多），并把 SP 的 sha12 写进每条记录和 summary。
  风格轴打分见 score_style.py（12 维逐维判官 + tier 折叠）。

输出：results/<release>.<tag>[.shardIofN].jsonl（逐题回答）和同名 .summary.json。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH_ROOT = Path(
    Path(os.environ.get("VIDEO_BENCH_ROOT")
         or "/apdcephfs_szcf/share_306661159/saffron-harbor-ae24/omniedu_video_bench/handheld_v4_runs")
)


def resolve_release(arg: str) -> tuple[Path, Path]:
    """返回 (release 目录, 评测 jsonl)。接受目录名 / 目录路径 / jsonl 路径。"""
    p = Path(arg)
    if p.suffix == ".jsonl":
        return p.parent, p
    if not p.is_absolute() and not p.exists():
        p = BENCH_ROOT / arg
    if not p.is_dir():
        raise SystemExit(f"找不到 release 目录: {arg}")
    for cand in [p / "video_eval.v4.jsonl", *sorted((p / "convert").glob("video_eval.*.jsonl"))]:
        if cand.exists() and ".local." not in cand.name:
            return p, cand
    raise SystemExit(f"{p} 下没有 video_eval*.jsonl")


def read_rows(path: Path, offset: int = 0, limit: int | None = None) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < offset:
                continue
            if limit is not None and len(rows) >= limit:
                break
            rows.append(json.loads(line))
    return rows


def sample_frames(video: Path):
    """按源时间每秒取一帧：第 k 个 unit 用 source_time >= k 秒的第一帧（越界则保持最后一帧）。"""
    import av

    frames = []
    next_sec = 0
    last = None
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            ts = float(frame.time) if frame.time is not None else 0.0
            last = frame
            if ts + 1e-3 >= next_sec:
                frames.append(frame.to_image().convert("RGB"))
                next_sec += 1
    if not frames and last is not None:
        frames.append(last.to_image().convert("RGB"))
    return frames


def prefill_user_text(runner, text: str) -> None:
    """在已 prefill 的 unit 末尾追加用户文本，并让 pending logits 位于文本之后。"""
    ids = runner.bundle.tokenizer.encode(text, add_special_tokens=False)
    if not ids:
        return
    decoder = runner.duplex.decoder
    if hasattr(decoder, "embed_tokens"):
        embeds = decoder.embed_tokens(ids)
    else:
        import torch

        embeds = torch.cat([decoder.embed_token(i) for i in ids], dim=0)
    runner.duplex.pending_logits, _ = decoder.feed(embeds, return_logits=True)
    schema = getattr(runner.duplex, "prefill_schema_tokens", None)
    if isinstance(schema, list) and schema and isinstance(schema[-1], list):
        schema[-1].extend(ids)


def protocol_prompts_for(release_dir: Path, row: dict) -> dict[str, str]:
    """slot_id → 官方作答格式要求。用 release 自带 code/ 里的 scorer，保证与打分口径一致。"""
    code = str(release_dir / "code")
    if code not in sys.path:
        sys.path.insert(0, code)
    from omniedu_bench.scorers import get

    return {
        s["slot_id"]: get(s["origin"]["scorer"]).protocol_prompt(s["origin"])
        for s in row.get("scored") or []
    }


def style_system_prompt(release_dir: Path) -> tuple[str, str]:
    """(追加了语音解题风格 SP 的 system prompt, 风格 SP 的 sha12)。SP 来自 release 自带的 paiti 封装，与风格判官同源。"""
    code = str(release_dir / "code")
    if code not in sys.path:
        sys.path.insert(0, code)
    from mcpmft.prompts import GANDER_DUPLEX_SYSTEM_PROMPT
    from omniedu_bench import paiti

    sp = paiti.solve_sp()
    return GANDER_DUPLEX_SYSTEM_PROMPT + "\n\n" + sp, paiti.sha12(sp)


def run_row(runner, row: dict, video: Path, args, prompts: dict[str, str] | None) -> dict:
    import numpy as np

    from mcpmft.prompts import GANDER_DUPLEX_SYSTEM_PROMPT

    system_prompt = args.system_prompt or GANDER_DUPLEX_SYSTEM_PROMPT
    frames = sample_frames(video)
    # 追问(followup)只有 intent（意图描述），没有可直接喂给模型的原话，这里只喂带 instruction 的轮次。
    asks = sorted(
        (a for a in row.get("asks") or [] if a.get("instruction")), key=lambda a: float(a["t"])
    )
    if not asks:
        raise ValueError("row has no literal-instruction asks")
    ask_unit = [int(math.floor(float(a["t"]))) for a in asks]
    by_unit: dict[int, list[int]] = {}
    for idx, u in enumerate(ask_unit):
        by_unit.setdefault(u, []).append(idx)
    last_ask_unit = max(ask_unit)
    n_units = max(len(frames), last_ask_unit + 1) + args.tail_units

    def ask_text(i: int) -> str:
        text = asks[i]["instruction"]
        if prompts is not None:
            text += "\n" + prompts.get(asks[i].get("slot_id"), "")
        return text

    silence = np.zeros(int(16000 * 1.0), dtype=np.float32)
    runner.prepare(system_prompt=system_prompt)

    answers = [""] * len(asks)
    first_speak_unit: list[int | None] = [None] * len(asks)
    decisions: list[str] = []
    current = -1  # 当前负责回答的 ask 下标
    spoke_since_last_ask = False
    t0 = time.time()
    for k in range(n_units):
        frame = frames[min(k, len(frames) - 1)]
        runner.duplex.streaming_prefill(
            audio_waveform=silence, frame_list=[frame], max_slice_nums=1
        )
        if k in by_unit:
            current = by_unit[k][-1]
            spoke_since_last_ask = False
            prefill_user_text(runner, "\n".join(ask_text(i) for i in by_unit[k]))
        out = runner.streaming_generate(
            max_new_speak_tokens_per_chunk=runner.params.max_new_speak_tokens_per_chunk,
            decode_mode=runner.params.decode_mode,
            temperature=runner.params.temperature,
            top_p=runner.params.top_p,
            top_k=runner.params.top_k,
        )
        if out.get("is_tool_call"):
            decisions.append("tool")
        elif out.get("is_interrupt"):
            decisions.append("interrupt")
        elif out.get("is_listen", True):
            decisions.append("listen")
        else:
            decisions.append("speak")
            if current >= 0:
                answers[current] += out.get("text") or ""
                spoke_since_last_ask = True
                if first_speak_unit[current] is None:
                    first_speak_unit[current] = k
        if (
            k >= last_ask_unit
            and spoke_since_last_ask
            and out.get("end_of_turn")
            and k >= len(frames) - 1
        ):
            break
    return {
        "answers": answers,
        "asks_used": asks,
        "first_speak_unit": first_speak_unit,
        "ask_unit": ask_unit,
        "n_units_run": len(decisions),
        "n_video_frames": len(frames),
        "decisions": "".join({"listen": "L", "speak": "S", "interrupt": "I", "tool": "T"}[d] for d in decisions),
        "elapsed_s": round(time.time() - t0, 2),
    }


def parse_shard(spec: str) -> tuple[int, int]:
    try:
        i, n = spec.split("/")
        i, n = int(i), int(n)
    except ValueError:
        raise SystemExit(f"--shard 需要形如 3/8，收到 {spec!r}")
    if not (n >= 1 and 0 <= i < n):
        raise SystemExit(f"--shard {spec} 不合法，需要 0 <= i < n")
    return i, n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--release", required=True, help="release 目录名/路径，或 video_eval*.jsonl 路径")
    ap.add_argument("--mode", choices=["natural", "protocol"], default="natural")
    ap.add_argument(
        "--style-sp", choices=["none", "spoken"], default="none",
        help="spoken = 在 system prompt 后追加『语音解题风格』被测 SP（风格轴用）；默认 none",
    )
    ap.add_argument("--base", default=str(HERE / "checkpoints/MiniCPM-o-4_5"))
    ap.add_argument("--checkpoint", default=str(HERE / "checkpoints/Gander/thinker"))
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5, help="最多跑多少行；<=0 表示全部")
    ap.add_argument("--shard", default="0/1", help="i/N：只跑第 i 片（行号 %% N == i），一片一张卡")
    ap.add_argument(
        "--tail", type=int, default=None,
        help="最后一个提问/视频结束后最多再等多少个 unit（默认 natural=12，protocol=40，因为要写解析；--style-sp spoken 时 40）",
    )
    ap.add_argument("--decode", choices=["greedy", "sampling"], default="greedy")
    ap.add_argument("--tag", default="gander")
    ap.add_argument("--out-dir", default=str(HERE / "results"))
    args = ap.parse_args()
    args.tail_units = (
        args.tail if args.tail is not None
        else (40 if args.mode == "protocol" or args.style_sp != "none" else 12)
    )

    shard_i, shard_n = parse_shard(args.shard)
    release_dir, jsonl = resolve_release(args.release)
    args.system_prompt, style_sp_sha = None, None
    if args.style_sp == "spoken":
        args.system_prompt, style_sp_sha = style_system_prompt(release_dir)
    rows = read_rows(jsonl, args.offset, None if args.limit <= 0 else args.limit)
    rows = rows[shard_i::shard_n]
    print(
        f"[bench] {release_dir.name} mode={args.mode} style_sp={args.style_sp}"
        f"{'@' + style_sp_sha if style_sp_sha else ''} shard={shard_i}/{shard_n}: "
        f"{len(rows)} rows from {jsonl.name}",
        flush=True,
    )

    from mcpmft.args import ModelArguments
    from mcpmft.infer.cli import DuplexConfig, _duplex_params
    from mcpmft.infer.common import load_for_infer
    from mcpmft.infer.online import OnlineRunner

    model_args = ModelArguments(
        model_name_or_path=args.base,
        processor_name_or_path=args.base,
        init_tts=False,
        device_map="cuda",
    )
    t = time.time()
    bundle = load_for_infer(
        model_args,
        checkpoint=args.checkpoint,
        init_token2wav=False,
        load_processor=False,
    )
    duplex_cfg = DuplexConfig(
        speak_text_tokens_per_unit=8,
        max_new_speak_tokens_per_chunk=11,
        talker_speech_tokens_per_unit=50,
        sliding_window_mode="context_slate",
        context_previous_max_tokens=1500,
        decode_mode=args.decode,
    )
    runner = OnlineRunner(bundle, _duplex_params(duplex_cfg, False))
    print(f"[bench] model ready in {time.time() - t:.1f}s", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{release_dir.name}.{args.tag}" + (f".shard{shard_i}of{shard_n}" if shard_n > 1 else "")
    out_path = out_dir / f"{stem}.jsonl"

    stats = {"rows": 0, "errors": 0, "asks": 0, "asks_skipped_no_instruction": 0, "asks_answered": 0}
    elapsed: list[float] = []
    with out_path.open("w", encoding="utf-8") as fout:
        for i, row in enumerate(rows):
            video = release_dir / "videos" / Path(row["video"]).name
            rec = {
                "item_id": row.get("item_id"),
                "mode": args.mode,
                "video": str(video),
                "source": row.get("source"),
                "style_sp": style_sp_sha,
            }
            try:
                if not video.exists():
                    raise FileNotFoundError(video)
                prompts = protocol_prompts_for(release_dir, row) if args.mode == "protocol" else None
                res = run_row(runner, row, video, args, prompts)
            except Exception as exc:  # 单题失败不影响整体
                stats["errors"] += 1
                rec["error"] = f"{type(exc).__name__}: {exc}"
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                print(f"[{i + 1}/{len(rows)}] ERROR {rec['item_id']}: {rec['error']}", flush=True)
                continue

            asks = res["asks_used"]
            stats["asks_skipped_no_instruction"] += len(row["asks"]) - len(asks)
            per_ask = []
            for a, ans in zip(asks, res["answers"]):
                per_ask.append(
                    {
                        "turn": int(a["turn"]),
                        "slot_id": a.get("slot_id"),
                        "t": a["t"],
                        "instruction": a["instruction"],
                        "answer": ans,
                    }
                )
                stats["asks"] += 1
                stats["asks_answered"] += int(bool(ans.strip()))
            rec["asks"] = per_ask
            rec.update({k: v for k, v in res.items() if k not in ("answers", "asks_used")})
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            stats["rows"] += 1
            elapsed.append(res["elapsed_s"])
            head = (per_ask[0]["answer"] or "<无回答>").replace("\n", " ")[:60]
            print(
                f"[{i + 1}/{len(rows)}] {rec['item_id']} units={res['n_units_run']} "
                f"{res['elapsed_s']}s  {res['decisions']}  -> {head}",
                flush=True,
            )

    summary = dict(stats)
    summary["answered_rate"] = round(stats["asks_answered"] / stats["asks"], 4) if stats["asks"] else None
    summary["mean_row_seconds"] = round(sum(elapsed) / len(elapsed), 2) if elapsed else None
    summary.update(release=release_dir.name, mode=args.mode, style_sp=style_sp_sha, checkpoint=args.checkpoint, out=str(out_path))
    (out_dir / f"{stem}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[bench] summary:", json.dumps(summary, ensure_ascii=False), flush=True)
    sys.exit(1 if stats["rows"] == 0 else 0)


if __name__ == "__main__":
    main()
