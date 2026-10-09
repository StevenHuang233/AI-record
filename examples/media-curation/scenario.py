"""范例场景：并行支线上的要素追踪（虚构数据）。

建模技巧：一个要素同一时刻只能在一个步骤上，而「画面」「声音」两条支线是并行的。
做法是把「素材 × 支线」各当作一个要素（如 v001/画面、v001/声音），汇合后再用素材本身（v001）继续往下走。
矩阵里不在该支线上的格子会显示「不经过」，这是正确的，不是漏记。
"""


def run(sb, pause):
    ds = [{"name": "原始视频", "ref": "media/raw_video", "role": "输入", "count": 5200}]
    sb.task("media-b1", kind="run", name="首批 4 个素材", pipeline="media", status="running", datasets=ds)
    vids = ["v001", "v002", "v003", "v004"]
    for v in vids:
        sb.item("media-b1", v, step="ingest", status="pending", name=f"素材 {v}")

    for v in vids:                                    # 入库 + 切分
        for st in ("ingest", "split"):
            sb.item("media-b1", v, step=st, status="running")
            pause(0.2)
            sb.item("media-b1", v, step=st, status="done")
    sb.milestone("切分完成", note="4 个素材切出 36 个片段", tags=["b1"])

    for v in vids:                                    # 两条支线：各自成为一个要素
        sb.item("media-b1", f"{v}/画面", step="caption", status="running", name=f"{v} 画面支线")
        sb.item("media-b1", f"{v}/声音", step="asr", status="running", name=f"{v} 声音支线")
    pause(0.5)
    for v in vids:
        sb.item("media-b1", f"{v}/画面", step="caption", status="done")
        if v == "v003":
            sb.item("media-b1", f"{v}/声音", step="asr", status="skipped", note="无人声，跳过转写")
        else:
            sb.item("media-b1", f"{v}/声音", step="asr", status="done")
        pause(0.25)

    for v in vids:                                    # 汇合后用素材本身继续走
        step_status = "failed" if v == "v004" else "done"
        sb.item("media-b1", v, step="align", status="running")
        pause(0.25)
        if v == "v003":
            sb.item("media-b1", v, step="align", status="failed", note="缺声音支线，无法对齐")
            continue
        sb.item("media-b1", v, step="filter", status="running")
        pause(0.25)
        sb.item("media-b1", v, step="filter", status=step_status, note="清晰度 0.42 < 0.6，被拒" if v == "v004" else None)
    sb.task("media-b1", status="done")
    sb.milestone("首批筛选完成", note="通过 2 / 4；被拒原因已记录，准备试验更宽松的阈值", tags=["b1"])
