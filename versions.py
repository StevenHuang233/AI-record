"""多个版本：同一个东西（数据 / Pipeline / 展示模块 / 校验集 / 任务 / 资源）的 v1.0、v2.0 …（仅标准库）

约定：条目带 family（同一个东西的所有版本写同一个值）+ version（版本号字符串）。
服务端在这里统一算出「谁是最新」，前端只负责显示——全站只有这一份判断逻辑，汇总数字也据此排除历史版本。

annotate() 写入的派生字段（只读，来自定义文件里的同名键会被先清掉）：
    vversion  这个条目的版本号（只要能解析出来就有，哪怕它是独一份）
    vfamily   所属 family（只有该 family 至少 2 个成员时才有）
    vrank     0 = 最新版，1、2… = 越来越旧的历史版本（同上，只有成组时才有）
    vcount    这个 family 一共几个版本
"""
from __future__ import annotations

import re

FAMILY_MAX, VERSION_MAX = 40, 20
DERIVED = ("vfamily", "vversion", "vrank", "vcount")
_DIGITS = re.compile(r"\d+")


def check_fv(obj: dict, where: str) -> None:
    """写入时校验：family / version 若出现，必须是非空短字符串（前端要做字符串运算，写成数字会出错）。"""
    for f, mx, ex in (("family", FAMILY_MAX, "同一个东西的所有版本写同一个值，如 \"KnowShiftQA\""),
                      ("version", VERSION_MAX, "如 \"v2.0\"")):
        if f in obj:
            v = obj[f]
            if not isinstance(v, str) or not v.strip() or len(v) > mx:
                raise ValueError(f"{where} 的 {f} 必须是 1~{mx} 字的非空字符串（{ex}）")


def ver_of(x: dict) -> str:
    """版本号：顶层 version → meta.version（旧写法）→ gallery.version（视频图库校验集）。"""
    for src in (x, x.get("meta"), x.get("gallery")):
        if isinstance(src, dict):
            v = src.get("version")
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
                return str(v).strip()[:VERSION_MAX]
    return ""


def fam_of(x: dict) -> str:
    """所属 family：显式写的优先；没写时，视频图库校验集用 gallery.group（要求同时有 gallery.version）。"""
    f = x.get("family")
    if isinstance(f, str) and f.strip():
        return f.strip()[:FAMILY_MAX]
    g = x.get("gallery")
    if isinstance(g, dict) and isinstance(g.get("group"), str) and g["group"].strip() and ver_of({"gallery": g}):
        return g["group"].strip()[:FAMILY_MAX]
    return ""


def num_key(v: str) -> tuple:
    """版本号 → 数字段元组，去掉末尾的 0：'v1.10' → (1, 10)，'v2' 与 'v2.0' 都是 (2,)。"""
    nums = [int(m) for m in _DIGITS.findall(v)]
    while nums and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def rank(members: list) -> list:
    """members 是同一个 family 的条目（保持传入顺序）。返回按「新 → 旧」排好的列表。
    版本号都含数字：按数字段逐段比；相同、或有任何一个不含数字：传入顺序靠后的算新。"""
    vers = [ver_of(x) for x in members]
    comparable = all(_DIGITS.search(v) for v in vers)
    keyed = [((num_key(v) if comparable else ()), i) for i, v in enumerate(vers)]
    order = sorted(range(len(members)), key=lambda i: keyed[i])        # 升序：最后一个最新
    return [members[i] for i in reversed(order)]


def annotate(items: list, scope=None) -> int:
    """就地给 items 标上 vversion / vfamily / vrank / vcount。scope(x) 返回同一个字符串的条目才会被归到一起
    （例如任务按 kind 分开）。items 的传入顺序就是「顺序靠后的算新」时的依据。返回历史版本（vrank>0）的个数。"""
    groups: dict = {}
    for x in items:
        if not isinstance(x, dict):
            continue
        for k in DERIVED:
            x.pop(k, None)                 # 定义文件里手写的这些键不可信
        if x.get("error"):
            continue
        v = ver_of(x)
        if v:
            x["vversion"] = v
        f = fam_of(x)
        if f:
            groups.setdefault((scope(x) if scope else "", f), []).append(x)
    hist = 0
    for (_, fam), members in groups.items():
        if len(members) < 2:
            continue
        for r, x in enumerate(rank(members)):
            x["vfamily"], x["vrank"], x["vcount"] = fam, r, len(members)
            hist += 1 if r else 0
    return hist


def is_hist(x: dict) -> bool:
    return isinstance(x, dict) and (x.get("vrank") or 0) > 0
