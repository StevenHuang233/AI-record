#!/usr/bin/env python3
"""status_board 上报库 + 命令行（仅标准库；依赖同目录的 sbstore.py）。

上报方式（二选一）:
    本地直写：与看板共用同一个状态目录（SB_STATE_DIR）时，直接读写文件（默认）
    HTTP    ：设置 SB_URL=http://host:port（和可选的 SB_TOKEN）后，改为调用看板写接口

脚本里用:
    import sb
    sb.task("train1", name="微调", kind="train", model={"name": "m-7b"},
            datasets=[{"name": "训练集", "ref": "main/clean", "path": "/data/x.jsonl", "role": "train"}],
            status="running", done=10, total=1000)
    sb.point("train1", "loss", 10, 2.31)                       # 曲线点
    sb.item("run1", "sample-001", step="clean", status="running")   # 流水线中某个要素走到哪一步
    sb.define("pipelines", "main", {...})                      # 写/更新结构图定义
    sb.milestone("v2 清洗规则升级", note="去掉了 xx")            # 迭代节点，时间线上显示为旗标

命令行见 `python3 sb.py -h`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import sbstore as store

ID_RE = store.ID_RE
GRAPH_KINDS = store.GRAPH_KINDS
TASK_STATUS = store.TASK_STATUS
NODE_STATUS = store.NODE_STATUS
check_id = store.check_id


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
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from None


def _remote() -> bool:
    return bool(os.environ.get("SB_URL"))


# ── 脚本库接口 ────────────────────────────────────────────────────────────────
def task(task_id: str, **fields) -> dict:
    """创建/更新任务（只传要改的字段；每次调用刷新心跳）。字段见 SKILL.md。"""
    check_id(task_id)
    if _remote():
        return _http("POST", f"/api/tasks/{task_id}", fields)
    return store.update_task(task_id, fields)


def point(task_id: str, name: str, x: float, y: float) -> dict:
    """给任务的曲线 name 追加一个点（如 loss）。"""
    return task(task_id, append={name: [x, y]})


def item(task_id: str, item_id: str, step: str = None, status: str = None, **kw) -> dict:
    """更新流水线执行任务里某个数据要素：走到了哪个步骤(step=节点id)、状态。"""
    return task(task_id, items=[{"id": str(item_id), "step": step, "status": status, **kw}])


def node(kind: str, gid: str, nid: str, **fields) -> dict:
    """更新 pipeline / dataset 节点的 status / progress / note / meta。"""
    check_id(gid), check_id(nid)
    if _remote():
        return _http("POST", f"/api/{kind}/{gid}/nodes/{nid}", fields)
    return store.update_node(kind, gid, nid, fields)


def define(coll: str, item_id: str, obj) -> dict:
    """写入 pipelines / datasets / modules 的定义。obj 可以是 dict，也可以是 JSON 文件路径。"""
    check_id(item_id)
    if isinstance(obj, str):
        with open(obj, encoding="utf-8") as f:
            obj = json.load(f)
    if _remote():
        return _http("PUT", f"/api/define/{coll}/{item_id}", obj)
    return store.define(coll, item_id, obj)


def milestone(title: str, note: str = "", tags=None) -> dict:
    """在时间线上打一个迭代节点（版本/重大变更）。"""
    body = {"title": title, "note": note, "tags": tags or []}
    if _remote():
        return _http("POST", "/api/milestone", body)
    return store.add_milestone(title, note, tags)


def mark(review_id: str, line: int, verdict, note: str = "", tags=None) -> dict:
    """给校验集的某一行打人工标记：verdict = "pass" | "fail" | "doubt" | None（清除）。line 从 1 开始。"""
    check_id(review_id)
    body = {"marks": [{"line": line, "v": verdict, "note": note, "tags": tags or []}]}
    if _remote():
        return _http("POST", f"/api/review/{review_id}/marks", body)
    return store.set_marks(review_id, body["marks"])


def review_rows(rows, rel_path: str) -> int:
    """把一批成果（可迭代的 dict）写成资源目录里的 .jsonl，供 define("reviews", …) 的 source 使用；返回写入行数。
    只能在资源目录所在机器上调用；写到 <资源目录>/<rel_path>（必须以 .jsonl 结尾，不允许 ..）。"""
    full = store.resolve_asset(rel_path)
    if not full or not rel_path.endswith(".jsonl"):
        raise ValueError("rel_path 必须是资源目录内的相对路径，且以 .jsonl 结尾")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp, n = full + ".tmp", 0
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, full)
    return n


def resource(pool_id: str, item_id: str, **fields) -> dict:
    """更新某个资源的动态信息（资源必须已在 resources 定义里存在）。
    可用字段：used（已用额度，与定义里 quota.unit 同单位）、allocations（谁占了多少，整体替换）、state（active/disabled/revoked/maintenance）、note、metrics。
    注意：这里和所有定义里都不能写密钥本身——看板只记录「密钥从哪取」（secret_ref）。"""
    check_id(pool_id)
    check_id(item_id)
    if _remote():
        return _http("POST", f"/api/resources/{pool_id}/{item_id}", fields)
    return store.update_usage(pool_id, item_id, fields)


def rm_task(task_id: str):
    check_id(task_id)
    if _remote():
        return _http("DELETE", f"/api/tasks/{task_id}")
    return store.delete_task(task_id)


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


def _cmd_show():
    raw = {c: {i: store.read_json(store.item_path(c, i), {}) for i in store.list_ids(c)} for c in store.COLLS}
    print(f"状态目录: {store.state_dir()}")
    for c in store.DEF_COLLS:
        if c == "reviews":
            print(f"[{c}] " + (", ".join(f"{i}(标记 {len((raw['marks'].get(i) or {}).get('marks') or {})})" for i in raw[c]) or "（空）"))
            continue
        print(f"[{c}] " + (", ".join(f"{i}({len(o.get('nodes', o.get('blocks', o.get('items', []))))})" for i, o in raw[c].items()) or "（空）"))
    print(f"[tasks] {len(raw['tasks'])} 个")
    for i, t in raw["tasks"].items():
        tot = t.get("total")
        pct = f"{100 * (t.get('done') or 0) / tot:.0f}%" if tot else (f"{100 * t['progress']:.0f}%" if t.get("progress") is not None else "-")
        print(f"  {i:<24} {t.get('kind', 'job'):<6} {t.get('status', '?'):<9} {pct:>5}  {t.get('name', '')}")
    print(f"[timeline] {len(store.events_refresh())} 个事件")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sb.py", description="status_board 上报工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("task", help="创建/更新任务")
    t.add_argument("id")
    t.add_argument("--name")
    t.add_argument("--kind", choices=store.TASK_KINDS)
    t.add_argument("--status", choices=sorted(TASK_STATUS))
    t.add_argument("--done", type=float)
    t.add_argument("--total", type=float)
    t.add_argument("--progress", type=float, help="0~1，没有 done/total 时使用")
    t.add_argument("--unit")
    t.add_argument("--message")
    t.add_argument("--model", help="模型名")
    t.add_argument("--pipeline")
    t.add_argument("--node")
    t.add_argument("--stale-after", type=float, dest="stale_after", help="多少秒无心跳判为停滞")
    t.add_argument("--metric", action="append", help="k=v，可重复")
    t.add_argument("--json", help="额外字段（JSON 对象），如 datasets / hparams / scores")

    n = sub.add_parser("node", help="更新图节点")
    n.add_argument("kind", choices=GRAPH_KINDS)
    n.add_argument("id")
    n.add_argument("node_id")
    n.add_argument("--status", choices=sorted(NODE_STATUS))
    n.add_argument("--progress", type=float)
    n.add_argument("--note")
    n.add_argument("--meta", action="append", help="k=v，可重复")

    d = sub.add_parser("define", help="写入定义：pipelines / datasets / modules")
    d.add_argument("coll", choices=store.DEF_COLLS)
    d.add_argument("id")
    d.add_argument("file", help="JSON 文件路径，- 表示 stdin")

    m = sub.add_parser("milestone", help="时间线打迭代节点")
    m.add_argument("title")
    m.add_argument("--note", default="")
    m.add_argument("--tag", action="append")

    i = sub.add_parser("item", help="更新流水线执行里的某个数据要素")
    i.add_argument("task_id")
    i.add_argument("item_id")
    i.add_argument("--step")
    i.add_argument("--status", choices=sorted(store.ITEM_STATUS))
    i.add_argument("--name")
    i.add_argument("--note")

    p = sub.add_parser("point", help="给任务曲线追加一个点")
    p.add_argument("task_id")
    p.add_argument("name")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)

    k = sub.add_parser("mark", help="给校验集某一行打人工标记")
    k.add_argument("review_id")
    k.add_argument("line", type=int, help="第几行，从 1 开始")
    k.add_argument("verdict", choices=["pass", "fail", "doubt", "clear"], help="clear = 清除标记")
    k.add_argument("--note", default="")
    k.add_argument("--tag", action="append")

    q = sub.add_parser("resource", help="更新资源的动态信息（已用额度 / 状态 / 备注 / 分配）。密钥本身绝不要写在这里")
    q.add_argument("pool")
    q.add_argument("item")
    q.add_argument("--used", type=float, help="已用额度（与 quota.unit 同单位）")
    q.add_argument("--state", choices=["active", "disabled", "revoked", "maintenance"])
    q.add_argument("--note", default=None)
    q.add_argument("--alloc", action="append", metavar="标签=数量[:任务id]",
                   help="分配项，可重复；整体替换已有分配。例：--alloc 微调=4:train-v2 --alloc 评测=2；传 --alloc none 清空")

    r = sub.add_parser("rm-task", help="删除任务")
    r.add_argument("id")

    v = sub.add_parser("validate", help="体检：定义是否完整、引用是否有效")
    v.add_argument("--strict", action="store_true", help="有警告也返回非 0")
    v.add_argument("--ignore-example", action="store_true", dest="ignore_example",
                   help="不报「残留范例内容」（检查 examples/ 自身时用）")
    sub.add_parser("show", help="打印当前状态概览")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "task":
            f = {k: getattr(a, k) for k in ("name", "kind", "status", "done", "total", "progress", "unit",
                                              "message", "pipeline", "node", "stale_after")}
            if a.model:
                f["model"] = {"name": a.model}
            if a.metric:
                f["metrics"] = _kv(a.metric)
            if a.json:
                f.update(json.loads(a.json))
            task(a.id, **f)
        elif a.cmd == "node":
            f = {k: getattr(a, k) for k in ("status", "progress", "note")}
            if a.meta:
                f["meta"] = _kv(a.meta)
            node(a.kind, a.id, a.node_id, **f)
        elif a.cmd == "define":
            obj = json.load(sys.stdin) if a.file == "-" else a.file
            define(a.coll, a.id, obj)
        elif a.cmd == "milestone":
            milestone(a.title, a.note, a.tag)
        elif a.cmd == "item":
            item(a.task_id, a.item_id, a.step, a.status, **{k: v for k, v in (("name", a.name), ("note", a.note)) if v})
        elif a.cmd == "point":
            point(a.task_id, a.name, a.x, a.y)
        elif a.cmd == "resource":
            f = {k: v for k, v in (("used", a.used), ("state", a.state), ("note", a.note)) if v is not None}
            if a.alloc is not None:
                al = []
                for x in ([] if a.alloc == ["none"] else a.alloc):
                    lab, _, rest = x.partition("=")
                    amt, _, tid = rest.partition(":")
                    al.append({"label": lab, "amount": float(amt), **({"task": tid} if tid else {})})
                f["allocations"] = al
            resource(a.pool, a.item, **f)
        elif a.cmd == "mark":
            mark(a.review_id, a.line, None if a.verdict == "clear" else a.verdict, a.note, a.tag)
        elif a.cmd == "rm-task":
            rm_task(a.id)
        elif a.cmd == "validate":
            if _remote():
                print("validate 只能在状态目录所在机器上运行（不要设置 SB_URL）", file=sys.stderr)
                return 1
            res = store.lint(ignore_example=a.ignore_example)
            for lvl, msg in res:
                print(("错误 " if lvl == "error" else "警告 ") + msg)
            ne = sum(1 for lvl, _ in res if lvl == "error")
            print(f"体检完成：{ne} 个错误，{len(res) - ne} 个警告" if res else "体检通过：没有发现问题")
            return 1 if ne or (a.strict and res) else 0
        elif a.cmd == "show":
            _cmd_show()
            return 0
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
