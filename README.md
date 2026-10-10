# status_board

通用、可移植的状态看板：**Pipeline 结构图（节点带中文作用标注）**、**数据管理图（多地址 + 来源/用处/处理情况）**、**进程记录（训练 / 流水线执行 / 评测）**、**定制展示模块**，并用**时间线**回放变化过程（以动画过渡而非直接切换）。

只依赖 Python 3.8+ 标准库，前端无 CDN、无构建步骤，整个目录拷走即可用。

```bash
python3 server.py                  # http://127.0.0.1:8765
python3 server.py --host 0.0.0.0 --port 8765    # 对外监听前必须先 export SB_TOKEN=xxx，否则写接口关闭
```

状态全部存放在 `state/`（`SB_STATE_DIR` 或 `--state-dir` 可改），实时模式每 2.5 秒轮询刷新。**新 clone 下来 `state/` 是空的**——看板上没有任何内容，直到你写入定义和任务；想先看效果，见下面的「范例」。

```mermaid
flowchart LR
    subgraph W["写入方"]
        L["脚本里 import sb"]
        C["命令行 sb.py"]
        R["远程机器 SB_URL"]
    end
    L --> S["state/<br/>pipelines · datasets · modules · tasks<br/>timeline.jsonl"]
    C --> S
    R -->|HTTP| V
    S --> V["server.py"]
    V --> P["网页：子页面 + 底部时间线"]
```

## 页面结构（子页面嵌套，米黄色基调）

用 URL hash 做多级子页面，带面包屑，可分享链接、浏览器前进后退可用：

| 地址 | 内容 |
|---|---|
| `#/` | 首页：六类入口、正在运行的任务、需要关注（失败/停滞/数据缺失/副本不一致）、最近里程碑 |
| `#/train` → `#/task/<id>` | 训练列表 → 详情：模型、使用的数据（含地址、用量）、超参数、训练曲线、指标 |
| `#/run` → `#/task/<id>` | 流水线执行：每个步骤上的数据要素数、「要素 × 步骤」矩阵 |
| `#/eval` → `#/task/<id>` | 评测：评测集及其地址、被测模型、进度与分数 |
| `#/pipelines` → `#/pipelines/<图>` → `#/pipelines/<图>/<步骤>` | 结构图（节点标注「作用」）→ 单个步骤子页面（输入输出、读写的数据、关联任务、上一步/下一步） |
| `#/datasets` → `#/datasets/<图>` → `#/datasets/<图>/<资产>` | 数据血缘图 + 清单 → 单份数据：**所有存放地址（含备份，自动校验副本一致性）**、含义、来源、用处、被处理情况 |
| `#/modules` → `#/modules/<id>` | 指标卡、柱状/折线图、表格、图片/音视频、jsonl 样本抽查、并排对比等 |
| `#/review` → `#/review/<id>` | **成果校验工作台**：审阅模型生成的结果 / 清洗出来的数据。规则自动检查（统计每条规则不通过的行数，点规则只看对应样本）+ 人工逐条标记「合格 / 不合格 / 存疑」+ 备注；`diff` 字段直接看清洗前后改了什么；翻页、筛选视图；键盘 `J/K` 切换、`1/2/3` 标记 |
| 底部时间线（全局） | 拖动 / 播放(0.5x–8x) / 上一个下一个 / 仅看关键事件 / 里程碑旗标；回放时节点平移、连线形变、进度条增长、曲线延伸、元素淡入淡出；回放中可继续点进子页面 |

## 快速使用

```python
import sb   # 本目录加入 sys.path，或直接拷贝 sb.py + sbstore.py

sb.define("pipelines", "main", "main.json")        # 写 Pipeline 结构图（字段见 skills/status-board/references/schema.md）
sb.define("datasets", "main", "data.json")         # 写数据管理图
sb.task("train1", kind="train", name="微调", pipeline="main", node="train",
        model={"name": "base-7b"}, datasets=[{"name": "训练集", "ref": "main/clean", "role": "训练集"}],
        status="running", done=10, total=1000, unit="step")
sb.point("train1", "loss", 10, 2.31)               # 曲线
sb.item("run1", "sample-01", step="clean", status="running")   # 流水线中的某个要素走到了哪步
sb.milestone("v2 清洗规则升级", note="…")            # 时间线旗标
```

```bash
python3 sb.py show        # 当前状态概览
python3 sb.py validate    # 体检：定义是否完整、引用是否有效
python3 sb.py -h          # 全部命令
```

远程机器上报：设 `SB_URL=http://host:port`（可选 `SB_TOKEN`），同样的代码自动改走 HTTP。

## 成果校验

用来**看模型生成的结果、清洗出来的数据**：把成果写成 `.jsonl` 放进资源目录，声明「看哪些字段、自动抓哪些问题」，然后在页面里逐条审阅。

```mermaid
flowchart LR
    A["成果 jsonl<br/>(资源目录)"] --> B["reviews 定义<br/>fields · rules · from"]
    B --> C["后台扫描全部行<br/>规则自动检查"]
    C --> D["工作台：统计 + 筛选视图"]
    D --> E["人工标记<br/>合格 / 不合格 / 存疑 + 备注"]
    E --> F["汇总进首页「需要关注」<br/>与时间线（仅汇总数）"]
```

```python
sb.review_rows(rows, "out/answers.jsonl")        # 把一批成果写成资源目录里的 jsonl
sb.define("reviews", "r1", {                      # 声明校验集（字段与规则见 skills/status-board/references/schema.md §7b）
    "title": "…", "description": "怎么判定合格", "kind": "model_output", "from": {"task": "…"},
    "source": "out/answers.jsonl",
    "fields": [{"label": "问题", "key": "q"}, {"label": "回答", "key": "a", "type": "long"}],
    "rules":  [{"id": "has_a", "field": "a", "op": "nonempty", "label": "回答不能为空"}]})
```

- 规则是**声明式 op**（非空、长度、包含、取值范围、字段相等、不重复、文件存在……），没有正则和表达式，不会执行配置里的任何代码。
- 大文件在后台线程扫描，页面先显示「正在扫描」；每行只记录失败的规则号，几十万行也不占多少内存。
- 人工标记存在 `state/marks/`，记录当时那一行的内容哈希：源文件改动后会提示「标记可能过期」。时间线里只记汇总数，不带逐条明细。

## 范例

`examples/` 里有五个**形状各异**的范例（线性链、扇入+关卡+扇出、并行支线、带人工关卡的纯步骤流、独立的成果校验集），内容全是虚构的，只用来对照字段写法，**不是模板**。

```bash
python3 examples/play.py                                     # 把五个范例模拟运行到一块临时看板（约 10 秒，只写 examples/.play/）
python3 server.py --state-dir examples/.play/all --port 8766
```

范例带 `_example` 标记并统一用 `/example/` 占位路径：页面上会打「范例」标签，抄进真实 `state/` 后 `sb.py validate` 会报警。详见 [`examples/README.md`](examples/README.md)。

## 给 AI 用

仓库自带 skill：`skills/status-board/`（`SKILL.md` + `references/schema.md` 字段格式 + `references/patterns.md` 流程形状与建模模式，流程类内容都用图表示），`.codebuddy/skills/status-board` 是指向它的软链，所以 clone 仓库后在仓库内工作的 AI 会直接发现它。想让其他项目里的 AI 也能用：`cp -r skills/status-board ~/.codebuddy/skills/`。AI 读完就知道怎么记录训练、评测、流水线执行、数据资产和里程碑，写完如何 `validate`，以及**怎么用范例而不被带偏**。

## 时间线是怎么来的

所有变更追加写入 `state/timeline.jsonl`（只追加的事件日志，每个事件带变更后的完整对象）。回放某个时刻 = 把事件折叠到该点。
自动记录：新增/修改/删除定义、任务状态变化与节流后的进度（同一任务每 60 秒最多一条，`SB_EVENT_GAP` 可调）、节点更新、里程碑。手工改 JSON 定义也会被服务补记。

## 安全说明

- 默认只监听 127.0.0.1，并校验 `Host`（防 DNS rebinding），非本机访问需 `SB_ALLOWED_HOSTS=name1,name2`。
- 写接口要求 `Content-Type: application/json`，带 `Origin` 时必须与 `Host` 一致（防跨站）；设置了 `SB_TOKEN` 则需 `Authorization: Bearer`。对外监听且未设 `SB_TOKEN` 时写接口整体关闭，页面也不返回数据真实路径。
- 写入字段白名单 + 长度/类型/数量校验；定义文件限制嵌套深度与大小。
- 资源目录（`/assets/`）只服务白名单扩展名的图片/音视频，解析真实路径后必须仍在资源目录内（防 `..`、软链逃逸）；`.jsonl` 只能通过 `samples` 块或成果校验读取，且校验集的 `source` 在**定义阶段**就会拒绝越界路径和软链。
- 成果校验的规则不支持正则（避免 ReDoS）；单次最多写 500 条标记；单行超过 200KB 跳过；扫描有行数与耗时上限。
- 页面所有文本经 `textContent` 渲染，不使用 `innerHTML`；令牌只走环境变量。

## 目录

```
server.py    HTTP 服务 + 状态汇总 + 时间线回放     sb.py     上报库 / 命令行
sbstore.py   存储层：校验、原子写、事件日志、体检   static/   前端（ES 模块：util graph views review pages timeline app）
review.py    成果校验：规则引擎、后台扫描、分页读取
state/       真实数据：board.json pipelines/ datasets/ modules/ reviews/ tasks/ marks/ assets/ timeline.jsonl（新 clone 为空）
examples/    范例（虚构内容）+ play.py 播放器；播放结果写在 examples/.play/（已忽略）
skills/status-board/              给 AI 的使用说明（.codebuddy/skills/ 下有软链）
```
