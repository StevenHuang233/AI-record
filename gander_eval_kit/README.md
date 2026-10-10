# gander_eval_kit

在 **Gander（Omni-Interaction-Agent）** 上跑视频评测集的一套脚本：把「视频画面 + 语音提问」实时推进前后台服务，
拿回模型的口语回答，用评测集自带的官方计分器出分，并在规则抽不到答案时用判官模型（LLM-as-a-judge）兜底。

只依赖已有的 conda 环境 `gander` 与本机 LiteLLM 网关，不改动上游仓库代码。

## 目录

| 文件 | 作用 |
|---|---|
| `test_video_bench.py` | 纯前台跑法：直接在进程内加载模型，每秒一帧 + 1 秒静音组成 duplex unit，提问以文本注入 |
| `gk_run.py` | 前后台跑法：走完整服务（前台 + 运行时 + 后台 Codex），画面走 `/ws/screen`、音频走 `/ws/duplex`，逐 unit 记录完整通话轨迹 |
| `make_gk_instances.py` | 生成 N 个相互独立的服务实例（端口、GPU、Codex home、工作目录各自隔离） |
| `score_official.py` | 用评测集自带的官方计分器出分（纯规则，不联网） |
| `score_with_judge.py` | 接判官后重跑打分：规则抽不到时交给模型兜底，失败的样本再跑一轮取更优 |
| `judge_litellm.py` | 判官模块：经 LiteLLM 网关调用，带指数退避重试；区分「可重试」与「不可重试」错误 |

## 三种跑法

| 跑法 | 前台 | 后台 | 提问方式 | 适用 |
|---|---|---|---|---|
| `natural` | ✓ | — | 文本注入 / 语音 | 基线：自然口语提问，不加作答格式 |
| `protocol` | ✓ | — | 文本注入 | 在提问后追加官方要求的作答格式 |
| `fullstack` | ✓ | ✓ | 语音 + `turn.final` 原话 | 完整前后台链路 |

三者**口径不同**，横向比较要同时看作答率与准确率：未抽到答案的按 0 分计入，单独统计 `answered_rate`。

## 快速开始

```bash
# 0. 凭据走环境变量，不落盘（默认读 /data/workspace/.env，可用 LITELLM_ENV_FILE 改）
export LITELLM_API_KEY=...

# 1. 生成两个服务实例（各占 3 张卡：Thinker / Talker / ASR 分开）
python make_gk_instances.py --n 2
for i in 0 1; do ./scripts/serve.sh bench/instances/g$i/serve.yaml & done

# 2. 跑评测（396 个视频分两片并行；--limit 0 表示全量）
python gk_run.py --port 8010 --shard 0/2 --limit 0 --tag fullstack
python gk_run.py --port 8011 --shard 1/2 --limit 0 --tag fullstack

# 3. 官方打分
python score_official.py --release release_gaokao_mm_v1.1 --tag fullstack

# 4. 判官兜底重打分（能显著捞回「讲了但没说选项」的回答）
python score_with_judge.py --tag fullstack --model gpt-5.6-luna --passes 2
```

结果写 `results/<release>.<tag>.jsonl`，报告写 `results/<release>.<tag>.report.json`（判官版后缀 `.judge.json`）。

## 通话轨迹

`gk_run.py` 在跑的同时记录整通「通话」，和结果写在同一行的 `trace_text` 字段，一行一个事件：

```
[  0.82s] 画面通道就绪（media_mode=omni）
[  0.92s] unit 0：画面帧 #0 + 静音
[  3.92s] ▶ 用户开口（unit 6）：马上要交了，第4题怎么做啊？
[  5.92s] → 把第 0 条提问原话交给后台（turn.final，不经 ASR）
[  6.76s] tool.call：tool_calls=[{"name": "task_start", ...}]
[  6.77s] 后台任务：第4题选择正确答案=None
```

噪声字段（token id、音频字节数）已剔除，一行不超过 300 字符，一通通话约 46 行。
结构化事件可用 `--keep-events` 一并存下来。

## 踩过的坑

这些都不是看文档能知道的，逐个验证过：

1. **太极 PAT 以 `tcn` 开头**。写成"token `tcnXXX`"时很容易把英文 token 一起粘进去变成 `tokentcnXXX`，
   服务端一律报 `invalid api token` / `HTTP 401`。升级客户端无效（3.3.6 已是最新）。

2. **`transformers 4.51.0` × `deepspeed 0.19.7` 循环导入**：`from transformers import PreTrainedModel` 失败。
   原因在 deepspeed 的 `runtime/hybrid_engine.py` 只捕获 `(ImportError, AttributeError)`，实际抛 `RuntimeError`。
   给该处 `except` 补 `RuntimeError` 即可（训练走 ZeRO，不碰 hybrid engine）。

3. **Thinker 训练 1 卡会 OOM**（8B 全参 + AdamW 状态放不下 140 GB），用 8 卡 ZeRO-2；Talker 冻结 Thinker，1 卡够。

4. **Talker 模式强制关闭视觉塔**，而示例里的 `irrelevant_video` 带图 → `'MiniCPMo' object has no attribute 'vpm'`。
   多卡时只有部分 rank 抛，rank 0 只显示 exit code 1，要看 `outputs/launcher/rank_N.log`。

5. **HF 数据集用 LFS 三步法**：`GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1` → `git lfs fetch --all` → `git lfs checkout`。
   **不要用 `git rm -r --cached .` 重置工作区**，会清空索引导致后续 checkout 找不到文件（曾出现 29 张图变 0 字节）。

6. **ceph 上不能 `du -sb` 目录**：dop-fuse 的目录项报虚高尺寸。只统计文件用
   `find <dir> -type f -printf "%s\n" | awk '{s+=$1} END{print s}'`。

7. **画面走独立的 `/ws/screen` 通道**，不是 `/ws/duplex`。先发 `media.mode {video:true, source:"camera"}`，
   用返回的 `screen.token` 连 `/ws/screen?session_id=...&token=...`，等 `screen.ready` 后才能发帧
   （元数据 JSON + JPEG 二进制各发一次）。

8. **前台只接收音频，没有直接喂文本的接口**。文本只能经 `turn.final` 交给后台。
   想省掉 ASR 就把标注原话直接放进 `turn.final`，但让前台"听到"提问仍需送语音。

9. **后台派发依赖前台产生 tool_call**（`task_tools_online.py` 里 `if not event.is_tool_call: return`）。
   驱动脚本必须等后台任务全部进入终态再关会话，否则后台结果注入不进前台
   （报 `RuntimeError: duplex live session is already closed`），表现为前台说"交给后台处理"然后没下文。

10. **打分脚本的 `mode` 必须在 `MODES` 白名单里**（`text` / `protocol` / `natural`）。
    用自定义 tag（如 `fullstack`）会被**静默跳过**，报告是空的 `{}`，不报错——很容易误以为跑崩了。

11. **LiteLLM 网关**：`gpt-5.6` 系列**不接受 `temperature` 参数**（传任何非默认值都 400 `Unsupported value`）；
    `/models`、`/model/info`、`/embeddings` 返回 `code 999`（网关侧解析空时间戳），管理接口不可用。

12. **判官错误要分两类**：限流 / 超时 / 5xx / 无可用节点 → 指数退避重试；
    额度耗尽 / key 无效 / 无权限 / 模型名不存在 / 参数不支持 → 立刻判死，不浪费额度。

13. **服务实例的 ASR 不能与 Gander 同卡**，一个实例至少 3 张卡。

14. **同一台机器上跑多个实例**要各自隔离 `runtime_dir` / `ledger_dir` / `codex_home` / 工作目录。

## 与本项目数据的关系

这里的脚本是**通用的**（换一个 release 只需改 `--release` 和 `VIDEO_BENCH_ROOT`）。
具体项目的看板定义、数据登记、模块与校验集，是调用这些脚本产出的，不属于本目录。
