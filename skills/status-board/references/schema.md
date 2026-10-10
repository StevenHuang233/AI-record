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

## 7b. 成果校验 `reviews/<id>.json`

用来**逐条审阅一批成果**：模型生成的回答、清洗出来的数据、标注结果……规则自动检查 + 人工标记（合格 / 不合格 / 存疑）。
成果是一个 `.jsonl`（一行一个 JSON 对象），放在资源目录里。校验集**可以独立存在**，不依赖 Pipeline。

```json
{
  "title": "标题", "description": "这批成果是什么、为什么要校验、怎么判定合格（判定标准写清楚）",
  "kind": "model_output | cleaned_data | generic",
  "from": {"task": "任务id", "dataset": "数据图id/节点id", "model": "模型名", "note": "批次说明"},
  "source": "相对资源目录的路径.jsonl",
  "page_size": 8,
  "fields": [
    {"label": "问题", "key": "question"},
    {"label": "回答", "key": "answer", "type": "long"},
    {"label": "前后对比", "type": "diff", "a": "字段A", "b": "字段B"},
    {"label": "配图", "key": "img", "type": "image"}
  ],
  "rules": [
    {"id": "has_answer", "field": "answer", "op": "nonempty", "label": "回答不能为空", "severity": "error"},
    {"id": "short_ok", "field": "answer", "op": "maxlen", "value": 2000, "label": "不超长", "severity": "warn"}
  ]
}
```

**`fields`**（页面上每条样本显示什么）：`key` 支持 `a.b.0` 取嵌套值；`type`：`text`(默认) `long`(可滚动长文本) `json` `image` `audio` `video`（值是资源目录内相对路径）`diff`（用 `a`/`b` 两个字段做逐字对比，删除红、新增绿）。

**`rules`**（自动检查，对**每一行**求值；不支持正则/表达式，是声明式的）：

| op | 通过的条件 | 需要 `value` |
|---|---|---|
| `exists` | 字段存在且不是 null | 否 |
| `nonempty` | 存在，且不是空串 / 全空白 / 空列表 / null | 否 |
| `minlen` `maxlen` | 字符串或列表长度 ≥ / ≤ value | 数字 |
| `contains` `notcontains` `startswith` `endswith` | 字符串包含 / 不含 / 开头 / 结尾 | 字符串 |
| `equals` `notequals` | 等于 / 不等于 | 标量 |
| `in` `notin` | 值在 / 不在列表里 | 列表（≤50） |
| `gt` `gte` `lt` `lte` | 数值比较（非数值视为不通过） | 数字 |
| `json` | 字符串能解析成 JSON | 否 |
| `asset_exists` | 字段值是资源目录内存在的文件 | 否 |
| `eq_field` `neq_field` | 与另一个字段相等 / 不等 | 另一个字段名 |
| `unique` | 全文件内该字段不重复 | 否 |

`severity`：`error`（默认）/ `warn`（提示级）。字段不存在时，除 `notcontains`/`notequals`/`notin`/`neq_field` 外一律判不通过——**想表达"没有就算了"请先用 `exists` 单独成规则**。

**人工标记**：页面里选中样本按 `1` 合格 / `2` 不合格 / `3` 存疑（再按一次取消），`J`/`K` 上下切换，`N` 写备注。脚本也可以标：

```python
sb.review_rows(rows, "out/answers.jsonl")                 # 把一批成果写成资源目录里的 jsonl（原子替换）
sb.define("reviews", "r1", {...})                         # 声明校验集
sb.mark("r1", 12, "fail", note="拒答", tags=["拒答"])       # line 从 1 开始，空行不计；verdict=None 清除
```

标记保存在 `state/marks/<id>.json`（记录标记时那一行的内容哈希，源文件改动后该标记会显示「可能过期」）；**时间线里只记汇总数**（合格/不合格/存疑），不带逐条明细，且同一校验集 60 秒内最多写一条事件。

**注意**
- `source` 只能是资源目录内的相对路径、以 `.jsonl` 结尾；`..`、绝对路径、指向目录外的软链都会被拒绝。
- 规则扫描在后台线程进行，文件大时先显示「正在扫描」；超过 `SB_REVIEW_MAX_ROWS`（默认 30 万行）或 `SB_REVIEW_BUDGET`（默认 60 秒）则只扫描前面部分并标注。
- 单行超过 200KB 会被跳过并计为"不是合法 JSON 对象"。
- 回放历史时不显示样本明细（读的是当前文件，和历史对不上），只显示当时的标记汇总。

## 7c. 资源管理 `resources/<id>.json`

登记**能被调用的资源**：API Key、GPU / CPU 主机、磁盘、对外服务。页面显示「怎么取、额度多少、谁在用、有没有问题」。

> **铁律：看板不保存密钥本身，只登记「密钥从哪取」。** 写进密钥会被拒绝（见下文「防泄露」）。

```json
{
  "title": "标题", "description": "这个资源池是什么、归谁管、怎么申请使用",
  "items": [
    {"id": "llm_main", "name": "主力大模型 Key", "kind": "apikey", "state": "active",
     "desc": "拿来做什么", "owner": "负责人", "access": "怎么调用 / 怎么申请",
     "provider": "服务商", "scopes": "权限范围", "rate_limit": "600 请求/分钟", "endpoint": "https://…",
     "secret_ref": {"env": "OPENAI_API_KEY"},
     "quota": {"limit": 5000000, "unit": "tokens", "period": "month"},
     "expires": "2027-01-31", "rotated": "2026-09-01", "rotate_days": 90,
     "used_by": [{"label": "批量标注", "task": "任务id"}]},
    {"id": "gpu_a", "name": "训练 GPU", "kind": "gpu", "host": "主机名", "count": 8, "model": "型号", "mem_gb": 80, "access": "ssh + 队列名"},
    {"id": "cpu_a", "name": "处理机", "kind": "cpu", "count": 128, "mem_gb": 1024, "probe": "local", "access": "…"},
    {"id": "disk_a", "name": "工作盘", "kind": "disk", "path": "/data/work", "capacity_bytes": 20000000000000, "fs": "nfs", "access": "挂载点说明"},
    {"id": "disk_bk", "name": "备份盘", "kind": "disk", "path": "/backup/work", "replica_of": "池id/disk_a", "readonly": true},
    {"id": "svc", "name": "推理网关", "kind": "service", "endpoint": "https://…", "protocol": "http", "auth_ref": "池id/llm_main"}
  ]
}
```

**字段是白名单**：通用字段（`id name kind state owner desc note access tags used_by`）+ 每种 `kind` 自己的字段，多写会被拒绝。

| kind | 专有字段 |
|---|---|
| `apikey` | `provider secret_ref quota rate_limit expires rotated rotate_days scopes endpoint` |
| `gpu` | `host count model mem_gb probe gpu_indices` |
| `cpu` | `host count model mem_gb probe` |
| `disk` | `host path capacity_bytes fs probe replica_of readonly` |
| `service` | `endpoint protocol auth_ref host` |
| `other` | `host count unit model` |

**`secret_ref`（三选一）**：`{"env": "变量名"}`（必须是大写字母/数字/下划线的**名字**）、`{"file": "/绝对路径"}`、`{"vault": "密钥库里的条目名"}`。页面只检查「有没有」：环境变量是否非空、文件是否存在/非空/权限是否 600，**从不读取内容**。注意检查的是**看板服务进程**的环境，不是你的 shell。

**动态信息（不写在定义里）**：已用额度、谁占了多少卡，用 `sb.resource` 上报，存在 `state/usage/<池id>.json`：

```python
sb.resource("池id", "llm_main", used=1250000)                                    # 额度已用（与 quota.unit 同单位）
sb.resource("池id", "gpu_a", allocations=[{"label": "微调 v2", "task": "任务id", "amount": 4, "until": "2026-12-31"}])   # 整体替换：释放 = 上报时去掉那一项
sb.resource("池id", "gpu_a", state="maintenance", note="周五升级驱动")             # 状态覆盖，不用改定义
```

`allocations` 每项只能有 `label task amount unit until note`；磁盘的 `unit` 可写 `GB/TB`，GPU / CPU 的 `amount` 是卡数 / 核数。

**自动判断（页面上的「正常 / 需关注 / 异常」）**

| 对象 | 需关注 | 异常 |
|---|---|---|
| API Key | 14 天内过期；超过轮换周期；额度 ≥ 90%；密钥文件权限过宽；没登记来源 | 已过期；额度用尽；环境变量未设置或为空；密钥文件不存在 / 为空 / 不可读 |
| 磁盘 | 已用 ≥ 85% | 已用 ≥ 95%；路径不存在 |
| GPU / CPU / 其他 | 维护中；分配已过期；CPU 负载 ≥ 100% | 分配总量 > 容量 |
| 已撤销 / 已停用 | 显示为「已停用」，不再做任何检查 | — |

**本机探测**（`"probe": "local"`，仅 `gpu` / `cpu` / `disk`）：探测的是**运行看板服务的这台机器**——GPU 逐卡利用率与显存（固定参数的 `nvidia-smi`）、CPU 核数与负载与内存、磁盘总量与已用。后台线程执行并缓存 5 秒，挂死的网络盘不会拖住页面；`gpu_indices: [0,1]` 可只取其中几张卡。想看**别的机器**的 GPU：在那台机器上跑脚本，用 `sb.resource(..., metrics={...}, used=...)` 上报；看板**不会主动连接**任何远程地址（避免 SSRF）。

**防泄露（多层）**
1. 定义里出现已知格式的密钥（`sk-…`、AWS、GitHub、Slack、Google、JWT、私钥、`Bearer …`、URL 账号密码、URL 参数里的 `api_key=`）→ 整个定义被拒绝，错误信息**不回显内容**。
2. 资源定义额外检查：任意字段里的高熵长串、`key / secret / token / password` 这类字段名 → 拒绝，并提示改用 `secret_ref`；`secret_ref.env` 写成了一串随机字符（把密钥填进了变量名）→ 拒绝。
3. 其他所有定义、任务的 `message / name / metrics`、用量的 `note`：疑似密钥被**静默替换**为 `[已隐去疑似密钥]`（上报不会因此中断，但密钥不落盘、不进时间线）。
4. `sb.py validate` 会扫描**整个状态目录**，发现疑似密钥报**错误**——兜底手改文件绕过 `sb` 的情况。
5. 对外监听（`--host 0.0.0.0`）时，不向浏览器返回 `host / path / endpoint / access` 和密钥文件路径。
6. 检测基于已知格式，**不是万能的**：自定义格式的密钥可能漏掉。不要依赖它，别把密钥交给看板。

**体检会提示**：缺 `desc / owner`；apikey 缺 `secret_ref / expires / quota 或 rate_limit`；gpu/cpu 缺 `count`（且没设 probe）；disk 缺 `path / capacity_bytes`；`access` 缺失；`used_by / auth_ref / replica_of` 引用不存在；用量里引用了已删除的资源或不存在的任务。已撤销 / 已停用的资源不再要求补全。

## 8. 命令行速查

```bash
python3 sb.py task <id> --kind train --name 微调 --status running --done 10 --total 100 --model base-7b \
        --json '{"datasets":[{"name":"x","ref":"g/n"}],"hparams":{"lr":2e-5}}'
python3 sb.py point <task> loss 100 1.9
python3 sb.py item <task> <要素> --step clean --status done
python3 sb.py node datasets <图> <节点> --status ready --meta count=70000
python3 sb.py define pipelines main main.json
python3 sb.py milestone "v2 发布" --note "…" --tag v2
python3 sb.py define reviews r1 review.json
python3 sb.py mark r1 12 fail --note "拒答"      # verdict: pass | fail | doubt | clear
python3 sb.py define resources pool res.json
python3 sb.py resource pool llm_main --used 1250000 --note "本月"
python3 sb.py resource pool gpu_a --alloc "微调=4:任务id" --alloc "评测=2"      # 整体替换；--alloc none 清空
python3 sb.py rm-task <id>
python3 sb.py show
python3 sb.py validate [--strict]
```

## 9. 体检 `validate` 检查什么

JSON 可解析、边引用的节点存在、图无环、孤立节点、缺 `title/description`；Pipeline 节点缺 `role/desc`、`reads/writes` 引用不存在；数据节点缺 `locations/source/purpose/processing`；校验集缺 `description/from`、源文件不存在、`from.dataset/task` 引用不存在；资源池缺 `desc/owner/secret_ref/expires` 等（见 §7c）；**状态目录里任何文件出现疑似密钥（错误）**；任务引用的 `pipeline/node/ref` 不存在、训练/评测缺 `model/datasets`、`run` 缺 `pipeline`。

## 10. 服务与安全

- 启动：`python3 server.py --port <p>`（默认只监听 127.0.0.1，并校验 Host）。对外监听必须 `export SB_TOKEN=…`，否则写接口关闭，且不向浏览器返回数据真实路径（`SB_SHOW_PATHS=1` 可开启）。
- 写接口：`POST /api/tasks/<id>`、`DELETE /api/tasks/<id>`、`POST /api/<pipelines|datasets>/<gid>/nodes/<nid>`、`PUT /api/define/<coll>/<id>`（coll 含 `reviews`）、`POST /api/milestone`、`POST /api/review/<id>/marks`、`POST /api/resources/<池id>/<资源id>`（更新用量）；读样本 `GET /api/review/<id>?view=&page=&size=&rule=`；要求 `Content-Type: application/json`，有令牌则需 `Authorization: Bearer`。
- 环境变量：`SB_STATE_DIR` `SB_ASSETS_DIR` `SB_TOKEN` `SB_URL` `SB_ALLOWED_HOSTS` `SB_EVENT_GAP` `SB_STAT_TTL` `SB_STAT_BUDGET` `SB_REVIEW_MAX_ROWS` `SB_REVIEW_BUDGET` `SB_PROBE_TTL`。
