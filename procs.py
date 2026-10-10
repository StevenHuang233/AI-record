"""进程存活检查：任务带了 pid 时，看板能在几秒内发现「进程已经没了，但任务还写着运行中」（仅标准库）

为什么要有它：脚本被 kill -9、OOM、机器重启时来不及上报失败，任务会一直显示「运行中」，
只能等心跳超时（默认 300 秒）才标「疑似停滞」。有了 pid，服务端每次汇总状态时顺手查一下，页面刷新一次就能发现。

只在「同一台机器、同一个 PID 命名空间」里有意义：
  - 记录了 host 且与服务所在机器不同 → 不检查（remote），避免误报；
  - 容器里的脚本 + 容器外的服务，hostname 往往不同；若相同却看不到对方的进程，会误报「已退出」——
    这种部署请不要给任务设 pid，或设环境变量 SB_PROC_CHECK=0 关闭检查。
"""
from __future__ import annotations

import os
import socket

PID_MAX = 2 ** 31 - 1


def hostname() -> str:
    try:
        return socket.gethostname()[:64]
    except OSError:
        return ""


def proc_info(pid: int):
    """读 /proc/<pid>/stat，返回 {"state": "R", "start": 起始时刻(jiffies)}；没有 /proc 或进程不存在返回 None。"""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    rest = raw[raw.rfind(")") + 2:].split()      # 进程名可含空格和括号：以最后一个 ")" 为界
    if len(rest) < 20:
        return None
    try:
        return {"state": rest[0], "start": int(rest[19])}
    except ValueError:
        return None


def self_fields() -> dict:
    """当前进程的身份，直接并进 sb.task(...) 的参数里：sb.task("x", status="running", **sb.self_proc())"""
    out = {"pid": os.getpid(), "host": hostname()}
    info = proc_info(os.getpid())
    if info:
        out["pid_start"] = info["start"]
    return {k: v for k, v in out.items() if v not in ("", None)}


def check(pid, host=None, pid_start=None) -> str:
    """返回 alive / dead / remote / unknown。remote = 不在本机，没法查；unknown = 参数不可用或关闭了检查。"""
    if os.environ.get("SB_PROC_CHECK") == "0":
        return "unknown"
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= PID_MAX:
        return "unknown"                                  # 绝不拿 0 / 负数去 kill：那是在对进程组发信号
    if host and host != hostname():
        return "remote"
    try:
        os.kill(pid, 0)                                   # 信号 0：只检查进程是否存在，不会发送任何信号
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass                                              # 进程存在，只是不属于当前用户
    except OSError:
        return "unknown"
    info = proc_info(pid)
    if info:
        if info["state"] in ("Z", "X"):                   # 僵尸 / 已死：kill(0) 仍然成功，但它已经退出了
            return "dead"
        if isinstance(pid_start, int) and pid_start and info["start"] != pid_start:
            return "dead"                                 # 同一个 pid 已被别的进程复用
    return "alive"
