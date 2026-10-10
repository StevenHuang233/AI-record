"""资源管理：把「资源池定义 + 动态用量」派生成页面要显示的状态（仅标准库）。

安全约束
  - 密钥：只检查「是否存在」（环境变量是否非空 / 文件是否存在、非空、权限），**绝不读取、绝不回传密钥内容**。
  - 本机探测（probe=local）：只执行固定参数的 nvidia-smi 与只读系统调用，参数不含任何来自定义文件的内容。
  - 探测都在后台线程里跑并缓存；接口只读缓存，磁盘挂载卡死（如失联的网络盘）也不会拖住页面。
  - 回放历史时不做任何实时探测（本机现在的状态和历史时刻无关）。
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import threading
import time

PROBE_TTL = float(os.environ.get("SB_PROBE_TTL", "5"))
UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4, "PB": 1024 ** 5}
WARN_DAYS = 14
HIDE_WHEN_REMOTE = ("host", "path", "endpoint", "access")      # 对外监听且未开 SB_SHOW_PATHS 时不向浏览器返回

_cache: dict = {}
_lock = threading.Lock()


# ── 后台探测 + 缓存 ──────────────────────────────────────────────────────────
def _run(key, fn):
    try:
        val = fn()
    except Exception as e:  # noqa: BLE001  探测失败只是「探测不到」，不能影响页面
        val = {"error": e.__class__.__name__}
    with _lock:
        _cache[key] = {"val": val, "ts": time.time()}


def cached(key, fn, ttl=PROBE_TTL):
    now = time.time()
    with _lock:
        c = _cache.get(key)
        if c and "val" in c and now - c["ts"] < ttl:
            return c["val"]
        if c and c.get("pending") and now - c.get("started", 0) < 30:
            return c.get("val") or {"pending": True}
        stale = c.get("val") if c else None
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = {"pending": True, "started": now, "val": stale, "ts": c["ts"] if c else 0}
    threading.Thread(target=_run, args=(key, fn), daemon=True).start()
    return stale or {"pending": True}


def probe_gpu() -> dict:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4)
    except FileNotFoundError:
        return {"error": "本机没有 nvidia-smi"}
    except subprocess.TimeoutExpired:
        return {"error": "nvidia-smi 超时"}
    if r.returncode != 0:
        return {"error": "nvidia-smi 执行失败"}
    cards = []
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            cards.append({"index": int(p[0]), "name": p[1], "mem_total": int(p[2]) * 1024 ** 2, "mem_used": int(p[3]) * 1024 ** 2,
                          "util": int(p[4]) / 100.0})
        except ValueError:
            continue
    return {"cards": cards}


def probe_cpu() -> dict:
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:
        cores = os.cpu_count() or 0
    out = {"cores": cores}
    try:
        out["load"] = list(os.getloadavg())
    except (OSError, AttributeError):
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0]) * 1024
        if "MemTotal" in mem:
            out["mem_total"], out["mem_avail"] = mem["MemTotal"], mem.get("MemAvailable")
    except OSError:
        pass
    return out


def probe_disk(path: str) -> dict:
    if not os.path.exists(path):
        return {"missing": True}
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free}


# ── 密钥状态：只看有没有，不读内容 ───────────────────────────────────────────
def secret_status(ref, show_paths: bool) -> dict:
    if not isinstance(ref, dict) or len(ref) != 1:
        return {"kind": "none", "state": "unregistered"}
    (k, v), = ref.items()
    if k == "env":
        return {"kind": "env", "name": v, "state": "set" if (os.environ.get(v) or "").strip() else "unset"}
    if k == "file":
        out = {"kind": "file"}
        if show_paths:
            out["name"] = v
        try:
            st = os.stat(v)
        except FileNotFoundError:
            return {**out, "state": "missing"}
        except OSError:
            return {**out, "state": "unreadable"}
        if not os.path.isfile(v) or not os.access(v, os.R_OK):
            return {**out, "state": "unreadable"}
        if st.st_size == 0:
            return {**out, "state": "empty"}
        out["state"] = "set"
        if st.st_mode & 0o077:
            out["loose_perm"] = True                    # 同组/其他用户可读写：密钥文件应只有属主可访问（chmod 600）
        return out
    return {"kind": "vault", "name": v, "state": "external"}


# ── 派生 ──────────────────────────────────────────────────────────────────────
def _days_between(a: datetime.date, b: datetime.date) -> int:
    return (a - b).days


def _alloc_amount(a: dict, kind: str) -> float:
    amt = a.get("amount") or 0
    if kind == "disk":
        return amt * UNIT_BYTES.get(str(a.get("unit") or "B").upper(), 1)
    return amt


def derive_item(it: dict, u: dict, today: datetime.date, live: bool, show_paths: bool) -> None:
    """就地补充：state / usage / allocations / probe / capacity / issues / health 等派生字段。"""
    kind = it["kind"]
    issues = []

    def bad(t): issues.append({"level": "bad", "text": t})
    def warn(t): issues.append({"level": "warn", "text": t})

    state = u.get("state") or it.get("state") or "active"
    it["state"] = state
    it["usage"] = {k: u[k] for k in ("used", "note", "metrics", "at") if k in u}
    allocs = []
    for a in u.get("allocations") or []:
        a = dict(a)
        try:
            a["expired"] = bool(a.get("until")) and datetime.date.fromisoformat(a["until"]) < today
        except ValueError:
            a["expired"] = False
        allocs.append(a)
    it["allocations"] = allocs
    off = state in ("disabled", "revoked")
    if state == "maintenance":
        warn("维护中，暂不可用")
    if off:
        issues.append({"level": "off", "text": "已撤销" if state == "revoked" else "已停用"})

    # ── 本机探测（仅实时）──
    probe = None
    if live and it.get("probe") == "local" and not off:
        if kind == "gpu":
            probe = cached(("gpu",), probe_gpu)
        elif kind == "cpu":
            probe = cached(("cpu",), probe_cpu, 3)
        elif kind == "disk" and it.get("path"):
            probe = cached(("disk", it["path"]), lambda p=it["path"]: probe_disk(p), 15)
    if probe is not None:
        if kind == "gpu" and "cards" in probe and it.get("gpu_indices"):
            probe = {"cards": [c for c in probe["cards"] if c["index"] in it["gpu_indices"]]}
        it["probed"] = probe              # 注意不要覆盖定义里的 probe="local"

    # ── 容量 / 分配 ──
    cap = None
    if kind in ("gpu", "cpu", "other"):
        cap = it.get("count")
        if cap is None and probe:
            cap = len(probe["cards"]) if "cards" in probe else probe.get("cores")
    elif kind == "disk":
        cap = it.get("capacity_bytes") or (probe or {}).get("total")
    alloc_total = sum(_alloc_amount(a, kind) for a in allocs)
    it["capacity"], it["allocated"] = cap, alloc_total
    if cap:
        it["free"] = max(0, cap - alloc_total)
        if alloc_total > cap:
            bad(f"分配超额：已分配 {alloc_total:g} > 容量 {cap:g}")
    for a in allocs:
        if a["expired"]:
            warn(f"分配「{a.get('label') or '未命名'}」已过期（{a['until']}），请确认是否释放")

    if off:
        it["issues"], it["health"] = issues, "off"
        return

    # ── 各类型检查 ──
    if kind == "apikey":
        if live:
            s = secret_status(it.get("secret_ref"), show_paths)
            it["secret"] = s
            st = s["state"]
            if st == "unregistered":
                warn("没有登记密钥来源（secret_ref）")
            elif st == "unset":
                bad(f"环境变量 {s['name']} 未设置或为空（注意：这里检查的是看板服务进程的环境）")
            elif st == "missing":
                bad("密钥文件不存在")
            elif st == "empty":
                bad("密钥文件是空的")
            elif st == "unreadable":
                bad("密钥文件不可读")
            if s.get("loose_perm"):
                warn("密钥文件权限过宽，应只允许属主读写（chmod 600）")
        else:
            ref = it.get("secret_ref")
            it["secret"] = {"kind": next(iter(ref), "none") if isinstance(ref, dict) else "none", "state": "history"}   # 回放历史：不检查本机环境
        if it.get("expires"):
            try:
                d = _days_between(datetime.date.fromisoformat(it["expires"]), today)
            except ValueError:
                d = None
            if d is not None:
                it["days_left"] = d
                if d < 0:
                    bad(f"已过期 {-d} 天")
                elif d <= WARN_DAYS:
                    warn(f"{d} 天后过期")
        if it.get("rotated") and it.get("rotate_days"):
            try:
                due = datetime.date.fromisoformat(it["rotated"]) + datetime.timedelta(days=it["rotate_days"])
                it["rotate_due"] = due.isoformat()
                if due < today:
                    warn(f"已超过轮换周期 {(today - due).days} 天（上次轮换 {it['rotated']}）")
            except ValueError:
                pass
        q = it.get("quota")
        if q and "used" in u and q.get("limit"):
            pct = u["used"] / q["limit"]
            it["quota_pct"] = pct
            if pct >= 1:
                bad("额度已用完")
            elif pct >= 0.9:
                warn(f"额度已用 {pct * 100:.0f}%")
    elif kind == "disk":
        used = (probe or {}).get("used")
        if used is None and u.get("used") is not None:
            used = u["used"]
        if probe and probe.get("missing"):
            bad("路径不存在")
        elif probe and probe.get("error"):
            warn("磁盘探测失败")
        total = cap
        if used is not None and total:
            pct = used / total
            it["used_pct"] = pct
            it["used_bytes"] = used
            if pct >= 0.95:
                bad(f"磁盘已用 {pct * 100:.0f}%，几乎写满")
            elif pct >= 0.85:
                warn(f"磁盘已用 {pct * 100:.0f}%")
    elif kind == "gpu":
        if probe and probe.get("error"):
            warn(f"无法探测 GPU：{probe['error']}")
        elif probe and "cards" in probe:
            if it.get("count") and len(probe["cards"]) < it["count"]:
                warn(f"定义了 {it['count']} 张，本机只探测到 {len(probe['cards'])} 张")
            cards = probe["cards"]
            if cards:
                it["util_avg"] = sum(c["util"] for c in cards) / len(cards)
                it["mem_pct"] = sum(c["mem_used"] for c in cards) / max(1, sum(c["mem_total"] for c in cards))
    elif kind == "cpu":
        if probe and probe.get("error"):
            warn("无法探测 CPU")
        elif probe and probe.get("load") and probe.get("cores"):
            it["load_pct"] = probe["load"][0] / probe["cores"]
            if it["load_pct"] >= 1.0:
                warn(f"CPU 负载偏高（1 分钟负载 {probe['load'][0]:.1f} / {probe['cores']} 核）")

    it["issues"] = issues
    it["health"] = "bad" if any(i["level"] == "bad" for i in issues) else "warn" if any(i["level"] == "warn" for i in issues) else "ok"


def derive(g: dict, usage, now: float, live: bool, show_paths: bool) -> None:
    """就地把一个资源池定义补充成页面状态。usage 是 state/usage/<id>.json 的内容（可为空）。"""
    today = datetime.date.fromtimestamp(now)
    umap = (usage or {}).get("items") if isinstance(usage, dict) else {}
    umap = umap if isinstance(umap, dict) else {}
    kinds: dict = {}
    for it in g.get("items", []):
        derive_item(it, umap.get(it["id"]) or {}, today, live, show_paths)
        k = kinds.setdefault(it["kind"], {"n": 0, "bad": 0, "warn": 0, "off": 0, "capacity": 0, "allocated": 0, "free": 0})
        k["n"] += 1
        if it["health"] in ("bad", "warn", "off"):
            k[it["health"]] += 1
        if it.get("capacity"):
            k["capacity"] += it["capacity"]
            k["allocated"] += it.get("allocated") or 0
            k["free"] += it.get("free") or 0
        if not show_paths:
            for f in HIDE_WHEN_REMOTE:
                it.pop(f, None)
            ref = it.get("secret_ref")
            if isinstance(ref, dict):
                ref.pop("file", None)
    g["summary"] = {"total": len(g.get("items", [])), "bad": sum(k["bad"] for k in kinds.values()),
                    "warn": sum(k["warn"] for k in kinds.values()), "by_kind": kinds}
    g["live_probe"] = live
