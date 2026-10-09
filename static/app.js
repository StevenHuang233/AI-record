"use strict";
/* 入口：取状态 → 生成离线 DOM → patch 进页面。实时模式每 2.5 秒轮询；时间线模式取历史时刻的状态。 */
import { h, patch, onAction, fmtTime } from "./util.js";
import { S } from "./store.js";
import { taskSection, pipelineCard, datasetCard, moduleCard } from "./views.js";
import { TL, mount, setEvents } from "./timeline.js";

const POLL_MS = 2500;
let seq = 0;

function render() {
  const st = S.state;
  if (!st) return;
  document.title = st.title;
  const sm = st.summary, bk = st.by_kind;
  const chip = (label, n, cls) => h("span", { class: `chip st-${cls}`, "data-key": "chip:" + label }, h("b", {}, n), label);
  const tasksOf = (k) => st.tasks.filter((t) => (t.kind || "job") === k);
  const hist = !st.live;

  const header = h("header", { class: "top", "data-key": "top" },
    h("div", { class: "top-row" }, h("h1", {}, st.title), st.subtitle ? h("span", { class: "subtitle" }, st.subtitle) : null, h("span", { class: "spacer" }),
      h("span", { class: "live" }, h("span", { class: `dot ${S.offline ? "off" : hist ? "hist" : ""}` }),
        S.offline ? "连接中断，显示的是旧数据" : hist ? `历史回放 · ${fmtTime(st.now)}` : `实时 · 更新于 ${fmtTime(S.lastOk / 1000)}`),
      h("nav", {}, [["训练", "t-train"], ["流水线执行", "t-run"], ["评测", "t-eval"], ["Pipeline", "pipelines"], ["数据管理", "datasets"], ["定制模块", "modules"]]
        .map(([l, id]) => h("a", { href: "#" + id, "data-key": "nav:" + id }, l)))),
    h("div", { class: "chips" }, chip("运行中", sm.running, "running"), chip("排队", sm.queued, "queued"), chip("已完成", sm.done, "done"), chip("失败", sm.failed, "failed"), chip("疑似停滞", sm.stale, "stale"),
      chip("训练", bk.train, "plain"), chip("流水线执行", bk.run, "plain"), chip("评测", bk.eval, "plain")));

  const emptyHint = (txt, code) => h("div", { class: "empty" }, txt, " ", h("code", {}, code));
  const secPipes = h("section", { id: "pipelines", "data-key": "sec:pipelines" }, h("h2", {}, "Pipeline 结构图", h("small", {}, "每个节点上标注了这一步的作用；点击节点看输入/输出/读写的数据/进度")),
    st.pipelines.length ? st.pipelines.map(pipelineCard) : emptyHint("暂无 Pipeline。用 sb.define 写入：", "python3 sb.py define pipelines <id> file.json"));
  const secData = h("section", { id: "datasets", "data-key": "sec:datasets" }, h("h2", {}, "数据管理", h("small", {}, "记录每份数据的存放地址（含备份）、含义、来源、用处与被处理情况")),
    st.datasets.length ? st.datasets.map(datasetCard) : emptyHint("暂无数据图。用 sb.define 写入：", "python3 sb.py define datasets <id> file.json"));
  const secMods = h("section", { id: "modules", "data-key": "sec:modules" }, h("h2", {}, "定制模块", h("small", {}, "数据集展示、效果展示等自定义内容")),
    st.modules.length ? st.modules.map(moduleCard) : emptyHint("暂无定制模块。用 sb.define 写入：", "python3 sb.py define modules <id> file.json"));

  const root = h("div", { id: "app", "data-key": "app" },
    h("div", { class: "wrap", "data-key": "wrap" }, header, S.offline ? h("div", { class: "banner", "data-key": "banner" }, "无法连接看板服务，正在重试…") : null,
      hist ? h("div", { class: "banner hist", "data-key": "histbanner" }, `正在回放历史（事件 #${st.at}）：${st.event ? st.event.label : ""}`) : null,
      h("div", { class: "group-title", "data-key": "gt" }, "进程记录"),
      taskSection("train", tasksOf("train"), "模型、使用的数据、训练曲线"),
      taskSection("run", tasksOf("run"), "每个数据要素走到了哪一步"),
      taskSection("eval", tasksOf("eval"), "评测集及地址、模型、进度与结果"),
      tasksOf("job").length ? taskSection("job", tasksOf("job")) : null,
      secPipes, secData, secMods));
  patch(document.getElementById("app"), root);
}

export async function load() {
  const my = ++seq;
  const url = TL.idx == null ? "/api/state" : `/api/state?at=${TL.idx}`;
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    if (my !== seq) return;          // 已有更新的请求，丢弃过期响应
    S.state = data; S.offline = false; S.lastOk = Date.now();
  } catch (e) {
    if (my !== seq) return;
    S.offline = true;
  }
  render();
}

async function pullTimeline() {
  try {
    const r = await fetch(`/api/timeline?since=${TL.events.length ? TL.events[TL.events.length - 1].i + 1 : 0}`, { cache: "no-store" });
    if (!r.ok) return;
    const d = await r.json();
    if (d.events.length || d.n !== TL.n || d.reset) setEvents(d.events, d.n, d.reset);
  } catch (e) { /* 忽略，下个周期重试 */ }
}

onAction("selnode", (key, id) => { S.sel[key] = id; render(); });
onAction("goref", (kind, ref) => {
  const [gid, nid] = ref.includes("/") ? [ref.slice(0, ref.indexOf("/")), ref.slice(ref.indexOf("/") + 1)] : [ref, null];
  if (nid) S.sel[`${kind}/${gid}`] = nid;
  render();
  const el = document.getElementById((kind === "pipelines" ? "pl-" : "ds-") + gid);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
});

async function loop() {
  await pullTimeline();
  if (TL.idx == null && !TL.playing) await load();
  setTimeout(loop, document.hidden ? POLL_MS * 4 : POLL_MS);
}

mount(document.getElementById("timeline"), { load });
load().then(loop);
