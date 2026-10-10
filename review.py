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
FACET_MAX_VALUES = 40          # 每个分面最多记多少个取值，其余归到「…其他」
MATRIX_SAMPLE = 4096           # 矩阵每格最多留多少个指标值算中位数，超了用均值近似（标注 approximate）


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


def facet_view(conf: dict) -> list:
    out = []
    for f in conf.get("facets") or []:
        out.append({"id": f["id"], "label": f.get("label") or f["id"], "field": f["field"]})
    return out


def collect_facets(facets: list, row: dict, line: int, acc: dict):
    """扫描每一行时顺手统计分面取值（存的是下标，不是字符串，省内存）。"""
    for k, f in enumerate(facets):
        ok, v = get_path(row, f["field"])
        if not ok or v is None or isinstance(v, (dict, list)):
            continue
        v = v if isinstance(v, (str, int, float, bool)) else str(v)
        vals = acc["values"][k]
        idx = vals[1].get(v)
        if idx is None:
            if len(vals[0]) >= FACET_MAX_VALUES:
                idx = vals[1].get("…其他")
                if idx is None:
                    idx = len(vals[0])
                    vals[0].append("…其他")
                    vals[1]["…其他"] = idx
            else:
                idx = len(vals[0])
                vals[0].append(v)
                vals[1][v] = idx
        acc["lines"][k][line] = idx
        vals[2][idx] = vals[2].get(idx, 0) + 1


def collect_matrix(conf: dict, row: dict, acc: dict):
    """矩阵单元格计数 + 指标抽样（每格最多存 MATRIX_SAMPLE 个值，超了用均值近似）。"""
    m = conf.get("matrix") or {}
    ids = [f["id"] for f in (conf.get("facets") or [])]
    if m.get("rows") not in ids or m.get("cols") not in ids:
        return
    ri, ci = ids.index(m["rows"]), ids.index(m["cols"])
    r_idx, c_idx = acc["lines"][ri].get(acc["line"]), acc["lines"][ci].get(acc["line"])
    if r_idx is None or c_idx is None:
        return
    key = (r_idx, c_idx)
    cell = acc["cells"].get(key)
    if cell is None:
        cell = acc["cells"][key] = {"n": 0, "vals": [], "sum": 0.0}
    cell["n"] += 1
    ok, num = get_path(row, m["metric"]) if m.get("metric") else (False, None)
    if not ok or isinstance(num, bool) or not isinstance(num, (int, float)):
        return
    cell["sum"] += num
    if len(cell["vals"]) < MATRIX_SAMPLE:
        cell["vals"].append(num)


def median(vals: list) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _scan(full: str, rules: list, key: tuple, conf: dict | None = None):
    t0 = time.time()
    conf = conf or {}
    fails = {}                    # 行号(1 起) -> [失败规则序号]
    per_rule = [0] * len(rules)
    total = bad_json = 0
    state: dict = {}
    truncated = False
    facets = conf.get("facets") or []
    acc = {"values": [[[], {}, {}] for _ in facets], "lines": [{} for _ in facets], "cells": {}, "line": 0}
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
                if facets or (conf.get("matrix") or {}):
                    acc["line"] = total
                    collect_facets(facets, row, total, acc)
                    collect_matrix(conf, row, acc)
        res = {"done": True, "rows": total, "bad_json": bad_json, "fails": fails, "per_rule": per_rule,
               "truncated": truncated, "elapsed": time.time() - t0, "ts": time.time(),
               "facet_values": [v[0] for v in acc["values"]], "facet_lines": acc["lines"], "matrix_cells": acc["cells"]}
    except OSError as e:
        res = {"done": True, "error": e.__class__.__name__, "ts": time.time()}
    with _lock:
        _scans[key] = res


def scan_status(full: str, rules: list, conf: dict | None = None, wait: float = 0) -> dict:
    """返回缓存的扫描结果；没有则启动后台扫描并返回 pending。conf（分面 / 矩阵）变了要重新扫描。
    wait > 0：最多等这么久让后台扫描完成（测试用；接口永远用默认值，不阻塞）。"""
    try:
        st = os.stat(full)
    except OSError:
        return {"error": "FileNotFoundError"}
    key = (full, st.st_mtime_ns, st.st_size, rules_fp(rules), rules_fp(conf or {}))
    with _lock:
        res = _scans.get(key)
        if res is not None:
            return res
        _scans[key] = {"pending": True}
        for k in [k for k in _scans if k[0] == full and k != key]:       # 同一个文件的旧版本缓存可以丢
            _scans.pop(k, None)
        if len(_scans) > 40:
            _scans.pop(next(iter(_scans)), None)
    threading.Thread(target=_scan, args=(full, rules, key), kwargs={"conf": conf or {}}, daemon=True).start()
    if wait > 0:
        deadline = time.time() + wait
        while time.time() < deadline:
            with _lock:
                res = _scans.get(key)
            if res and not res.get("pending"):
                return res
            time.sleep(0.02)
    return {"pending": True}


def _facet_filter(conf: dict, scan: dict, q: dict) -> tuple[dict, dict]:
    """解析查询里的分面筛选 → ({分面id: 取值}, {分面id: 下标})。取值不在扫描结果里就忽略。"""
    raw = q.get("facets")
    try:
        want = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        want = {}
    if not isinstance(want, dict):
        want = {}
    facets = conf.get("facets") or []
    values = scan.get("facet_values") or []
    lines = scan.get("facet_lines") or []
    picked, picked_idx = {}, {}
    for k, f in enumerate(facets):
        v = want.get(f["id"])
        if v is None or v == "":
            continue
        names = values[k] if k < len(values) else []
        if v in names:
            picked[f["id"]] = v
            picked_idx[k] = names.index(v)
    return picked, picked_idx


def facet_summary(conf: dict, scan: dict, picked: dict, picked_idx: dict) -> list:
    """每个分面的取值与条数。某个分面的条数按「除它自己外其他筛选都生效」算，
    这样页面上每行的按钮数字不会因为选了它自己而全部变成同一个值。"""
    facets = conf.get("facets") or []
    values = scan.get("facet_values") or []
    lines = scan.get("facet_lines") or []
    others = {k: i for k, i in picked_idx.items()}
    out = []
    for k, f in enumerate(facets):
        names = values[k] if k < len(values) else []
        mine = {kk: i for kk, i in others.items() if kk != k}      # 除自己以外已选的分面
        counts = [0] * len(names)
        for line in (lines[k] if k < len(lines) else {}):
            if mine and any((lines[o] or {}).get(line) != i for o, i in mine.items()):
                continue
            idx = (lines[k] or {}).get(line)
            if idx is not None and idx < len(counts):
                counts[idx] += 1
        vals = [{"v": n, "n": c} for n, c in zip(names, counts) if c]
        vals.sort(key=lambda x: -x["n"])
        out.append({"id": f["id"], "label": f.get("label") or f["id"], "values": vals})
    return out


def matrix_summary(conf: dict, scan: dict, picked_idx: dict) -> dict | None:
    """两个分面交叉的矩阵：每格显示条数和指标中位数（旧浏览站 8 格那样的看法）。"""
    m = conf.get("matrix") or {}
    ids = [f["id"] for f in (conf.get("facets") or [])]
    if m.get("rows") not in ids or m.get("cols") not in ids:
        return None
    ri, ci = ids.index(m["rows"]), ids.index(m["cols"])
    values = scan.get("facet_values") or []
    cells = scan.get("matrix_cells") or {}
    rnames = values[ri] if ri < len(values) else []
    cnames = values[ci] if ci < len(values) else []
    grid = []
    for a, rn in enumerate(rnames):
        row = []
        for b, cn in enumerate(cnames):
            cell = cells.get((a, b)) or cells.get(f"{a}|{b}") or {}
            n = int(cell.get("n") or 0)
            vals = cell.get("vals") or []
            med = median(vals) if vals else (cell.get("sum", 0) / n if n else 0)
            row.append({"n": n, "med": round(med, 2), "approx": bool(vals) and len(vals) >= MATRIX_SAMPLE})
        grid.append(row)
    return {"rows": rnames, "cols": cnames, "row_label": next((f.get("label") or f["id"] for f in (conf.get("facets") or []) if f["id"] == m["rows"]), m["rows"]),
            "col_label": next((f.get("label") or f["id"] for f in (conf.get("facets") or []) if f["id"] == m["cols"]), m["cols"]),
            "rows_id": m["rows"], "cols_id": m["cols"],
            "metric": m.get("metric"), "grid": grid,
            "picked": {ids[k]: (values[k][i] if k < len(values) and i < len(values[k]) else None) for k, i in picked_idx.items()}}


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
    conf = {"facets": d.get("facets") or [], "matrix": d.get("matrix") or {}}
    scan = scan_status(full, rules, conf, wait=float(q.get("_wait") or 0))
    picked, picked_idx = ({}, {}) if (scan.get("pending") or scan.get("error")) else _facet_filter(conf, scan, q)
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
    elif view in ("fail", "pass") and pending:
        pool = []
    else:
        pool, _ = line_numbers(scan, view, marks, 0, max(rows_total or 0, 1), rows_total)
        pool = list(pool) if view != "all" else list(range(1, (rows_total or 0) + 1))
    if picked_idx:                       # 分面筛选：和其他筛选是「并且」的关系
        fmap = scan.get("facet_lines") or []
        pool = [ln for ln in pool if all((fmap[k] or {}).get(ln) == i for k, i in picked_idx.items() if k < len(fmap))]
    if rule_filter not in (None, "") and not pending:
        lines, sel_total = pool[pg * size:(pg + 1) * size], len(pool)
    else:
        lines, sel_total = pool[pg * size:(pg + 1) * size], len(pool)
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
           "marked": counts, "scan": _public_scan(scan, rules), "mtime": os.path.getmtime(full), "picked": picked}
    if not (scan.get("pending") or scan.get("error")):
        if conf["facets"]:
            out["facets"] = facet_summary(conf, scan, picked, picked_idx)
        if conf.get("matrix"):
            out["matrix"] = matrix_summary(conf, scan, picked_idx)
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
