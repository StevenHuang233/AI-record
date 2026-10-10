"""视频图库视图（clip gallery）：把一份「每行一条视频」的 .jsonl 展示成图库 —— 卡片网格、8 格矩阵、风格分布、
详情抽屉（播放器 + 时间轴 + 提问 + 题目与答案 + 动作 / 段信息）。

只做「读行 → 视图需要的字段」的映射，不执行任何配置里的代码；路径只做字符串改写，真正的文件访问仍然走
sbstore.resolve_asset 的沙箱（/assets/ 请求时才校验）。

行的约定（默认映射 = video_eval.v4.jsonl 的结构，任何字段都可以在定义的 gallery 里改写）：
    item_id            这条的 id                      images[]          抽帧（第 0 张当缩略图）
    video              视频路径                        instruction       学生指令（卡片标题）
    target.stem / 题面 题目文字                        gold_answer       参考答案
    cell / turns       8 格的「格」与「轮次」           n_pages / target_page   页数 / 目标页
    asks[]             提问 {turn,t,page,instruction}  page_windows[]    每页可见区间
    video_meta         {duration,fps,width,height,style,style_name,optics,seed,render_version,
                        stages[][],segments[],transitions[],action_metrics[][],blur_*}
    scored[]           计分题 {num,page,stem,gold,asks[],科目,学段}
"""
from __future__ import annotations

DEFAULTS = {
    "id": "item_id", "thumb": "images.0", "video": "video", "frames": "images",
    "title": "instruction", "subtitle": ["target.stem", "题面"], "gold": ["gold_answer", "target.gold"],
    "duration": "video_meta.duration", "pages": "n_pages", "target_page": "target_page",
    "asks": "asks", "windows": "page_windows", "cell": "cell", "turns": "turns",
    "style": "video_meta.style", "style_name": "video_meta.style_name", "data_source": "source",
    "optics": "video_meta.optics", "meta": "video_meta", "scored": "scored", "frame_pages": "frame_pages",
    "cells": ["单图单题", "单图多题", "多图单题", "多图多题"], "turn_names": ["单轮", "多轮"],
}
PATH_KEYS = ("id", "thumb", "video", "frames", "title", "subtitle", "gold", "duration", "pages", "target_page", "asks",
             "windows", "cell", "turns", "style", "style_name", "data_source", "optics", "meta", "scored", "frame_pages")
SUBTITLE_CAP = 120


def config(g: dict | None) -> dict:
    """合并默认映射；返回的字典里 *_path 键都是「候选路径列表」（第一个取得到值的生效）。"""
    g = g or {}
    out = {"media_map": g.get("media_map") or {}, "group": g.get("group"), "version": g.get("version"),
           "badge": g.get("badge"), "cells": list(g.get("cells") or DEFAULTS["cells"]),
           "turn_names": list(g.get("turn_names") or DEFAULTS["turn_names"])}
    for k in PATH_KEYS:
        v = g.get(k, DEFAULTS[k])
        out[k] = [v] if isinstance(v, str) else list(v)
    return out


def get(row, paths):
    """按候选路径取值：a.b.0 形式；第一个存在且非空的生效。取不到返回 None。"""
    for p in paths:
        v = row
        for part in str(p).split("."):
            if isinstance(v, dict) and part in v:
                v = v[part]
            elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
                v = v[int(part)]
            else:
                v = None
                break
        if v is not None and v != "" and v != []:
            return v
    return None


def map_path(p, media_map: dict):
    """评测行里存的是生成它那台机器上的绝对路径。改写成「根别名/根内相对路径」：
      anchor: 找第一个以它开头的路径段（如 release_），从那一段开始往后；
      after : 取第一次出现的 after 之后的部分。
    都找不到就原样返回（可能本来就是相对路径）。只做字符串处理，不碰文件系统。"""
    if not isinstance(p, str) or not p:
        return None
    mm = media_map or {}
    prefix = mm.get("prefix") or ""
    anchor, after = mm.get("anchor"), mm.get("after")
    if anchor:
        parts = p.split("/")
        for k, part in enumerate(parts[:-1]):
            if part.startswith(anchor):
                return prefix + "/".join(parts[k:])
    if after and after in p:
        return prefix + p.split(after, 1)[1]
    if p.startswith("/") or "\\" in p:       # 没映射上的绝对路径：服务端不会放行，也不该把真实目录暴露给浏览器
        return None
    return p


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def compact(row: dict, cfg: dict) -> tuple:
    """扫描时每行留一个小元组：筛选、排序、搜索、8 格统计都只用它，不用再读文件。
    (id, cell, turns, dur, n_pages, n_asks, target_known, style, data_source, text_lc)"""
    asks = get(row, cfg["asks"])
    pages = get(row, cfg["pages"])
    tp = get(row, cfg["target_page"])
    if tp is None and isinstance(row, dict):
        tp = row.get(cfg["target_page"][0]) if cfg["target_page"] else None
    title = str(get(row, cfg["title"]) or "")
    sub = str(get(row, cfg["subtitle"]) or "")
    rid = str(get(row, cfg["id"]) or "")
    style = get(row, cfg["style_name"]) or get(row, cfg["style"]) or ""
    return (rid, str(get(row, cfg["cell"]) or ""), str(get(row, cfg["turns"]) or ""),
            _num(get(row, cfg["duration"])), int(pages) if isinstance(pages, (int, float)) else None,
            len(asks) if isinstance(asks, list) else 0,
            (tp is not None) if pages else None, str(style), str(get(row, cfg["data_source"]) or ""),
            (title[:160] + " " + sub[:160] + " " + rid).lower())


C_ID, C_CELL, C_TURNS, C_DUR, C_PAGES, C_ASKS, C_TKNOWN, C_STYLE, C_SRC, C_TEXT = range(10)


def median(vals):
    s = sorted(vals)
    n = len(s)
    return None if not n else (float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def summarize(gal: dict) -> dict:
    """8 格矩阵：每格条数、时长中位 / 范围、提问最多几次；风格分布。"""
    table: dict = {}
    styles: dict = {}
    for t in gal.values():
        node = table.setdefault(f"{t[C_CELL] or '-'}|{t[C_TURNS] or '-'}", {"n": 0, "dur": [], "asks": 0, "pages": 0})
        node["n"] += 1
        if t[C_DUR]:
            node["dur"].append(t[C_DUR])
        node["asks"] = max(node["asks"], t[C_ASKS])
        node["pages"] = max(node["pages"], t[C_PAGES] or 0)
        if t[C_STYLE]:
            styles[t[C_STYLE]] = styles.get(t[C_STYLE], 0) + 1
    cells = {}
    for k, node in table.items():
        d = node["dur"]
        cells[k] = {"n": node["n"], "dur_med": round(median(d), 1) if d else None,
                    "dur_min": round(min(d), 1) if d else None, "dur_max": round(max(d), 1) if d else None,
                    "asks_max": node["asks"], "pages_max": node["pages"] or None}
    return {"cells": cells, "styles": styles}


def select(gal: dict, f: dict, lines=None) -> list:
    """按筛选 / 排序挑出行号。f: cell turns pages(1|multi) target(known|unknown) style sort q。
    lines：先限定在这些行号里（例如先按「规则不通过 / 已标记」缩小）。"""
    pool = sorted(gal) if lines is None else [ln for ln in lines if ln in gal]
    def ok(t):
        if f.get("cell") and t[C_CELL] != f["cell"]:
            return False
        if f.get("turns") and t[C_TURNS] != f["turns"]:
            return False
        if f.get("pages") == "1" and (t[C_PAGES] or 1) != 1:
            return False
        if f.get("pages") == "multi" and (t[C_PAGES] or 1) <= 1:
            return False
        if f.get("target") == "known" and t[C_TKNOWN] is not True:
            return False
        if f.get("target") == "unknown" and t[C_TKNOWN] is not False:
            return False
        if f.get("style") and t[C_STYLE] != f["style"]:
            return False
        if f.get("q") and f["q"].strip().lower() not in t[C_TEXT]:
            return False
        return True
    pool = [ln for ln in pool if ok(gal[ln])]
    sort = f.get("sort")
    if sort == "dur_desc":
        pool.sort(key=lambda ln: -(gal[ln][C_DUR] or 0))
    elif sort == "dur_asc":
        pool.sort(key=lambda ln: gal[ln][C_DUR] or 1e9)
    elif sort == "asks":
        pool.sort(key=lambda ln: -gal[ln][C_ASKS])
    return pool


def light(row: dict, line: int, cfg: dict) -> dict:
    """卡片要用的字段（列表只给这些；详情走 item）。"""
    mm = cfg["media_map"]
    asks = get(row, cfg["asks"])
    pages = get(row, cfg["pages"])
    tp = get(row, cfg["target_page"])
    if tp is None and cfg["target_page"]:
        tp = row.get(cfg["target_page"][0]) if isinstance(row, dict) else None
    return {"line": line, "id": str(get(row, cfg["id"]) or f"第 {line} 行"),
            "cell": get(row, cfg["cell"]) or "", "turns": get(row, cfg["turns"]) or "",
            "style": get(row, cfg["style"]) or "", "style_name": get(row, cfg["style_name"]) or "",
            "instruction": get(row, cfg["title"]) or "",
            "question": str(get(row, cfg["subtitle"]) or "")[:SUBTITLE_CAP],
            "duration": _num(get(row, cfg["duration"])), "n_pages": pages, "target_page": tp,
            "n_asks": len(asks) if isinstance(asks, list) else 0,
            "data_source": get(row, cfg["data_source"]) or "",
            "thumb": map_path(get(row, cfg["thumb"]), mm), "has_video": bool(get(row, cfg["video"]))}


def _stages(meta):
    pages = []
    for page in meta.get("stages") or []:
        pages.append([{"type": s.get("type"), "name": s.get("name", s.get("type")), "start": s.get("start"),
                       "end": s.get("end"), "amount": s.get("amount"), "direction": s.get("direction")}
                      for s in (page or []) if isinstance(s, dict)])
    return pages


def _scored(row, cfg):
    out = []
    asks = get(row, cfg["asks"]) or []
    for slot in get(row, cfg["scored"]) or []:
        if not isinstance(slot, dict):
            continue
        mine = [a for a in asks if isinstance(a, dict) and a.get("slot_id") == slot.get("slot_id")]
        out.append({"num": slot.get("num"), "page": slot.get("page"), "subject": slot.get("科目"),
                    "stage": slot.get("学段"), "stem": slot.get("stem") or "", "gold": slot.get("gold") or "",
                    "asks": [{"turn": a.get("turn"), "t": a.get("t"), "kind": a.get("kind")} for a in mine]})
    return out


def detail(row: dict, line: int, cfg: dict) -> dict:
    """详情抽屉要用的全部字段（照旧浏览站 _v4_detail + item 的结构）。"""
    mm = cfg["media_map"]
    it = light(row, line, cfg)
    meta = get(row, cfg["meta"]) or {}
    meta = meta if isinstance(meta, dict) else {}
    frames = get(row, cfg["frames"]) or []
    frame_pages = get(row, cfg["frame_pages"]) or []
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    protocol = origin.get("protocol") if isinstance(origin.get("protocol"), dict) else {}
    skeleton = row.get("skeleton") if isinstance(row.get("skeleton"), dict) else {}
    inner = skeleton.get("skeleton") if isinstance(skeleton.get("skeleton"), dict) else {}
    intents = [t.get("intent", "") for t in (inner.get("turns") or []) if isinstance(t, dict)]
    asks = []
    for a in get(row, cfg["asks"]) or []:
        if isinstance(a, dict):
            asks.append({"turn": a.get("turn"), "t": a.get("t"), "page": a.get("page"), "kind": a.get("kind"),
                         "instruction": a.get("instruction") or "", "intent": a.get("intent") or ""})
    target = row.get("target") if isinstance(row.get("target"), dict) else {}
    it.update({
        "video": map_path(get(row, cfg["video"]), mm),
        "frames": [map_path(p, mm) for p in frames if isinstance(p, str)],
        "frame_pages": frame_pages,
        "optics": get(row, cfg["optics"]) or [],
        "gold": get(row, cfg["gold"]) or "", "question": str(get(row, cfg["subtitle"]) or ""),
        "cue_kind": row.get("cue_kind") or "", "tone": row.get("tone") or "",
        "frame_synth": row.get("frame_synth") or "", "frame_synth_src": row.get("frame_synth_src") or "",
        "asks": asks, "page_windows": get(row, cfg["windows"]) or [],
        "detail": {
            "pages_stages": _stages(meta), "segments": meta.get("segments") or [],
            "transitions": meta.get("transitions") or [], "fps": meta.get("fps"),
            "canvas": f"{meta.get('width')}×{meta.get('height')}" if meta.get("width") else "",
            "seed": meta.get("seed"), "render_version": meta.get("render_version"),
            "blur_limit_sigma": meta.get("blur_limit_sigma"), "blur_peak_sigma": meta.get("blur_peak_sigma"),
            "action_metrics": meta.get("action_metrics") or [],
            "target_num": target.get("question_num") or target.get("num"),
            "n_candidates": row.get("n_candidates"), "学段": row.get("学段"), "科目": row.get("科目"),
            "intents": intents, "bench": origin.get("bench"), "scorer": origin.get("scorer"),
            "keyword": protocol.get("keyword"), "question_type": protocol.get("question_type"),
            "full_score": protocol.get("score"), "scored": _scored(row, cfg),
        },
    })
    return it
