#!/usr/bin/env python3
"""status_board 看板服务（仅标准库，可整体拷走）。

    python3 server.py                                  # 127.0.0.1:8765
    python3 server.py --host 0.0.0.0 --port 8765       # 对外监听前必须先设置 SB_TOKEN，否则写接口自动关闭

环境变量: SB_STATE_DIR 状态目录 | SB_TOKEN 写接口令牌 | SB_ALLOWED_HOSTS 额外允许的 Host(逗号分隔) | SB_ASSET_ROOTS 额外只读资源根（别名=路径:别名=路径）
          SB_ASSETS_DIR 展示模块可引用的资源目录 | SB_SHOW_PATHS=1 对外监听时也向浏览器返回数据真实路径
"""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import resources
import review
import sbstore as store
import procs
import versions

STATIC = os.path.join(store.ROOT, "static")
STATIC_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.(js|css|html)$")
STATIC_MIME = {"js": "application/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
               "html": "text/html; charset=utf-8"}
ASSET_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg",
              ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
STALE_DEFAULT = 300.0
STAT_TTL = float(os.environ.get("SB_STAT_TTL", "300"))
STAT_BUDGET = float(os.environ.get("SB_STAT_BUDGET", "30"))
FILE_TTL = float(os.environ.get("SB_STAT_FILE_TTL", "3"))     # 单个文件的 stat 几乎免费，正在被追加写入的 jsonl / 日志几秒就刷新
REFRESH_MIN, REFRESH_MAX = 5.0, 86400.0                        # 地址上 refresh 字段的允许范围（秒）
TIMELINE_MAX = int(os.environ.get("SB_TIMELINE_MAX", "5000"))

CONFIG = {"token": "", "writable": True, "allowed_hosts": set(LOOPBACK), "loopback": True, "show_paths": True}


# ── 资源目录（图片 / 音视频 / jsonl 样本）：沙箱解析在 sbstore 里，与成果校验共用 ──────
assets_dir = store.assets_dir
resolve_asset = store.resolve_asset


def _shrink(o, depth=0):
    if depth > 6:
        return "…"
    if isinstance(o, str):
        return o[:2000]
    if isinstance(o, list):
        return [_shrink(x, depth + 1) for x in o[:50]]
    if isinstance(o, dict):
        return {str(k)[:60]: _shrink(v, depth + 1) for k, v in list(o.items())[:50]}
    return o


_sample_cache: dict = {}


def read_samples(rel, skip, limit) -> dict:
    full = resolve_asset(rel)
    if not full or not os.path.isfile(full) or os.path.splitext(full)[1].lower() not in (".jsonl", ".json"):
        return {"error": f"样本文件不可用（需在资源目录内的 .jsonl）: {str(rel)[:80]}"}
    try:
        skip = max(0, min(int(skip or 0), 200000))
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        skip, limit = 0, 10
    key = (full, os.path.getmtime(full), skip, limit)
    if key in _sample_cache:
        return _sample_cache[key]
    out, i = [], 0
    try:
        with open(full, "rb") as f:
            while len(out) < limit:
                line = f.readline(200001)
                if not line:
                    break
                too_long = len(line) >= 200001 and not line.endswith(b"\n")
                if too_long:
                    while True:                       # 丢弃该行剩余部分
                        chunk = f.readline(1 << 20)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                if i >= skip:
                    if too_long:
                        out.append({"_raw": "（该行超过 200KB，已跳过）"})
                    else:
                        try:
                            out.append(_shrink(json.loads(line)))
                        except ValueError:
                            out.append({"_raw": line[:500].decode("utf-8", "replace")})
                i += 1
    except OSError as e:
        return {"error": f"读取失败: {e.__class__.__name__}"}
    res = {"items": out, "skip": skip}
    if len(_sample_cache) > 64:
        _sample_cache.clear()
    _sample_cache[key] = res
    return res


# ── 目录大小统计（后台线程 + 缓存，接口永不阻塞）─────────────────────────────
_stat_cache: dict = {}
_stat_lock = threading.Lock()


def _walk_size(path: str) -> dict:
    deadline = time.time() + STAT_BUDGET
    size = files = 0
    stack, truncated = [path], False
    while stack:
        if time.time() > deadline:
            truncated = True
            break
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            size += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return {"size": size, "files": files, "truncated": truncated}


def _stat_worker(path: str):
    try:
        if os.path.isfile(path):
            res = {"size": os.path.getsize(path), "files": 1, "truncated": False, "kind": "file"}
        elif os.path.isdir(path):
            res = {**_walk_size(path), "kind": "dir"}
        else:
            res = {"missing": True}
    except OSError as e:
        res = {"error": e.__class__.__name__}
    res["ts"] = time.time()
    with _stat_lock:
        _stat_cache[path] = res


def path_stat(path: str, ttl=None) -> dict:
    """路径的实测大小 / 文件数：后台线程统计，永远立刻返回缓存（过期的旧值先顶着，后台刷新，页面不会被慢盘拖住）。
    刷新间隔：地址上写了 refresh 就用它；否则单个文件 SB_STAT_FILE_TTL（默认 3 秒），目录 SB_STAT_TTL（默认 300 秒，逐文件累加较贵）。"""
    now = time.time()
    with _stat_lock:
        c = _stat_cache.get(path)
        if c is None:
            _stat_cache[path] = {"pending": True, "ts": 0, "started": now}
        elif c.get("pending") and now - c.get("started", 0) < STAT_BUDGET * 3:
            return {"pending": True}
        elif not c.get("pending") and now - c["ts"] < (ttl if ttl is not None else FILE_TTL if c.get("kind") == "file" else STAT_TTL):
            return c
        else:
            _stat_cache[path] = {**c, "pending": True, "started": now}
    threading.Thread(target=_stat_worker, args=(path,), daemon=True).start()
    return {k: v for k, v in (c or {"pending": True}).items() if k != "started"}


# ── 状态汇总 ──────────────────────────────────────────────────────────────────
def load_raw() -> dict:
    """直接读磁盘文件，得到「当前」原始状态。"""
    raw = {}
    for c in store.COLLS:
        raw[c] = {}
        for iid in store.list_ids(c):
            try:
                obj = store.read_json(store.item_path(c, iid))
                if not isinstance(obj, dict):
                    raise ValueError("顶层必须是对象")
            except (ValueError, OSError) as e:
                obj = {"__error__": f"{iid}.json 解析失败: {e}"}
            raw[c][iid] = obj
    return raw


def _counts_from_items(items) -> dict:
    c = {}
    for it in items or []:
        step = it.get("step")
        if not step:
            continue
        st = it.get("status") or "pending"
        st = "done" if st == "skipped" else st
        d = c.setdefault(step, {"running": 0, "done": 0, "failed": 0, "pending": 0})
        if st in d:
            d[st] += 1
    return c


def _task_view(t: dict, now: float, live: bool = True) -> dict:
    shards = t.get("shards") or []
    done, total = t.get("done"), t.get("total")
    if total in (None, 0) and shards:
        total = sum(s.get("total") or 0 for s in shards) or None
        done = sum(s.get("done") or 0 for s in shards)
    for s in shards:
        st = s.get("total")
        s["progress"] = min(1.0, (s.get("done") or 0) / st) if st else (1.0 if s.get("status") == "done" else None)
    if total:
        p = min(1.0, (done or 0) / total)
    elif t.get("progress") is not None:
        p = t["progress"]
    else:
        p = 1.0 if t.get("status") == "done" else None
    status = t.get("status", "running")
    start = t.get("started_at") or t.get("created_at") or now
    end = t.get("finished_at") or now
    t["progress"], t["done"], t["total"] = p, done, total
    t["elapsed"] = max(0.0, end - start)
    t["eta"] = None
    if status == "running" and p and 0 < p < 1:
        t["eta"] = t["elapsed"] * (1 - p) / p
    t["rate"] = (done / t["elapsed"]) if (done and t["elapsed"] > 5 and status == "running") else None
    stale_after = t.get("stale_after") or STALE_DEFAULT
    t["idle"] = max(0.0, now - t.get("updated_at", now))
    t["stale"] = status == "running" and t["idle"] > stale_after
    t["dead"] = False
    if live and t.get("pid") and status in ("running", "queued"):          # 回放历史时不查：过去的 pid 现在不在是正常的
        t["proc"] = procs.check(t["pid"], t.get("host"), t.get("pid_start"))      # alive / dead / remote / unknown
        t["dead"] = t["proc"] == "dead"                                            # 任务还写着运行中，但进程已经没了
        if t["dead"]:
            t["eta"] = None
    if t.get("items") and not t.get("step_counts"):
        t["step_counts"] = _counts_from_items(t["items"])
    return t


def _pick_status(c: list):
    if not c:
        return None
    if "running" in c:
        return "running"
    if "failed" in c:
        return "failed"
    if all(x in ("done", "skipped") for x in c):
        return "skipped" if all(x == "skipped" for x in c) else "done"
    return "pending"


def _clean_graph(g: dict) -> dict:
    g["nodes"] = [n for n in g.get("nodes", []) if isinstance(n, dict) and isinstance(n.get("id"), str)]
    ids = {n["id"] for n in g["nodes"]}
    g["edges"] = [{"from": e["from"], "to": e["to"], "label": str(e.get("label") or "")}
                  for e in (g.get("edges") or [])
                  if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids]
    g["groups"] = [{"id": str(x.get("id") or i), "name": str(x.get("name") or ""), "desc": str(x.get("desc") or ""),
                    "nodes": [i2 for i2 in x.get("nodes", []) if i2 in ids]}
                   for i, x in enumerate(g.get("groups") or []) if isinstance(x, dict)]
    return g


def _derive_pipeline(g: dict, tasks: list):
    nodes = g["nodes"]
    children = {n["id"]: [] for n in nodes}
    for e in g["edges"]:
        children[e["from"]].append(e["to"])
    desc = {}
    for nid in children:                       # 每个节点的全部后代
        seen, stack = set(), list(children[nid])
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(children[x])
        desc[nid] = seen
    runs = [t for t in tasks if t.get("kind") == "run" and t.get("pipeline") == g["id"] and t.get("step_counts")]
    for n in nodes:
        nid = n["id"]
        linked = [t for t in tasks if t.get("pipeline") == g["id"] and t.get("node") == nid and t.get("kind") != "run"]
        n["tasks"] = [t["id"] for t in linked]
        cands = [t["status"] for t in linked]
        ps = [t["progress"] for t in linked if t.get("progress") is not None]
        if any(t.get("stale") for t in linked):
            n["stale"] = True
        agg = {"running": 0, "done": 0, "failed": 0, "pending": 0}
        passed = total_items = 0
        for r in runs:
            sc = r["step_counts"]
            total_items += sum(sum(c.values()) for c in sc.values())
            for k in agg:
                agg[k] += sc.get(nid, {}).get(k, 0)
            passed += sum(sum(sc.get(d, {}).values()) for d in desc[nid])
            if sc.get(nid) and sum(sc[nid].values()):
                n["tasks"].append(r["id"])
        if runs and (sum(agg.values()) or passed):
            n["items"] = {**agg, "passed": passed, "total": total_items}
            if agg["running"]:
                cands.append("running")
            if agg["failed"]:
                cands.append("failed")
            if agg["done"] or passed:
                cands.append("done")
            if agg["pending"] and not (agg["done"] or passed):
                cands.append("pending")
            if total_items:
                ps.append(min(1.0, (passed + agg["done"]) / total_items))
        st = _pick_status(cands)
        if st:
            n["status"] = st
        if ps:
            n["progress"] = sum(ps) / len(ps)
        n.setdefault("status", "pending")


def _derive_dataset(g: dict):
    versions.annotate(g["nodes"])                      # 同一份数据的多个版本：图内按 family 归组
    for n in g["nodes"]:
        meta = n.get("meta") if isinstance(n.get("meta"), dict) else {}
        locs = n.get("locations") if isinstance(n.get("locations"), list) else []
        if not locs and isinstance(meta.get("path"), str):      # 兼容旧写法
            locs = [{"label": "主", "role": "primary", "path": meta["path"], "auto_stat": bool(meta.get("auto_stat"))}]
        out = []
        for loc in locs:
            if not isinstance(loc, dict):
                continue
            loc = dict(loc)
            if loc.get("auto_stat") and isinstance(loc.get("path"), str):
                rf = loc.get("refresh")
                loc["stat"] = path_stat(loc["path"], max(REFRESH_MIN, min(REFRESH_MAX, float(rf))) if isinstance(rf, (int, float)) and not isinstance(rf, bool) else None)
            out.append(loc)
        measured = [l for l in out if isinstance(l.get("stat"), dict) and "size" in l["stat"] and not l["stat"].get("truncated")]
        if len(out) >= 2:
            if len(measured) >= 2:
                same = len({l["stat"]["size"] for l in measured}) == 1 and len({l["stat"]["files"] for l in measured}) == 1
                n["replica"] = "consistent" if same else "mismatch"
            elif any((l.get("stat") or {}).get("missing") for l in out):
                n["replica"] = "missing_copy"
            else:
                n["replica"] = "unchecked"
        primary = next((l for l in out if l.get("role") == "primary"), out[0] if out else None)
        pst = (primary or {}).get("stat") or {}
        n.setdefault("status", "ready")
        if pst.get("missing"):
            n["status"] = "missing"
        if "size" in pst:
            n["size_bytes"], n["size_exact"] = pst["size"], not pst.get("truncated")
        elif isinstance(meta.get("size_bytes"), (int, float)):
            n["size_bytes"], n["size_exact"] = meta["size_bytes"], True
        if not CONFIG["show_paths"]:                     # 对外监听时不向浏览器暴露真实路径
            for l in out:
                l.pop("path", None)
            meta.pop("path", None)
        n["locations"] = out


def _derive_module(m: dict):
    for b in m.get("blocks", []):
        if isinstance(b, dict) and b.get("type") == "samples" and isinstance(b.get("source"), str):
            res = read_samples(b["source"], b.get("skip"), b.get("limit"))
            if "items" in res:
                b["items"] = res["items"]
            else:
                b["error"] = res["error"]


def _derive_review(r: dict, marks_obj, live: bool):
    """校验集在状态里只带「汇总」：人工标记数 + 规则扫描结果。样本明细走单独的 /api/review 接口。
    回放历史时，标记汇总取时间线里当时的快照；规则扫描读的是当前文件，所以只在实时模式显示。"""
    marks_obj = marks_obj if isinstance(marks_obj, dict) else {}
    if "counts" in marks_obj:                          # 时间线快照：只有汇总
        c = dict(marks_obj["counts"])
    else:
        c = {"pass": 0, "weak": 0, "fail": 0, "doubt": 0}
        for m in (marks_obj.get("marks") or {}).values():
            if isinstance(m, dict) and m.get("v") in c:
                c[m["v"]] += 1
    r["marked"] = c
    if live:
        sm = review.summary(r["id"], r)
        r["rows"], r["scan"] = sm.get("rows"), sm.get("scan")
        if sm.get("error"):
            r["source_error"] = sm["error"]
    if not CONFIG["show_paths"]:
        r.pop("source", None)                          # 资源目录相对路径也不对外暴露
    r["fields"] = [{k: v for k, v in f.items() if k in ("label", "key", "type", "a", "b")} for f in r.get("fields", []) if isinstance(f, dict)]
    if isinstance(r.get("gallery"), dict):             # 状态里只带侧栏要用的分组信息；字段映射留在服务端
        g = r["gallery"]
        r["gallery"] = {k: g.get(k) for k in ("group", "version", "badge") if g.get(k)} or {"group": None}


def build_state(raw: dict, now: float, live: bool = True) -> dict:
    board = {}
    try:
        board = store.read_json(os.path.join(store.state_dir(), "board.json"), {}) or {}
    except (ValueError, OSError):
        pass
    tasks = []
    for iid, t in raw["tasks"].items():
        if "__error__" not in t:
            t = copy.deepcopy(t)
            t["id"] = iid
            tasks.append(_task_view(t, now, live))
    tasks.sort(key=lambda t: (t.get("started_at") or t.get("created_at") or 0))
    versions.annotate(tasks, scope=lambda t: t.get("kind") or "job")           # 版本号相同 / 不可比时：开始得晚的算新
    tasks.sort(key=lambda t: -(t.get("started_at") or t.get("created_at") or 0))
    out = {"now": now, "title": str(board.get("title") or "状态看板")[:80],
           "subtitle": str(board.get("subtitle") or "")[:200], "writable": CONFIG["writable"], "tasks": tasks}
    for kind in store.GRAPH_KINDS + ("modules", "reviews", "resources"):
        items = []
        for iid, obj in raw[kind].items():
            if "__error__" in obj:
                items.append({"id": iid, "title": iid, "error": obj["__error__"]})
                continue
            g = copy.deepcopy(obj)
            g["id"] = iid
            try:
                if kind == "modules":
                    _derive_module(g)
                elif kind == "reviews":
                    _derive_review(g, raw["marks"].get(iid), live)
                elif kind == "resources":
                    resources.derive(g, raw["usage"].get(iid), now, live, CONFIG["show_paths"])
                else:
                    _clean_graph(g)
                    (_derive_pipeline if kind == "pipelines" else _derive_dataset)(g, *([tasks] if kind == "pipelines" else []))
            except Exception as e:  # noqa: BLE001  单个定义写坏不能拖垮整页
                g = {"id": iid, "title": g.get("title") or iid, "error": f"渲染失败: {e.__class__.__name__}: {e}"}
            items.append(g)
        versions.annotate(items)                                         # Pipeline / 数据图 / 展示模块 / 校验集：同类里按 family 归组
        out[kind] = items
    out["summary"] = {s: sum(1 for t in tasks if t["status"] == s) for s in store.TASK_STATUS}
    out["summary"]["stale"] = sum(1 for t in tasks if t["stale"])
    out["summary"]["dead"] = sum(1 for t in tasks if t.get("dead") and not versions.is_hist(t))
    out["by_kind"] = {k: sum(1 for t in tasks if t.get("kind", "job") == k) for k in store.TASK_KINDS}
    return out


def state_at(at):
    """at=None 取实时；否则回放到第 at 个事件之后的状态。"""
    if at is None:
        s = build_state(load_raw(), time.time(), True)
        s["live"] = True
    else:
        evs = store.events_refresh()
        if not 0 <= at < len(evs):
            raise ValueError("at 超出范围")
        folded = store.fold(at)
        s = build_state(folded, evs[at].get("ts") or time.time(), False)
        s["live"], s["at"] = False, at
        s["event"] = store.public_event(at, evs[at])
    s["timeline"] = {"n": len(store.events_refresh())}
    return s


# ── HTTP ──────────────────────────────────────────────────────────────────────
ROUTE_TASK = re.compile(r"^/api/tasks/([^/]+)$")
ROUTE_NODE = re.compile(r"^/api/(pipelines|datasets)/([^/]+)/nodes/([^/]+)$")
ROUTE_DEFINE = re.compile(r"^/api/define/(pipelines|datasets|modules|reviews|resources)/([^/]+)$")
ROUTE_RES = re.compile(r"^/api/resources/([^/]+)/([^/]+)$")
ROUTE_REVIEW = re.compile(r"^/api/review/([^/]+)$")
ROUTE_MARKS = re.compile(r"^/api/review/([^/]+)/marks$")
ROUTE_GALLERY = re.compile(r"^/api/review/([^/]+)/(gallery|item)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "status_board"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("SB_ACCESS_LOG"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str):
        self._headers(code, ctype, len(body))
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if not CONFIG["loopback"]:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("[") and "]" in host:
            host = host[1:host.index("]")]
        elif host.count(":") == 1:
            host = host.split(":")[0]
        return host in CONFIG["allowed_hosts"]

    def _write_allowed(self) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "Host 不被允许"})
            return False
        if not CONFIG["writable"]:
            self._json(403, {"error": "写接口已关闭：对外监听时需设置 SB_TOKEN"})
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            self._json(403, {"error": "跨源请求被拒绝"})
            return False
        if CONFIG["token"]:
            auth = self.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], CONFIG["token"])):
                self._json(401, {"error": "令牌无效"})
                return False
        return True

    def _body(self, limit=256 * 1024):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise ValueError("Content-Type 必须是 application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n) or b"{}")

    def _asset(self, rel: str):
        full = resolve_asset(rel)
        ctype = ASSET_MIME.get(os.path.splitext(full)[1].lower()) if full else None
        if not full or not ctype or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                return self._headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
            code = 206
        length = end - start + 1
        extra = {"Accept-Ranges": "bytes"}
        if code == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(code, ctype, length, extra)
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Host 不被允许（可设 SB_ALLOWED_HOSTS）"})
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/":
            path = "/index.html"
        name = path[1:]
        if STATIC_RE.match(name):
            try:
                with open(os.path.join(STATIC, name), "rb") as f:
                    return self._send(200, f.read(), STATIC_MIME[name.rsplit(".", 1)[1]])
            except FileNotFoundError:
                return self._json(404, {"error": "not found"})
        if path.startswith("/assets/"):
            return self._asset(unquote(path[len("/assets/"):]))
        try:
            if path == "/api/state":
                at = q.get("at", [None])[0]
                return self._json(200, state_at(int(at) if at not in (None, "") else None))
            if path == "/api/timeline":
                since = int(q.get("since", ["0"])[0] or 0)
                evs = store.events_refresh()
                n = len(evs)
                base = max(since, n - TIMELINE_MAX, 0)
                return self._json(200, {"n": n, "base": base, "reset": base > since and since > 0,
                                        "events": [store.public_event(i, evs[i]) for i in range(base, n)
                                                   if evs[i].get("type") != "noop"]})
            m = ROUTE_GALLERY.match(path)
            if m:
                return self._review_gallery(m.group(1), m.group(2), {k: v[0] for k, v in q.items()})
            m = ROUTE_REVIEW.match(path)
            if m:
                return self._review_page(m.group(1), {k: v[0] for k, v in q.items()})
            if path == "/healthz":
                return self._json(200, {"ok": True})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def _review_page(self, rid: str, q: dict):
        """读一页校验样本。回放历史时不提供（样本读的是当前文件，和历史状态对不上）。"""
        store.check_id(rid)
        d = store.read_json(store.item_path("reviews", rid), None)
        if not isinstance(d, dict):
            return self._json(404, {"error": f"校验集 {rid} 不存在"})
        try:
            store.clean_definition("reviews", d)
        except ValueError as e:
            return self._json(400, {"error": f"校验集定义无效: {e}"})
        res = review.page(rid, d, q)
        if "error" in res:
            return self._json(404, res)
        return self._json(200, res)

    def _review_gallery(self, rid: str, what: str, q: dict):
        """图库视图：gallery = 一页卡片 + 8 格统计；item = 一条的完整详情。回放历史时同样不提供。"""
        store.check_id(rid)
        d = store.read_json(store.item_path("reviews", rid), None)
        if not isinstance(d, dict):
            return self._json(404, {"error": f"校验集 {rid} 不存在"})
        try:
            store.clean_definition("reviews", d)
        except ValueError as e:
            return self._json(400, {"error": f"校验集定义无效: {e}"})
        if what == "item":
            try:
                line = int(q.get("line") or 0)
            except ValueError:
                return self._json(400, {"error": "line 必须是整数"})
            if not 1 <= line <= 50_000_000:
                return self._json(400, {"error": "line 必须是 >= 1 的整数"})
            res = review.gallery_item(rid, d, line)
        else:
            res = review.gallery_page(rid, d, q)
        return self._json(404 if "error" in res else 200, res)

    def do_POST(self):
        if not self._write_allowed():
            return
        path = urlparse(self.path).path
        try:
            m = ROUTE_TASK.match(path)
            if m:
                return self._json(200, store.update_task(m.group(1), self._body()))
            m = ROUTE_NODE.match(path)
            if m:
                return self._json(200, store.update_node(m.group(1), m.group(2), m.group(3), self._body()))
            m = ROUTE_RES.match(path)
            if m:
                return self._json(200, store.update_usage(m.group(1), m.group(2), self._body()))
            m = ROUTE_MARKS.match(path)
            if m:
                b = self._body()
                return self._json(200, store.set_marks(m.group(1), b.get("marks") if isinstance(b, dict) else None))
            if path == "/api/milestone":
                b = self._body()
                return self._json(200, store.add_milestone(b.get("title", ""), b.get("note", ""), b.get("tags")))
            self._json(404, {"error": "not found"})
        except FileNotFoundError as e:
            self._json(404, {"error": str(e)})
        except (ValueError, TypeError, AttributeError) as e:
            self._json(400, {"error": str(e)})

    def do_PUT(self):
        if not self._write_allowed():
            return
        m = ROUTE_DEFINE.match(urlparse(self.path).path)
        if not m:
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, store.define(m.group(1), m.group(2), self._body(1024 * 1024)))
        except (ValueError, TypeError) as e:
            self._json(400, {"error": str(e)})

    def do_DELETE(self):
        if not self._write_allowed():
            return
        m = ROUTE_TASK.match(urlparse(self.path).path)
        if not m:
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, {"deleted": store.delete_task(m.group(1))})
        except ValueError as e:
            self._json(400, {"error": str(e)})


def _sync_loop():
    while True:
        try:
            store.sync_files()
        except Exception as e:  # noqa: BLE001
            print(f"[sync] {e.__class__.__name__}: {e}", file=sys.stderr)
        time.sleep(2.0)


def main():
    ap = argparse.ArgumentParser(description="status_board 看板服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--state-dir", help="状态目录（默认 ./state）")
    ap.add_argument("--asset-root", action="append", default=[], metavar="别名=/绝对路径",
                    help="额外的只读资源根（可重复）：视频/图片留在原地不用复制，样本里写 <别名>/<相对路径>")
    a = ap.parse_args()
    if a.state_dir:
        os.environ["SB_STATE_DIR"] = a.state_dir
    if a.asset_root:       # 和环境变量 SB_ASSET_ROOTS 合并（命令行在前）；只在启动时登记，没有任何接口能添加
        os.environ["SB_ASSET_ROOTS"] = ":".join(a.asset_root + [os.environ.get("SB_ASSET_ROOTS", "")]).strip(":")
    store.ensure_dirs()
    mimetypes.init()
    CONFIG["token"] = os.environ.get("SB_TOKEN", "")
    CONFIG["loopback"] = a.host in LOOPBACK
    CONFIG["writable"] = CONFIG["loopback"] or bool(CONFIG["token"])
    CONFIG["show_paths"] = CONFIG["loopback"] or os.environ.get("SB_SHOW_PATHS") == "1"
    CONFIG["allowed_hosts"] |= {h.strip().lower() for h in os.environ.get("SB_ALLOWED_HOSTS", "").split(",") if h.strip()}
    for item in (os.environ.get("SB_ASSET_ROOTS") or "").split(":"):        # 登记了但目录不存在的，启动时就说出来
        name = item.split("=", 1)[0].strip()
        if "=" in item and name and name not in store.asset_roots():
            print(f"警告：额外资源根「{name}」无效（别名不合法或目录不存在），已忽略", flush=True)
    store.sync_files()
    threading.Thread(target=_sync_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"status_board: http://{a.host}:{a.port}  状态目录={store.state_dir()}  "
          f"写接口={'开启' if CONFIG['writable'] else '关闭（对外监听需设 SB_TOKEN）'}"
          f"{'，需令牌' if CONFIG['token'] else ''}  路径显示={'是' if CONFIG['show_paths'] else '否'}"
          f"{'  额外只读资源根=' + ','.join(sorted(store.asset_roots())) if store.asset_roots() else ''}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
