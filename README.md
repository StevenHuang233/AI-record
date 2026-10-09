# status_board

通用、可移植的状态看板：Pipeline 结构图（带中文说明）、数据管理图、任务进度条。
只依赖 Python 3.8+ 标准库，前端无 CDN，整个目录拷走即可用。

```bash
python3 server.py                  # http://127.0.0.1:8765
python3 server.py --host 0.0.0.0 --port 8765    # 对外监听，必须先 export SB_TOKEN=xxx，否则写接口关闭
```

状态全部存放在 `state/`（可用 `SB_STATE_DIR` 或 `--state-dir` 更换），看板每 2.5 秒轮询刷新。

## 1. 上报任务进度

```python
import sb                                   # 把本目录加入 PYTHONPATH，或直接拷贝 sb.py
sb.task("train", name="模型训练", pipeline="demo", node="train",
        status="running", done=300, total=1000, unit="step", message="loss=0.52",
        metrics={"loss": 0.52}, stale_after=600)

for x in sb.track(items, "etl", name="数据清洗", pipeline="demo", node="clean"):   # 自动上报并收尾
    ...
```

```bash
python3 sb.py task etl --name 清洗 --done 10 --total 100 --pipeline demo --node clean
python3 sb.py task etl --status done
python3 sb.py rm-task etl
```

- 没有 `total` 时可传 `progress`(0~1)；两者都没有且 running 时显示不定长进度条。
- 多分片：`shards=[{"name":"s0","done":3,"total":10}, ...]`，总进度自动汇总。
- `status`：`queued/running/done/failed/cancelled`。running 任务超过 `stale_after` 秒（默认 300）没更新，显示"疑似停滞"。
- 任务带 `pipeline` + `node` 后，对应 Pipeline 节点的状态和进度自动由任务汇总。
- 上报失败只打印告警，不会让业务脚本中断（`track` 内部）。

远程机器上报：设 `SB_URL=http://host:8765`（可选 `SB_TOKEN`），同样的代码自动改走 HTTP。

## 2. 定义 Pipeline 结构图 `state/pipelines/<id>.json`

```json
{
  "title": "标题", "description": "整体中文说明",
  "nodes": [{"id": "clean", "name": "清洗", "kind": "process", "desc": "步骤的中文说明"}],
  "edges": [{"from": "collect", "to": "clean", "label": "可选"}]
}
```

`kind`：`source/process/model/train/eval/output/gate`（其他值原样显示）。节点自动分层布局并编号，下方给出"步骤说明"列表。
手动改节点：`python3 sb.py node pipelines demo clean --status done --note "..."`（有关联任务时以任务为准）。

## 3. 定义数据管理图 `state/datasets/<id>.json`

```json
{
  "title": "...", "description": "...",
  "nodes": [{"id": "raw", "name": "原始语料", "kind": "raw", "desc": "...", "status": "ready",
             "meta": {"storage": "共享盘A", "size_bytes": 123, "count": 69999, "count_unit": "条",
                      "format": "jsonl", "version": "v1", "updated": "2026-10-01", "owner": "me",
                      "path": "/abs/path", "auto_stat": true}}],
  "edges": [{"from": "raw", "to": "clean", "label": "清洗"}]
}
```

- `kind`：`raw/processed/derived/external/model`；`status`：`ready/building/stale/missing/failed`。
- `auto_stat: true` + `path`（绝对路径）：服务后台线程统计目录/文件大小，结果缓存 5 分钟（`SB_STAT_TTL`），
  单次最长 30 秒（`SB_STAT_BUDGET`，超时标注"仅为下限"）；路径不存在则标为"缺失"。不跟随软链。
- 页面会汇总总量、按 `storage` 的占比条和资产清单。
- 动态更新元数据：`python3 sb.py node datasets demo clean --status ready --meta count=70000`（`path`/`auto_stat` 不可经接口修改）。

## 4. 安全说明

- 默认只监听 127.0.0.1，并校验 `Host` 头（防 DNS rebinding），非本机访问需 `SB_ALLOWED_HOSTS=name1,name2`。
- 写接口要求 `Content-Type: application/json`，带 `Origin` 时必须与 `Host` 一致（防跨站）；设置了 `SB_TOKEN` 则需 `Authorization: Bearer`。
- 对外监听且未设 `SB_TOKEN` 时写接口整体关闭；此时页面不返回数据节点的 `path`。令牌只走环境变量，不要写进文件。
- 写入字段白名单 + 长度/类型校验；所有页面文本经 `textContent` 渲染，不用 `innerHTML`。

## 5. 目录

```
server.py   HTTP 服务 + 状态汇总      sb.py   读写层 / 命令行 / 脚本库
static/     index.html app.js style.css
state/      board.json  pipelines/  datasets/  tasks/   （tasks 由上报自动生成）
```
