"""范例场景：成果校验（虚构数据）。

演示的手法：
  - 成果文件（.jsonl）放进资源目录；用 sb.define("reviews", …) 声明字段与规则
  - 人工标记：合格 / 不合格 / 存疑 + 备注；同一行可以改判、清除
  - 校验集独立存在，不依赖 Pipeline；只用 from.dataset 指向数据图做溯源
  - 校验完成后用里程碑记录结论，让时间线上能回看「什么时候校验过、结论是什么」
"""


def run(sb, pause):
    sb.milestone("清洗规则 v3 试跑完成", note="开始抽检清洗结果", tags=["gal"])

    # 清洗对比：先把规则抓出来的明显问题标不合格，再抽几条正常的标合格
    for ln, note in ((11, "清洗过头：只剩半截"), (22, "清洗过头"), (33, "清洗过头"), (44, "清洗过头"), (55, "清洗过头")):
        sb.mark("gal_clean", ln, "fail", note, ["过度清洗"])
        pause(0.15)
    for ln in (1, 2, 3, 4, 5, 6, 7):
        sb.mark("gal_clean", ln, "pass")
        pause(0.1)
    sb.mark("gal_clean", 8, "doubt", "原文就是空的，算不算合格？")
    sb.mark("gal_clean", 8, "pass", "确认：原文为空，清洗后保持空即可")          # 改判：存疑 → 合格
    sb.mark("gal_clean", 9, "fail")
    sb.mark("gal_clean", 9, None)                                              # 标错了，清除
    sb.milestone("清洗结果抽检结论", note="规则 v3 对短句有过度清洗，已记录 5 条样例，调整后重跑", tags=["gal"])

    # 模型回答：规则抓出的问题基本都不合格；其余按回答是否正确判断
    for ln, v, note in ((7, "fail", "拒答"), (9, "fail", "空回答"), (13, "fail", "答错且很慢"), (14, "fail", "拒答"), (18, "fail", "空回答"),
                        (1, "pass", ""), (2, "pass", ""), (3, "pass", ""), (4, "doubt", "回答与参考不完全一致，语义接近"), (6, "pass", "")):
        sb.mark("gal_answers", ln, v, note)
        pause(0.12)
    sb.milestone("模型回答抽检完成", note="拒答与空回答占主要问题，下一步检查提示词", tags=["gal"])
