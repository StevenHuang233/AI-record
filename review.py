"""成果校验（review）：读取 .jsonl 成果、跑声明式规则、按页返回样本（仅标准库）。

设计要点
  - 规则是声明式 op，没有正则、没有表达式求值：不会 ReDoS，也不会执行任何配置里的代码。
  - 全量扫描在后台线程里进行，结果按「文件 mtime + 规则指纹」缓存；接口永远只读缓存或单页，不阻塞。
  - 每行只记录「失败的规则序号」，几十万行也只占很小内存；超过扫描预算（行数/耗时）则标注「仅扫描了前 N 行」。
  - 只读资源目录内的文件（沿用 sbstore.resolve_asset 的沙箱）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import sbstore as store

SCAN_MAX_ROWS = int(os.environ.get("SB_REVIEW_MAX_ROWS", "300000"))
SCAN_BUDGET = float(os.environ.get("SB_REVIEW_BUDGET", "60"))
LINE_MAX = 200_000
STR_CAP = 4000
UNIQUE_CAP = 500_000


# ── 取值与脱敏 ────────────────────────────────────────────────────────────────
def get_path(obj, key):
    """a.b.0.c 形式取值；取不到返回 (False, None)。"""
    v = obj
    for p in str(key).split("."):
        if isinstance(v, dict) and p in v:
            v = v[p]
        elif isinstance(v, list) and p.isdigit() and int(p) < len(v):
            v = v[int(p)]
        else:
            return False, None
    return True, v


def shrink(o, depth=0):
    if depth > 6:
        return "…"
    if isinstance(o, str):
        return o if len(o) <= STR_CAP else o[:STR_CAP] + "…"
    if isinstance(o, list):
        return [shrink(x, depth + 1) for x in o[:50]]
    if isinstance(o, dict):
        return {str(k)[:60]: shrink(v, depth + 1) for k, v in list(o.items())[:50]}
    return o


def row_hash(raw: bytes) -> int:
    """行内容哈希（31 位）：源文件被改动后，用来识别「这个标记标的已经不是同一行了」。"""
    return int.from_bytes(hashlib.blake2b(raw.strip(), digest_size=4).digest(), "big") & 0x7FFFFFFF


# ── 规则求值 ──────────────────────────────────────────────────────────────────
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _empty(v):
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0) or (isinstance(v, str) and not v.strip())


def eval_rule(rule: dict, row: dict, state: dict):
    """返回 True=通过，False=不通过。state 用于 unique 这类需要跨行的规则。"""
    op, field = rule["op"], rule["field"]
    found, v = get_path(row, field)
    val = rule.get("value")
    if op == "exists":
        return found and v is not None
    if op == "nonempty":
        return found and not _empty(v)
    if op == "unique":
        if not found or v is None:
            return False
        seen = state.setdefault(rule["id"], set())
        key = json.dumps(v, sort_keys=True, ensure_ascii=False) if not isinstance(v, str) else v
        if key in seen:
            return False
        if len(seen) < UNIQUE_CAP:
            seen.add(key)
        return True
    if op == "asset_exists":
        if not found or not isinstance(v, str):
            return False
        full = store.resolve_asset(v)
        return bool(full and os.path.isfile(full))
    if op == "json":
        if not found or not isinstance(v, str):
            return False
        try:
            json.loads(v)
            return True
        except ValueError:
            return False
    if op in ("eq_field", "neq_field"):
        f2, v2 = get_path(row, val)
        same = found and f2 and v == v2
        return same if op == "eq_field" else (found and f2 and not same)
    if not found:
        return False
    if op in ("minlen", "maxlen"):
        try:
            n = len(v)
        except TypeError:
            return False
        return n >= val if op == "minlen" else n <= val
    if op in ("gt", "gte", "lt", "lte"):
        if not _num(v):
            return False
        return {"gt": v > val, "gte": v >= val, "lt": v < val, "lte": v <= val}[op]
    if op in ("in", "notin"):
        hit = v in val if not isinstance(v, (list, dict)) else False
        return hit if op == "in" else not hit
    if op in ("equals", "notequals"):
        return (v == val) if op == "equals" else (v != val)
    if op in ("contains", "notcontains", "startswith", "endswith"):
        if not isinstance(v, str):
            return False
        s = str(val)
        r = {"contains": s in v, "notcontains": s not in v, "startswith": v.startswith(s), "endswith": v.endswith(s)}[op]
        return r
    return False


def rules_fp(rules) -> str:
    return hashlib.blake2b(json.dumps(rules, sort_keys=True, ensure_ascii=False).encode(), digest_size=6).hexdigest()


# ── 全量扫描（后台 + 缓存）────────────────────────────────────────────────────
_scans: dict = {}
_lock = threading.Lock()


def _scan(full: str, rules: list, key: tuple):
    t0 = time.time()
    fails = {}                    # 行号(1 起) -> [失败规则序号]
    per_rule = [0] * len(rules)
    total = bad_json = 0
    state: dict = {}
    truncated = False
    try:
        with open(full, "rb") as f:
            while True:
                if total >= SCAN_MAX_ROWS or time.time() - t0 > SCAN_BUDGET:
                    truncated = bool(f.peek(1)) if hasattr(f, "peek") else True
                    break
                raw = f.readline(LINE_MAX + 1)
                if not raw:
                    break
                if len(raw) > LINE_MAX and not raw.endswith(b"\n"):
                    while True:
                        chunk = f.readline(1 << 20)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    row = None
                else:
                    try:
                        row = json.loads(raw)
                        if not isinstance(row, dict):
                            row = None
                    except ValueError:
                        row = None
                if not raw.strip():
                    continue
                total += 1
                if row is None:
                    bad_json += 1
                    fails[total] = [-1]            # -1 = 这一行本身不是合法的 JSON 对象
                    continue
                failed = []
                for i, r in enumerate(rules):
                    if not eval_rule(r, row, state):
                        failed.append(i)
                        per_rule[i] += 1
                if failed:
                    fails[total] = failed
        res = {"done": True, "rows": total, "bad_json": bad_json, "fails": fails, "per_rule": per_rule,
               "truncated": truncated, "elapsed": time.time() - t0, "ts": time.time()}
    except OSError as e:
        res = {"done": True, "error": e.__class__.__name__, "ts": time.time()}
    with _lock:
        _scans[key] = res


def scan_status(full: str, rules: list) -> dict:
    """返回缓存的扫描结果；没有则启动后台扫描并返回 pending。"""
    try:
        st = os.stat(full)
    except OSError:
        return {"error": "FileNotFoundError"}
    key = (full, st.st_mtime_ns, st.st_size, rules_fp(rules))
    with _lock:
        res = _scans.get(key)
        if res is not None:
            return res
        _scans[key] = {"pending": True}
        for k in [k for k in _scans if k[0] == full and k != key]:       # 同一个文件的旧版本缓存可以丢
            _scans.pop(k, None)
        if len(_scans) > 40:
            _scans.pop(next(iter(_scans)), None)
    threading.Thread(target=_scan, args=(full, rules, key), daemon=True).start()
    return {"pending": True}


# ── 按页读取 ──────────────────────────────────────────────────────────────────
def read_rows(full: str, lines: list) -> dict:
    """读取指定行号（1 起）。一次顺序扫描，最多取 50 行。"""
    want = sorted({int(x) for x in lines if isinstance(x, int) and x >= 1})[:50]
    out = {}
    if not want:
        return out
    last = want[-1]
    i = 0
    with open(full, "rb") as f:
        while i < last:
            raw = f.readline(LINE_MAX + 1)
            if not raw:
                break
            if len(raw) > LINE_MAX and not raw.endswith(b"\n"):
                while True:
                    chunk = f.readline(1 << 20)
                    if not chunk or chunk.endswith(b"\n"):
                        break
                if raw.strip():
                    i += 1
                    if i in want:
                        out[i] = {"too_long": True, "h": 0}
                continue
            if not raw.strip():
                continue
            i += 1
            if i in want:
                try:
                    row = json.loads(raw)
                    out[i] = {"row": shrink(row), "h": row_hash(raw)} if isinstance(row, dict) else {"bad": True, "raw": raw[:300].decode("utf-8", "replace"), "h": row_hash(raw)}
                except ValueError:
                    out[i] = {"bad": True, "raw": raw[:300].decode("utf-8", "replace"), "h": row_hash(raw)}
    return out


def line_numbers(scan: dict, view: str, marks: dict, page: int, size: int, total_rows):
    """按筛选视图计算这一页应该读哪些行号，以及这个视图下共有多少行。
    view: all | fail（规则不通过）| pass（规则全通过）| unmarked（没人看过）| mpass | mfail | mdoubt（人工判定）"""
    mv = lambda ln: (marks.get(str(ln)) or {}).get("v")  # noqa: E731
    fails = scan.get("fails") or {}
    if view == "all":
        n = total_rows or 0
        base = range(1, n + 1)
        sel_total = n
        start = page * size
        return list(base[start:start + size]), sel_total
    if view == "fail":
        pool = sorted(fails)
    elif view == "pass":
        n = total_rows or 0
        pool = [i for i in range(1, n + 1) if i not in fails]
    elif view == "unmarked":
        n = total_rows or 0
        pool = [i for i in range(1, n + 1) if not mv(i)]
    elif view in ("mpass", "mfail", "mdoubt"):
        want = {"mpass": "pass", "mfail": "fail", "mdoubt": "doubt"}[view]
        pool = sorted(int(k) for k, m in marks.items() if isinstance(m, dict) and m.get("v") == want)
    else:
        pool = []
    return pool[page * size:(page + 1) * size], len(pool)


def page(rid: str, d: dict, q: dict) -> dict:
    """组装一页校验数据：样本 + 规则结果 + 人工标记 + 全局汇总。"""
    full = store.resolve_asset(d["source"])
    if not full or not os.path.isfile(full):
        return {"error": f"源文件不存在: {d['source']}"}
    rules = d.get("rules") or []
    scan = scan_status(full, rules)
    marks_file = store.read_json(store.item_path("marks", rid), None) or {}
    marks = marks_file.get("marks") or {}
    try:
        size = max(1, min(int(q.get("size") or d.get("page_size") or 10), 50))
        pg = max(0, min(int(q.get("page") or 0), 5_000_000))
    except (TypeError, ValueError):
        size, pg = 10, 0
    view = q.get("view") or "all"
    if view not in ("all", "fail", "pass", "unmarked", "mpass", "mfail", "mdoubt"):
        view = "all"
    if rid and q.get("rule") not in (None, ""):
        view = "fail"
    pending = bool(scan.get("pending")) or scan.get("error") == "FileNotFoundError"
    rows_total = scan.get("rows") if not pending and "rows" in scan else None
    if rows_total is None:                               # 扫描还没完成：用快速计数先把总行数给出来
        rows_total = _quick_count(full)
    lines, sel_total = ([], 0)
    rule_filter = q.get("rule")
    if rule_filter not in (None, "") and not pending:
        idx = next((i for i, r in enumerate(rules) if r["id"] == rule_filter), None)
        pool = sorted(ln for ln, fl in (scan.get("fails") or {}).items() if idx is not None and idx in fl)
        lines, sel_total = pool[pg * size:(pg + 1) * size], len(pool)
    elif view in ("fail", "pass") and pending:
        lines, sel_total = [], 0
    else:
        lines, sel_total = line_numbers(scan, view, marks, pg, size, rows_total)
    got = read_rows(full, lines)
    items = []
    for ln in lines:
        g = got.get(ln)
        if g is None:
            continue
        m = marks.get(str(ln)) or None
        it = {"line": ln, "h": g.get("h"), "fails": [rules[i]["id"] if i >= 0 else "__bad_json__" for i in (scan.get("fails") or {}).get(ln, [])]}
        if "row" in g:
            it["row"] = g["row"]
        elif g.get("too_long"):
            it["bad"] = "（该行超过 200KB，已跳过）"
        else:
            it["bad"] = g.get("raw") or ""
        if m:
            it["mark"] = m
            if m.get("h") is not None and g.get("h") is not None and m["h"] != g["h"]:
                it["mark_stale"] = True               # 源文件这一行变了：标记可能已过期
        items.append(it)
    counts = {"pass": 0, "fail": 0, "doubt": 0}
    for m in marks.values():
        if isinstance(m, dict) and m.get("v") in counts:
            counts[m["v"]] += 1
    out = {"rows": rows_total, "view": view, "page": pg, "size": size, "selected": sel_total, "items": items,
           "marked": counts, "scan": _public_scan(scan, rules), "mtime": os.path.getmtime(full)}
    return out


def _public_scan(scan: dict, rules: list) -> dict:
    if scan.get("pending"):
        return {"pending": True}
    if scan.get("error"):
        return {"error": scan["error"]}
    n_fail = len(scan.get("fails") or {})
    return {"rows": scan["rows"], "bad_json": scan["bad_json"], "fail_rows": n_fail, "pass_rows": scan["rows"] - n_fail,
            "per_rule": [{"id": r["id"], "label": r.get("label") or r["id"], "fail": scan["per_rule"][i],
                          "severity": r.get("severity", "error")} for i, r in enumerate(rules)],
            "truncated": scan["truncated"], "elapsed": round(scan["elapsed"], 2)}


_count_cache: dict = {}


def _quick_count(full: str):
    """扫描未完成时先给个总行数：按换行数估计，结果按 mtime 缓存。"""
    try:
        st = os.stat(full)
    except OSError:
        return None
    key = (full, st.st_mtime_ns, st.st_size)
    if key in _count_cache:
        return _count_cache[key]
    n = 0
    with open(full, "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            n += buf.count(b"\n")
        if st.st_size and not buf and f.tell() and _last_byte(full) != b"\n":
            n += 1
    if len(_count_cache) > 40:
        _count_cache.clear()
    _count_cache[key] = n
    return n


def _last_byte(full: str):
    with open(full, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)


def summary(rid: str, d: dict) -> dict:
    """列表页/首页用的轻量汇总：只读缓存，不触发扫描之外的重活。"""
    full = store.resolve_asset(d.get("source"))
    marks_file = store.read_json(store.item_path("marks", rid), None) or {}
    c = {"pass": 0, "fail": 0, "doubt": 0}
    for m in (marks_file.get("marks") or {}).values():
        if isinstance(m, dict) and m.get("v") in c:
            c[m["v"]] += 1
    out = {"marked": c}
    if not full or not os.path.isfile(full):
        out["error"] = "源文件不存在"
        return out
    out["rows"] = _quick_count(full)
    rules = d.get("rules") or []
    if rules:
        sc = scan_status(full, rules)
        out["scan"] = _public_scan(sc, rules)
    return out
