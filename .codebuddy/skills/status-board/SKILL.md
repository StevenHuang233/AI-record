---
name: status-board
description: 用 status_board 看板记录并展示项目的执行情况。当需要记录或更新模型训练进度、评测进度、Pipeline 执行进度、数据资产（含多地址备份、来源/用处/被处理情况）、迭代版本、效果展示或数据集抽样时使用，也用于创建/修改 Pipeline 结构图与数据管理图，或在这个仓库里做任何上报。看板有时间线，可动画回放变化。
---

# status-board

`status_board` 是一个仅依赖 Python 标准库的看板：AI 或脚本把「做了什么、跑到哪了、数据在哪」写进状态目录，网页自动显示，并按时间线回放变化。仓库根目录即看板本体（`server.py`、`sb.py`、`sbstore.py`、`static/`、`state/`）。

## 核心原则

1. **边做边记，不要事后补。** 开始一个任务就上报 `running`，结束时置 `done` / `failed`。用户靠看板了解进展，没记录等于没做。
2. **只通过 `sb`（库或命令行）写状态**，不要手改 `state/tasks/`、`state/timeline.jsonl`。定义文件（`pipelines/ datasets/ modules/`）可用 `sb.define` 或直接改 JSON，服务会自动补记到时间线。
3. **写完定义必须体检**：`python3 sb.py validate`。有「警告」就补全，目标是 0 错误 0 警告。
4. **不要虚构数据。** 不知道的大小/条数/来源就不填，不要编；地址要真实可核对。路径只写事实，不写凭据。
5. **上报失败不能拖垮业务**：库调用外层包 try，或用 `sb.track` / `sb.task` 的 safe 方式；看板没起不影响主任务。

## 上手（3 步）

```bash
cd <仓库根>
python3 server.py --port <空闲端口>        # 已有人在跑就不要重复启动；先 curl localhost:<端口>/healthz
python3 sb.py show                          # 看当前有哪些 Pipeline / 数据图 / 任务
python3 sb.py validate                      # 体检
```

脚本里：

```python
import sys; sys.path.insert(0, "<仓库根>"); import sb
```

远程机器上报：`export SB_URL=http://host:port`（可选 `SB_TOKEN`），同样的代码改走 HTTP。`validate` 只能在状态目录所在机器跑。

## 你要记录什么 → 用什么

| 要记录的事 | 做法 | 详见 |
|---|---|---|
| Pipeline 每一步是什么、起什么作用 | `sb.define("pipelines", id, {...})`，每个节点写 `role`（一句话作用，直接显示在图上）+ `desc` | `references/schema.md` §1 |
| 每份数据：在哪、什么意义 | `sb.define("datasets", id, {...})`：`locations`（**有备份必须写多条**）+ `source` + `purpose` + `processing` | §2 |
| 模型训练 | `sb.task(id, kind="train", model=..., datasets=[...], hparams=..., ...)` + `sb.point(id,"loss",step,v)` | §3 |
| Pipeline 执行（每个数据要素到哪一步） | `sb.task(id, kind="run", pipeline=...)` + `sb.item(id, 要素id, step=节点id, status=...)` | §4 |
| 评测 | `sb.task(id, kind="eval", model=..., datasets=[评测集+地址], scores={...})` | §5 |
| 迭代/版本节点 | `sb.milestone("v2 清洗规则升级", note="…")` | §6 |
| 数据集展示、效果展示 | `sb.define("modules", id, {"blocks":[...]})` | §7 |

## 标准工作流

**A. 接手一个新项目（先建「地图」）**
1. 读项目，列出流程步骤与数据资产。
2. 写 Pipeline 定义：每个节点 `role`/`desc`/`inputs`/`outputs`，用 `reads`/`writes`（`"数据图id/节点id"`）把步骤与数据连起来；可用 `groups` 划分阶段。
3. 写数据图定义：每份数据填 `locations`/`source`/`purpose`/`processing`，边上写加工过程（`label`）。本机路径加 `"auto_stat": true` 可自动统计大小并校验备份是否一致。
4. `sb.milestone("初始化看板")`，然后 `python3 sb.py validate`。

**B. 跑任务（边跑边记）**
1. 开始：`sb.task(id, kind=..., name=..., pipeline=..., node=..., model=..., datasets=[...], status="running", total=N)`。带上 `pipeline`+`node`，图上对应节点会自动联动。
2. 过程中：节流上报（约每秒/每 N 步一次）：`sb.task(id, done=n)`；曲线用 `sb.point`；指标用 `metrics={...}`。长循环用 `for x in sb.track(it, id, ...)` 自动收尾。
3. 结束：`sb.task(id, status="done")`（有 `total` 会自动补满）；异常 `status="failed", message="原因"`。
4. 有版本意义的变化：`sb.milestone(...)`。

**C. 数据发生变化（新增/迁移/加工/备份）**
立刻更新数据图：改 `locations`、`processing`、`status`，或 `sb.node("datasets", gid, nid, status=..., meta={...})`。新增加工产物就加节点和边。**改动会自动进入时间线**，不要删历史、不要把旧信息直接抹掉而不留 `processing` 说明。

**D. 做效果展示/数据集展示** → 写 `modules` 定义（见 `references/schema.md` §7），资源放资源目录（默认 `state/assets/`，`SB_ASSETS_DIR` 可改），用相对路径引用。

## 常见坑

- `id` 只能含字母数字 `_ . -`，≤64 字符；节点引用格式是 `"<图id>/<节点id>"`。
- 流水线执行任务里**要素很多（>200）时不要逐个 `sb.item`**，改传汇总：`sb.task(id, step_counts={"clean":{"running":3,"done":120,"failed":1,"pending":40}})`。
- 进度更新间隔：同一任务的普通进度每 60 秒才写入时间线一次（状态变化必写），不用担心刷屏；调试可设 `SB_EVENT_GAP=1`。
- 任务超过 `stale_after`（默认 300s）没心跳会标「疑似停滞」。长耗时步骤要设大 `stale_after` 或定时心跳（`sb.task(id, message="…")`）。
- 曲线每条最多 300 点（超出自动抽稀），`sb.point` 随便调。
- 数据 `locations[].path` 在对外监听（`--host 0.0.0.0`）时默认不返回给浏览器。
- 不要把令牌/密钥写进任何定义、任务或 note。

## 修改看板本身时

改 `static/*.js` 后至少检查：括号配平、`import/export` 名字一致；渲染文本一律经 `h()`/`s()`（`textContent`），**不要用 `innerHTML`**。动画依赖 `util.js` 的 `patch`（增量合并 + `data-key`），新增视图元素要给稳定的 `data-key`，否则无法平滑过渡。
