#!/usr/bin/env python3
"""status_board 存储层（仅标准库）。

负责：状态目录布局、入参清洗校验、原子写、跨进程加锁、时间线事件日志、体检(lint)。
server.py 与 sb.py 都只通过本模块读写状态，保证两条写入路径行为一致。

状态目录（默认 <本文件目录>/state，可用环境变量 SB_STATE_DIR 改）:
    pipelines/<id>.json   Pipeline 结构图定义（人写/AI 写）
    datasets/<id>.json    数据管理图定义
    modules/<id>.json     定制展示模块定义
    tasks/<id>.json       任务记录（训练/评测/流水线执行/普通任务，由脚本上报）
    timeline.jsonl        时间线：只追加的事件日志，回放动画用
    assets/               模块里引用的图片/音视频/jsonl（可用 SB_ASSETS_DIR 改）
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
TASK_STATUS = {"queued", "running", "done", "failed", "cancelled"}
TASK_KINDS = ("job", "train", "eval", "run")
NODE_STATUS = {"pending", "running", "done", "failed", "skipped",
               "ready", "building", "missing", "stale"}
ITEM_STATUS = {"pending", "running", "done", "failed", "skipped"}
TERMINAL = {"done", "failed", "cancelled"}
GRAPH_KINDS = ("pipelines", "datasets")
DEF_COLLS = ("pipelines", "datasets", "modules")
COLLS = DEF_COLLS + ("tasks",)
PROTECTED_META = {"path", "auto_stat"}
EVENT_GAP = float(os.environ.get("SB_EVENT_GAP", "60"))   # 普通进度更新写入时间线的最小间隔(秒)
STATUS_CN = {"queued": "排队", "running": "运行中", "done": "已完成", "failed": "失败", "cancelled": "已取消"}
MAX_SERIES_POINTS = 300
MAX_ITEMS = 500


# ── 路径、文件、锁 ────────────────────────────────────────────────────────────
def state_dir() -> str:
    return os.path.abspath(os.environ.get("SB_STATE_DIR") or os.path.join(ROOT, "state"))


def check_id(x) -> str:
    if not isinstance(x, str) or not ID_RE.match(x):
        raise ValueError("id 只能包含字母数字 _ . -，且以字母数字开头，长度 ≤ 64")
    return x


def item_path(coll: str, item_id: str) -> str:
    check_id(item_id)
    return os.path.join(state_dir(), coll, item_id + ".json")


def ensure_dirs():
    for c in COLLS:
        os.makedirs(os.path.join(state_dir(), c), exist_ok=True)


@contextlib.contextmanager
def lock():
    """跨进程互斥，保证多个脚本同时上报不丢更新。"""
    ensure_dirs()
    f = open(os.path.join(state_dir(), ".lock"), "a")
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: str, obj) -> None:
    """原子写：先写临时文件再 rename，读方永远看不到半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def list_ids(coll: str) -> list:
    try:
        names = os.listdir(os.path.join(state_dir(), coll))
    except FileNotFoundError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and not n.startswith("."))


def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False)


# ── 入参清洗 ──────────────────────────────────────────────────────────────────
def _num(v, name, lo=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < lo:
        raise ValueError(f"{name} 必须是 >= {lo} 的有限数字")
    return v


def _str(v, name, n):
    if not isinstance(v, str):
        raise ValueError(f"{name} 必须是字符串")
    return v[:n]


def _scalars(d, name, limit=50, drop=()):
    if not isinstance(d, dict) or len(d) > limit:
        raise ValueError(f"{name} 必须是不超过 {limit} 项的对象")
    out = {}
    for k, v in d.items():
        if not isinstance(k, str) or k in drop:
            continue
        if isinstance(v, bool) or v is None:
            out[k[:40]] = v
        elif isinstance(v, (int, float)):
            out[k[:40]] = _num(v, f"{name}.{k}", -1e18)
        elif isinstance(v, str):
            out[k[:40]] = v[:400]
        else:
            raise ValueError(f"{name}.{k} 只支持标量")
    return out


def _pair(p, name):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise ValueError(f"{name} 的点必须是 [x, y]")
    return [_num(p[0], name + ".x", -1e18), _num(p[1], name + ".y", -1e18)]


def _is_pair(v):
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v))


def _points(v, name):
    if _is_pair(v):
        return [_pair(v, name)]
    if not isinstance(v, list) or len(v) > MAX_SERIES_POINTS:
        raise ValueError(f"{name} 必须是 [x,y] 或不超过 {MAX_SERIES_POINTS} 个点的列表")
    return [_pair(p, name) for p in v]


def clean_task_patch(p: dict) -> dict:
    if not isinstance(p, dict):
        raise ValueError("请求体必须是 JSON 对象")
    out = {}
    for k, v in p.items():
        if v is None or k == "id":
            continue
        if k in ("name", "unit"):
            out[k] = _str(v, k, 120)
        elif k == "message":
            out[k] = _str(v, k, 500)
        elif k in ("pipeline", "node"):
            out[k] = check_id(v)
        elif k == "kind":
            if v not in TASK_KINDS:
                raise ValueError(f"kind 必须是 {list(TASK_KINDS)}")
            out[k] = v
        elif k == "status":
            if v not in TASK_STATUS:
                raise ValueError(f"status 必须是 {sorted(TASK_STATUS)}")
            out[k] = v
        elif k in ("done", "total", "started_at", "stale_after"):
            out[k] = _num(v, k)
        elif k == "progress":
            out[k] = min(1.0, _num(v, k))
        elif k in ("metrics", "scores", "hparams", "model"):
            out[k] = _scalars(v, k, 30)
        elif k == "datasets":
            if not isinstance(v, list) or len(v) > 30:
                raise ValueError("datasets 必须是不超过 30 项的列表")
            out[k] = [_scalars(d, "datasets[]", 14) for d in v]
        elif k == "series":
            if not isinstance(v, dict) or len(v) > 8:
                raise ValueError("series 必须是不超过 8 条曲线的对象")
            out[k] = {str(n)[:40]: _points(pts, f"series.{n}") for n, pts in v.items()}
        elif k == "append":
            if not isinstance(v, dict) or len(v) > 8:
                raise ValueError("append 必须是 {曲线名: [x,y] 或 [[x,y],...]}")
            out[k] = {str(n)[:40]: _points(pts, f"append.{n}") for n, pts in v.items()}
        elif k == "items":
            if not isinstance(v, list) or len(v) > 200:
                raise ValueError("items 必须是不超过 200 项的列表")
            its = []
            for it in v:
                if not isinstance(it, dict) or not isinstance(it.get("id"), (str, int)) or not str(it["id"]):
                    raise ValueError("items 每项必须有 id")
                c = {"id": str(it["id"])[:64]}
                if it.get("name") is not None:
                    c["name"] = _str(it["name"], "item.name", 120)
                if it.get("note") is not None:
                    c["note"] = _str(it["note"], "item.note", 300)
                if it.get("step") is not None:
                    c["step"] = check_id(it["step"])
                if it.get("status") is not None:
                    if it["status"] not in ITEM_STATUS:
                        raise ValueError(f"item.status 必须是 {sorted(ITEM_STATUS)}")
                    c["status"] = it["status"]
                its.append(c)
            out[k] = its
        elif k == "step_counts":
            if not isinstance(v, dict) or len(v) > 300:
                raise ValueError("step_counts 必须是 {节点id: {running,done,failed,pending}}")
            sc = {}
            for nid, c in v.items():
                check_id(nid)
                if not isinstance(c, dict):
                    raise ValueError("step_counts 每项必须是对象")
                sc[nid] = {kk: int(_num(c.get(kk, 0), f"step_counts.{nid}.{kk}"))
                           for kk in ("running", "done", "failed", "pending")}
            out[k] = sc
        elif k == "shards":
            if not isinstance(v, list) or len(v) > 64:
                raise ValueError("shards 必须是不超过 64 项的列表")
            shards = []
            for s in v:
                if not isinstance(s, dict):
                    raise ValueError("shards 每项必须是对象")
                c = {"name": _str(s.get("name", ""), "shard.name", 40)}
                for f in ("done", "total"):
                    if s.get(f) is not None:
                        c[f] = _num(s[f], f"shard.{f}")
                if s.get("status") is not None:
                    if s["status"] not in TASK_STATUS:
                        raise ValueError("shard.status 非法")
                    c["status"] = s["status"]
                shards.append(c)
            out[k] = shards
        else:
            raise ValueError(f"不支持的字段: {k}")
    return out


def clean_node_patch(p: dict) -> dict:
    if not isinstance(p, dict):
        raise ValueError("请求体必须是 JSON 对象")
    out = {}
    for k, v in p.items():
        if v is None:
            continue
        if k == "status":
            if v not in NODE_STATUS:
                raise ValueError(f"status 必须是 {sorted(NODE_STATUS)}")
            out[k] = v
        elif k == "progress":
            out[k] = min(1.0, _num(v, k))
        elif k == "note":
            out[k] = _str(v, k, 500)
        elif k == "meta":
            out[k] = _scalars(v, k, drop=PROTECTED_META)
        else:
            raise ValueError(f"不支持的字段: {k}")
    return out


def clean_json(v, depth=0):
    """递归校验并截断任意 JSON（用于定义文件）。"""
    if depth > 8:
        raise ValueError("JSON 嵌套过深（>8）")
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            raise ValueError("不允许 NaN / Infinity")
        return v
    if isinstance(v, str):
        return v[:4000]
    if isinstance(v, list):
        if len(v) > 1000:
            raise ValueError("列表过长（>1000）")
        return [clean_json(x, depth + 1) for x in v]
    if isinstance(v, dict):
        if len(v) > 100:
            raise ValueError("对象键过多（>100）")
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise ValueError("键必须是字符串")
            out[k[:80]] = clean_json(x, depth + 1)
        return out
    raise ValueError("不支持的 JSON 类型")


def clean_definition(coll: str, obj) -> dict:
    if coll not in DEF_COLLS:
        raise ValueError(f"coll 必须是 {list(DEF_COLLS)}")
    obj = clean_json(obj)
    if not isinstance(obj, dict):
        raise ValueError("定义必须是 JSON 对象")
    obj.pop("id", None)
    if coll in GRAPH_KINDS:
        nodes = obj.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > 300:
            raise ValueError("nodes 必须是 1~300 个节点的列表")
        ids = set()
        for n in nodes:
            if not isinstance(n, dict):
                raise ValueError("nodes 每项必须是对象")
            check_id(n.get("id"))
            if n["id"] in ids:
                raise ValueError(f"节点 id 重复: {n['id']}")
            ids.add(n["id"])
            if "status" in n and n["status"] not in NODE_STATUS:
                raise ValueError(f"节点 {n['id']} 的 status 非法: {n['status']}")
        edges = obj.get("edges", [])
        if not isinstance(edges, list) or len(edges) > 1000:
            raise ValueError("edges 必须是列表")
        for e in edges:
            if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
                raise ValueError(f"边引用了不存在的节点: {e}")
        groups = obj.get("groups", [])
        if not isinstance(groups, list):
            raise ValueError("groups 必须是列表")
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("nodes"), list) or any(x not in ids for x in g["nodes"]):
                raise ValueError(f"groups 引用了不存在的节点: {g}")
    else:
        blocks = obj.get("blocks")
        if not isinstance(blocks, list) or len(blocks) > 200 or any(
                not isinstance(b, dict) or not isinstance(b.get("type"), str) for b in blocks):
            raise ValueError("blocks 必须是不超过 200 个 {type: ...} 的列表")
    if len(json.dumps(obj, ensure_ascii=False)) > 1_000_000:
        raise ValueError("定义过大（>1MB）")
    return obj


# ── 时间线事件日志 ────────────────────────────────────────────────────────────
_EV = {"off": 0, "list": [], "path": None}
_EV_LOCK = threading.RLock()


def events_path() -> str:
    return os.path.join(state_dir(), "timeline.jsonl")


def events_refresh() -> list:
    """增量读取新事件；只消费到最后一个换行（容忍正在写入的半行）。"""
    path = events_path()
    with _EV_LOCK:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            _EV.update(off=0, list=[], path=path)
            return _EV["list"]
        if size < _EV["off"] or _EV["path"] != path:
            _EV.update(off=0, list=[], path=path)
        if size > _EV["off"]:
            with open(path, "rb") as f:
                f.seek(_EV["off"])
                data = f.read(size - _EV["off"])
            end = data.rfind(b"\n")
            if end >= 0:
                for line in data[:end + 1].split(b"\n")[:-1]:
                    try:
                        ev = json.loads(line)
                        if not isinstance(ev, dict):
                            raise ValueError
                    except ValueError:
                        ev = {"type": "noop"}
                    _EV["list"].append(ev)
                _EV["off"] += end + 1
        return _EV["list"]


def _append_event(ev: dict) -> None:
    ev.setdefault("ts", time.time())
    line = (json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with open(events_path(), "ab") as f:
        f.write(line)


def fold(upto=None) -> dict:
    """把事件 0..upto 折叠成 {coll: {id: obj}}（upto=None 表示全部）。"""
    evs = events_refresh()
    end = len(evs) if upto is None else max(0, min(len(evs), upto + 1))
    st = {c: {} for c in COLLS}
    for ev in evs[:end]:
        c, t = ev.get("coll"), ev.get("type")
        if c not in st or not isinstance(ev.get("id"), str):
            continue
        if t == "put" and isinstance(ev.get("obj"), dict):
            st[c][ev["id"]] = ev["obj"]
        elif t == "del":
            st[c].pop(ev["id"], None)
    return st


def public_event(i: int, ev: dict) -> dict:
    return {"i": i, "ts": ev.get("ts"), "type": ev.get("type"), "coll": ev.get("coll"), "id": ev.get("id"),
            "label": ev.get("label", ""), "sig": bool(ev.get("sig")), "note": ev.get("note", ""),
            "tags": ev.get("tags") or []}


def _node_map(g):
    if not isinstance(g, dict):
        return {}
    return {n["id"]: n for n in (g.get("nodes") or []) if isinstance(n, dict) and isinstance(n.get("id"), str)}


def _def_label(coll, iid, old, new) -> str:
    cn = {"pipelines": "Pipeline", "datasets": "数据图", "modules": "展示模块"}[coll]
    title = (new.get("title") if isinstance(new, dict) else None) or iid
    if old is None:
        return f"新增{cn}「{title}」"
    parts = []
    if coll in GRAPH_KINDS:
        a, b = _node_map(old), _node_map(new)
        nm = lambda m, i: str(m[i].get("name") or i)  # noqa: E731
        add = [nm(b, i) for i in b if i not in a]
        rm = [nm(a, i) for i in a if i not in b]
        chg = [nm(b, i) for i in b if i in a and a[i].get("status") != b[i].get("status")]
        for tag, lst in (("新增节点", add), ("移除节点", rm), ("状态变化", chg)):
            if lst:
                parts.append(f"{tag} " + "、".join(lst[:3]) + ("…" if len(lst) > 3 else ""))
        ea = {(e.get("from"), e.get("to")) for e in old.get("edges", []) if isinstance(e, dict)}
        eb = {(e.get("from"), e.get("to")) for e in new.get("edges", []) if isinstance(e, dict)}
        if ea != eb:
            parts.append("连线调整")
    return f"更新{cn}「{title}」" + ("：" + "；".join(parts) if parts else "")


def _task_label(t: dict) -> str:
    name = t.get("name") or t.get("id")
    p = None
    if t.get("total"):
        p = min(1.0, (t.get("done") or 0) / t["total"])
    elif t.get("progress") is not None:
        p = t["progress"]
    kind = {"train": "训练", "eval": "评测", "run": "流水线执行"}.get(t.get("kind"), "任务")
    return f"{kind}「{name}」{STATUS_CN.get(t.get('status'), t.get('status'))}" + (f" {p * 100:.0f}%" if p is not None else "")


def _slim_task(t: dict) -> dict:
    """写入时间线的任务快照：曲线最多保留 100 点，控制日志体积。"""
    t = json.loads(json.dumps(t))
    for name, pts in (t.get("series") or {}).items():
        if len(pts) > 100:
            step = len(pts) / 100.0
            t["series"][name] = [pts[int(i * step)] for i in range(100)] + [pts[-1]]
    return t


def _log_put(coll, iid, obj, label, sig):
    _append_event({"type": "put", "coll": coll, "id": iid, "obj": obj, "label": label, "sig": sig})


def add_milestone(title: str, note: str = "", tags=None) -> dict:
    title = _str(title, "title", 120).strip()
    if not title:
        raise ValueError("title 不能为空")
    note = _str(note or "", "note", 2000)
    tags = [_str(x, "tag", 30) for x in (tags or [])][:8]
    ev = {"type": "milestone", "label": title, "note": note, "tags": tags, "sig": True}
    with lock():
        _append_event(ev)
    return ev


def sync_files() -> int:
    """把手工修改的定义文件、绕过 sb 写入的任务补记进时间线；返回新增事件数。"""
    n = 0
    with lock():
        known = fold(None)
        for coll in COLLS:
            files = set(list_ids(coll))
            for iid in files:
                if not ID_RE.match(iid):
                    continue
                try:
                    obj = read_json(item_path(coll, iid))
                except (ValueError, OSError):
                    continue
                if not isinstance(obj, dict):
                    continue
                old = known[coll].get(iid)
                if coll == "tasks":
                    if old is None:
                        _log_put(coll, iid, _slim_task(obj), _task_label(obj), True)
                        n += 1
                elif old is None or _canon(old) != _canon(obj):
                    _log_put(coll, iid, obj, _def_label(coll, iid, old, obj), True)
                    n += 1
            for iid in list(known[coll]):
                if iid not in files:
                    _append_event({"type": "del", "coll": coll, "id": iid, "sig": True,
                                   "label": f"删除 {coll}/{iid}"})
                    n += 1
    return n


# ── 写操作 ────────────────────────────────────────────────────────────────────
def update_task(task_id: str, patch: dict) -> dict:
    clean = clean_task_patch(patch)
    path = item_path("tasks", task_id)
    now = time.time()
    with lock():
        t = read_json(path, None) or {}
        existed = bool(t)
        old_status = t.get("status")
        if not existed:
            t["created_at"] = now
        items_patch = clean.pop("items", None)
        append = clean.pop("append", None)
        t.update(clean)
        if items_patch:
            cur = {str(i["id"]): i for i in t.get("items", [])}
            for it in items_patch:
                cur.setdefault(it["id"], {}).update(it, at=now)
            if len(cur) > MAX_ITEMS:
                raise ValueError(f"items 超过 {MAX_ITEMS} 个，请改用 step_counts 汇总")
            t["items"] = list(cur.values())
        if append:
            series = t.setdefault("series", {})
            for name, pts in append.items():
                arr = series.setdefault(name, [])
                arr.extend(pts)
                while len(arr) > MAX_SERIES_POINTS:
                    del arr[1:-1:2]
            if len(series) > 8:
                raise ValueError("曲线条数超过 8")
        t["id"] = task_id
        t["updated_at"] = now
        t.setdefault("status", "running")
        t.setdefault("kind", "job")
        if t["status"] == "running" and not t.get("started_at"):
            t["started_at"] = now
        if t["status"] in TERMINAL:
            t["finished_at"] = t.get("finished_at") or now
            if t["status"] == "done" and t.get("total") and "done" not in clean:
                t["done"] = t["total"]
        else:
            t.pop("finished_at", None)
        if len(json.dumps(t, ensure_ascii=False)) > 300_000:
            raise ValueError("任务记录过大（>300KB）")
        status_changed = t["status"] != old_status
        log = (not existed) or status_changed or (now - t.get("logged_at", 0) >= EVENT_GAP)
        if log:
            t["logged_at"] = now
        write_json(path, t)
        if log:
            _log_put("tasks", task_id, _slim_task(t), _task_label(t), (not existed) or status_changed)
    return t


def delete_task(task_id: str) -> bool:
    path = item_path("tasks", task_id)
    with lock():
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        _append_event({"type": "del", "coll": "tasks", "id": task_id, "sig": True, "label": f"删除任务 {task_id}"})
        return True


def update_node(kind: str, gid: str, nid: str, patch: dict) -> dict:
    if kind not in GRAPH_KINDS:
        raise ValueError("kind 必须是 pipelines 或 datasets")
    check_id(nid)
    clean = clean_node_patch(patch)
    path = item_path(kind, gid)
    with lock():
        g = read_json(path, None)
        if g is None:
            raise FileNotFoundError(f"{kind}/{gid} 不存在")
        for n in g.get("nodes", []):
            if isinstance(n, dict) and n.get("id") == nid:
                old = json.loads(json.dumps(g))
                meta = clean.pop("meta", None)
                n.update(clean)
                if meta:
                    n.setdefault("meta", {}).update(meta)
                if _canon(old) != _canon(g):
                    write_json(path, g)
                    _log_put(kind, gid, g, f"节点「{n.get('name') or nid}」更新"
                             + (f"：{clean['status']}" if "status" in clean else ""), True)
                return n
        raise FileNotFoundError(f"节点 {nid} 不存在")


def define(coll: str, item_id: str, obj) -> dict:
    """写入（覆盖）一个定义文件并记入时间线；内容没变则不产生事件。"""
    check_id(item_id)
    clean = clean_definition(coll, obj)
    path = item_path(coll, item_id)
    with lock():
        try:
            old = read_json(path, None)
        except ValueError:
            old = None
        if old is not None and _canon(old) == _canon(clean):
            return clean
        write_json(path, clean)
        _log_put(coll, item_id, clean, _def_label(coll, item_id, old, clean), True)
    return clean


# ── 体检 ──────────────────────────────────────────────────────────────────────
def lint() -> list:
    """检查定义是否完整、引用是否有效。返回 [(level, message)]，level 为 error / warn。"""
    out = []

    def add(level, msg):
        out.append((level, msg))

    raw = {c: {} for c in COLLS}
    for c in COLLS:
        for iid in list_ids(c):
            try:
                o = read_json(item_path(c, iid))
                if not isinstance(o, dict):
                    raise ValueError("顶层必须是对象")
                raw[c][iid] = o
            except (ValueError, OSError) as e:
                add("error", f"{c}/{iid}.json 无法解析: {e}")
    nodes_of = {k: {gid: _node_map(g) for gid, g in raw[k].items()} for k in GRAPH_KINDS}

    def ref_ok(kind, ref):
        if not isinstance(ref, str) or "/" not in ref:
            return False
        gid, nid = ref.split("/", 1)
        return nid in nodes_of[kind].get(gid, {})

    for kind in GRAPH_KINDS:
        for gid, g in raw[kind].items():
            tag = f"{kind}/{gid}"
            try:
                clean_definition(kind, g)
            except ValueError as e:
                add("error", f"{tag}: {e}")
                continue
            if not g.get("title"):
                add("warn", f"{tag}: 缺少 title")
            if not g.get("description"):
                add("warn", f"{tag}: 缺少 description（整体中文说明）")
            ids = [n["id"] for n in g["nodes"]]
            linked = {e["from"] for e in g.get("edges", [])} | {e["to"] for e in g.get("edges", [])}
            indeg = {i: 0 for i in ids}
            for e in g.get("edges", []):
                indeg[e["to"]] += 1
            q, seen = [i for i in ids if indeg[i] == 0], 0
            while q:
                x = q.pop()
                seen += 1
                for e in g.get("edges", []):
                    if e["from"] == x:
                        indeg[e["to"]] -= 1
                        if indeg[e["to"]] == 0:
                            q.append(e["to"])
            if seen != len(ids):
                add("error", f"{tag}: 存在环，无法分层布局")
            for n in g["nodes"]:
                nt = f"{tag}:{n['id']}"
                if not n.get("name"):
                    add("warn", f"{nt}: 缺少 name")
                if len(ids) > 1 and n["id"] not in linked:
                    add("warn", f"{nt}: 孤立节点（没有任何连线）")
                if kind == "pipelines":
                    if not n.get("role"):
                        add("warn", f"{nt}: 缺少 role（一句话说明这一步的作用，会直接显示在结构图上）")
                    if not n.get("desc"):
                        add("warn", f"{nt}: 缺少 desc（详细说明）")
                    for r in (n.get("reads") or []) + (n.get("writes") or []):
                        if not ref_ok("datasets", r):
                            add("warn", f"{nt}: reads/writes 引用的数据节点不存在: {r}")
                else:
                    locs = n.get("locations")
                    if not isinstance(locs, list) or not locs:
                        add("warn", f"{nt}: 缺少 locations（数据存放地址；有备份要写多个）")
                    else:
                        for loc in locs:
                            if not isinstance(loc, dict) or not loc.get("path"):
                                add("warn", f"{nt}: locations 里有条目缺少 path")
                    for f, cn in (("source", "来源"), ("purpose", "用处"), ("processing", "被处理情况")):
                        if not n.get(f):
                            add("warn", f"{nt}: 缺少 {f}（{cn}）")
    for tid, t in raw["tasks"].items():
        tt = f"tasks/{tid}"
        p = t.get("pipeline")
        if p:
            if p not in raw["pipelines"]:
                add("warn", f"{tt}: 引用的 pipeline 不存在: {p}")
            elif t.get("node") and t["node"] not in nodes_of["pipelines"].get(p, {}):
                add("warn", f"{tt}: 引用的节点不存在: {p}/{t['node']}")
        for d in t.get("datasets") or []:
            if d.get("ref") and not ref_ok("datasets", d["ref"]):
                add("warn", f"{tt}: datasets.ref 不存在: {d['ref']}")
            if not d.get("ref") and not d.get("path"):
                add("warn", f"{tt}: datasets 条目既没有 ref 也没有 path（无法追溯数据位置）")
        kind = t.get("kind", "job")
        if kind in ("train", "eval"):
            if not t.get("model"):
                add("warn", f"{tt}: {kind} 任务缺少 model")
            if not t.get("datasets"):
                add("warn", f"{tt}: {kind} 任务缺少 datasets（用了哪些数据/评测集）")
        if kind == "run" and not p:
            add("warn", f"{tt}: run 任务缺少 pipeline")
    return out
