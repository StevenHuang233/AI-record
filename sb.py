#!/usr/bin/env python3
"""status_board 状态读写层 + 命令行 + 脚本库（仅标准库，可整体拷走）。

状态目录（默认 <本文件目录>/state，可用环境变量 SB_STATE_DIR 改）:
    board.json              看板标题等（可选）
    pipelines/<id>.json     Pipeline 结构图定义
    datasets/<id>.json      数据管理图定义
    tasks/<id>.json         任务进度（由脚本上报，勿手写 updated_at）

上报方式（二选一）:
    本地直写：与看板服务共享同一目录/文件系统时，直接读写 JSON 文件（默认）
    HTTP    ：设置 SB_URL=http://host:port（和可选的 SB_TOKEN）后，改为调用看板的写接口

脚本里用:
    import sb
    sb.task("train", name="模型训练", pipeline="p1", node="train", status="running", done=10, total=100)
    for x in sb.track(items, "etl", name="数据清洗"): ...
    sb.node("pipelines", "p1", "clean", status="done")

命令行:
    python3 sb.py task train --name 模型训练 --done 10 --total 100 --message "step 10"
    python3 sb.py node datasets d1 raw --status ready --meta count=69999
    python3 sb.py rm-task train
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
TASK_STATUS = {"queued", "running", "done", "failed", "cancelled"}
NODE_STATUS = {"pending", "running", "done", "failed", "skipped",
               "ready", "building", "missing", "stale"}
TERMINAL = {"done", "failed", "cancelled"}
GRAPH_KINDS = ("pipelines", "datasets")
PROTECTED_META = {"path", "auto_stat"}   # 只能通过定义文件设置，API 不可改


# ── 路径与文件 ────────────────────────────────────────────────────────────────
def state_dir() -> str:
    return os.path.abspath(os.environ.get("SB_STATE_DIR") or os.path.join(ROOT, "state"))


def check_id(x) -> str:
    if not isinstance(x, str) or not ID_RE.match(x):
        raise ValueError("id 只能包含字母数字 _ . -，且以字母数字开头，长度 ≤ 64")
    return x


def item_path(kind: str, item_id: str) -> str:
    check_id(item_id)
    return os.path.join(state_dir(), kind, item_id + ".json")


def ensure_dirs():
    for k in ("pipelines", "datasets", "tasks"):
        os.makedirs(os.path.join(state_dir(), k), exist_ok=True)


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


# ── 入参清洗（HTTP 与本地共用） ───────────────────────────────────────────────
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
            out[k[:40]] = v[:200]
        else:
            raise ValueError(f"{name}.{k} 只支持标量")
    return out


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
        elif k == "status":
            if v not in TASK_STATUS:
                raise ValueError(f"status 必须是 {sorted(TASK_STATUS)}")
            out[k] = v
        elif k in ("done", "total", "started_at", "stale_after"):
            out[k] = _num(v, k)
        elif k == "progress":
            out[k] = min(1.0, _num(v, k))
        elif k == "metrics":
            out[k] = _scalars(v, k)
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


# ── 本地写操作（服务端也调用这里） ────────────────────────────────────────────
def update_task(task_id: str, patch: dict) -> dict:
    clean = clean_task_patch(patch)
    path = item_path("tasks", task_id)
    now = time.time()
    with lock():
        t = read_json(path, None) or {}
        if not t:
            t["created_at"] = now
        t.update(clean)
        t["id"] = task_id
        t["updated_at"] = now
        t.setdefault("status", "running")
        if t["status"] == "running" and not t.get("started_at"):
            t["started_at"] = now
        if t["status"] in TERMINAL:
            t["finished_at"] = t.get("finished_at") or now
            if t["status"] == "done" and t.get("total") and "done" not in clean:
                t["done"] = t["total"]
        else:
            t.pop("finished_at", None)
        write_json(path, t)
    return t


def delete_task(task_id: str) -> bool:
    path = item_path("tasks", task_id)
    with lock():
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


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
                meta = clean.pop("meta", None)
                n.update(clean)
                if meta:
                    n.setdefault("meta", {}).update(meta)
                write_json(path, g)
                return n
        raise FileNotFoundError(f"节点 {nid} 不存在")


# ── 脚本库接口（自动选择 本地直写 / HTTP） ────────────────────────────────────
def _http(method: str, path: str, body=None):
    url = os.environ["SB_URL"].rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if os.environ.get("SB_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["SB_TOKEN"])
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]!r}") from None


def task(task_id: str, **fields) -> dict:
    """创建/更新任务（只传要改的字段；每次调用都会刷新心跳）。"""
    check_id(task_id)
    if os.environ.get("SB_URL"):
        return _http("POST", f"/api/tasks/{task_id}", fields)
    return update_task(task_id, fields)


def node(kind: str, gid: str, nid: str, **fields) -> dict:
    """更新 pipeline / dataset 节点的状态、进度、说明或 meta。"""
    check_id(gid), check_id(nid)
    if os.environ.get("SB_URL"):
        return _http("POST", f"/api/{kind}/{gid}/nodes/{nid}", fields)
    return update_node(kind, gid, nid, fields)


def rm_task(task_id: str):
    check_id(task_id)
    if os.environ.get("SB_URL"):
        return _http("DELETE", f"/api/tasks/{task_id}")
    return delete_task(task_id)


def _safe_task(task_id, **fields):
    """上报失败不能拖垮业务脚本。"""
    try:
        task(task_id, **fields)
    except Exception as e:  # noqa: BLE001
        print(f"[sb] 上报失败（已忽略）: {e}", file=sys.stderr)


def track(iterable, task_id: str, total=None, every: float = 1.0, **fields):
    """包装迭代器，自动节流上报进度；循环结束置 done，异常置 failed。"""
    if total is None:
        try:
            total = len(iterable)
        except TypeError:
            total = None
    _safe_task(task_id, status="running", done=0, total=total, **fields)
    n, last = 0, time.time()
    try:
        for x in iterable:
            yield x
            n += 1
            if time.time() - last >= every:
                _safe_task(task_id, done=n)
                last = time.time()
    except Exception as e:
        _safe_task(task_id, status="failed", done=n, message=f"{type(e).__name__}: {e}"[:300])
        raise
    _safe_task(task_id, status="done", done=n, total=total if total is not None else n)


# ── 命令行 ────────────────────────────────────────────────────────────────────
def _kv(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = float(v) if "." in v or "e" in v.lower() else int(v)
        except ValueError:
            out[k] = v
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sb.py", description="status_board 上报工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("task", help="创建/更新任务")
    t.add_argument("id")
    t.add_argument("--name")
    t.add_argument("--status", choices=sorted(TASK_STATUS))
    t.add_argument("--done", type=float)
    t.add_argument("--total", type=float)
    t.add_argument("--progress", type=float, help="0~1，没有 done/total 时使用")
    t.add_argument("--unit")
    t.add_argument("--message")
    t.add_argument("--pipeline")
    t.add_argument("--node")
    t.add_argument("--stale-after", type=float, dest="stale_after", help="多少秒无心跳判为停滞")
    t.add_argument("--metric", action="append", help="k=v，可重复")

    n = sub.add_parser("node", help="更新图节点")
    n.add_argument("kind", choices=GRAPH_KINDS)
    n.add_argument("id")
    n.add_argument("node_id")
    n.add_argument("--status", choices=sorted(NODE_STATUS))
    n.add_argument("--progress", type=float)
    n.add_argument("--note")
    n.add_argument("--meta", action="append", help="k=v，可重复")

    r = sub.add_parser("rm-task", help="删除任务")
    r.add_argument("id")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "task":
            f = {k: getattr(a, k) for k in ("name", "status", "done", "total", "progress", "unit",
                                              "message", "pipeline", "node", "stale_after")}
            if a.metric:
                f["metrics"] = _kv(a.metric)
            task(a.id, **f)
        elif a.cmd == "node":
            f = {k: getattr(a, k) for k in ("status", "progress", "note")}
            if a.meta:
                f["meta"] = _kv(a.meta)
            node(a.kind, a.id, a.node_id, **f)
        else:
            rm_task(a.id)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
