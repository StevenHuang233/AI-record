"""范例场景：训练 + 评测 + 流水线执行（虚构数据）。

演示的上报手法：
  - kind=run 用 sb.item 逐个要素上报（要素少时）
  - kind=run 用 step_counts 汇总上报（要素很多时）
  - kind=train：model / hparams / datasets(含 used 用量) / 曲线 sb.point
  - kind=eval：scores
  - 里程碑
"""
import math
import random


def run(sb, pause):
    random.seed(1)
    sb.milestone("v1 流水线上线", note="首次定义 Pipeline 与数据图", tags=["v1"])

    # ── 1) 少量要素：逐个上报 ────────────────────────────────────────────
    ds = [{"name": "清洗后数据", "ref": "ml/clean", "role": "输入", "count": 69999}]
    sb.task("run-0930", kind="run", name="2026-09-30 批次", pipeline="ml", status="running", datasets=ds)
    ids = [f"s{i:02d}" for i in range(1, 9)]
    for i in ids:
        sb.item("run-0930", i, step="collect", status="pending", name=f"样本组 {i}")
    for step in ("collect", "clean", "label"):
        for i in ids:
            sb.item("run-0930", i, step=step, status="running")
            pause(0.2)
            if step == "label" and i == "s06":      # 失败一次，重试后恢复：矩阵里能看到状态变化
                sb.item("run-0930", i, step=step, status="failed", note="第 1 次调用超时")
                pause(0.3)
                sb.item("run-0930", i, step=step, status="running", note="重试中")
                pause(0.2)
                sb.item("run-0930", i, step=step, status="done", note="第 2 次重试成功")
            else:
                sb.item("run-0930", i, step=step, status="done")
        pause(0.2)
    sb.task("run-0930", status="done")

    # ── 2) 大量要素：只上报每个步骤上的汇总数（每次传完整快照）────────────
    sb.task("run-1001", kind="run", name="2026-10-01 全量批次", pipeline="ml", status="running", datasets=ds, unit="条")
    z = lambda **k: {"running": 0, "done": 0, "failed": 0, "pending": 0, **k}  # noqa: E731
    for snap in (          # 每个快照里各步骤的数量加起来等于总要素数 40000
        {"collect": z(running=8000, done=12000, pending=20000)},
        {"clean": z(running=6000, done=18000, failed=120, pending=15880)},
        {"clean": z(running=120), "label": z(running=2500, done=21000, pending=16380)},   # 失败的 120 条重试中
        {"label": z(done=40000)},
    ):
        sb.task("run-1001", step_counts=snap)
        pause(0.5)
    sb.task("run-1001", status="done")

    sb.milestone("v2 清洗规则升级", note="加入近似去重，保留率约 58%", tags=["v2"])

    # ── 3) 训练：曲线 + 数据用量 ─────────────────────────────────────────
    labels = lambda used: [{"name": "模型标注结果", "ref": "ml/labels", "role": "训练集", "count": 40000, "used": used}]  # noqa: E731
    sb.task("train-v2", kind="train", name="微调 v2", pipeline="ml", node="train", status="running",
            done=0, total=1000, unit="step", stale_after=600,
            model={"name": "base-7b", "type": "全参微调"}, hparams={"lr": 2e-5, "batch": 64, "epochs": 3},
            datasets=labels(0))
    for step in range(0, 1001, 100):
        loss = 2.4 * math.exp(-step / 380) + 0.35 + random.uniform(-0.03, 0.03)
        sb.task("train-v2", done=step, message=f"step {step}", metrics={"loss": round(loss, 3)}, datasets=labels(min(40000, step * 40)))
        sb.point("train-v2", "loss", step, loss)
        sb.point("train-v2", "eval_loss", step, loss + 0.12)
        pause(0.3)
    sb.task("train-v2", status="done")

    # ── 4) 评测：评测集 + 分数 ───────────────────────────────────────────
    sb.task("eval-v2", kind="eval", name="评测 v2", pipeline="ml", node="eval", status="running", done=0, total=600, unit="题",
            model={"name": "base-7b + v2 权重"},
            datasets=[{"name": "评测集", "ref": "ml/bench", "role": "评测集", "count": 15000, "used": 600}])
    for n in range(0, 601, 120):
        sb.task("eval-v2", done=n, message=f"已评 {n} 题")
        pause(0.3)
    sb.task("eval-v2", status="done", scores={"accuracy": 0.583, "作答率": 0.92}, metrics={"报错": 0})
    sb.milestone("v2 评测完成", note="准确率相对 v1 提升 4.1 个点", tags=["v2"])
