"""范例场景：同一个东西的多个版本（虚构数据）。

演示的手法：
  - 数据、Pipeline、展示模块、校验集、任务、资源：都用同一对字段 family（同一个东西写同一个值）+ version（版本号字符串）
  - 页面上每个地方都一样：最新版照常显示，历史版本收在「▸ 历史版本 N 个」后面，默认收起，点开才展开
  - 谁是最新由服务端按版本号里的数字段算（v1.10 > v1.9），与书写顺序无关；被取代的历史版本不会让页面报「异常」
  - 任务：历史版本若还在跑 / 排队 / 疑似停滞，会自动展开，不会被收起来看不见
"""


def run(sb, pause):
    sb.milestone("流程改版到 v2.0", note="新增质检环节；旧版 v1.0 保留对照", tags=["ver"])

    # 训练任务：v1.0 早已完成，v2.0 正在跑（family 相同 → 任务列表里只显示 v2.0，v1.0 收起）
    sb.task("ver-train-v1", kind="train", name="微调", family="train-main", version="v1.0", status="done", done=100, total=100, unit="step",
            model={"name": "示例模型"}, message="第一版微调，已被 v2.0 取代",
            datasets=[{"name": "清洗后数据 v1.9", "ref": "vd/clean_v19", "role": "训练集", "count": 78000, "used": 78000}])
    pause(0.3)
    sb.task("ver-train-v2", kind="train", name="微调", family="train-main", version="v2.0", status="running", done=30, total=100, unit="step",
            model={"name": "示例模型"}, message="第二版微调：用了清洗后数据 v2.0",
            datasets=[{"name": "清洗后数据 v2.0", "ref": "vd/clean_v2", "role": "训练集", "count": 69999, "used": 20000}])
    for step in (40, 55, 70):
        sb.task("ver-train-v2", done=step)
        pause(0.2)

    # 评测任务：v0.9 还在跑（历史版本在跑 → 自动展开，不会被收起来看不见）
    sb.task("ver-eval-a", kind="eval", name="评测", family="eval-main", version="v1.0", status="running", done=10, total=100, unit="题", model={"name": "示例模型"},
            datasets=[{"name": "评测集", "path": "/example/versions/bench.jsonl", "role": "评测集", "count": 100}])
    sb.task("ver-eval-b", kind="eval", name="评测", family="eval-main", version="v2.0", status="done", done=100, total=100, unit="题", model={"name": "示例模型"},
            datasets=[{"name": "评测集", "path": "/example/versions/bench.jsonl", "role": "评测集", "count": 100}], scores={"accuracy": 0.81})
    sb.milestone("两版训练 / 评测都登记完成", note="历史版本不会进「需要关注」", tags=["ver"])
