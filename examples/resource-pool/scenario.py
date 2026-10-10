"""范例场景：资源池（虚构数据）。

演示的手法：
  - 定义里只登记「是什么、怎么取、额度多少」；已用额度、占用情况用 sb.resource 持续上报
  - 密钥只有来源（env / file / vault）：本范例的环境变量故意没设置，所以页面上会显示「未设置」告警——这是在演示告警，不是缺陷
  - 分配（allocations）整体替换：每次上报完整的占用清单，释放就是上报时去掉那一项
  - 状态覆盖：维护 / 停用用 state，不用改定义
"""


def run(sb, pause):
    sb.milestone("资源池登记完成", note="登记 API Key、GPU、CPU、磁盘与推理网关", tags=["res"])
    # 先建两个小任务：资源里的 used_by / allocations 可以链接到它们（任务不存在时体检会提示）
    sb.task("pool-label", name="批量标注", status="running", done=3, total=10, unit="批", message="调用主力大模型标注")
    sb.task("pool-train", name="微调 v2", kind="train", status="running", done=1, total=10, unit="epoch", model={"name": "示例模型"},
            datasets=[{"name": "示例训练集", "path": "/example/work/train.jsonl"}])
    for used in (400_000, 1_250_000, 2_600_000, 3_900_000, 4_700_000):
        sb.resource("pool", "llm_main", used=used, note="标注批次进行中")
        pause(0.2)
    sb.resource("pool", "llm_backup", used=120_000)
    sb.resource("pool", "search_key", used=18_600)
    sb.milestone("主力 Key 额度接近上限", note="本月已用 94%，需要申请追加或切到备用 Key", tags=["res"])

    sb.resource("pool", "gpu_train", allocations=[{"label": "微调 v2", "task": "pool-train", "amount": 4, "unit": "卡"}])
    pause(0.2)
    sb.resource("pool", "gpu_train", allocations=[{"label": "微调 v2", "task": "pool-train", "amount": 4}, {"label": "批量推理", "amount": 3}])
    pause(0.2)
    sb.resource("pool", "gpu_train", allocations=[{"label": "微调 v2", "task": "pool-train", "amount": 4}, {"label": "批量推理", "amount": 3},
                                                   {"label": "临时实验", "amount": 2, "note": "忘记释放了？"}])      # 总量 9 > 8：超额告警
    sb.resource("pool", "gpu_infer", allocations=[{"label": "在线评测", "amount": 2, "until": "2020-01-01", "note": "到期未释放"}])   # 过期分配告警
    sb.resource("pool", "cpu_etl", allocations=[{"label": "清洗任务", "amount": 96}])
    for used in (8 * 10**12, 14 * 10**12, 17 * 10**12, 18_600_000_000_000):
        sb.resource("pool", "disk_work", used=used)
        pause(0.2)
    sb.resource("pool", "disk_backup", used=9 * 10**12)
    sb.resource("pool", "gpu_infer", state="maintenance", note="周五晚做驱动升级")
    sb.milestone("工作盘接近写满", note="已用 93%，需要清理中间产物", tags=["res"])
