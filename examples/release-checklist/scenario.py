"""范例场景：没有数据图的纯步骤流程（虚构数据）。

演示的上报手法：
  - sb.node 手工置位 Pipeline 节点（没有关联任务的步骤）
  - 有关联任务的步骤（灰度、全量）状态由任务自动汇总，不需要再手工置位
  - 「等待人工」的关卡：节点置 running + note 写清楚在等谁
"""


def run(sb, pause):
    n = lambda nid, **kw: sb.node("pipelines", "release", nid, **kw)  # noqa: E731
    sb.milestone("1.8.0 开始发布", tags=["1.8.0"])

    n("build", status="running")
    pause(0.4)
    n("build", status="done", note="提交 a1b2c3d 构建成功")
    n("test", status="running")
    pause(0.4)
    n("test", status="done", note="单测 1243 通过 / 集成 86 通过；1 个偶发失败重试后通过")
    n("stage", status="running")
    pause(0.4)
    n("stage", status="done")

    sb.task("release-canary", name="灰度观察 30 分钟", pipeline="release", node="canary", status="running",
            total=30, done=0, unit="分钟", metrics={"错误率%": 0.12, "P95ms": 182})
    for m in range(0, 31, 6):
        sb.task("release-canary", done=m)
        pause(0.25)
    sb.task("release-canary", status="done")

    n("approve", status="running", note="等待负责人审批灰度指标")
    pause(0.8)
    n("approve", status="done", note="负责人确认：错误率与延迟均优于旧版，批准全量")

    sb.task("release-rollout", name="分批全量", pipeline="release", node="rollout", status="running",
            shards=[{"name": "20%", "done": 0, "total": 1}, {"name": "50%", "done": 0, "total": 1}, {"name": "100%", "done": 0, "total": 1}])
    for k, name in enumerate(("20%", "50%", "100%")):
        sb.task("release-rollout", shards=[{"name": s, "done": 1 if i <= k else 0, "total": 1, "status": "done" if i <= k else "queued"}
                                           for i, s in enumerate(("20%", "50%", "100%"))])
        pause(0.4)
    sb.task("release-rollout", status="done")
    sb.milestone("1.8.0 发布完成", note="全量完成，旧版本保留 24 小时用于回滚", tags=["1.8.0"])
