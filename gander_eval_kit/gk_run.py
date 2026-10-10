#!/usr/bin/env python
"""在 Gander 整套服务（前台 + 运行时 + 后台 Codex）上跑 GAOKAO-MM 视频评测集。

模拟浏览器客户端（static/app.js、video.js）：
  - 每 1 秒一个 unit：先发 screen.frame（元数据 JSON + JPEG 二进制），再发 audio.frame（元数据 + PCM16）；
  - 提问时刻 t 所在 unit 开始，把该条提问的中文语音（bench/gk_tts.py 预合成）逐 unit 送入；
  - 语音说完的那个 unit 之后发 turn.final，text 直接用标注原话（不经过 ASR）；
  - 之后继续送画面 + 静音，直到下一条提问或本行结束；回答 = 该提问之后收到的所有非 listen 文本 chunk。

输出格式与 test_video_bench.py 一致（asks[].answer），可直接用 score_official.py 出官方分。

用法（先起服务，端口 8010+i）：
  python bench/gk_run.py --port 8010 --shard 0/2 --tag fullstack [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
import time
import uuid
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from test_video_bench import read_rows, resolve_release, sample_frames  # noqa: E402

SR = 16000
TERMINAL = {"completed", "done", "succeeded", "failed", "cancelled", "canceled", "error"}


def pending_tasks(state: dict) -> int:
    """后台还有几个任务没进终态。前台把问题派给后台后，必须等它们回来再关会话，
    否则后台结果注入不进前台（服务端会报 duplex live session is already closed）。"""
    tasks = ((state.get("last_task_status") or {}).get("tasks")) or []
    return sum(1 for t in tasks if str(t.get("status", "")).lower() not in TERMINAL)


def jpeg_bytes(img, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def load_tts(tts_dir: Path) -> dict[str, np.ndarray]:
    import soundfile as sf

    index = json.loads((tts_dir / "index.json").read_text(encoding="utf-8"))
    out = {}
    for text, info in index.items():
        audio, sr = sf.read(tts_dir / info["file"], dtype="float32")
        assert sr == SR, (info["file"], sr)
        pad = (-len(audio)) % SR
        out[text] = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
    return out


async def run_row(args, row: dict, video: Path, tts: dict[str, np.ndarray]) -> dict:
    import websockets

    frames = sample_frames(video)
    jpgs = [jpeg_bytes(f) for f in frames]
    asks = sorted((a for a in row.get("asks") or [] if a.get("instruction")), key=lambda a: float(a["t"]))
    if not asks:
        raise ValueError("row has no literal-instruction asks")
    wavs = [tts[a["instruction"]] for a in asks]
    n_speech_units = [len(w) // SR for w in wavs]

    # 每条提问的开口 unit：不早于 floor(t)，也不早于上一条提问说完 + 1。
    start_unit: list[int] = []
    for i, a in enumerate(asks):
        u = int(math.floor(float(a["t"])))
        if i:
            u = max(u, start_unit[-1] + n_speech_units[i - 1] + 1)
        start_unit.append(u)
    n_units = max(len(frames), start_unit[-1] + n_speech_units[-1] + 1) + args.tail_units

    url = f"ws://127.0.0.1:{args.port}/ws/duplex?session_id=gk_{uuid.uuid4().hex[:8]}"
    events: list[dict] = []
    t0 = time.time()
    state = {"ready": False, "done": False, "media_done": False, "current": -1, "screen": None}
    answers = [""] * len(asks)
    spoke_units = [0] * len(asks)
    last_end_of_turn = {"ask": -1}

    trace_lines: list[str] = []

    def log(kind, **kw):
        ts = round(time.time() - t0, 2)
        events.append({"t": ts, "kind": kind, **kw})
        # 同一份事件另存一份人类可读的轨迹，供看板逐条回看整通"通话"。
        if kind == "chunk":
            act = "聆听" if kw.get("is_listen") else "说话"
            tail = "（本轮结束）" if kw.get("eot") else ""
            trace_lines.append(f"[{ts:6.2f}s] 前台{act}{tail}：{kw.get('text', '')}")
        elif kind == "ask_start":
            trace_lines.append(f"[{ts:6.2f}s] ▶ 用户开口（unit {kw.get('unit')}）：{kw.get('instruction')}")
        elif kind == "turn.final":
            trace_lines.append(f"[{ts:6.2f}s] → 把第 {kw.get('ask')} 条提问原话交给后台（turn.final，不经 ASR）")
        elif kind == "task_status":
            tasks = (kw.get("task") or {}).get("tasks") or []
            if tasks:
                desc = "，".join(f"{t.get('id') or t.get('name') or '任务'}={t.get('status')}" for t in tasks)
                trace_lines.append(f"[{ts:6.2f}s] 后台任务：{desc}")
            else:
                trace_lines.append(f"[{ts:6.2f}s] 后台任务：无")
        elif kind == "media.mode.done":
            trace_lines.append(f"[{ts:6.2f}s] 画面通道就绪（media_mode={kw.get('media_mode')}）")
        else:
            # 噪声字段不进轨迹：token id、原始工具文本、音频字节数会把一行撑到几千字符
            skip = {"generated_token_ids", "raw_tool_text", "audio_bytes", "audio_format",
                    "audio_sample_rate", "unit_id", "generation_id", "index"}
            parts = []
            for k, v in kw.items():
                if k in skip:
                    continue
                if isinstance(v, (dict, list)):
                    s = json.dumps(v, ensure_ascii=False)
                else:
                    s = str(v)
                if len(s) > 160:
                    s = s[:160] + "…"
                parts.append(f"{k}={s}")
            trace_lines.append(f"[{ts:6.2f}s] {kind}" + (("：" + " ".join(parts)) if parts else ""))

    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:

        async def receiver():
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    data = json.loads(message)
                    typ = data.get("type")
                    if typ == "ready":
                        state["ready"] = True
                    elif typ == "media.mode.done":
                        state["screen"] = data.get("screen")
                        state["media_done"] = True
                        log("media.mode.done", media_mode=data.get("media_mode"))
                    elif typ == "chunk":
                        text = data.get("text") or ""
                        if not data.get("is_listen") and text and state["current"] >= 0:
                            answers[state["current"]] += text
                            spoke_units[state["current"]] += 1
                        if data.get("end_of_turn") and not data.get("is_listen"):
                            last_end_of_turn["ask"] = state["current"]
                        if text or data.get("end_of_turn"):
                            log("chunk", text=text, is_listen=data.get("is_listen"),
                                eot=data.get("end_of_turn"), ask=state["current"])
                    elif typ in ("audio.chunk", "audio.done", "playback.cancel"):
                        continue
                    elif typ == "task_status":
                        log("task_status", task=data.get("task"))
                    else:
                        slim = {k: v for k, v in data.items() if k != "type" and len(json.dumps(v, ensure_ascii=False)) < 400}
                        log(typ or "unknown", **slim)
                    if typ == "session.done":
                        state["done"] = True
                        return
            except websockets.ConnectionClosed:
                state["done"] = True

        recv = asyncio.create_task(receiver())
        for _ in range(900):
            if state["ready"]:
                break
            await asyncio.sleep(0.1)
        if not state["ready"]:
            raise RuntimeError("no ready within 90s")

        await ws.send(json.dumps({"type": "media.mode", "video": True, "source": args.source}))
        for _ in range(300):
            if state["media_done"]:
                break
            await asyncio.sleep(0.1)
        if not state["media_done"] or not state["screen"]:
            raise RuntimeError("media.mode not accepted")
        sid = url.split("session_id=")[1]
        sws = await websockets.connect(
            f"ws://127.0.0.1:{args.port}{state['screen']['path']}?session_id={sid}&token={state['screen']['token']}",
            max_size=None, ping_interval=None)
        first = json.loads(await asyncio.wait_for(sws.recv(), timeout=30))
        if first.get("type") != "screen.ready":
            raise RuntimeError(f"screen channel: {first}")
        screen_stats = {"accepted": 0, "dropped": 0}

        async def screen_reader():
            try:
                async for m in sws:
                    if isinstance(m, bytes):
                        continue
                    t = json.loads(m).get("type")
                    if t == "screen.frame.accepted":
                        screen_stats["accepted"] += 1
                    elif t in ("screen.frame.dropped", "error"):
                        screen_stats["dropped"] += 1
            except websockets.ConnectionClosed:
                pass

        sreader = asyncio.create_task(screen_reader())

        async def send_frame(idx: int) -> None:
            await sws.send(json.dumps({
                "type": "screen.frame", "frame_id": f"gk_{idx}", "captured_at_ms": int(time.time() * 1000),
                "encoding": "jpeg", "video_source": args.source,
                "metadata": {"width": frames[0].width, "height": frames[0].height},
            }))
            await sws.send(jpgs[min(idx, len(jpgs) - 1)])

        seq = sample = 0
        silence = np.zeros(SR, dtype=np.float32)
        speaking: dict[int, tuple[int, int]] = {}   # unit -> (ask idx, offset unit)
        for i, u in enumerate(start_unit):
            for j in range(n_speech_units[i]):
                speaking[u + j] = (i, j)
        final_at = {start_unit[i] + n_speech_units[i] - 1: i for i in range(len(asks))}
        asked_at: dict[int, int] = {}
        unit_t0 = time.time()
        last_poll = 0.0
        k = 0
        for k in range(n_units):
            now_ms = int(time.time() * 1000)
            await send_frame(k)
            if k in speaking:
                i, j = speaking[k]
                chunk = wavs[i][j * SR:(j + 1) * SR]
                if j == 0:
                    state["current"] = i
                    asked_at[i] = k
                    log("ask_start", ask=i, unit=k, instruction=asks[i]["instruction"])
            else:
                chunk = silence
            seq += 1
            await ws.send(json.dumps({
                "type": "audio.frame", "sequence": seq, "start_sample": sample,
                "sample_count": int(len(chunk)), "captured_at_ms": now_ms,
            }))
            await ws.send(pcm16(chunk))
            sample += len(chunk)
            trace_lines.append(
                f"[{round(time.time() - t0, 2):6.2f}s] unit {k}：画面帧 #{min(k, len(jpgs) - 1)} + "
                f"{'提问语音' if k in speaking else '静音'}")
            if k in final_at:
                i = final_at[k]
                await ws.send(json.dumps({
                    "type": "turn.final", "turn_id": f"gk-{uuid.uuid4().hex[:8]}",
                    "text": asks[i]["instruction"], "start_ms": int(asked_at[i] * 1000),
                    "end_ms": int((k + 1) * 1000), "timestamp_ms": int(time.time() * 1000),
                    "timezone": "Asia/Shanghai",
                }))
                log("turn.final", ask=i, unit=k)
            # 实时节奏：每 unit 至少 args.pace 秒。
            wait = unit_t0 + (k + 1) * args.pace - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            if time.time() - last_poll > 5:
                last_poll = time.time()
                await ws.send(json.dumps({"type": "task_status"}))
            past_last_ask = k >= start_unit[-1] + n_speech_units[-1]
            if (past_last_ask and k >= len(frames) - 1 and answers[-1]
                    and last_end_of_turn["ask"] == len(asks) - 1 and pending_tasks(state) == 0):
                break
            if state["done"]:
                break

        # 视频播完后继续送末帧 + 静音，等后台结果被前台说出来（最多 args.max_wait 秒）。
        deadline = time.time() + args.max_wait
        settle = 0
        while time.time() < deadline and not state["done"]:
            if time.time() - last_poll > 5:
                last_poll = time.time()
                await ws.send(json.dumps({"type": "task_status"}))
            if answers[-1] and last_end_of_turn["ask"] == len(asks) - 1 and pending_tasks(state) == 0:
                settle += 1
                if settle > args.settle_units:
                    break
            else:
                settle = 0
            k += 1
            await send_frame(len(jpgs) - 1)
            seq += 1
            await ws.send(json.dumps({"type": "audio.frame", "sequence": seq, "start_sample": sample,
                                      "sample_count": SR, "captured_at_ms": int(time.time() * 1000)}))
            await ws.send(pcm16(silence))
            sample += SR
            await asyncio.sleep(args.pace)

        await ws.send(json.dumps({"type": "stop"}))
        try:
            await asyncio.wait_for(recv, timeout=30)
        except asyncio.TimeoutError:
            recv.cancel()
        sreader.cancel()
        await sws.close()

    return {
        "trace_text": "\n".join(trace_lines),
        "answers": answers, "asks_used": asks, "ask_unit": start_unit,
        "first_speak_unit": [None if not s else 0 for s in spoke_units],
        "n_units_run": k + 1, "n_video_frames": len(frames),
        "elapsed_s": round(time.time() - t0, 2), "screen_frames": screen_stats, "events": events,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default="release_gaokao_mm_v1.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5, help="<=0 表示全部")
    ap.add_argument("--tag", default="fullstack")
    ap.add_argument("--source", default="camera", choices=["camera", "screen"])
    ap.add_argument("--pace", type=float, default=0.5, help="每个 unit 至少间隔多少秒（1.0 = 真实时间）")
    ap.add_argument("--tail-units", type=int, default=2)
    ap.add_argument("--max-wait", type=float, default=180.0, help="视频播完后最多再等多少秒（等后台任务回来）")
    ap.add_argument("--mode", default="natural", help="写入结果里的 mode 字段（打分脚本只认 natural/protocol）")
    ap.add_argument("--settle-units", type=int, default=2)
    ap.add_argument("--tts-dir", default=str(HERE / "tts_gk"))
    ap.add_argument("--out-dir", default=str(ROOT / "results"))
    ap.add_argument("--keep-events", action="store_true")
    args = ap.parse_args()

    release_dir, jsonl = resolve_release(args.release)
    si, sn = (int(x) for x in args.shard.split("/"))
    rows = read_rows(jsonl, args.offset, None)
    rows = [r for idx, r in enumerate(rows, start=args.offset) if idx % sn == si]
    if args.limit > 0:
        rows = rows[: args.limit]
    tts = load_tts(Path(args.tts_dir))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if sn == 1 else f".shard{si}of{sn}"
    out = out_dir / f"{release_dir.name}.{args.tag}{suffix}.jsonl"
    done_ids = set()
    if out.exists():
        done_ids = {json.loads(l)["item_id"] for l in out.open(encoding="utf-8") if l.strip()}
    print(f"[gk] port={args.port} shard={args.shard} rows={len(rows)} 已完成={len(done_ids)} -> {out}", flush=True)

    n_ok = n_err = n_ans = n_ask = 0
    with out.open("a", encoding="utf-8") as f:
        for n, row in enumerate(rows, 1):
            item_id = row.get("item_id")
            if item_id in done_ids:
                continue
            video = release_dir / "videos" / Path(row["video"]).name
            rec = {"item_id": item_id, "mode": args.mode, "video": str(video), "source": row.get("source")}
            try:
                res = asyncio.run(run_row(args, row, video, tts))
            except Exception as exc:  # noqa: BLE001
                n_err += 1
                rec["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[{n}/{len(rows)}] {item_id} ERROR {rec['error']}", flush=True)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                continue
            asks = res["asks_used"]
            rec["asks"] = [
                {"turn": a.get("turn"), "slot_id": a.get("slot_id"), "t": a["t"],
                 "instruction": a["instruction"], "answer": ans}
                for a, ans in zip(asks, res["answers"])
            ]
            rec.update({k: v for k, v in res.items() if k not in ("answers", "asks_used", "events")})
            if args.keep_events:
                rec["events"] = res["events"]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_ok += 1
            n_ask += len(asks)
            n_ans += sum(1 for a in res["answers"] if a.strip())
            print(f"[{n}/{len(rows)}] {item_id} {res['elapsed_s']}s answered={sum(1 for a in res['answers'] if a.strip())}/{len(asks)}"
                  f" ans0={res['answers'][0][:50]!r}", flush=True)
    print(f"[gk] 完成 ok={n_ok} err={n_err} asks={n_ask} answered={n_ans}", flush=True)


if __name__ == "__main__":
    main()
