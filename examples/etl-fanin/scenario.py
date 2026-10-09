"""范例场景：多源并行抽取（job + 分片）→ 汇入 → 质检失败 → 调阈值 → 重跑通过（虚构数据）。

演示的上报手法：
  - 并行的普通任务（kind 省略 = job），各自挂到不同节点上
  - shards：一个任务内的多个分片各自有进度
  - 失败要素 + 备注，之后用里程碑记录「做了什么修复」，再把要素置为完成
"""


def run(sb, pause):
    sb.milestone("ETL 流程定义完成", note="三个来源并行抽取，汇入后质检再分流", tags=["v1"])

    # 三路抽取并行：每轮同时推进三个任务
    sb.task("etl-ex-orders", name="抽取订单", pipeline="etl", node="ex_orders", status="running", total=3200000, done=0, unit="行")
    sb.task("etl-ex-web", name="抽取访问日志", pipeline="etl", node="ex_web", status="running",
            shards=[{"name": f"{a:02d}-{a + 5:02d}时", "done": 0, "total": 6, "status": "running"} for a in (0, 6, 12, 18)])
    sb.task("etl-ex-partner", name="抽取合作方文件", pipeline="etl", node="ex_partner", status="running", total=14, done=0, unit="个文件")
    for k in range(1, 6):
        sb.task("etl-ex-orders", done=3200000 * k // 5)
        sb.task("etl-ex-web", shards=[{"name": f"{a:02d}-{a + 5:02d}时", "done": min(6, k + (a // 6)), "total": 6,
                                       "status": "done" if min(6, k + (a // 6)) >= 6 else "running"} for a in (0, 6, 12, 18)])
        sb.task("etl-ex-partner", done=min(14, 3 * k))
        pause(0.35)
    sb.task("etl-ex-orders", status="done")
    sb.task("etl-ex-web", status="done")
    sb.task("etl-ex-partner", status="done")

    # 三个日分区依次走 合并 → 质检 → 写入 → 日报
    ds = [{"name": "合并宽表", "ref": "etl/merged", "role": "输入"}]
    sb.task("etl-run-0930", kind="run", name="9 月 28–30 日分区", pipeline="etl", status="running", datasets=ds)
    days = ["0928", "0929", "0930"]
    for d in days:
        sb.item("etl-run-0930", d, step="merge", status="pending", name=f"{d[:2]}月{d[2:]}日分区")
    for d in days:
        sb.item("etl-run-0930", d, step="merge", status="running")
        pause(0.3)
        sb.item("etl-run-0930", d, step="dq_gate", status="running")
        pause(0.3)
        if d == "0929":
            sb.item("etl-run-0930", d, step="dq_gate", status="failed", note="空值率 8.1% 超过阈值 5%")
            continue
        sb.item("etl-run-0930", d, step="load_wh", status="running")
        pause(0.3)
        sb.item("etl-run-0930", d, step="report", status="done")

    sb.milestone("质检阈值与空值规则调整", note="合作方渠道码缺失时按「未知渠道」补全，不再计入空值", tags=["v1.1"])
    for step in ("dq_gate", "load_wh"):
        sb.item("etl-run-0930", "0929", step=step, status="running", note="规则调整后重跑")
        pause(0.35)
    sb.item("etl-run-0930", "0929", step="report", status="done", note="重跑通过")
    sb.task("etl-run-0930", status="done")
