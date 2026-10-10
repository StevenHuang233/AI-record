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

import versions as _ver

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
DEF_COLLS = ("pipelines", "datasets", "modules", "reviews", "resources")
COLLS = DEF_COLLS + ("tasks", "marks", "usage")  # marks：成果校验的人工标记；usage：资源池的动态用量（与静态定义分开存）
RES_KINDS = ("apikey", "gpu", "cpu", "disk", "service", "other")
RES_STATES = ("active", "disabled", "revoked", "maintenance")
RES_PERIODS = ("day", "month", "year", "total")
MAX_ALLOCS = 30
PROTECTED_META = {"path", "auto_stat"}
MARK_VERDICTS = ("pass", "weak", "fail", "doubt")
MAX_MARKS = 20000
REVIEW_KINDS = ("model_output", "cleaned_data", "generic")
FIELD_TYPES = ("text", "long", "json", "image", "audio", "video", "diff")
RULE_OPS = ("nonempty", "exists", "minlen", "maxlen", "contains", "notcontains", "startswith", "endswith",
            "equals", "notequals", "in", "notin", "gt", "gte", "lt", "lte", "json", "asset_exists",
            "eq_field", "neq_field", "unique")
RULE_NEEDS_VALUE = {"minlen", "maxlen", "contains", "notcontains", "startswith", "endswith", "equals", "notequals",
                    "in", "notin", "gt", "gte", "lt", "lte", "eq_field", "neq_field"}
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


# ── 资源目录（图片 / 音视频 / jsonl 样本）：展示模块与成果校验共用 ─────────────────
def assets_dir() -> str:
    return os.path.realpath(os.environ.get("SB_ASSETS_DIR") or os.path.join(state_dir(), "assets"))


def asset_roots() -> dict:
    """额外的只读资源根：环境变量 SB_ASSET_ROOTS=别名=/绝对路径:别名=/绝对路径。

    为什么要有：视频、图片这类大文件常常已经在共享盘上，复制一份进 state/assets/ 既占本机磁盘又跟不上更新；
    软链又行不通（资源解析取真实路径，指向根外的软链一律当越界拒绝）。登记成「根」就能直接读，文件留在原地。

    约束：
      - 只能在启动时通过环境变量登记；没有任何接口能添加（否则有人能把 /etc 登记成根）。
      - 别名只允许字母数字 _ . -，不能和主根下的第一级目录重名（见 resolve_asset 的优先级）。
      - 路径必须是存在的目录；不存在的会被忽略（并在体检里提示）。
      - 额外根永远只读：写入（sb.review_rows）只认主根。
    """
    out = {}
    for item in (os.environ.get("SB_ASSET_ROOTS") or "").split(":"):
        if "=" not in item:
            continue
        name, path = item.split("=", 1)
        name = name.strip()
        if not ID_RE.match(name) or not path.strip():
            continue
        real = os.path.realpath(path.strip())
        if os.path.isdir(real):
            out[name] = real
    return out


def _inside(root: str, full: str) -> bool:
    try:
        return os.path.commonpath([root, full]) == root
    except ValueError:
        return False


def resolve_asset(rel, writable=False):
    """把相对路径解析到资源目录内；越界（.. / 软链指向外部）一律返回 None。

    读：先看第一段是不是额外根的别名（SB_ASSET_ROOTS），是就在那个根里解析；否则在主根（state/assets）里解析。
    写（writable=True）：只认主根，额外根一律返回 None。
    不论哪个根，都是「先取目标真实路径，再要求它落在这个根的真实路径下」。"""
    if not isinstance(rel, str) or not rel or len(rel) > 400 or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    extra = asset_roots()
    if len(parts) > 1 and parts[0] in extra:
        if writable:
            return None
        root = extra[parts[0]]
        full = os.path.realpath(os.path.join(root, *parts[1:]))
        return full if _inside(root, full) else None
    root = assets_dir()
    full = os.path.realpath(os.path.join(root, *parts))
    return full if _inside(root, full) else None


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
def display_width(s) -> int:
    """文字在结构图上占的宽度单位（与前端 util.js 的 clip() 一致：中文等宽字符 2，其余 1）。"""
    return sum(2 if ord(c) > 255 else 1 for c in str(s or ""))


PIPE_NAME_MAX, PIPE_ROLE_MAX = 22, 34       # 结构图节点上 name / role 各自的显示上限（单位见 display_width）
PIPE_KINDS = ("source", "process", "model", "train", "eval", "output", "gate")


def _num(v, name, lo=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < lo:
        raise ValueError(f"{name} 必须是 >= {lo} 的有限数字")
    return v


# ── 密钥防泄露：看板只记录「密钥从哪取」，绝不记录密钥本身 ─────────────────────────
_SECRET_PATTERNS = [
    ("OpenAI 风格密钥(sk-…)", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("AWS 访问密钥 ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub 令牌", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("Slack 令牌", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API 密钥", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 令牌", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}")),
    ("URL 里的账号密码", re.compile(r"://[^/\s:@]+:[^/\s@]{3,}@")),
    ("URL 参数里的密钥", re.compile(r"(?i)[?&;](?:api[_-]?key|apikey|access[_-]?token|token|secret|sig|signature|password|passwd)=[^&\s#]{8,}")),
]
_SECRET_KEYNAME = re.compile(r"(?i)(secret|passw|token|api[_-]?key|credential|private)")
REDACTED = "[已隐去疑似密钥]"


def find_secret(s: str):
    """字符串里有没有疑似密钥；有则返回类型名（仅按已知格式匹配，宁可漏报也不误杀普通文本）。"""
    for name, rx in _SECRET_PATTERNS:
        if rx.search(s):
            return name
    return None


def redact_secrets(s: str) -> str:
    for _, rx in _SECRET_PATTERNS:
        s = rx.sub(REDACTED, s)
    return s


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _looks_high_entropy(s: str) -> bool:
    """长、无空白、字符类型 ≥3 种且熵高：像随机密钥而不像人写的文字/路径/UUID。"""
    if len(s) < 32 or any(c.isspace() for c in s) or s.startswith("/") or "://" in s:
        return False
    classes = sum(bool(x) for x in (any(c.islower() for c in s), any(c.isupper() for c in s),
                                    any(c.isdigit() for c in s), any(not c.isalnum() for c in s)))
    return classes >= 3 and _entropy(s) >= 4.0


def reject_secrets(o, where="", strict=False, depth=0):
    """定义文件里出现疑似密钥 → 直接拒绝写入（不回显内容）。strict=True 额外检查高熵长串与「键名像密钥」的字段。"""
    if depth > 10:
        return
    if isinstance(o, str):
        kind = find_secret(o)
        if kind:
            raise ValueError(f"{where or '内容'} 里含有疑似{kind}。看板不保存密钥本身：请改用 secret_ref（环境变量名 / 文件路径 / 密钥库名称）")
        if strict and _looks_high_entropy(o):
            raise ValueError(f"{where or '内容'} 看起来像一串随机密钥（长度 {len(o)}）。看板不保存密钥本身：请改用 secret_ref")
    elif isinstance(o, list):
        for i, x in enumerate(o):
            reject_secrets(x, f"{where}[{i}]", strict, depth + 1)
    elif isinstance(o, dict):
        for k, v in o.items():
            w = f"{where}.{k}" if where else str(k)
            if strict and isinstance(v, str) and _SECRET_KEYNAME.search(str(k)) and len(v) >= 16 \
                    and not any(c.isspace() for c in v) and not v.startswith("/") and _looks_keyish(v):
                raise ValueError(f"{w} 的字段名像密钥且值像密钥。看板不保存密钥本身：请改用 secret_ref")
            reject_secrets(v, w, strict, depth + 1)


def _looks_keyish(v: str) -> bool:
    classes = sum(bool(x) for x in (any(c.islower() for c in v), any(c.isupper() for c in v),
                                    any(c.isdigit() for c in v), any(not c.isalnum() for c in v)))
    return classes >= 3


def _str(v, name, n):
    if not isinstance(v, str):
        raise ValueError(f"{name} 必须是字符串")
    return redact_secrets(v[:n])


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
            out[k[:40]] = redact_secrets(v[:400])
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
        elif k in ("family", "version"):
            _ver.check_fv({k: v}, "任务")
            out[k] = v.strip()
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
        return redact_secrets(v[:4000])
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


GALLERY_KEYS = ("id", "thumb", "video", "frames", "title", "subtitle", "gold", "duration", "pages", "target_page", "asks",
                "windows", "cell", "turns", "style", "style_name", "data_source", "optics", "meta", "scored", "frame_pages")
GALLERY_EXTRA = ("media_map", "group", "version", "badge", "cells", "turn_names")


def _check_gallery(g) -> None:
    """图库视图的映射：只允许已知键；字段路径是字符串（或候选列表），媒体路径改写只做字符串替换。"""
    if g is None:
        return
    if not isinstance(g, dict):
        raise ValueError("gallery 必须是对象")
    for k, v in g.items():
        if k not in GALLERY_KEYS + GALLERY_EXTRA:
            raise ValueError(f"gallery 不支持的字段: {k}")
        if k in GALLERY_KEYS:
            ok = (isinstance(v, str) and 0 < len(v) <= 80) or (
                isinstance(v, list) and 0 < len(v) <= 4 and all(isinstance(x, str) and 0 < len(x) <= 80 for x in v))
            if not ok:
                raise ValueError(f"gallery.{k} 必须是字段路径（字符串，或最多 4 个候选路径的列表）")
        elif k == "media_map":
            if not isinstance(v, dict) or any(m not in ("anchor", "after", "prefix") or not isinstance(x, str) or len(x) > 200
                                              for m, x in v.items()):
                raise ValueError("gallery.media_map 只能有 anchor / after / prefix（字符串）")
            if "/" in (v.get("prefix") or "")[:1] or ".." in (v.get("prefix") or ""):
                raise ValueError("gallery.media_map.prefix 必须是相对路径前缀（不能以 / 开头，不能含 ..）")
        elif k in ("cells", "turn_names"):
            if not isinstance(v, list) or not 0 < len(v) <= 8 or any(not isinstance(x, str) or len(x) > 20 for x in v):
                raise ValueError(f"gallery.{k} 必须是 1~8 个短字符串的列表")
        elif not isinstance(v, str) or len(v) > 80:
            raise ValueError(f"gallery.{k} 必须是不超过 80 字的字符串")


def _check_review(obj: dict) -> None:
    """成果校验集定义：source 必须是资源目录内的 .jsonl；规则用声明式 op（不支持正则，避免 ReDoS）。"""
    src = obj.get("source")
    if not isinstance(src, str) or not src.endswith(".jsonl") or resolve_asset(src) is None:
        raise ValueError("source 必须是资源目录内的相对路径，且以 .jsonl 结尾（不能含 .. 或绝对路径）")
    if obj.get("kind") is not None and obj["kind"] not in REVIEW_KINDS:
        raise ValueError(f"kind 必须是 {list(REVIEW_KINDS)}")
    frm = obj.get("from")
    if frm is not None:
        if not isinstance(frm, dict) or any(not isinstance(v, (str, int, float)) for v in frm.values()):
            raise ValueError("from 必须是 {task, dataset, model, note} 这类标量对象")
    fields = obj.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 20:
        raise ValueError("fields 必须是 1~20 项的列表（每项 {label, key, type}）")
    for f in fields:
        if not isinstance(f, dict):
            raise ValueError("fields 每项必须是对象")
        t = f.get("type", "text")
        if t not in FIELD_TYPES:
            raise ValueError(f"fields.type 必须是 {list(FIELD_TYPES)}")
        keys = ("a", "b") if t == "diff" else ("key",)
        for k in keys:
            if not isinstance(f.get(k), str) or not f[k] or len(f[k]) > 80:
                raise ValueError(f"fields 里 type={t} 的项必须有字符串字段 {k}")
    _check_gallery(obj.get("gallery"))
    facets = obj.get("facets", [])
    if not isinstance(facets, list) or len(facets) > 6:
        raise ValueError("facets 必须是不超过 6 项的列表（每项 {id, label, field}）")
    fids = set()
    for f in facets:
        if not isinstance(f, dict):
            raise ValueError("facets 每项必须是对象")
        check_id(f.get("id"))
        if f["id"] in fids:
            raise ValueError(f"分面 id 重复: {f['id']}")
        fids.add(f["id"])
        if not isinstance(f.get("field"), str) or not f["field"] or len(f["field"]) > 80:
            raise ValueError(f"分面 {f['id']} 必须有字符串 field")
    matrix = obj.get("matrix")
    if matrix is not None:
        if not isinstance(matrix, dict):
            raise ValueError("matrix 必须是对象")
        if matrix.get("rows") not in fids or matrix.get("cols") not in fids:
            raise ValueError("matrix 的 rows / cols 必须是 facets 里声明过的分面 id")
        if matrix.get("metric") is not None and (not isinstance(matrix["metric"], str) or len(matrix["metric"]) > 80):
            raise ValueError("matrix.metric 必须是字符串字段名")
    rules = obj.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 30:
        raise ValueError("rules 必须是不超过 30 条的列表")
    seen = set()
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("rules 每项必须是对象")
        check_id(r.get("id"))
        if r["id"] in seen:
            raise ValueError(f"规则 id 重复: {r['id']}")
        seen.add(r["id"])
        if r.get("op") not in RULE_OPS:
            raise ValueError(f"规则 {r['id']} 的 op 必须是 {list(RULE_OPS)}")
        if not isinstance(r.get("field"), str) or not r["field"] or len(r["field"]) > 80:
            raise ValueError(f"规则 {r['id']} 必须有字符串 field")
        if r["op"] in RULE_NEEDS_VALUE and "value" not in r:
            raise ValueError(f"规则 {r['id']}（{r['op']}）必须有 value")
        v = r.get("value")
        if isinstance(v, list):
            if len(v) > 50 or any(isinstance(x, (dict, list)) for x in v):
                raise ValueError(f"规则 {r['id']} 的 value 列表最多 50 个标量")
        elif isinstance(v, dict):
            raise ValueError(f"规则 {r['id']} 的 value 不能是对象")
        if r["op"] in ("in", "notin") and not isinstance(v, list):
            raise ValueError(f"规则 {r['id']}（{r['op']}）的 value 必须是列表")
        if r["op"] in ("minlen", "maxlen", "gt", "gte", "lt", "lte") and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ValueError(f"规则 {r['id']}（{r['op']}）的 value 必须是数字")
        if r.get("severity", "error") not in ("error", "warn"):
            raise ValueError(f"规则 {r['id']} 的 severity 必须是 error 或 warn")
    ps = obj.get("page_size")
    if ps is not None and (isinstance(ps, bool) or not isinstance(ps, int) or not 1 <= ps <= 50):
        raise ValueError("page_size 必须是 1~50 的整数")
    # group_by：分组浏览（左侧可折叠的「一级 → 二级」导航），最多两级。
    # 每级 {key: 按哪个字段分组, label: 取显示名称用的字段(可选), catalog: [{parent, v, label}] 没有样本的分组也列出来(可选)}
    gb = obj.get("group_by")
    if gb is not None:
        if not isinstance(gb, list) or not 1 <= len(gb) <= 2:
            raise ValueError("group_by 必须是 1~2 项的列表（每项 {key, label, catalog}）")
        for g in gb:
            if not isinstance(g, dict) or not isinstance(g.get("key"), str) or not g["key"] or len(g["key"]) > 80:
                raise ValueError("group_by 每项必须有字符串 key")
            if g.get("label") is not None and (not isinstance(g["label"], str) or len(g["label"]) > 80):
                raise ValueError("group_by.label 必须是字符串")
            cat = g.get("catalog")
            if cat is not None:
                if not isinstance(cat, list) or len(cat) > 60:
                    raise ValueError("group_by.catalog 必须是不超过 60 项的列表")
                for c in cat:
                    if not isinstance(c, dict) or not isinstance(c.get("v"), str) or len(c["v"]) > 40 \
                            or any(k not in ("parent", "v", "label") or not isinstance(x, str) or len(x) > 80 for k, x in c.items()):
                        raise ValueError("group_by.catalog 每项必须是 {parent, v, label} 的字符串对象")
    sm = obj.get("summary")
    if sm is not None and (not isinstance(sm, list) or len(sm) > 4 or any(not isinstance(x, str) or not x or len(x) > 80 for x in sm)):
        raise ValueError("summary 必须是不超过 4 个字段名的列表（卡片收起后显示的摘要）")


_RES_COMMON = {"id", "name", "kind", "state", "owner", "desc", "note", "access", "tags", "used_by", "family", "version"}
_RES_BY_KIND = {
    "apikey": {"provider", "secret_ref", "quota", "rate_limit", "expires", "rotated", "rotate_days", "scopes", "endpoint"},
    "gpu": {"host", "count", "model", "mem_gb", "probe", "gpu_indices"},
    "cpu": {"host", "count", "model", "mem_gb", "probe"},
    "disk": {"host", "path", "capacity_bytes", "fs", "probe", "replica_of", "readonly"},
    "service": {"endpoint", "protocol", "auth_ref", "host"},
    "other": {"host", "count", "unit", "model"},
}
_RES_SECRETY_FIELDS = {"value", "key", "secret", "token", "password", "passwd", "api_key", "apikey", "access_key", "private_key"}
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def _date(v, name):
    import datetime
    if not isinstance(v, str):
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 格式的日期字符串")
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 格式的日期（收到 {v[:20]!r}）") from None
    return v


def _check_secret_ref(ref, where):
    if not isinstance(ref, dict) or len(ref) != 1:
        raise ValueError(f"{where}.secret_ref 必须是 {{env: 环境变量名}} / {{file: 绝对路径}} / {{vault: 密钥库中的名称}} 三选一")
    (k, v), = ref.items()
    if k == "env":
        if not isinstance(v, str) or not _ENV_RE.match(v) or len(v) > 48 or (len(v) >= 32 and "_" not in v):
            raise ValueError(f"{where}.secret_ref.env 必须是环境变量的「名字」（如 OPENAI_API_KEY，大写字母/数字/下划线）——"
                             "看起来你填的是密钥本身？看板不保存密钥")
    elif k == "file":
        if not isinstance(v, str) or not v.startswith("/") or len(v) > 400 or "\x00" in v:
            raise ValueError(f"{where}.secret_ref.file 必须是存放密钥的文件的绝对路径")
    elif k == "vault":
        if not isinstance(v, str) or not 1 <= len(v) <= 80:
            raise ValueError(f"{where}.secret_ref.vault 必须是密钥库里的条目名称（1~80 字符）")
    else:
        raise ValueError(f"{where}.secret_ref 的键只能是 env / file / vault，收到 {k!r}")


def _pos(v, name, integer=False, lo=0.0, strict_pos=False):
    _num(v, name, lo)
    if strict_pos and v <= 0:
        raise ValueError(f"{name} 必须大于 0")
    if integer and int(v) != v:
        raise ValueError(f"{name} 必须是整数")
    return v


def _check_resources(obj: dict) -> None:
    """资源池定义：只记录「资源是什么、怎么取、额度多少」，不记录密钥本身。"""
    extra = set(obj) - {"title", "description", "items", "_example"}
    if extra:
        raise ValueError(f"不支持的顶层字段: {sorted(extra)}（只有 title / description / items）")
    items = obj.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        raise ValueError("items 必须是 1~200 项的列表")
    ids = set()
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("items 每项必须是对象")
        check_id(it.get("id"))
        w = f"items[{it['id']}]"
        if it["id"] in ids:
            raise ValueError(f"资源 id 重复: {it['id']}")
        ids.add(it["id"])
        kind = it.get("kind")
        if kind not in RES_KINDS:
            raise ValueError(f"{w}.kind 必须是 {list(RES_KINDS)}")
        _ver.check_fv(it, w)
        bad = [k for k in it if k not in _RES_COMMON and k not in _RES_BY_KIND[kind]]
        if bad:
            hint = "；看板不保存密钥本身，请用 secret_ref 指向它" if set(bad) & _RES_SECRETY_FIELDS else ""
            raise ValueError(f"{w}（{kind}）不支持字段 {bad}{hint}")
        if it.get("state") is not None and it["state"] not in RES_STATES:
            raise ValueError(f"{w}.state 必须是 {list(RES_STATES)}")
        for f, n in (("name", 80), ("owner", 60), ("desc", 400), ("note", 400), ("access", 400), ("provider", 60), ("host", 120),
                     ("model", 80), ("scopes", 200), ("rate_limit", 80), ("endpoint", 300), ("protocol", 20), ("fs", 20), ("unit", 12)):
            if f in it and (not isinstance(it[f], str) or len(it[f]) > n):
                raise ValueError(f"{w}.{f} 必须是不超过 {n} 字的字符串")
        if "tags" in it and (not isinstance(it["tags"], list) or len(it["tags"]) > 10 or any(not isinstance(x, str) or len(x) > 30 for x in it["tags"])):
            raise ValueError(f"{w}.tags 必须是不超过 10 个短字符串")
        if "used_by" in it:
            ub = it["used_by"]
            if not isinstance(ub, list) or len(ub) > 30:
                raise ValueError(f"{w}.used_by 必须是不超过 30 项的列表")
            for u in ub:
                if not isinstance(u, dict) or set(u) - {"label", "task", "dataset"}:
                    raise ValueError(f"{w}.used_by 每项只能有 label / task / dataset")
                if "task" in u:
                    check_id(u["task"])
                if "dataset" in u and (not isinstance(u["dataset"], str) or not _REF_RE.match(u["dataset"])):
                    raise ValueError(f"{w}.used_by.dataset 必须是 数据图id/节点id")
        if "secret_ref" in it:
            _check_secret_ref(it["secret_ref"], w)
        elif kind == "apikey" and it.get("state", "active") == "active":
            pass                                        # 缺失由体检提示，而不是拒绝（允许先登记、后补）
        if "quota" in it:
            q = it["quota"]
            if not isinstance(q, dict) or set(q) - {"limit", "unit", "period"}:
                raise ValueError(f"{w}.quota 必须是 {{limit, unit, period}}")
            _pos(q.get("limit"), f"{w}.quota.limit", strict_pos=True)
            if q.get("unit") is not None and (not isinstance(q["unit"], str) or len(q["unit"]) > 12):
                raise ValueError(f"{w}.quota.unit 必须是不超过 12 字的字符串")
            if q.get("period", "month") not in RES_PERIODS:
                raise ValueError(f"{w}.quota.period 必须是 {list(RES_PERIODS)}")
        for f in ("expires", "rotated"):
            if f in it:
                _date(it[f], f"{w}.{f}")
        if "rotate_days" in it:
            _pos(it["rotate_days"], f"{w}.rotate_days", integer=True, strict_pos=True)
        for f in ("count", "mem_gb", "capacity_bytes"):
            if f in it:
                _pos(it[f], f"{w}.{f}", integer=(f != "mem_gb"), strict_pos=True)
        if "probe" in it and it["probe"] != "local":
            raise ValueError(f"{w}.probe 目前只支持 \"local\"（探测运行看板服务的这台机器）")
        if "gpu_indices" in it:
            gi = it["gpu_indices"]
            if not isinstance(gi, list) or len(gi) > 64 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in gi):
                raise ValueError(f"{w}.gpu_indices 必须是 GPU 序号（非负整数）的列表")
        if "path" in it and (not isinstance(it["path"], str) or not it["path"].startswith("/") or len(it["path"]) > 400 or "\x00" in it["path"]):
            raise ValueError(f"{w}.path 必须是绝对路径")
        if "readonly" in it and not isinstance(it["readonly"], bool):
            raise ValueError(f"{w}.readonly 必须是 true/false")
        for f in ("replica_of", "auth_ref"):
            if f in it and (not isinstance(it[f], str) or not _REF_RE.match(it[f])):
                raise ValueError(f"{w}.{f} 必须是 资源池id/资源id")


def clean_usage_patch(p: dict) -> dict:
    """资源的动态信息（已用量、分配情况、状态覆盖）：与静态定义分开存。"""
    if not isinstance(p, dict):
        raise ValueError("请求体必须是 JSON 对象")
    out = {}
    for k, v in p.items():
        if v is None:
            continue
        if k == "used":
            out[k] = _num(v, k)
        elif k == "note":
            out[k] = _str(v, k, 300)
        elif k == "state":
            if v not in RES_STATES:
                raise ValueError(f"state 必须是 {list(RES_STATES)}")
            out[k] = v
        elif k == "metrics":
            out[k] = _scalars(v, k, 20)
        elif k == "allocations":
            if not isinstance(v, list) or len(v) > MAX_ALLOCS:
                raise ValueError(f"allocations 必须是不超过 {MAX_ALLOCS} 项的列表（整体替换）")
            al = []
            for a in v:
                if not isinstance(a, dict) or set(a) - {"label", "task", "amount", "unit", "until", "note"}:
                    raise ValueError("allocations 每项只能有 label / task / amount / unit / until / note")
                c = {"label": _str(a.get("label", ""), "allocation.label", 60), "amount": _num(a.get("amount"), "allocation.amount")}
                if a.get("task") is not None:
                    c["task"] = check_id(a["task"])
                if a.get("unit") is not None:
                    c["unit"] = _str(a["unit"], "allocation.unit", 12)
                if a.get("until") is not None:
                    c["until"] = _date(a["until"], "allocation.until")
                if a.get("note") is not None:
                    c["note"] = _str(a["note"], "allocation.note", 120)
                al.append(c)
            out[k] = al
        else:
            raise ValueError(f"不支持的字段: {k}")
    return out


def clean_definition(coll: str, obj) -> dict:
    if coll not in DEF_COLLS:
        raise ValueError(f"coll 必须是 {list(DEF_COLLS)}")
    if coll == "resources":
        reject_secrets(obj, strict=True)           # 在 clean_json 静默隐去之前先拒绝：让写入方知道出了问题
    obj = clean_json(obj)
    if not isinstance(obj, dict):
        raise ValueError("定义必须是 JSON 对象")
    obj.pop("id", None)
    if coll != "resources":
        _ver.check_fv(obj, f"{coll} 定义")                # 资源池本身不分版本，版本写在 items 的每一项上
    for k in _ver.DERIVED:
        obj.pop(k, None)                                  # 派生字段由服务端算，写进定义里的一律丢弃
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
            if coll == "datasets":
                _ver.check_fv(n, f"节点 {n['id']}")
            for k in _ver.DERIVED:
                n.pop(k, None)
        edges = obj.get("edges", [])
        if not isinstance(edges, list) or len(edges) > 1000:
            raise ValueError("edges 必须是列表")
        for e in edges:
            if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
                raise ValueError(f"边引用了不存在的节点: {e}")
        layout = obj.get("layout")
        if layout is not None and layout not in ("graph", "list"):
            raise ValueError('layout 只能是 "graph"（关系图）或 "list"（按分组的模块化清单）')
        groups = obj.get("groups", [])
        if not isinstance(groups, list):
            raise ValueError("groups 必须是列表")
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("nodes"), list) or any(x not in ids for x in g["nodes"]):
                raise ValueError(f"groups 引用了不存在的节点: {g}")
    elif coll == "reviews":
        _check_review(obj)
    elif coll == "resources":
        _check_resources(obj)
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
    cn = {"pipelines": "Pipeline", "datasets": "数据图", "modules": "展示模块", "reviews": "校验集", "resources": "资源池"}[coll]
    title = (new.get("title") if isinstance(new, dict) else None) or iid
    if old is None:
        return f"新增{cn}「{title}」"
    parts = []
    if coll == "resources":
        a = {x.get("id"): x for x in old.get("items", []) if isinstance(x, dict)}
        b = {x.get("id"): x for x in new.get("items", []) if isinstance(x, dict)}
        nm = lambda m, i: str(m[i].get("name") or i)  # noqa: E731
        for tag, lst in (("新增", [nm(b, i) for i in b if i not in a]), ("移除", [nm(a, i) for i in a if i not in b]),
                         ("状态变化", [nm(b, i) for i in b if i in a and a[i].get("state") != b[i].get("state")])):
            if lst:
                parts.append(f"{tag} " + "、".join(lst[:3]) + ("…" if len(lst) > 3 else ""))
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


def _mark_counts(marks: dict) -> dict:
    c = {v: 0 for v in MARK_VERDICTS}
    for m in marks.values():
        if isinstance(m, dict) and m.get("v") in c:
            c[m["v"]] += 1
    return c


def _slim_marks(obj: dict) -> dict:
    """写入时间线的标记快照：只保留汇总数，不带每条标记（明细在 state/marks/ 里）。"""
    if "counts" in obj and "marks" not in obj:
        return {"counts": obj["counts"], "n": obj.get("n", sum(obj["counts"].values()))}
    c = _mark_counts(obj.get("marks") or {})
    return {"counts": c, "n": sum(c.values())}


def _marks_label(rid: str, slim: dict) -> str:
    d = read_json(item_path("reviews", rid), None) if ID_RE.match(rid) else None
    title = (d or {}).get("title") or rid if isinstance(d, dict) else rid
    c = slim["counts"]
    return f"校验「{title}」：合理 {c['pass']} · 偏弱 {c['weak']} · 判错 {c['fail']} · 不确定 {c['doubt']}"


def _usage_view(u: dict) -> dict:
    """时间线里的用量快照：去掉内部字段 _logged_at。"""
    return {k: v for k, v in u.items() if not k.startswith("_")}


def _usage_label(gid: str, u: dict, iid=None) -> str:
    d = read_json(item_path("resources", gid), None) if ID_RE.match(gid) else None
    title = (d.get("title") if isinstance(d, dict) else None) or gid
    items = {x.get("id"): x for x in (d.get("items") or []) if isinstance(x, dict)} if isinstance(d, dict) else {}
    its = (u.get("items") or {})
    if iid and iid in its:
        name = (items.get(iid) or {}).get("name") or iid
        e = its[iid]
        bits = []
        if "used" in e:
            bits.append(f"已用 {e['used']:g}")
        if e.get("state"):
            bits.append(e["state"])
        if e.get("allocations") is not None:
            bits.append(f"{len(e['allocations'])} 项分配")
        return f"资源「{title}」· {name}：" + "，".join(bits) if bits else f"资源「{title}」· {name} 更新"
    return f"资源「{title}」用量更新（{len(its)} 项）"


def update_usage(gid: str, iid: str, patch: dict) -> dict:
    """更新某个资源的动态信息（已用额度 / 分配情况 / 状态覆盖）。资源必须已在定义里存在。"""
    check_id(iid)
    clean = clean_usage_patch(patch)          # 字符串里的疑似密钥在这里已被隐去（与任务 message 一致：上报不因备注被拒而中断）
    d = read_json(item_path("resources", gid), None)
    if not isinstance(d, dict):
        raise FileNotFoundError(f"资源池 {gid} 不存在")
    if not any(isinstance(x, dict) and x.get("id") == iid for x in d.get("items", [])):
        raise FileNotFoundError(f"资源 {gid}/{iid} 不存在")
    path, now = item_path("usage", gid), time.time()
    with lock():
        u = read_json(path, None) or {}
        items = u.setdefault("items", {})
        cur = items.setdefault(iid, {})
        old_state = cur.get("state")
        cur.update(clean)
        cur["at"] = now
        if len(items) > 200:
            raise ValueError("用量记录超过 200 项")
        first = "_logged_at" not in u
        log = first or cur.get("state") != old_state or now - u["_logged_at"] >= EVENT_GAP
        if log:
            u["_logged_at"] = now
        write_json(path, u)
        if log:
            _log_put("usage", gid, _usage_view(u), _usage_label(gid, u, iid), first or cur.get("state") != old_state)
    return cur


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
                elif coll == "usage":
                    view, now = _usage_view(obj), time.time()
                    if old is None or (_canon(old) != _canon(view) and now - obj.get("_logged_at", 0) >= EVENT_GAP):
                        obj["_logged_at"] = now
                        write_json(item_path(coll, iid), obj)
                        _log_put(coll, iid, view, _usage_label(iid, view), old is None)
                        n += 1
                elif coll == "marks":
                    # 时间线只存汇总数；明细变化靠节流补记，保证停止标记后最终汇总也会进入时间线
                    slim, now = _slim_marks(obj), time.time()
                    if old is None or (_canon(old) != _canon(slim) and now - obj.get("_logged_at", 0) >= EVENT_GAP):
                        obj["_logged_at"] = now
                        write_json(item_path(coll, iid), obj)
                        _log_put(coll, iid, slim, _marks_label(iid, slim), old is None)
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


def set_marks(rid: str, entries: list) -> dict:
    """批量写入 / 清除成果校验的人工标记。

    entries: [{"line": 第几行(从 1 开始), "v": "pass"|"weak"|"fail"|"doubt"|None(清除), "note": 备注, "tags": [..], "h": 行哈希(可选)}]
    返回汇总 {"counts": {pass, fail, doubt}, "n": 已标记总数}。"""
    check_id(rid)
    if not isinstance(entries, list) or not entries or len(entries) > 500:
        raise ValueError("marks 必须是 1~500 项的列表")
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("marks 每项必须是对象")
        ln = e.get("line")
        if isinstance(ln, bool) or not isinstance(ln, int) or not 1 <= ln <= 50_000_000:
            raise ValueError("line 必须是 >= 1 的整数（第几行，从 1 开始）")
        if "v" not in e or (e["v"] is not None and e["v"] not in MARK_VERDICTS):
            raise ValueError(f"v 必须是 {list(MARK_VERDICTS)} 之一，或 null（清除标记）")
        tags = e.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError("tags 必须是列表")
        h = e.get("h")
        if h is not None and (isinstance(h, bool) or not isinstance(h, int)):
            raise ValueError("h 必须是整数")
        clean.append({"line": ln, "v": e["v"], "note": _str(e.get("note") or "", "note", 500),
                      "tags": [_str(x, "tag", 30) for x in tags][:5], "h": h})
    if not os.path.exists(item_path("reviews", rid)):
        raise FileNotFoundError(f"校验集 {rid} 不存在")
    path, now = item_path("marks", rid), time.time()
    with lock():
        obj = read_json(path, None) or {}
        marks = obj.setdefault("marks", {})
        for c in clean:
            if c["v"] is None:
                marks.pop(str(c["line"]), None)
                continue
            m = {"v": c["v"], "at": now}
            if c["note"]:
                m["note"] = c["note"]
            if c["tags"]:
                m["tags"] = c["tags"]
            if c["h"] is not None:
                m["h"] = c["h"]
            marks[str(c["line"])] = m
        if len(marks) > MAX_MARKS:
            raise ValueError(f"标记数超过 {MAX_MARKS}")
        slim = _slim_marks(obj)
        first = "_logged_at" not in obj
        log = first or now - obj["_logged_at"] >= EVENT_GAP
        if log:
            obj["_logged_at"] = now
        write_json(path, obj)
        if log:
            _log_put("marks", rid, slim, _marks_label(rid, slim), first)
    return slim


# ── 体检 ──────────────────────────────────────────────────────────────────────
EXAMPLE_PATH_PREFIX = "/example/"      # 范例里的占位路径统一用这个前缀，真实数据不会出现


def _has_example_path(o) -> bool:
    if isinstance(o, str):
        return o.startswith(EXAMPLE_PATH_PREFIX)
    if isinstance(o, list):
        return any(_has_example_path(x) for x in o)
    if isinstance(o, dict):
        return any(_has_example_path(v) for v in o.values())
    return False


def lint_versions(add, scope_label: str, pairs: list, what: str) -> None:
    """版本相关的体检，对所有「同一个东西有多个版本」的地方通用。pairs = [(id, 条目)]，同一个 scope 里的才会归到一起。
    只看显式写的 family（图库校验集靠 gallery.group 兜底的不检查：那是旧写法）。"""
    fams: dict = {}
    for iid, x in pairs:
        f = x.get("family")
        if isinstance(f, str) and f.strip():
            fams.setdefault(f, []).append((iid, x))
            if not _ver.ver_of(x):
                add("warn", f"{scope_label}:{iid}: 写了 family「{f}」却没有 version（分不出新旧；版本号如 \"v2.0\"）")
    for fam, ms in fams.items():
        if len(ms) < 2:
            add("warn", f"{scope_label}: family「{fam}」只有 1 个{what}——没有多个版本就不需要 family，去掉它")
            continue
        seen: dict = {}
        for iid, x in ms:
            v = _ver.ver_of(x)
            if v and v in seen:
                add("warn", f"{scope_label}: family「{fam}」里{what}「{seen[v]}」和「{iid}」的 version 都是「{v}」——同一个东西的版本号不能重复")
            elif v:
                seen[v] = iid
        with_ver = [_ver.ver_of(x) for _, x in ms if _ver.ver_of(x)]
        if any(not any(ch.isdigit() for ch in v) for v in with_ver):
            add("warn", f"{scope_label}: family「{fam}」的版本号里有不含数字的（如 \"final\"）——无法比大小，会按{what}在列表里的先后判断新旧（越靠后越新）")


def lint(ignore_example: bool = False) -> list:
    """检查定义是否完整、引用是否有效。返回 [(level, message)]，level 为 error / warn。
    ignore_example=True 时不报「残留范例内容」（用于检查 examples/ 自身）。"""
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
    # 安全网：状态目录里任何文件出现疑似密钥都是错误（手改文件会绕过 sb 的拦截）。只报位置，不回显内容
    for c in COLLS:
        for iid, o in raw[c].items():
            try:
                reject_secrets(o, strict=(c == "resources"))
            except ValueError as e:
                add("error", f"{c}/{iid}.json: {e}")
    if not ignore_example:
        for c in COLLS:
            for iid, o in raw[c].items():
                if o.get("_example"):
                    add("warn", f"{c}/{iid}: 带有范例标记 _example——这是 examples/ 里的范例内容。"
                                "真实项目请按项目事实重写，不要沿用范例的名称/数字/路径，并删除该标记")
                elif _has_example_path(o):
                    add("warn", f"{c}/{iid}: 含 {EXAMPLE_PATH_PREFIX} 开头的范例占位路径，请换成真实地址")
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
            owner = {}
            for gr in g.get("groups") or []:
                for nid in (gr.get("nodes") or []):
                    if nid in owner:
                        add("warn", f"{tag}: 节点「{nid}」同时属于分组「{owner[nid]}」和「{gr.get('name') or gr.get('id')}」——分组框会重叠，一个节点只应属于一个分组")
                    else:
                        owner[nid] = gr.get("name") or gr.get("id")
            for n in g["nodes"]:
                nt = f"{tag}:{n['id']}"
                if not n.get("name"):
                    add("warn", f"{nt}: 缺少 name")
                if len(ids) > 1 and n["id"] not in linked:
                    add("warn", f"{nt}: 孤立节点（没有任何连线）")
                if kind == "pipelines":
                    if n.get("kind") is not None and n["kind"] not in PIPE_KINDS:
                        add("warn", f"{nt}: kind「{n['kind']}」不在 {list(PIPE_KINDS)} 里（图上会原样显示这个英文词）")
                    if n.get("name") and display_width(n["name"]) > PIPE_NAME_MAX:
                        add("warn", f"{nt}: name 太长，结构图上只显示前 {PIPE_NAME_MAX // 2} 个汉字，后面会被截断")
                    if n.get("role") and display_width(n["role"]) > PIPE_ROLE_MAX:
                        add("warn", f"{nt}: role 太长（{display_width(n['role'])} > {PIPE_ROLE_MAX}），结构图上只显示前 {PIPE_ROLE_MAX // 2} 个汉字；完整内容请放 desc")
                    if not n.get("role"):
                        add("warn", f"{nt}: 缺少 role（一句话说明这一步的作用，会直接显示在结构图上）")
                    if not n.get("desc"):
                        add("warn", f"{nt}: 缺少 desc（详细说明）")
                    for r in (n.get("reads") or []) + (n.get("writes") or []):
                        if not ref_ok("datasets", r):
                            add("warn", f"{nt}: reads/writes 引用的数据节点不存在: {r}")
                else:
                    if not n.get("brief"):
                        grp = next((x.get("name") for x in (g.get("groups") or [])
                                    if n["id"] in (x.get("nodes") or [])), None)
                        add("warn", f"{nt}: 缺少 brief（一句话说明这份数据是什么 / 有多少，会直接显示在结构图节点上"
                                    f"{'、分组「' + grp + '」里' if grp else ''}）")
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
            if kind == "datasets":
                lint_versions(add, tag, [(n["id"], n) for n in g["nodes"]], "节点")
    for rid, r in raw["reviews"].items():
        rt = f"reviews/{rid}"
        try:
            clean_definition("reviews", r)
        except ValueError as e:
            add("error", f"{rt}: {e}")
            continue
        if not r.get("title"):
            add("warn", f"{rt}: 缺少 title")
        if not r.get("description"):
            add("warn", f"{rt}: 缺少 description（这批成果是什么、为什么要校验、怎么判定合格）")
        full = resolve_asset(r["source"])
        if not full or not os.path.isfile(full):
            add("warn", f"{rt}: 源文件不存在: {r['source']}（应放在资源目录 {assets_dir()} 下）")
        frm = r.get("from") or {}
        if frm.get("dataset") and not ref_ok("datasets", frm["dataset"]):
            add("warn", f"{rt}: from.dataset 引用的数据节点不存在: {frm['dataset']}")
        if frm.get("task") and frm["task"] not in raw["tasks"]:
            add("warn", f"{rt}: from.task 引用的任务不存在: {frm['task']}")
        if not frm:
            add("warn", f"{rt}: 缺少 from（这批成果来自哪个任务/数据/模型，便于追溯）")
    res_ids = {gid: {x.get("id") for x in (g.get("items") or []) if isinstance(x, dict)} for gid, g in raw["resources"].items()}
    for gid, g in raw["resources"].items():
        rt = f"resources/{gid}"
        try:
            clean_definition("resources", g)
        except ValueError as e:
            add("error", f"{rt}: {e}")
            continue
        if not g.get("title"):
            add("warn", f"{rt}: 缺少 title")
        if not g.get("description"):
            add("warn", f"{rt}: 缺少 description（这个资源池是什么、归谁管、怎么申请使用）")
        lint_versions(add, rt, [(it["id"], it) for it in g["items"]], "资源")
        for it in g["items"]:
            it_t = f"{rt}:{it['id']}"
            if not it.get("desc"):
                add("warn", f"{it_t}: 缺少 desc（这个资源拿来做什么）")
            if not it.get("owner"):
                add("warn", f"{it_t}: 缺少 owner（出问题找谁）")
            k = it["kind"]
            if it.get("state") in ("revoked", "disabled"):
                continue                      # 已撤销 / 已停用的资源不再要求补全额度、过期、地址等信息
            if k == "apikey":
                if "secret_ref" not in it:
                    add("warn", f"{it_t}: 缺少 secret_ref（密钥从哪取：env / file / vault）")
                if "expires" not in it:
                    add("warn", f"{it_t}: 缺少 expires（过期日期；没有过期时间的密钥请写一个复核日期）")
                if "quota" not in it and "rate_limit" not in it:
                    add("warn", f"{it_t}: 缺少 quota / rate_limit（额度或限速）")
            elif k in ("gpu", "cpu") and "count" not in it and it.get("probe") != "local":
                add("warn", f"{it_t}: 缺少 count（{'卡数' if k == 'gpu' else '核数'}），或设 probe=local 自动探测")
            elif k == "disk":
                if "path" not in it:
                    add("warn", f"{it_t}: 缺少 path（挂载点 / 目录）")
                if "capacity_bytes" not in it and it.get("probe") != "local":
                    add("warn", f"{it_t}: 缺少 capacity_bytes，或设 probe=local 自动探测")
            if k in ("gpu", "cpu", "disk", "service") and not it.get("access"):
                add("warn", f"{it_t}: 缺少 access（别人怎么用它：ssh / 队列 / 挂载 / 地址）")
            for u in it.get("used_by") or []:
                if u.get("task") and u["task"] not in raw["tasks"]:
                    add("warn", f"{it_t}: used_by.task 引用的任务不存在: {u['task']}")
                if u.get("dataset") and not ref_ok("datasets", u["dataset"]):
                    add("warn", f"{it_t}: used_by.dataset 引用的数据节点不存在: {u['dataset']}")
            for f in ("replica_of", "auth_ref"):
                if it.get(f):
                    g2, i2 = it[f].split("/", 1)
                    if i2 not in res_ids.get(g2, set()):
                        add("warn", f"{it_t}: {f} 引用的资源不存在: {it[f]}")
    for coll_name, cn in (("pipelines", "Pipeline"), ("datasets", "数据图"), ("modules", "展示模块"), ("reviews", "校验集")):
        lint_versions(add, coll_name, [(i, o) for i, o in raw[coll_name].items()], cn)
    by_kind: dict = {}
    for tid, t in raw["tasks"].items():
        by_kind.setdefault(t.get("kind") or "job", []).append((tid, t))
    for k, pairs in by_kind.items():
        lint_versions(add, f"tasks({k})", pairs, "任务")
    for gid, u in raw["usage"].items():
        for iid, e in (u.get("items") or {}).items():
            if iid not in res_ids.get(gid, set()):
                add("warn", f"usage/{gid}: 用量记录引用了不存在的资源 {iid}（定义已被删除？）")
            for a in (e.get("allocations") or []) if isinstance(e, dict) else []:
                if isinstance(a, dict) and a.get("task") and a["task"] not in raw["tasks"]:
                    add("warn", f"usage/{gid}:{iid}: 分配里引用的任务不存在: {a['task']}")
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
