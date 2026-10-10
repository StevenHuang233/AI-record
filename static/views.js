"use strict";
/* 视图：把服务端状态渲染成离线 DOM（由 util.patch 增量合并进页面）。所有文本经 textContent 写入。 */
import { h, s, bar, badge, clip, fmtBytes, fmtDur, fmtNum, fmtPct, fmtTime, assetUrl, foldBtn, foldBody, isFolded,
         TASK_ST, NODE_ST, ITEM_ST, TASK_KIND, PIPE_KIND, DATA_KIND, LOC_ROLE, REPLICA, META_LABEL, META_HIDE } from "./util.js";
import { S, resolveRef, findGraph } from "./store.js";
import { drawGraph, orderedNodes, ancestors, nodeSize } from "./graph.js";
import { heatmapBlock, flowBlock } from "./transition.js";

const COLORS = ["#2f6f9f", "#4f8a4b", "#c28a1e", "#8a5a9e", "#b94a3a", "#3f8f8f"];
const asArr = (x) => (Array.isArray(x) ? x : x == null || x === "" ? [] : [x]);

/* ── 通用小部件 ───────────────────────────────────────────────────────────── */
const tag = (txt, cls = "") => h("span", { class: `tag ${cls}` }, txt);
const kvSpan = (k, v) => h("span", {}, k + " ", h("b", {}, v));
/* 来自 examples/ 的范例内容（定义里带 _example 标记）：页面上显式标出，避免被当成真实数据 */
export const exTag = (g) => (g && g._example ? h("span", { class: "tag ex", title: "来自 examples/ 的范例，内容是虚构的，请勿照抄" }, "范例") : null);
const empty = (...kids) => h("div", { class: "empty" }, kids);
const noteLine = (txt) => h("div", { class: "warn" }, txt);

/* 子页面链接：kind = pipelines|datasets|task，ref = "图id" 或 "图id/节点id" */
export const hrefOf = (kind, ref) => `#/${kind}/${String(ref).split("/").map(encodeURIComponent).join("/")}`;
function link(kind, ref, label) {
  return h("a", { class: "xlink", href: hrefOf(kind, ref) }, label);
}

function field(label, content) {
  if (content == null || content === "" || (Array.isArray(content) && !content.length)) return null;
  return h("div", { class: "fld" }, h("div", { class: "fk" }, label), h("div", { class: "fv" }, content));
}

function legend(map, keys) {
  return h("div", { class: "legend" }, keys.map((k) => h("span", { class: `st-${k}` }, h("i"), map[k])));
}

function mediaEl(src, type) {
  const url = assetUrl(src);
  const t = type || (/\.(mp4|webm)$/i.test(src) ? "video" : /\.(mp3|wav|ogg|m4a)$/i.test(src) ? "audio" : "image");
  if (t === "video") return h("video", { src: url, controls: true, preload: "metadata", class: "media" });
  if (t === "audio") return h("audio", { src: url, controls: true, preload: "none", class: "media" });
  return h("img", { src: url, loading: "lazy", alt: String(src), class: "media" });
}

/* ── 曲线图 ───────────────────────────────────────────────────────────────── */
function lineChart(key, series, o = {}) {
  const names = Object.keys(series || {}).filter((n) => Array.isArray(series[n]) && series[n].length);
  if (!names.length) return null;
  const W = o.w || 460, H = o.h || 170, L = 48, R = 14, T = 12, B = 26;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const n of names) for (const [x, y] of series[n]) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) { y1 += 1; y0 -= 1; }
  const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
  const sx = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const sy = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const yf = (v) => (Math.abs(v) >= 1000 ? v.toExponential(1) : Math.abs(v) >= 100 ? v.toFixed(0) : +v.toPrecision(3));
  const grid = [0, 1, 2, 3].map((i) => {
    const v = y0 + ((y1 - y0) * i) / 3, y = sy(v);
    return s("g", { "data-key": "gl" + i }, s("line", { class: "gridl", x1: L, x2: W - R, y1: Math.round(y), y2: Math.round(y) }),
      s("text", { class: "axt", x: L - 6, y: Math.round(y) + 4, "text-anchor": "end" }, String(yf(v))));
  });
  const paths = names.map((n, i) => {
    const d = series[n].map(([x, y], k) => `${k ? "L" : "M"}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(" ");
    const [lx, ly] = series[n][series[n].length - 1];
    return s("g", { "data-key": "ln:" + n, style: { color: COLORS[i % COLORS.length] } },
      s("path", { class: "line", d, style: { d: `path("${d}")` } }),
      s("circle", { class: "lastpt", r: 3.5, cx: 0, cy: 0, style: { transform: `translate(${sx(lx).toFixed(1)}px, ${sy(ly).toFixed(1)}px)` } }));
  });
  const svg = s("svg", { class: "chart-svg", viewBox: `0 0 ${W} ${H}`, "data-key": "csvg" }, grid, paths,
    s("text", { class: "axt", x: L, y: H - 6 }, String(+x0.toPrecision(5))), s("text", { class: "axt", x: W - R, y: H - 6, "text-anchor": "end" }, String(+x1.toPrecision(5))));
  const lg = h("div", { class: "legend" }, names.map((n, i) => {
    const last = series[n][series[n].length - 1][1];
    return h("span", { "data-key": "lg:" + n }, h("i", { style: { background: COLORS[i % COLORS.length] } }), `${n} 最新 ${yf(last)}`);
  }));
  return h("div", { class: "chart", "data-key": "chart:" + key }, o.title ? h("div", { class: "chart-title" }, o.title) : null, svg, lg);
}

/* ── 任务卡 ───────────────────────────────────────────────────────────────── */
const taskFoldKey = (t) => "task:" + t.id;
const taskClass = (t) => `card task st-${t.status}` + (isFolded(taskFoldKey(t)) ? " folded" : "");

function taskTop(t) {
  const kvs = [];
  if (t.total) kvs.push(kvSpan("进度", `${fmtNum(Math.round(t.done || 0))} / ${fmtNum(t.total)}${t.unit ? " " + t.unit : ""}`));
  kvs.push(kvSpan("已用", fmtDur(t.elapsed)));
  if (t.status === "running") kvs.push(kvSpan("预计剩余", fmtDur(t.eta)));
  if (t.rate) kvs.push(kvSpan("速率", `${t.rate.toFixed(t.rate < 10 ? 2 : 0)}${t.unit ? " " + t.unit : ""}/s`));
  const kind = t.kind || "job", fk = taskFoldKey(t);
  return [
    h("div", { class: "task-head" }, foldBtn(fk), tag(TASK_KIND[kind] || kind, `kind-${kind}`), h("span", { class: "name", title: t.name || t.id }, t.name || t.id),
      t.stale ? h("span", { class: "badge stale" }, "疑似停滞") : null, badge(t.status, TASK_ST)),
    h("div", { class: "pct-row" }, bar(t.progress, t.status, { stale: t.stale }), h("span", { class: "pct" }, fmtPct(t.progress))),   // 收起后仍保留：监督时最需要看的就是进度
    foldBody(fk, [
      h("div", { class: "id" }, t.id, t.pipeline ? [" · 所属 ", link("pipelines", t.pipeline + (t.node ? "/" + t.node : ""), t.pipeline + (t.node ? " / " + t.node : ""))] : null),
      h("div", { class: "kv" }, kvs),
      t.stale ? noteLine(`已 ${fmtDur(t.idle)} 没有收到心跳，进程可能已卡死或退出`) : null,
      t.message ? h("div", { class: "msg" }, t.message) : null]),
  ];
}


function scalarsBox(title, obj, cls = "") {
  const es = Object.entries(obj || {});
  if (!es.length) return null;
  return h("div", { class: "fld" }, h("div", { class: "fk" }, title),
    h("div", { class: `metrics ${cls}` }, es.map(([k, v]) => h("span", { class: "metric", "data-key": "m:" + k }, `${k}: ${typeof v === "number" ? fmtNum(+v.toPrecision(6)) : v}`))));
}

function dataUsage(t, title) {
  const ds = t.datasets || [];
  if (!ds.length) return h("div", { class: "fld" }, h("div", { class: "fk" }, title), noteLine(`未记录${title}。请在任务里填写 datasets（名称 / ref / path）`));
  const rows = ds.map((d, i) => {
    const hit = d.ref ? resolveRef("datasets", d.ref) : null;
    const locs = hit ? hit.n.locations || [] : [];
    const used = d.used != null && d.count ? Math.min(1, d.used / d.count) : null;
    return h("tr", { "data-key": "du:" + i },
      h("td", {}, hit ? link("datasets", d.ref, d.name || hit.n.name) : (d.name || "-"), d.note ? h("div", { class: "id" }, d.note) : null),
      h("td", {}, d.role ? tag(d.role) : "-"),
      h("td", { class: "num" }, d.used != null ? `${fmtNum(d.used)}${d.count ? " / " + fmtNum(d.count) : ""}` : (d.count != null ? fmtNum(d.count) : "-"),
        used != null ? bar(used, "running", { sm: true }) : null),
      h("td", {}, d.path ? h("code", {}, d.path) : locs.length ? locs.map((l, k) => h("div", { "data-key": "p" + k }, tag(l.label || LOC_ROLE[l.role] || "地址"), " ", l.path ? h("code", {}, l.path) : "（路径已隐藏）")) : "-"));
  });
  return h("div", { class: "fld" }, h("div", { class: "fk" }, title),
    h("div", { class: "tbl-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["数据集", "用途", "用量/总量", "地址"].map((c) => h("th", {}, c)))), h("tbody", {}, rows))));
}

function modelBox(t) {
  const m = t.model || {};
  const es = Object.entries(m);
  if (!es.length) return h("div", { class: "fld" }, h("div", { class: "fk" }, "模型"), noteLine("未记录模型（请填写 model）"));
  return h("div", { class: "fld" }, h("div", { class: "fk" }, "模型"),
    h("div", { class: "metrics" }, es.map(([k, v]) => h("span", { class: "metric", "data-key": "mo:" + k }, k === "name" ? `${v}` : `${k}: ${v}`))));
}

function shardsBox(t) {
  return t.shards && t.shards.length ? h("div", { class: "shards" }, t.shards.map((sh, i) =>
    h("div", { class: "shard", "data-key": "sh" + i }, h("span", {}, sh.name || "分片"),
      bar(sh.progress, sh.status || (sh.progress >= 1 ? "done" : t.status), { sm: true }), h("span", {}, fmtPct(sh.progress))))) : null;
}

function trainCard(t) {
  return h("div", { class: taskClass(t), "data-key": "task:" + t.id, "data-fade": 1 },
    taskTop(t), foldBody(taskFoldKey(t), [shardsBox(t),
    h("div", { class: "cols2" },
      h("div", {}, modelBox(t), dataUsage(t, "训练使用的数据"), scalarsBox("超参数", t.hparams), scalarsBox("最新指标", t.metrics)),
      h("div", {}, lineChart("tr-" + t.id, t.series, { title: "训练曲线" }) || h("div", { class: "empty sm" }, "还没有曲线数据（用 sb.point 上报 loss 等）")))]));
}

function evalCard(t) {
  const sc = Object.entries(t.scores || {});
  return h("div", { class: taskClass(t), "data-key": "task:" + t.id, "data-fade": 1 },
    taskTop(t), foldBody(taskFoldKey(t), [shardsBox(t),
    h("div", { class: "cols2" },
      h("div", {}, modelBox(t), dataUsage(t, "评测集（及地址）")),
      h("div", {}, sc.length ? h("div", { class: "fld" }, h("div", { class: "fk" }, "评测结果"),
        h("table", {}, h("tbody", {}, sc.map(([k, v]) => h("tr", { "data-key": "sc:" + k }, h("td", {}, k), h("td", { class: "num" }, h("b", {}, typeof v === "number" ? fmtNum(+v.toPrecision(6)) : String(v))))))))
        : h("div", { class: "empty sm" }, t.status === "running" ? "评测进行中，结果出来后显示在这里" : "没有记录评测结果（scores）"),
      scalarsBox("其他指标", t.metrics), lineChart("ev-" + t.id, t.series, { title: "曲线" })))]));
}

function runCard(t) {
  const g = t.pipeline ? findGraph("pipelines", t.pipeline) : null;
  const items = t.items || [];
  let body;
  if (!g || g.error) body = noteLine(t.pipeline ? `找不到所属 Pipeline「${t.pipeline}」` : "没有关联 pipeline（请填写 pipeline）");
  else {
    const steps = orderedNodes(g);
    const sc = t.step_counts || {};
    const strip = h("div", { class: "steps-strip" }, steps.map((n, i) => {
      const c = sc[n.id] || {};
      const txt = [c.running ? `▶${c.running}` : "", c.done ? `✓${fmtNum(c.done)}` : "", c.failed ? `✗${c.failed}` : "", c.pending ? `…${c.pending}` : ""].filter(Boolean).join(" ");
      return h("a", { class: `ss st-${n.status}`, "data-key": "ss:" + n.id, href: hrefOf("pipelines", g.id + "/" + n.id) },
        h("div", { class: "ss-n" }, i + 1), h("div", { class: "ss-name" }, n.name || n.id), h("div", { class: "ss-role" }, clip(n.role || "", 22)),
        h("div", { class: "ss-c" }, txt || "—"));
    }));
    let matrix = null;
    if (items.length) {
      const anc = ancestors(g);
      const rows = items.slice(0, 60).map((it) => h("tr", { "data-key": "it:" + it.id },
        h("td", { class: "iname", title: it.id }, clip(it.name || it.id, 26)),
        steps.map((n) => {
          let st = "none";
          if (n.id === it.step) st = it.status || "pending";
          else if (it.step && anc.get(it.step)?.has(n.id)) st = "done";
          else if (it.step && anc.get(n.id)?.has(it.step)) st = "pending";
          return h("td", { class: `cell ic-${st}`, "data-key": "c:" + n.id, title: `${n.name || n.id}：${ITEM_ST[st] || "不经过"}` });
        }),
        h("td", { class: "inote" }, clip(it.note || "", 40))));
      matrix = h("div", { class: "fld" }, h("div", { class: "fk" }, `要素 × 步骤（共 ${items.length} 个${items.length > 60 ? "，仅显示前 60" : ""}）`),
        h("div", { class: "tbl-wrap" }, h("table", { class: "matrix" },
          h("thead", {}, h("tr", {}, h("th", {}, "要素"), steps.map((n, i) => h("th", { class: "num", title: n.name || n.id }, String(i + 1))), h("th", {}, "备注"))),
          h("tbody", {}, rows))),
        h("div", { class: "legend" }, ["done", "running", "pending", "failed"].map((k) => h("span", { class: `ic-${k}` }, h("i"), ITEM_ST[k]))));
    }
    body = [h("div", { class: "fld" }, h("div", { class: "fk" }, `各步骤上的数据要素数（所属：${g.title || g.id}）`), strip), matrix];
  }
  return h("div", { class: taskClass(t), "data-key": "task:" + t.id, "data-fade": 1 }, taskTop(t),
    foldBody(taskFoldKey(t), [shardsBox(t), body, dataUsage({ datasets: t.datasets }, "处理的数据"), scalarsBox("指标", t.metrics)]));
}

function jobCard(t) {
  return h("div", { class: taskClass(t), "data-key": "task:" + t.id, "data-fade": 1 }, taskTop(t),
    foldBody(taskFoldKey(t), [shardsBox(t), t.datasets && t.datasets.length ? dataUsage(t, "使用的数据") : null, scalarsBox("指标", t.metrics),
    lineChart("jb-" + t.id, t.series, { title: "曲线", h: 130 })]));
}

export const taskCard = (t) => ({ train: trainCard, eval: evalCard, run: runCard }[t.kind] || jobCard)(t);

/* ── 节点详情 ─────────────────────────────────────────────────────────────── */
function relLinks(kind, g, nid, nav) {
  const nm = new Map(g.nodes.map((n) => [n.id, n]));
  const label = (i, edgeLbl) => (nm.get(i).name || i) + (edgeLbl.get(i) ? `（${edgeLbl.get(i)}）` : "");
  /* nav=true（子页面）→ 跳转到上/下游节点的子页面；否则只在当前图里切换选中节点 */
  const mk = (ids, edgeLbl) => ids.map((i) => (nav
    ? h("a", { class: "xlink", href: hrefOf(kind, `${g.id}/${i}`), "data-key": "rl" + i }, label(i, edgeLbl))
    : h("a", { class: "xlink", "data-act": "selnode", "data-a1": `${kind}/${g.id}`, "data-a2": i, role: "button", tabindex: 0, "data-key": "rl" + i }, label(i, edgeLbl))));
  const upL = new Map(), dnL = new Map();
  const ups = g.edges.filter((e) => e.to === nid).map((e) => { upL.set(e.from, e.label); return e.from; });
  const downs = g.edges.filter((e) => e.from === nid).map((e) => { dnL.set(e.to, e.label); return e.to; });
  return [field("上游", ups.length ? mk(ups, upL) : null), field("下游", downs.length ? mk(downs, dnL) : null)];
}

const refLinks = (kind, refs) => asArr(refs).map((r) => {
  const hit = resolveRef(kind, r);
  return hit ? link(kind, r, hit.n.name || r) : h("span", { class: "tag", title: "引用不存在" }, String(r));
});

export function pipeNodeDetail(g, n, nav) {
  if (!n) return h("div", { class: "panel", "data-key": "detail" }, h("h4", {}, "步骤详情"), h("span", { class: "desc" }, "点击结构图里的节点或右侧列表查看这一步的作用、输入输出和进度。"));
  const tasks = (n.tasks || []).map((id) => S.state.tasks.find((t) => t.id === id)).filter(Boolean);
  const groups = (g.groups || []).filter((x) => x.nodes.includes(n.id)).map((x) => x.name);
  const it = n.items, nk = `pn:${g.id}/${n.id}`;
  return h("div", { class: (nav ? "panel flat" : "panel") + (isFolded(nk) ? " folded" : ""), "data-key": "detail" }, nav ? null : h("h4", {}, "步骤详情"),
    h("div", {}, foldBtn(nk), h("span", { class: "ttl" }, n.name || n.id), badge(n.status, NODE_ST), n.stale ? h("span", { class: "badge stale", style: { marginLeft: "6px" } }, "疑似停滞") : null,
      nav ? null : h("a", { class: "xlink open", href: hrefOf("pipelines", `${g.id}/${n.id}`) }, "打开步骤子页面 →")),
    isFolded(nk) ? (n.progress != null ? h("div", { class: "pct-row" }, bar(n.progress, n.status, { sm: true }), h("span", { class: "pct" }, fmtPct(n.progress))) : null) : [
    field("作用", n.role), field("详细说明", n.desc), field("输入", n.inputs), field("输出", n.outputs),
    field("所属阶段", groups.length ? groups.map((x) => tag(x)) : null),
    field("读取的数据", n.reads ? refLinks("datasets", n.reads) : null), field("写入的数据", n.writes ? refLinks("datasets", n.writes) : null),
    n.note ? field("备注", n.note) : null,
    asArr(n.blocks).map((b, i) => h("div", { class: "blk", "data-key": "nb" + i }, (() => {
      try { return blockEl(b, `${g.id}-${n.id}-${i}`); } catch (e) { return h("div", { class: "err" }, `步骤内的展示块渲染失败: ${e.message}`); }
    })())),
    n.progress != null ? h("div", { class: "pct-row" }, bar(n.progress, n.status, { sm: true }), h("span", { class: "pct" }, fmtPct(n.progress))) : null,
    it ? field("数据要素", `处理中 ${it.running} · 已完成本步 ${it.done} · 失败 ${it.failed} · 排队 ${it.pending} · 已流转到后续步骤 ${it.passed}（总 ${it.total}）`) : null,
    tasks.length ? field("关联任务", tasks.map((t) => h("div", { class: "pct-row", "data-key": "lt:" + t.id },
      h("a", { class: "xlink", href: hrefOf("task", t.id), style: { minWidth: "110px" } }, t.name || t.id), bar(t.progress, t.status, { sm: true, stale: t.stale }), h("span", { class: "pct" }, fmtPct(t.progress))))) : null,
    relLinks("pipelines", g, n.id, nav)]);
}

function processingLines(p) {
  return asArr(p).map((x) => (typeof x === "string" ? x : x && typeof x === "object" ? `${x.step ? x.step + "：" : ""}${x.desc || ""}${x.to ? "（→ " + x.to + "）" : ""}` : String(x)));
}

export function dataNodeDetail(g, n, nav) {
  if (!n) return h("div", { class: "panel", "data-key": "detail" }, h("h4", {}, "数据详情"), h("span", { class: "desc" }, "点击图中的数据资产或右侧清单，查看它的含义、来源、用处、存放地址和被处理情况。"));
  const ref = `${g.id}/${n.id}`, m = n.meta || {};
  const nmap = new Map(g.nodes.map((x) => [x.id, x]));
  const derived = [];
  for (const e of g.edges) {
    if (e.to === n.id) derived.push(`由「${nmap.get(e.from).name || e.from}」${e.label ? "经「" + e.label + "」" : ""}加工得到`);
    if (e.from === n.id) derived.push(`${e.label ? "经「" + e.label + "」" : ""}产出/用于「${nmap.get(e.to).name || e.to}」`);
  }
  for (const pg of S.state.pipelines) {
    if (pg.error) continue;
    for (const pn of pg.nodes) {
      if (asArr(pn.reads).includes(ref)) derived.push(`被 Pipeline「${pg.title || pg.id}」步骤「${pn.name || pn.id}」读取`);
      if (asArr(pn.writes).includes(ref)) derived.push(`由 Pipeline「${pg.title || pg.id}」步骤「${pn.name || pn.id}」写入`);
    }
  }
  const users = S.state.tasks.filter((t) => asArr(t.datasets).some((d) => d.ref === ref));
  const lines = [...processingLines(n.processing), ...derived];
  const locs = n.locations || [];
  const locTable = locs.length ? h("div", { class: "tbl-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["标签", "角色", "存储", "地址", "实测大小", "状态"].map((c) => h("th", {}, c)))),
    h("tbody", {}, locs.map((l, i) => {
      const st = l.stat;
      const sTxt = !st ? (l.verified ? `登记于 ${l.verified}` : "未实测") : st.pending ? "统计中…" : st.missing ? "路径不存在" : st.error ? "失败：" + st.error
        : `${fmtBytes(st.size)} · ${fmtNum(st.files)} 个文件${st.truncated ? "（下限）" : ""}`;
      return h("tr", { "data-key": "loc:" + i }, h("td", {}, l.label || "-"), h("td", {}, tag(LOC_ROLE[l.role] || l.role || "地址", l.role === "primary" ? "kind-train" : "")),
        h("td", {}, l.storage || "-"), h("td", {}, l.path ? h("code", {}, l.path) : "（路径已隐藏）", l.note ? h("div", { class: "id" }, l.note) : null),
        h("td", { class: "num" }, sTxt), h("td", {}, st ? (st.missing ? badge("missing", NODE_ST) : st.pending ? "-" : badge("ready", NODE_ST)) : "-"));
    })))) : noteLine("未记录存放地址（请填写 locations；有备份请写多条）");
  const rows = Object.entries(m).filter(([k, v]) => !META_HIDE.has(k) && v != null && v !== "");
  const dk = `dn:${g.id}/${n.id}`;
  return h("div", { class: (nav ? "panel flat" : "panel") + (isFolded(dk) ? " folded" : ""), "data-key": "detail" }, nav ? null : h("h4", {}, "数据详情"),
    h("div", {}, foldBtn(dk), h("span", { class: "ttl" }, n.name || n.id), badge(n.status, NODE_ST), n.kind ? tag(DATA_KIND[n.kind] || n.kind) : null,
      n.replica ? h("span", { class: `badge rep-${n.replica}`, style: { marginLeft: "6px" } }, REPLICA[n.replica]) : null,
      nav ? null : h("a", { class: "xlink open", href: hrefOf("datasets", `${g.id}/${n.id}`) }, "打开数据子页面 →")),
    n.brief ? h("div", { class: "kv brief", "data-key": "brief" }, n.brief) : null,         // 收起后只剩这一句话：它就是摘要
    isFolded(dk) ? null : [
    field("含义", n.desc), field("来源", n.source), field("用处", n.purpose),
    field("被处理情况", lines.length ? h("ul", { class: "bul" }, lines.map((x, i) => h("li", { "data-key": "pl" + i }, x))) : noteLine("未记录被处理情况（请填写 processing）")),
    h("div", { class: "fld" }, h("div", { class: "fk" }, `存放地址（${locs.length} 个）`), locTable),
    field("被使用", users.length ? users.map((t) => h("div", { "data-key": "us:" + t.id }, tag(TASK_KIND[t.kind] || "任务"), " ", link("task", t.id, t.name || t.id), " ",
      tag(asArr(t.datasets).find((d) => d.ref === ref).role || "用于"), " ", badge(t.status, TASK_ST))) : null),
    n.progress != null ? h("div", { class: "pct-row" }, bar(n.progress, n.status, { sm: true }), h("span", { class: "pct" }, fmtPct(n.progress))) : null,
    rows.length ? h("dl", { class: "meta" }, rows.map(([k, v]) => [h("dt", {}, META_LABEL[k] || k), h("dd", {}, k === "size_bytes" ? fmtBytes(v) : typeof v === "number" ? fmtNum(v) : String(v))])) : null,
    relLinks("datasets", g, n.id, nav)]);
}

/* ── Pipeline / 数据管理 ──────────────────────────────────────────────────── */
export function pipelineCard(g) {
  if (g.error) return h("div", { class: "card err", "data-key": "pl:" + g.id }, `${g.id}: ${g.error}`);
  const kind = "pipelines", key = `${kind}/${g.id}`;
  const sel = S.sel[key] || (g.nodes.find((n) => n.status === "running") || {}).id || null;
  const { el, order } = drawGraph(kind, g, sel, key);
  const doneN = g.nodes.filter((n) => n.status === "done" || n.status === "skipped").length;
  const prog = g.nodes.length ? g.nodes.reduce((a, n) => a + (["done", "skipped"].includes(n.status) ? 1 : n.status === "running" ? (n.progress || 0) : 0), 0) / g.nodes.length : null;
  const st = g.nodes.some((n) => n.status === "failed") ? "failed" : g.nodes.some((n) => n.status === "running") ? "running" : prog === 1 ? "done" : "pending";
  const steps = h("ol", { class: "steps" }, order.map((i, k) => {
    const n = g.nodes[i];
    return h("li", { class: `st-${n.status} ${n.id === sel ? "sel" : ""}`, "data-key": "st:" + n.id, "data-act": "selnode", "data-a1": key, "data-a2": n.id, role: "button", tabindex: 0 },
      h("span", { class: "n" }, k + 1),
      h("div", {}, h("div", { class: "sn" }, n.name || n.id), n.role ? h("div", { class: "sr" }, n.role) : null, n.desc ? h("div", { class: "sd" }, n.desc) : null),
      badge(n.status, NODE_ST));
  }));
  const fk = "pl:" + g.id;
  return h("div", { class: "card" + (isFolded(fk) ? " folded" : ""), id: "pl-" + g.id, "data-key": "pl:" + g.id, "data-fade": 1 },
    h("div", { class: "g-head" }, foldBtn(fk), h("h3", {}, g.title || g.id), exTag(g), badge(st, NODE_ST), h("span", { class: "kv" }, `${doneN} / ${g.nodes.length} 步完成`), bar(prog, st, { sm: true }), h("span", { class: "pct" }, fmtPct(prog))),
    foldBody(fk, [g.description ? h("p", { class: "desc" }, g.description) : null,
    (g.groups || []).some((x) => x.nodes.length) ? h("div", { class: "grp-legend" }, g.groups.filter((x) => x.nodes.length).map((x, i) => h("span", { class: `gl gc-${i % 5}`, "data-key": "gg" + i }, h("b", {}, x.name), x.desc ? "：" + x.desc : ""))) : null,
    el, legend(NODE_ST, ["pending", "running", "done", "failed", "skipped"]),
    h("div", { class: "cols" }, pipeNodeDetail(g, g.nodes.find((n) => n.id === sel)), h("div", { class: "panel", "data-key": "steps" }, h("h4", {}, "各步骤的作用（按流程顺序）"), steps))]));
}

/* 一行数据资产（清单视图和分组模块共用） */
function assetRow(g, n, sel, key, maxSz) {
  const m = n.meta || {}, sz = nodeSize(n);
  return h("tr", { class: `click ${n.id === sel ? "sel" : ""}`, "data-key": "dr:" + n.id, "data-act": "selnode", "data-a1": key, "data-a2": n.id, role: "button", tabindex: 0 },
    h("td", {}, h("b", {}, n.name || n.id), n.brief ? h("div", { class: "id" }, clip(n.brief, 60)) : null),
    h("td", {}, DATA_KIND[n.kind] || n.kind || "-"),
    h("td", {}, (n.locations || []).length ? `${n.locations.length} 个` : "未记录", n.replica ? [" ", h("span", { class: `badge rep-${n.replica}` }, REPLICA[n.replica])] : null),
    h("td", { class: "num" }, sz != null ? [fmtBytes(sz), " ", h("span", { class: "sizebar", style: { width: Math.max(2, 60 * sz / maxSz) + "px" } })] : "-"),
    h("td", { class: "num" }, m.count != null ? `${fmtNum(m.count)} ${m.count_unit || "条"}` : "-"),
    h("td", {}, m.version || "-"), h("td", {}, badge(n.status, NODE_ST)));
}

const ASSET_COLS = ["名称（含一句话说明）", "类型", "存放地址", "大小", "条数", "版本", "状态"];

/* 按 groups 分组的模块化清单：一组一块，块里是表格。layout="list" 时用（关系图看不清几十份数据时用它） */
export function datasetList(g, sel, key, maxSz) {
  const groups = (g.groups || []).filter((x) => (x.nodes || []).length);
  const used = new Set(groups.flatMap((x) => x.nodes));
  const buckets = [...groups.map((x) => ({ name: x.name, desc: x.desc, ids: x.nodes })),
                   ...(g.nodes.some((n) => !used.has(n.id))
                     ? [{ name: "未分组", desc: "", ids: g.nodes.filter((n) => !used.has(n.id)).map((n) => n.id) }] : [])];
  const nmap = new Map(g.nodes.map((n) => [n.id, n]));
  return buckets.map((b, i) => {
    const gk = "dg:" + g.id + ":" + b.name;
    return h("div", { class: "card dgroup" + (isFolded(gk) ? " folded" : ""), "data-key": "dg:" + i, "data-fade": 1 },
      h("div", { class: "g-head" }, foldBtn(gk), h("h3", {}, b.name), h("span", { class: "kv" }, `${b.ids.length} 份`),
        b.desc ? h("span", { class: "desc" }, "：" + b.desc) : null),
      foldBody(gk, [h("div", { class: "tbl-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ASSET_COLS.map((c) => h("th", {}, c)))),
        h("tbody", {}, b.ids.filter((id) => nmap.has(id)).map((id) => assetRow(g, nmap.get(id), sel, key, maxSz)))))]));
  });
}

export function datasetCard(g) {
  if (g.error) return h("div", { class: "card err", "data-key": "ds:" + g.id }, `${g.id}: ${g.error}`);
  const kind = "datasets", key = `${kind}/${g.id}`;
  const sel = S.sel[key] || null;
  const asList = (S.sel[`${key}#view`] || (g.layout === "list" ? "list" : "")) === "list";
  const { el, order } = asList ? { el: null, order: orderedNodes(g).map((n) => g.nodes.indexOf(n)) }
                               : drawGraph(kind, g, sel, key);
  const sized = g.nodes.map((n) => ({ n, sz: nodeSize(n) }));
  const total = sized.reduce((a, x) => a + (x.sz || 0), 0);
  const maxSz = Math.max(1, ...sized.map((x) => x.sz || 0));
  const byStore = new Map();
  for (const x of sized) {
    if (!x.sz) continue;
    const p = (x.n.locations || [])[0];
    const k = (p && p.storage) || (x.n.meta || {}).storage || "未标注";
    byStore.set(k, (byStore.get(k) || 0) + x.sz);
  }
  const stores = [...byStore.entries()].sort((a, b) => b[1] - a[1]);
  const issues = g.nodes.filter((n) => ["missing", "failed", "stale"].includes(n.status) || n.replica === "mismatch" || n.replica === "missing_copy");
  const noLoc = g.nodes.filter((n) => !(n.locations || []).length).length;
  const rows = order.map((i) => assetRow(g, g.nodes[i], sel, key, maxSz));
  const toggle = h("a", { class: "xlink", "data-key": "view-toggle", "data-act": "dsview", "data-a1": key,
    "data-a2": asList ? "graph" : "list", role: "button", tabindex: 0, title: "切换显示方式" },
    asList ? "切成关系图" : "切成分组清单");
  const fk = "ds:" + g.id;
  const overview = h("div", { class: "card" + (isFolded(fk) ? " folded" : ""), id: "ds-" + g.id, "data-key": "ds:" + g.id, "data-fade": 1 },
    h("div", { class: "g-head" }, foldBtn(fk), h("h3", {}, g.title || g.id), exTag(g), h("span", { class: "kv" }, `${g.nodes.length} 个数据资产`, total ? ` · 已知总量 ${fmtBytes(total)}` : ""),
      issues.length ? h("span", { class: "badge st-failed" }, `${issues.length} 项异常`) : null, noLoc ? h("span", { class: "badge stale" }, `${noLoc} 项未记录地址`) : null, toggle),
    foldBody(fk, [g.description ? h("p", { class: "desc" }, g.description) : null,
    stores.length ? h("div", { class: "mt" }, h("div", { class: "kv" }, "主存储位置占比"),
      h("div", { class: "stack" }, stores.map(([k, v], i) => h("span", { class: `sp-${i % 6}`, "data-key": "sp" + i, style: { width: (100 * v / total) + "%" }, title: `${k}: ${fmtBytes(v)}` }))),
      h("div", { class: "legend" }, stores.map(([k, v], i) => h("span", { "data-key": "sl" + i }, h("i", { class: `sp-${i % 6}` }), `${k} ${fmtBytes(v)}（${(100 * v / total).toFixed(1)}%）`)))) : null,
    legend(NODE_ST, ["ready", "building", "stale", "missing", "failed"])]));
  if (isFolded(fk)) return h("div", { class: asList ? "ds-list" : "ds-graph", "data-key": (asList ? "dsl:" : "dsg:") + g.id }, overview);
  if (asList) {
    return h("div", { class: "ds-list", "data-key": "dsl:" + g.id },
      overview,
      h("div", { class: "legend", "data-key": "dsltip" }, "按分组列出每一份数据：点任意一行看它的含义、来源、用处和存放地址。"),
      datasetList(g, sel, key, maxSz),
      dataNodeDetail(g, g.nodes.find((n) => n.id === sel)));
  }
  return h("div", { class: "ds-graph", "data-key": "dsg:" + g.id }, overview,
    (g.groups || []).some((x) => x.nodes.length) ? h("div", { class: "grp-legend" }, g.groups.filter((x) => x.nodes.length).map((x, i) => h("span", { class: `gl gc-${i % 5}`, "data-key": "gg" + i }, h("b", {}, x.name), x.desc ? "：" + x.desc : ""))) : null,
    el,
    h("div", { class: "legend" }, "图示：节点=数据资产，箭头=数据流向（箭头上的文字是加工过程），虚线流动=下游正在处理。点击节点查看地址、来源、用处和处理情况。"),
    h("div", { class: "cols" }, dataNodeDetail(g, g.nodes.find((n) => n.id === sel)),
      h("div", { class: "panel", "data-key": "list" }, h("h4", {}, "数据资产清单"), h("div", { class: "tbl-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ASSET_COLS.map((c) => h("th", {}, c)))), h("tbody", {}, rows))))));
}

/* ── 定制展示模块 ─────────────────────────────────────────────────────────── */
function getPath(obj, key) {
  let v = obj;
  for (const p of String(key).split(".")) { if (v == null) return undefined; v = v[p]; }
  return v;
}

function renderValue(v, type) {
  if (v == null) return h("span", { class: "id" }, "—");
  if ((type === "image" || type === "audio" || type === "video") && typeof v === "string") return mediaEl(v, type);
  if (typeof v === "string") return h("div", { class: type === "long" ? "longtext" : "text" }, v.length > 1200 ? v.slice(0, 1200) + "…" : v);
  if (typeof v === "number") return fmtNum(v);
  return h("div", { class: "longtext" }, clip(JSON.stringify(v), 600));
}

function blockEl(b, key) {
  switch (b.type) {
    case "heading": return h("h3", { class: "mh" }, b.text || "");
    case "text": return h("p", { class: "desc" }, b.text || "");
    case "kv": {
      const es = Array.isArray(b.items) ? b.items.map((x) => [x.k, x.v]) : Object.entries(b.data || {});
      return h("dl", { class: "meta" }, es.map(([k, v], i) => [h("dt", { "data-key": "k" + i }, String(k)), h("dd", { "data-key": "v" + i }, typeof v === "number" ? fmtNum(v) : String(v))]));
    }
    case "metrics": return h("div", { class: "mcards" }, asArr(b.items).map((m, i) => {
      const d = typeof m.delta === "number" ? m.delta : null;
      const good = d == null ? "" : ((d > 0) === (m.better !== "lower") ? "good" : "bad");
      return h("div", { class: "mcard", "data-key": "mc" + i }, h("div", { class: "mc-l" }, m.label || ""),
        h("div", { class: "mc-v" }, typeof m.value === "number" ? fmtNum(m.value) : String(m.value ?? "-"), m.unit ? h("small", {}, " " + m.unit) : null),
        d != null ? h("div", { class: `mc-d ${good}` }, (d > 0 ? "▲ +" : d < 0 ? "▼ " : "= ") + fmtNum(d)) : null, m.note ? h("div", { class: "id" }, m.note) : null);
    }));
    case "table": {
      const cols = asArr(b.columns), rows = asArr(b.rows);
      return h("div", { class: "tbl-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, String(c))))),
        h("tbody", {}, rows.slice(0, 200).map((r, i) => h("tr", { "data-key": "tr" + i }, asArr(r).map((c) => h("td", { class: typeof c === "number" ? "num" : "" }, typeof c === "number" ? fmtNum(c) : String(c ?? ""))))))));
    }
    case "chart": {
      if (b.kind === "bar") {
        const labels = asArr(b.labels), series = asArr(b.series);
        const max = typeof b.max === "number" ? b.max : Math.max(1e-9, ...series.flatMap((x) => asArr(x.values).map(Number)));
        return h("div", { class: "bars" }, b.title ? h("div", { class: "chart-title" }, b.title) : null, labels.map((lb, i) => h("div", { class: "brow", "data-key": "br" + i },
          h("div", { class: "bl" }, String(lb)), h("div", { class: "bgrp" }, series.map((sr, k) => {
            const v = Number(asArr(sr.values)[i] ?? 0);
            return h("div", { class: "bi", "data-key": "bi" + k }, bar(Math.max(0, Math.min(1, v / max)), "running", { sm: true }), h("span", { class: "bv" }, `${series.length > 1 ? sr.name + " " : ""}${fmtNum(v)}`));
          })))));
      }
      const ser = {};
      asArr(b.series).forEach((x, i) => { ser[x.name || "s" + i] = asArr(x.points).filter((p) => Array.isArray(p) && p.length === 2); });
      return lineChart(key, ser, { title: b.title }) || h("div", { class: "empty sm" }, "图表没有数据");
    }
    case "images": case "media":
      return h("div", { class: "gallery", style: { "grid-template-columns": `repeat(${Math.max(1, Math.min(6, b.columns || 3))}, minmax(0, 1fr))` } },
        asArr(b.items).map((it, i) => h("figure", { "data-key": "g" + i }, mediaEl(String(it.src || ""), it.type), it.caption ? h("figcaption", {}, it.caption) : null)));
    case "samples": {
      if (b.error) return noteLine(b.error);
      const items = asArr(b.items);
      let fields = asArr(b.fields);
      if (!fields.length && items[0] && typeof items[0] === "object") fields = Object.keys(items[0]).slice(0, 6).map((k) => ({ label: k, key: k }));
      return h("div", { class: "samples" }, items.map((r, i) => h("div", { class: "sample", "data-key": "sm" + i }, h("div", { class: "sidx" }, `#${(b.skip || 0) + i + 1}`),
        fields.map((f, k) => h("div", { class: "sf", "data-key": "sf" + k }, h("div", { class: "fk" }, f.label || f.key), renderValue(getPath(r, f.key), f.type))))));
    }
    case "heatmap": return heatmapBlock(b, key);
    case "flow": return flowBlock(b, key);
    case "compare": return h("div", {}, asArr(b.items).map((it, i) => h("div", { class: "cmp", "data-key": "cm" + i }, it.title ? h("div", { class: "chart-title" }, it.title) : null,
      h("div", { class: "cmp-row", style: { "grid-template-columns": `repeat(${Math.max(1, asArr(it.sides).length)}, minmax(0, 1fr))` } }, asArr(it.sides).map((sd, k) => h("div", { class: "cmp-side", "data-key": "sd" + k },
        h("div", { class: "fk" }, sd.label || ""), sd.image ? mediaEl(sd.image, "image") : null, sd.video ? mediaEl(sd.video, "video") : null, sd.audio ? mediaEl(sd.audio, "audio") : null,
        sd.text ? renderValue(sd.text, "long") : null))))));
    case "progress": return h("div", {}, asArr(b.items).map((it, i) => h("div", { class: "pct-row", "data-key": "pr" + i }, h("span", { class: "id", style: { minWidth: "150px" } }, it.label || ""),
      bar(typeof it.value === "number" ? Math.max(0, Math.min(1, it.value)) : null, "running", { sm: true }), h("span", { class: "pct" }, fmtPct(it.value)), it.note ? h("span", { class: "id" }, it.note) : null)));
    default: return h("div", { class: "err" }, `未知的模块类型: ${b.type}`);
  }
}

export function moduleCard(m) {
  if (m.error) return h("div", { class: "card err", "data-key": "md:" + m.id }, `${m.id}: ${m.error}`);
  const fk = "md:" + m.id;
  return h("div", { class: "card" + (isFolded(fk) ? " folded" : ""), id: "md-" + m.id, "data-key": "md:" + m.id, "data-fade": 1 }, h("h3", {}, foldBtn(fk), m.title || m.id, " ", exTag(m)),
    foldBody(fk, [m.description ? h("p", { class: "desc" }, m.description) : null,
    asArr(m.blocks).map((b, i) => {
      let el;
      try { el = blockEl(b, `${m.id}-${i}`); } catch (e) { el = h("div", { class: "err" }, `模块块渲染失败: ${e.message}`); }
      return h("div", { class: "blk", "data-key": "b" + i }, el);
    })]));
}
