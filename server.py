#!/usr/bin/env python3
"""status_board 看板服务（仅标准库，可整体拷走）。

    python3 server.py                       # 127.0.0.1:8765
    python3 server.py --host 0.0.0.0 --port 8765    # 对外监听前必须先设置 SB_TOKEN，否则写接口自动关闭

环境变量: SB_STATE_DIR  状态目录   SB_TOKEN  写接口令牌   SB_ALLOWED_HOSTS  额外允许的 Host（逗号分隔）
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import sb

STATIC = os.path.join(sb.ROOT, "static")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 64 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
STALE_DEFAULT = 300.0
STAT_TTL = float(os.environ.get("SB_STAT_TTL", "300"))
STAT_BUDGET = float(os.environ.get("SB_STAT_BUDGET", "30"))

CONFIG = {"token": "", "writable": True, "allowed_hosts": set(LOOPBACK), "loopback": True}


# ── 目录大小统计（后台线程 + 缓存，接口永不阻塞）─────────────────────────────
_stat_cache: dict = {}
_stat_lock = threading.Lock()


def _walk_size(path: str) -> dict:
    deadline = time.time() + STAT_BUDGET
    size = files = 0
    stack = [path]
    truncated = False
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
            res = {"size": os.path.getsize(path), "files": 1, "truncated": False}
        elif os.path.isdir(path):
            res = _walk_size(path)
        else:
            res = {"missing": True}
    except OSError as e:
        res = {"error": e.__class__.__name__}
    res["ts"] = time.time()
    with _stat_lock:
        _stat_cache[path] = res


def path_stat(path: str) -> dict:
    now = time.time()
    with _stat_lock:
        c = _stat_cache.get(path)
        if c is None:
            _stat_cache[path] = {"pending": True, "ts": 0, "started": now}
            c = None
        elif c.get("pending") and now - c.get("started", 0) < STAT_BUDGET * 3:
            return {"pending": True}
        elif not c.get("pending") and now - c["ts"] < STAT_TTL:
            return c
        else:
            _stat_cache[path] = {**c, "pending": True, "started": now}
    threading.Thread(target=_stat_worker, args=(path,), daemon=True).start()
    return {k: v for k, v in (c or {"pending": True}).items() if k != "started"}


# ── 状态汇总 ──────────────────────────────────────────────────────────────────
def _load_dir(kind: str) -> list:
    d = os.path.join(sb.state_dir(), kind)
    out = []
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json") and not n.startswith("."))
    except FileNotFoundError:
        return out
    for n in names:
        iid = n[:-5]
        try:
            obj = sb.read_json(os.path.join(d, n))
            if not isinstance(obj, dict):
                raise ValueError("顶层必须是对象")
            obj["id"] = iid
            out.append(obj)
        except (ValueError, OSError) as e:
            out.append({"id": iid, "title": iid, "error": f"{n} 解析失败: {e}"})
    return out


def _task_view(t: dict, now: float) -> dict:
    t = dict(t)
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
    return t


def _derive_nodes(kind: str, g: dict, tasks: list):
    nodes = [n for n in g.get("nodes", []) if isinstance(n, dict) and n.get("id")]
    g["nodes"] = nodes
    g["edges"] = [{"from": e["from"], "to": e["to"], "label": str(e.get("label") or "")}
                  for e in (g.get("edges") or [])
                  if isinstance(e, dict) and isinstance(e.get("from"), str) and isinstance(e.get("to"), str)]
    if kind == "pipelines":
        for n in nodes:
            ts = [t for t in tasks if t.get("pipeline") == g["id"] and t.get("node") == n["id"]]
            n["tasks"] = [t["id"] for t in ts]
            if not ts:
                continue
            sts = {t["status"] for t in ts}
            if "running" in sts:
                n["status"] = "running"
            elif "failed" in sts:
                n["status"] = "failed"
            elif sts <= {"done"}:
                n["status"] = "done"
            elif sts <= {"done", "cancelled"}:
                n["status"] = "skipped"
            else:
                n["status"] = "pending"
            ps = [t["progress"] for t in ts if t.get("progress") is not None]
            if ps:
                n["progress"] = sum(ps) / len(ps)
            if any(t.get("stale") for t in ts):
                n["stale"] = True
    else:
        for n in nodes:
            meta = n.get("meta") or {}
            if meta.get("auto_stat") and isinstance(meta.get("path"), str):
                st = path_stat(meta["path"])
                n["stat"] = st
                if st.get("missing"):
                    n["status"] = "missing"
                elif not st.get("pending") and not st.get("error"):
                    n.setdefault("status", "ready")
    for n in nodes:
        n.setdefault("status", "pending" if kind == "pipelines" else "ready")
        if kind == "datasets":
            n.get("meta", {}).pop("path", None) if CONFIG["loopback"] is False else None


def build_state() -> dict:
    now = time.time()
    board = sb.read_json(os.path.join(sb.state_dir(), "board.json"), {}) or {}
    tasks = sorted((_task_view(t, now) for t in _load_dir("tasks") if "error" not in t),
                   key=lambda t: (t["status"] != "running", -t.get("updated_at", 0)))
    out = {"now": now, "title": str(board.get("title") or "状态看板")[:80],
           "subtitle": str(board.get("subtitle") or "")[:200],
           "writable": CONFIG["writable"],
           "tasks": tasks}
    for kind in sb.GRAPH_KINDS:
        gs = _load_dir(kind)
        for g in gs:
            if "error" not in g:
                _derive_nodes(kind, g, tasks)
        out[kind] = gs
    out["summary"] = {s: sum(1 for t in tasks if t["status"] == s) for s in sb.TASK_STATUS}
    out["summary"]["stale"] = sum(1 for t in tasks if t["stale"])
    return out


# ── HTTP ──────────────────────────────────────────────────────────────────────
ROUTE_TASK = re.compile(r"^/api/tasks/([^/]+)$")
ROUTE_NODE = re.compile(r"^/api/(pipelines|datasets)/([^/]+)/nodes/([^/]+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "status_board"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("SB_ACCESS_LOG"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if not CONFIG["loopback"]:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("[") and "]" in host:      # [::1]:8765
            host = host[1:host.index("]")]
        elif host.count(":") == 1:                    # name:port
            host = host.split(":")[0]
        return host in CONFIG["allowed_hosts"]

    def _write_allowed(self) -> bool:
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

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Host 不被允许（可设 SB_ALLOWED_HOSTS）"})
        path = urlparse(self.path).path
        if path in ASSETS:
            fn, ctype = ASSETS[path]
            try:
                with open(os.path.join(STATIC, fn), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except FileNotFoundError:
                return self._json(404, {"error": "静态文件缺失"})
        if path == "/api/state":
            return self._json(200, build_state())
        if path == "/healthz":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def _body(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise ValueError("Content-Type 必须是 application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "Host 不被允许"})
        if not self._write_allowed():
            return
        path = urlparse(self.path).path
        try:
            m = ROUTE_TASK.match(path)
            if m:
                return self._json(200, sb.update_task(m.group(1), self._body()))
            m = ROUTE_NODE.match(path)
            if m:
                return self._json(200, sb.update_node(m.group(1), m.group(2), m.group(3), self._body()))
            self._json(404, {"error": "not found"})
        except FileNotFoundError as e:
            self._json(404, {"error": str(e)})
        except (ValueError, json.JSONDecodeError) as e:
            self._json(400, {"error": str(e)})

    def do_DELETE(self):
        if not self._host_ok():
            return self._json(403, {"error": "Host 不被允许"})
        if not self._write_allowed():
            return
        m = ROUTE_TASK.match(urlparse(self.path).path)
        if not m:
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, {"deleted": sb.delete_task(m.group(1))})
        except ValueError as e:
            self._json(400, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="status_board 看板服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--state-dir", help="状态目录（默认 ./state）")
    a = ap.parse_args()
    if a.state_dir:
        os.environ["SB_STATE_DIR"] = a.state_dir
    sb.ensure_dirs()
    CONFIG["token"] = os.environ.get("SB_TOKEN", "")
    CONFIG["loopback"] = a.host in LOOPBACK
    CONFIG["writable"] = CONFIG["loopback"] or bool(CONFIG["token"])
    CONFIG["allowed_hosts"] |= {h.strip().lower() for h in os.environ.get("SB_ALLOWED_HOSTS", "").split(",") if h.strip()}
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"status_board: http://{a.host}:{a.port}  状态目录={sb.state_dir()}  "
          f"写接口={'开启' if CONFIG['writable'] else '关闭（对外监听需设 SB_TOKEN）'}"
          f"{'，需令牌' if CONFIG['token'] else ''}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
