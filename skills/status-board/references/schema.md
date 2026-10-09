# status_board 数据格式参考

所有 `id`：字母数字 `_ . -`，≤64。节点引用写作 `"<图id>/<节点id>"`。
写入定义：`sb.define(coll, id, dict_or_json_path)` 或 `python3 sb.py define <coll> <id> <file|->`（`-` 读 stdin）。

## 1. Pipeline `pipelines/<id>.json`

```json
{
  "title": "标题", "description": "整体中文说明",
  "groups": [{"id": "prep", "name": "数据准备", "desc": "阶段说明", "nodes": ["collect", "clean"]}],
  "nodes": [{
    "id": "clean", "name": "清洗过滤", "kind": "process",
    "role": "一句话说明这一步的作用（显示在结构图节点上，≤17 字最佳）",
    "desc": "详细说明：做了什么、规则、边界",
    "inputs": "输入是什么", "outputs": "输出是什么",
    "reads": ["数据图id/节点id"], "writes": ["数据图id/节点id"]
  }],
  "edges": [{"from": "collect", "to": "clean", "label": "可选，写传递的内容"}]
}
```

- `kind`：`source process model train eval output gate`。
- 图必须无环；节点自动分层、编号，按流程顺序排步骤说明。
- **节点状态来自任务**：带 `pipeline`+`node` 的普通/训练/评测任务、以及 `kind=run` 任务里的要素 `step`，自动汇总成节点 `status/progress`。没有任务关联时可手动 `sb.node("pipelines", gid, nid, status=..., note=...)`。

## 2. 数据管理 `datasets/<id>.json`

```json
{
  "title": "...", "description": "...",
  "nodes": [{
    "id": "raw", "name": "原始语料", "kind": "raw", "status": "ready",
    "desc": "含义：这份数据是什么",
    "source": "来源：从哪来、什么时候、谁给的",
    "purpose": "用处：拿来做什么，有什么限制（如严禁混入训练）",
    "processing": ["被处理情况：做过什么、被谁读取过", "可以写多条"],
    "locations": [
      {"label": "主", "role": "primary", "storage": "共享盘A", "path": "/abs/path", "verified": "2026-10-01", "auto_stat": true},
      {"label": "异地备份", "role": "backup", "storage": "共享盘B", "path": "/backup/path", "note": "每周日同步"}
    ],
    "meta": {"size_bytes": 123, "count": 69999, "count_unit": "条", "format": "jsonl", "version": "v1", "updated": "2026-10-01", "owner": "me"}
  }],
  "edges": [{"from": "raw", "to": "clean", "label": "加工过程，如：清洗去重"}]
}
```

- `kind`：`raw processed derived external model`；`status`：`ready building stale missing failed`。
- **地址是核心**：`locations` 每条必须有 `path`；`role`：`primary backup mirror remote cache`。**有双地址/多副本就写多条**。
- `auto_stat: true`（在某条 location 上，路径须是**服务所在机器**可访问的绝对路径）：后台统计实际大小与文件数，缓存 5 分钟；主地址不存在则节点置 `missing`；≥2 个地址都统计完成时自动比对大小+文件数，显示「副本一致 / 不一致」。
- `processing` 可写字符串列表，或 `{"step":"清洗","desc":"…","to":"clean"}`。页面还会自动合并「图上的入边/出边」和「Pipeline 里 reads/writes 它的步骤」，所以只需补充图和 Pipeline 表达不了的内容。
- 页面会列出哪些任务在 `datasets[].ref` 里使用了这份数据。

## 3. 训练任务 `kind="train"`

```python
sb.task("train-v2", kind="train", name="微调 v2", pipeline="main", node="train",
        status="running", done=0, total=1000, unit="step", stale_after=600,
        model={"name": "base-7b", "type": "全参微调"},          # 任意标量键
        hparams={"lr": 2e-5, "batch": 64, "epochs": 3},
        datasets=[{"name": "标注数据", "ref": "main/labels", "role": "训练集", "count": 40000, "used": 0}],
        metrics={"loss": 2.4})
sb.task("train-v2", done=100, metrics={"loss": 1.9},
        datasets=[{"name": "标注数据", "ref": "main/labels", "role": "训练集", "count": 40000, "used": 4000}])
sb.point("train-v2", "loss", 100, 1.9)          # 追加曲线点，最多 8 条曲线，每条 300 点
sb.task("train-v2", status="done")
```

`datasets[]` 每项：`name`、`ref`（`"数据图id/节点id"`，推荐，页面自动带出地址）或 `path`、`role`、`count`（总量）、`used`（已使用量，页面显示用量条）、`note`。**列表整体替换**：更新 `used` 时要把完整列表重新传。

## 4. Pipeline 执行 `kind="run"`

```python
sb.task("run-0930", kind="run", name="9-30 批次", pipeline="main", status="running",
        datasets=[{"name": "清洗后数据", "ref": "main/clean", "role": "输入"}])
sb.item("run-0930", "s01", step="clean", status="running", name="样本组 s01")   # 要素 s01 当前在 clean 步
sb.item("run-0930", "s01", step="clean", status="done")
sb.item("run-0930", "s01", step="label", status="failed", note="三次重试失败，已丢弃")
```

- `step` 是 Pipeline 的**节点 id**；`status`：`pending running done failed skipped`。
- 页面展示「各步骤上的要素数」和「要素 × 步骤」矩阵；流经的步骤自动按图的先后关系点亮；Pipeline 节点显示 `▶处理中 ✓完成 ✗失败 …排队` 和 `passed`（已流转到后续步骤的数量）。
- 要素 >200：用 `step_counts={"节点id": {"running":n,"done":n,"failed":n,"pending":n}}` 汇总，不要逐个 item（上限 500 个）。

## 5. 评测任务 `kind="eval"`

```python
sb.task("eval-v2", kind="eval", name="评测 v2", pipeline="main", node="eval",
        status="running", done=0, total=646, unit="题",
        model={"name": "base-7b + v2"},
        datasets=[{"name": "评测集", "ref": "main/bench", "role": "评测集", "count": 646, "used": 0}])
sb.task("eval-v2", done=300, message="已评 300 题")
sb.task("eval-v2", status="done", scores={"accuracy": 0.125, "作答率": 0.488}, metrics={"报错": 0})
```

评测用的**数据集及其地址**通过 `datasets[].ref`（推荐，地址在数据图里统一维护）或 `path` 体现；模型写 `model`。

## 6. 里程碑 / 时间线

`sb.milestone(title, note="", tags=["v2"])` 在时间线上打旗标，供回放时快速跳转。用于：版本发布、规则/Pipeline 结构变更、重大实验开始/结束。

时间线自动记录：新增/修改/删除定义、任务状态变化与（节流后的）进度、节点更新、里程碑。页面可拖动、播放（0.5x–8x）、只看关键事件；变化以动画过渡（节点位置、连线形变、进度条增长、曲线延伸、元素淡入淡出）。

## 7. 定制展示模块 `modules/<id>.json`

```json
{"title": "...", "description": "...", "blocks": [ {"type": "..."}, ... ]}
```

| type | 字段 |
|---|---|
| `heading` | `text` |
| `text` | `text` |
| `kv` | `items:[{k,v}]` 或 `data:{k:v}` |
| `metrics` | `items:[{label,value,unit,delta,better:"higher"\|"lower",note}]` 指标卡，`delta` 自动染色 |
| `table` | `columns:[...]`, `rows:[[...]]`（≤200 行） |
| `chart` | `kind:"bar"`: `labels`, `series:[{name,values}]`, `max` ；否则折线: `series:[{name,points:[[x,y],...]}]`；均可带 `title` |
| `images` / `media` | `columns`(1–6), `items:[{src,caption,type:"image"\|"video"\|"audio"}]`，`src` 相对资源目录 |
| `samples` | `source`（资源目录内 `.jsonl`）, `skip`, `limit`(≤50), `fields:[{label,key,type}]`；`key` 支持 `a.b`；`type`: `long image audio video` |
| `compare` | `items:[{title, sides:[{label, text/image/video/audio}]}]`，并排对比 |
| `progress` | `items:[{label,value(0-1),note}]` |

资源目录默认 `state/assets/`（`SB_ASSETS_DIR` 可改）。`src`/`source` 是相对路径，禁止 `..`；图片/音视频支持 Range（可拖动播放）。`.jsonl` 不能被直接下载，只能通过 `samples` 读取（单行 >200KB 会被跳过）。

## 8. 命令行速查

```bash
python3 sb.py task <id> --kind train --name 微调 --status running --done 10 --total 100 --model base-7b \
        --json '{"datasets":[{"name":"x","ref":"g/n"}],"hparams":{"lr":2e-5}}'
python3 sb.py point <task> loss 100 1.9
python3 sb.py item <task> <要素> --step clean --status done
python3 sb.py node datasets <图> <节点> --status ready --meta count=70000
python3 sb.py define pipelines main main.json
python3 sb.py milestone "v2 发布" --note "…" --tag v2
python3 sb.py rm-task <id>
python3 sb.py show
python3 sb.py validate [--strict]
```

## 9. 体检 `validate` 检查什么

JSON 可解析、边引用的节点存在、图无环、孤立节点、缺 `title/description`；Pipeline 节点缺 `role/desc`、`reads/writes` 引用不存在；数据节点缺 `locations/source/purpose/processing`；任务引用的 `pipeline/node/ref` 不存在、训练/评测缺 `model/datasets`、`run` 缺 `pipeline`。

## 10. 服务与安全

- 启动：`python3 server.py --port <p>`（默认只监听 127.0.0.1，并校验 Host）。对外监听必须 `export SB_TOKEN=…`，否则写接口关闭，且不向浏览器返回数据真实路径（`SB_SHOW_PATHS=1` 可开启）。
- 写接口：`POST /api/tasks/<id>`、`DELETE /api/tasks/<id>`、`POST /api/<pipelines|datasets>/<gid>/nodes/<nid>`、`PUT /api/define/<coll>/<id>`、`POST /api/milestone`；要求 `Content-Type: application/json`，有令牌则需 `Authorization: Bearer`。
- 环境变量：`SB_STATE_DIR` `SB_ASSETS_DIR` `SB_TOKEN` `SB_URL` `SB_ALLOWED_HOSTS` `SB_EVENT_GAP` `SB_STAT_TTL` `SB_STAT_BUDGET`。
