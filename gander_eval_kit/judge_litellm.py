#!/usr/bin/env python
"""LLM-as-a-judge：经 LiteLLM 聚合网关调判官模型，带指数退避重试。

两个入口：
  chat(prompt, model)              —— 通用判官（rubric 类计分器用）
  llm_extract(text, question_type, n_sub, model)  —— 兼容官方 extract.llm_extract 签名，
        规则抽不到答案时的兜底：让模型从口语讲解里抄录最终答案，并要求逐字引用佐证。

重试策略（针对「失败的部分」）：
  - 可重试：限流 429、超时、5xx、连接错误、无可用节点 → 指数退避 3s/6s/12s
  - 不重试：额度耗尽、key 无效、无权限、模型名不存在 → 立刻放行，让上层记录失败

凭据只从环境变量读（默认 /data/workspace/.env，可用 LITELLM_ENV_FILE 改），不写进任何文件。

用法：
  python judge_litellm.py --probe                    # 探测当前模型是否可用
  python judge_litellm.py --model gpt-5.6-luna --probe
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

DEFAULT_MODEL = "gpt-5.6-luna"
ENV_FILE = Path(os.environ.get("LITELLM_ENV_FILE", "/data/workspace/.env"))

# 可重试：上游临时问题
RETRYABLE = (
    "ratelimit", "429", "timeout", "timed out", "serviceunavailable", "503", "500", "502", "504",
    "connection", "no available instances", "no deployments available", "no healthy deployments",
    "overloaded", "internal server error", "temporarily",
)
# 不可重试：重试多少次都一样
FATAL = (
    "budget", "额度", "invalid proxy server token", "permissiondenied", "not allowed to access",
    "invalid model name", "authentication", "unauthorized", "401", "403",
    "unsupported value", "unsupported parameter",     # 参数不被上游接受，重试多少次都一样
)


def load_env() -> None:
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def client():
    from openai import OpenAI

    load_env()
    return OpenAI(api_key=os.environ["LITELLM_API_KEY"], base_url=os.environ["LITELLM_BASE_URL"],
                  timeout=120, max_retries=0)     # 重试由本模块自己做，SDK 层不重试


def _classify(exc: Exception) -> str:
    msg = str(exc).lower()
    for key in FATAL:
        if key in msg:
            return "fatal"
    for key in RETRYABLE:
        if key in msg:
            return "retryable"
    return "retryable"      # 未知错误按可重试处理，交给退避上限兜住


def chat(prompt: str, model: str = DEFAULT_MODEL, *, temperature: float | None = None, max_tokens: int = 800,
         max_retries: int = 4, base_delay: float = 3.0, log=None) -> tuple[str | None, str]:
    """返回 (回复文本, 状态)。状态：ok / fatal:<原因> / exhausted:<原因>

    temperature 默认不传：gpt-5.6 系列不接受该参数（传任何非默认值都 400 Unsupported value）。
    """
    c = client()
    kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    last = ""
    for attempt in range(max_retries):
        try:
            r = c.chat.completions.create(**kwargs)
            return (r.choices[0].message.content or "").strip(), "ok"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            kind = _classify(exc)
            if log:
                log(f"[judge] 第 {attempt + 1} 次失败（{kind}）{last[:120]}")
            if kind == "fatal":
                return None, f"fatal:{last}"
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    return None, f"exhausted:{last}"


# ---------------------------------------------------------------- 官方 llm_extract 兼容实现

def _norm(text: str) -> str:
    import re

    return re.sub(r"\s+", "", (text or "").strip())


def _letters(value) -> str:
    import re

    return "".join(re.findall(r"[A-Za-z]+", str(value or ""))).upper()


def _official_helpers():
    """优先复用 release 自带的归一化实现，取不到就用上面的简易版。"""
    try:                                            # noqa: SIM105
        from omniedu_bench import extract as ex

        return ex._norm, ex._letters
    except Exception:                               # noqa: BLE001
        return _norm, _letters


def llm_extract(text: str, question_type: str, n_sub: int, model: str = DEFAULT_MODEL,
                *, max_retries: int = 4, log=None) -> tuple[list[str] | None, dict]:
    """与官方 extract.llm_extract 同签名、同返回值：(答案列表|None, trace)。

    多了指数退避重试；校验口径不变——每个答案必须带逐字出现在原文里的引用，否则作废。
    """
    trace: dict = {"method": f"llm:{model}", "evidence": []}
    if not (text or "").strip():
        return None, {**trace, "reason": "empty_input"}

    norm_fn, letters_fn = _official_helpers()
    prompt = (f"下面是老师对一道选择题（共 {n_sub} 个小问，题型 {question_type}）的口头讲解。"
              "只抄录老师明确给出的最终答案，不要自己做题；老师没明确给出的小问填 null。"
              '输出 JSON：{"answers": ["A", ...], "quotes": ["原文中给出该答案的那句话", ...]}，'
              "quotes 必须逐字摘自讲解。\n\n讲解：\n" + text[:6000])

    for attempt in range(max_retries):
        reply, status = chat(prompt, model, max_tokens=400, max_retries=1, log=log)
        if reply is None:
            if status.startswith("fatal"):
                return None, {**trace, "reason": f"llm_error:{status}"}
            continue                                  # 可重试错误：下一轮
        try:
            data = json.loads(reply[reply.find("{"): reply.rfind("}") + 1])
            answers = data.get("answers") or []
            quotes = data.get("quotes") or []
        except (ValueError, KeyError, AttributeError):
            continue
        if len(answers) != n_sub or len(quotes) != n_sub:
            continue                                  # 形状不对，换个说法再来一次
        normalized = norm_fn(text)
        ok = True
        for answer, quote in zip(answers, quotes):
            if not answer or not quote or norm_fn(quote) not in normalized \
                    or letters_fn(answer)[:1] not in norm_fn(quote).upper():
                ok = False
                break
        if ok:
            return [letters_fn(a) for a in answers], {**trace, "evidence": quotes,
                                                      "attempts": attempt + 1}
        trace = {**trace, "evidence": quotes}         # 引用对不上，重试
    return None, {**trace, "reason": "llm_unsupported_quote"}


def make_judge(model: str = DEFAULT_MODEL, log=None):
    """给 rubric 类计分器用的判官：judge(prompt) -> str。"""
    def judge(prompt: str) -> str:
        reply, status = chat(prompt, model, max_tokens=1200, log=log)
        if reply is None and log:
            log(f"[judge] 判官调用失败：{status[:160]}")
        return reply or ""
    return judge


def probe(model: str = DEFAULT_MODEL) -> int:
    reply, status = chat("只回答两个字：可用", model, max_tokens=16, max_retries=1)
    print(f"[{model}] {status} -> {reply!r}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--probe", action="store_true", help="只探测模型是否可用")
    args = ap.parse_args()
    raise SystemExit(probe(args.model) if args.probe else 0)
