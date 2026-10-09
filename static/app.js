"use strict";
/* 状态看板前端：所有文本都经 textContent / createTextNode 写入，不使用 innerHTML。 */

const SVGNS = "http://www.w3.org/2000/svg";
const POLL_MS = 2500;
const TASK_ST = { queued: "排队中", running: "运行中", done: "已完成", failed: "失败", cancelled: "已取消" };
const NODE_ST = { pending: "待执行", running: "运行中", done: "已完成", failed: "失败", skipped: "已跳过",
                  ready: "就绪", building: "生成中", missing: "缺失", stale: "已过期" };
const PIPE_KIND = { source: "数据源", process: "处理", model: "模型", train: "训练", eval: "评测", output: "产出", gate: "校验" };
const DATA_KIND = { raw: "原始数据", processed: "加工数据", derived: "衍生/结果", external: "外部数据", model: "模型/权重" };
const META_LABEL = { size_bytes: "大小", size: "大小", count: "条数", count_unit: "单位", format: "格式", version: "版本",
                     updated: "更新时间", storage: "存储位置", owner: "负责人", alias: "别名", path: "路径" };
const META_HIDE = new Set(["auto_stat", "count_unit"]);

let STATE = null;
let OFFLINE = false;
let LAST_OK = 0;
const SEL = {};          // "kind/gid" -> node id

/* ── DOM 辅助 ─────────────────────────────────────────────────────────────── */
function build(el, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const h = (tag, attrs, ...kids) => build(document.createElement(tag), attrs, kids);
const s = (tag, attrs, ...kids) => build(document.createElementNS(SVGNS, tag), attrs, kids);

/* ── 格式化 ───────────────────────────────────────────────────────────────── */
function fmtBytes(n) {
  if (n == null || isNaN(n)) return "-";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : String(n)) + " " + u[i];
}
function fmtDur(sec) {
  if (sec == null || !isFinite(sec)) return "-";
  sec = Math.round(sec);
  const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  if (d) return `${d}天${hh}时`;
  if (hh) return `${hh}时${m}分`;
  if (m) return `${m}分${ss}秒`;
  return `${ss}秒`;
}
const fmtNum = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : String(n));
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
const fmtPct = (p) => (p == null ? "--" : (p * 100).toFixed(p >= 0.9995 || p === 0 ? 0 : 1) + "%");
function metaVal(k, v) {
  if (k === "size_bytes") return fmtBytes(v);
  if (k === "updated" && typeof v === "number" && v > 1e9) return fmtTime(v);
  return typeof v === "number" ? fmtNum(v) : String(v);
}
function clip(str, maxUnits) {
  let w = 0, out = "";
  for (const ch of String(str ?? "")) {
    const cw = ch.charCodeAt(0) > 255 ? 2 : 1;
    if (w + cw > maxUnits) return out + "…";
    w += cw; out += ch;
  }
  return out;
}

/* ── 通用组件 ─────────────────────────────────────────────────────────────── */
const badge = (st, map) => h("span", { class: `badge st-${st}` }, map[st] || st);

function bar(p, status, opts = {}) {
  const indet = p == null && status === "running";
  return h("div", { class: `bar ${opts.sm ? "sm" : ""} ${indet ? "indet" : ""} st-${status} ${opts.stale ? "stale" : ""}` },
    h("div", { class: "fill", style: { width: indet ? "100%" : (p == null ? 0 : Math.round(p * 1000) / 10) + "%" } }));
}

function taskCard(t) {
  const kvs = [];
  if (t.total) kvs.push(h("span", {}, "进度 ", h("b", {}, `${fmtNum(Math.round(t.done || 0))} / ${fmtNum(t.total)}${t.unit ? " " + t.unit : ""}`)));
  kvs.push(h("span", {}, "已用 ", h("b", {}, fmtDur(t.elapsed))));
  if (t.status === "running") kvs.push(h("span", {}, "预计剩余 ", h("b", {}, fmtDur(t.eta))));
  if (t.rate) kvs.push(h("span", {}, "速率 ", h("b", {}, `${t.rate.toFixed(t.rate < 10 ? 2 : 0)}${t.unit ? " " + t.unit : ""}/s`)));
  if (t.pipeline) kvs.push(h("span", {}, "所属 ", h("b", {}, `${t.pipeline}${t.node ? " / " + t.node : ""}`)));
  const card = h("div", { class: `card task st-${t.status}` },
    h("div", { class: "task-head" },
      h("span", { class: "name", title: t.name || t.id }, t.name || t.id),
      t.stale ? h("span", { class: "badge stale" }, "疑似停滞") : null,
      badge(t.status, TASK_ST)),
    h("div", { class: "id" }, t.id),
    h("div", { class: "pct-row" }, bar(t.progress, t.status, { stale: t.stale }), h("span", { class: "pct" }, fmtPct(t.progress))),
    h("div", { class: "kv" }, kvs),
    t.stale ? h("div", { class: "warn" }, `已 ${fmtDur(t.idle)} 没有收到心跳，进程可能已卡死或退出`) : null,
    t.message ? h("div", { class: "msg" }, t.message) : null,
    t.shards && t.shards.length ? h("div", { class: "shards" }, t.shards.map((sh) =>
      h("div", { class: "shard" }, h("span", {}, sh.name || "分片"),
        bar(sh.progress, sh.status || (sh.progress >= 1 ? "done" : t.status), { sm: true }), h("span", {}, fmtPct(sh.progress))))) : null,
    t.metrics && Object.keys(t.metrics).length ? h("div", { class: "metrics" },
      Object.entries(t.metrics).map(([k, v]) => h("span", { class: "metric" }, `${k}: ${typeof v === "number" ? fmtNum(+v.toPrecision(6)) : v}`))) : null);
  return card;
}

/* ── 图布局 ───────────────────────────────────────────────────────────────── */
function layout(nodes, edges, W, H, GX, GY) {
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const es = edges.filter((e) => idx.has(e.from) && idx.has(e.to) && e.from !== e.to);
  const layer = nodes.map(() => 0);
  for (let it = 0; it < nodes.length; it++) {
    let changed = false;
    for (const e of es) {
      const a = idx.get(e.from), b = idx.get(e.to);
      if (layer[b] < layer[a] + 1 && layer[a] + 1 < nodes.length) { layer[b] = layer[a] + 1; changed = true; }
    }
    if (!changed) break;
  }
  const cols = [];
  nodes.forEach((_, i) => { (cols[layer[i]] = cols[layer[i]] || []).push(i); });
  const cs = cols.filter(Boolean);
  const row = new Array(nodes.length).fill(0);
  cs.forEach((c) => c.forEach((i, r) => { row[i] = r; }));
  for (let sweep = 0; sweep < 3; sweep++) {
    for (let c = 1; c < cs.length; c++) {
      const bary = (i) => {
        const ps = es.filter((e) => idx.get(e.to) === i).map((e) => row[idx.get(e.from)]);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : row[i];
      };
      cs[c].sort((a, b) => bary(a) - bary(b));
      cs[c].forEach((i, r) => { row[i] = r; });
    }
  }
  const maxRows = Math.max(1, ...cs.map((c) => c.length));
  const totalH = maxRows * H + (maxRows - 1) * GY;
  const pos = new Array(nodes.length), order = [];
  cs.forEach((c, ci) => c.forEach((i, r) => {
    const colH = c.length * H + (c.length - 1) * GY;
    pos[i] = { x: ci * (W + GX), y: (totalH - colH) / 2 + r * (H + GY) };
    order.push(i);
  }));
  return { pos, order, es, idx, width: Math.max(1, cs.length) * W + (cs.length - 1) * GX, height: totalH };
}

/* ── 图渲染 ───────────────────────────────────────────────────────────────── */
function nodeSize(n) {
  const m = n.meta || {};
  if (n.stat && n.stat.size != null) return n.stat.size;
  return typeof m.size_bytes === "number" ? m.size_bytes : null;
}

function drawNode(kind, g, n, p, num, selected, W, H, onSel) {
  const isData = kind === "datasets";
  const m = n.meta || {};
  const cls = `node st-${n.status} ${selected ? "sel" : ""} ${n.stale ? "stale" : ""}`;
  const grp = s("g", { class: cls, transform: `translate(${p.x},${p.y})`, tabindex: 0, role: "button",
                       "aria-label": n.name || n.id, onclick: onSel,
                       onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSel(); } } },
    s("title", {}, `${n.name || n.id}${n.desc ? "\n" + n.desc : ""}`),
    s("rect", { class: "box", width: W, height: H, rx: 10 }),
    s("rect", { class: "stripe", width: 6, height: H, rx: 3 }));
  if (isData) {
    const sz = nodeSize(n);
    const bits = [];
    if (sz != null) bits.push(fmtBytes(sz)); else if (m.size) bits.push(String(m.size));
    if (m.count != null) bits.push(`${fmtNum(m.count)} ${m.count_unit || "条"}`);
    if (n.stat && n.stat.pending) bits.push("统计中…");
    grp.append(
      s("ellipse", { class: "ico", cx: 28, cy: 20, rx: 11, ry: 4.5 }),
      s("path", { class: "ico", d: "M17 20v26c0 2.6 5 4.7 11 4.7s11-2.1 11-4.7V20M17 33c0 2.6 5 4.7 11 4.7s11-2.1 11-4.7" }),
      s("text", { class: "t1", x: 50, y: 26 }, clip(n.name || n.id, 24)),
      s("text", { class: "t2", x: 50, y: 44 }, clip(bits.join(" · ") || (DATA_KIND[n.kind] || n.kind || ""), 28)),
      s("text", { class: "t3", x: 50, y: 62 }, clip(`${NODE_ST[n.status] || n.status}${m.storage ? " · " + m.storage : ""}${m.version ? " · " + m.version : ""}`, 28)));
  } else {
    const pr = n.progress;
    const sub = n.status === "running" && pr != null ? `${NODE_ST.running} · ${fmtPct(pr)}`
      : `${NODE_ST[n.status] || n.status}${n.kind ? " · " + (PIPE_KIND[n.kind] || n.kind) : ""}${n.stale ? " · 疑似停滞" : ""}`;
    grp.append(
      s("circle", { class: "idx", cx: 22, cy: 24, r: 10 }),
      s("text", { class: "idxt", x: 22, y: 28 }, String(num)),
      s("text", { class: "t1", x: 40, y: 28 }, clip(n.name || n.id, 20)),
      s("text", { class: "t3", x: 40, y: 47 }, clip(sub, 24)));
    if (pr != null || n.status === "running") {
      grp.append(s("rect", { class: "ptrack", x: 14, y: H - 14, width: W - 28, height: 6, rx: 3 }),
        s("rect", { class: "pfill", x: 14, y: H - 14, width: Math.max(0, (W - 28) * (pr || 0)), height: 6, rx: 3 }));
    }
  }
  return grp;
}

function drawGraph(kind, g, selId, onSel) {
  const isData = kind === "datasets";
  const W = isData ? 220 : 190, H = isData ? 70 : 76, GX = isData ? 80 : 70, GY = 26, PAD = 14;
  const nodes = g.nodes, L = layout(nodes, g.edges || [], W, H, GX, GY);
  const vw = L.width + PAD * 2, vh = L.height + PAD * 2;
  const svg = s("svg", { class: "graph", viewBox: `0 0 ${vw} ${vh}`, width: vw, height: vh, role: "img", "aria-label": g.title || g.id },
    s("defs", {}, ["", "-on", "-run"].map((sfx, i) => s("marker", { id: `ah${sfx}-${kind}-${g.id}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      s("path", { d: "M0 0L10 5L0 10z", fill: ["#8a93a3", "#1f9d55", "#2f6fed"][i] })))));
  const gEdges = s("g", {}), gNodes = s("g", {});
  svg.append(gEdges, gNodes);
  const off = (p) => ({ x: p.x + PAD, y: p.y + PAD });
  for (const e of L.es) {
    const a = L.idx.get(e.from), b = L.idx.get(e.to);
    const pa = off(L.pos[a]), pb = off(L.pos[b]);
    const x1 = pa.x + W, y1 = pa.y + H / 2, x2 = pb.x, y2 = pb.y + H / 2;
    const dx = Math.max(30, Math.abs(x2 - x1) / 2);
    const sa = nodes[a].status, sb = nodes[b].status;
    const mode = sb === "running" && (sa === "done" || sa === "ready" || sa === "running") ? "run" : (sa === "done" || sa === "ready") ? "on" : "";
    gEdges.append(s("path", { class: `edge ${mode}`, d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`,
      "marker-end": `url(#ah${mode ? "-" + mode : ""}-${kind}-${g.id})` }));
    if (e.label) gEdges.append(s("text", { class: "elabel", x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 5 }, clip(e.label, 18)));
  }
  const num = new Map(L.order.map((i, k) => [i, k + 1]));
  nodes.forEach((n, i) => {
    const p = off(L.pos[i]);
    gNodes.append(drawNode(kind, g, n, p, num.get(i), n.id === selId, W, H, () => onSel(n.id)));
  });
  return { svg, order: L.order };
}

/* ── 节点详情 ─────────────────────────────────────────────────────────────── */
function relLinks(g, nid, onSel) {
  const nm = new Map(g.nodes.map((n) => [n.id, n]));
  const mk = (ids) => ids.map((i) => h("a", { onclick: () => onSel(i) }, nm.get(i).name || i));
  const ups = (g.edges || []).filter((e) => e.to === nid && nm.has(e.from)).map((e) => e.from);
  const downs = (g.edges || []).filter((e) => e.from === nid && nm.has(e.to)).map((e) => e.to);
  return h("div", { class: "links" },
    ups.length ? h("div", {}, "上游：", mk(ups)) : null,
    downs.length ? h("div", {}, "下游：", mk(downs)) : null);
}

function nodeDetail(kind, g, n, onSel) {
  if (!n) return h("div", { class: "panel" }, h("h4", {}, "节点详情"), h("span", { class: "desc" }, "点击图中的节点或右侧列表查看说明。"));
  const m = n.meta || {};
  const rows = [];
  if (n.kind) rows.push(["类型", (kind === "datasets" ? DATA_KIND : PIPE_KIND)[n.kind] || n.kind]);
  if (n.stat) {
    if (n.stat.pending) rows.push(["实测大小", "统计中…"]);
    else if (n.stat.missing) rows.push(["实测大小", "路径不存在"]);
    else if (n.stat.error) rows.push(["实测大小", "统计失败：" + n.stat.error]);
    else rows.push(["实测大小", `${fmtBytes(n.stat.size)}，${fmtNum(n.stat.files)} 个文件${n.stat.truncated ? "（统计超时，仅为下限）" : ""}；${fmtTime(n.stat.ts)}`]);
  }
  for (const [k, v] of Object.entries(m)) {
    if (META_HIDE.has(k) || v == null || v === "") continue;
    if (k === "count") rows.push([META_LABEL.count, `${fmtNum(v)} ${m.count_unit || "条"}`]);
    else rows.push([META_LABEL[k] || k, metaVal(k, v)]);
  }
  const tasks = (n.tasks || []).map((id) => STATE.tasks.find((t) => t.id === id)).filter(Boolean);
  return h("div", { class: "panel" },
    h("h4", {}, "节点详情"),
    h("div", {}, h("span", { class: "ttl" }, n.name || n.id), badge(n.status, NODE_ST), n.stale ? h("span", { class: "badge stale", style: { marginLeft: "6px" } }, "疑似停滞") : null),
    n.desc ? h("p", { class: "desc", style: { marginBottom: "4px" } }, n.desc) : null,
    n.note ? h("div", { class: "msg" }, "备注：" + n.note) : null,
    n.progress != null ? h("div", { class: "pct-row" }, bar(n.progress, n.status, { sm: true }), h("span", { class: "pct" }, fmtPct(n.progress))) : null,
    rows.length ? h("dl", { class: "meta" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : null,
    tasks.length ? h("div", { class: "mt" }, tasks.map((t) => h("div", { class: "pct-row" },
      h("span", { class: "id", style: { minWidth: "90px" } }, t.name || t.id), bar(t.progress, t.status, { sm: true, stale: t.stale }), h("span", { class: "pct" }, fmtPct(t.progress))))) : null,
    relLinks(g, n.id, onSel));
}

/* ── Pipeline 区块 ────────────────────────────────────────────────────────── */
function legend(map, keys) {
  return h("div", { class: "legend" }, keys.map((k) => h("span", { class: `st-${k}` }, h("i"), map[k])));
}

function graphScroll(key, svg) {
  return h("div", { class: "graph-scroll", "data-key": key }, svg);
}

function pipelineCard(g) {
  const kind = "pipelines", key = `${kind}/${g.id}`;
  if (g.error) return h("div", { class: "card err" }, g.error);
  const sel = SEL[key] || (g.nodes.find((n) => n.status === "running") || {}).id || null;
  const onSel = (id) => { SEL[key] = id; render(); };
  const { svg, order } = drawGraph(kind, g, sel, onSel);
  const doneN = g.nodes.filter((n) => n.status === "done" || n.status === "skipped").length;
  const prog = g.nodes.length ? g.nodes.reduce((a, n) => a + (n.status === "done" || n.status === "skipped" ? 1 : (n.status === "running" ? (n.progress || 0) : 0)), 0) / g.nodes.length : null;
  const anyRun = g.nodes.some((n) => n.status === "running");
  const anyFail = g.nodes.some((n) => n.status === "failed");
  const st = anyFail ? "failed" : anyRun ? "running" : (prog === 1 ? "done" : "pending");
  const steps = h("ol", { class: "steps" }, order.map((i, k) => {
    const n = g.nodes[i];
    return h("li", { class: `st-${n.status} ${n.id === sel ? "sel" : ""}`, onclick: () => onSel(n.id) },
      h("span", { class: "n" }, k + 1),
      h("div", {}, h("div", { class: "sn" }, n.name || n.id), n.desc ? h("div", { class: "sd" }, n.desc) : null),
      badge(n.status, NODE_ST));
  }));
  return h("div", { class: "card" },
    h("div", { class: "g-head" }, h("h3", {}, g.title || g.id), badge(st, NODE_ST),
      h("span", { class: "kv" }, `${doneN} / ${g.nodes.length} 步完成`), bar(prog, st, { sm: true }), h("span", { class: "pct" }, fmtPct(prog))),
    g.description ? h("p", { class: "desc" }, g.description) : null,
    graphScroll(key, svg),
    legend(NODE_ST, ["pending", "running", "done", "failed", "skipped"]),
    h("div", { class: "cols" }, nodeDetail(kind, g, g.nodes.find((n) => n.id === sel), onSel),
      h("div", { class: "panel" }, h("h4", {}, "步骤说明（按执行顺序）"), steps)));
}

/* ── 数据管理区块 ─────────────────────────────────────────────────────────── */
function datasetCard(g) {
  const kind = "datasets", key = `${kind}/${g.id}`;
  if (g.error) return h("div", { class: "card err" }, g.error);
  const sel = SEL[key] || null;
  const onSel = (id) => { SEL[key] = id; render(); };
  const { svg, order } = drawGraph(kind, g, sel, onSel);
  const sized = g.nodes.map((n) => ({ n, sz: nodeSize(n) }));
  const total = sized.reduce((a, x) => a + (x.sz || 0), 0);
  const maxSz = Math.max(1, ...sized.map((x) => x.sz || 0));
  const byStore = new Map();
  for (const x of sized) {
    if (!x.sz) continue;
    const k = (x.n.meta || {}).storage || "未标注";
    byStore.set(k, (byStore.get(k) || 0) + x.sz);
  }
  const stores = [...byStore.entries()].sort((a, b) => b[1] - a[1]);
  const issues = g.nodes.filter((n) => ["missing", "failed", "stale"].includes(n.status));
  const rows = order.map((i) => {
    const n = g.nodes[i], m = n.meta || {}, sz = nodeSize(n);
    return h("tr", { class: `click ${n.id === sel ? "sel" : ""}`, onclick: () => onSel(n.id) },
      h("td", {}, h("b", {}, n.name || n.id)),
      h("td", {}, DATA_KIND[n.kind] || n.kind || "-"),
      h("td", {}, m.storage ? h("span", { class: "tag" }, m.storage) : "-"),
      h("td", { class: "num" }, sz != null ? [fmtBytes(sz), " ", h("span", { class: "sizebar", style: { width: Math.max(2, 60 * sz / maxSz) + "px" } })] : (m.size || "-")),
      h("td", { class: "num" }, m.count != null ? `${fmtNum(m.count)} ${m.count_unit || "条"}` : "-"),
      h("td", {}, m.version || "-"),
      h("td", {}, m.updated != null ? metaVal("updated", m.updated) : "-"),
      h("td", {}, badge(n.status, NODE_ST)));
  });
  return h("div", { class: "card" },
    h("div", { class: "g-head" }, h("h3", {}, g.title || g.id),
      h("span", { class: "kv" }, `${g.nodes.length} 个数据资产`, total ? ` · 已知总量 ${fmtBytes(total)}` : ""),
      issues.length ? h("span", { class: "badge st-failed" }, `${issues.length} 项异常`) : null),
    g.description ? h("p", { class: "desc" }, g.description) : null,
    graphScroll(key, svg),
    h("div", { class: "legend" }, "图示：节点=数据资产，箭头=数据流向（箭头标注为加工过程），虚线流动=下游正在处理。"),
    legend(NODE_ST, ["ready", "building", "stale", "missing", "failed"]),
    stores.length ? h("div", { class: "mt" }, h("div", { class: "kv" }, "存储位置占比"),
      h("div", { class: "stack" }, stores.map(([k, v], i) => h("span", { class: `sp-${i % 6}`, style: { width: (100 * v / total) + "%" }, title: `${k}: ${fmtBytes(v)}` }))),
      h("div", { class: "legend" }, stores.map(([k, v], i) => h("span", {}, h("i", { class: `sp-${i % 6}`, style: { background: "" } }), `${k} ${fmtBytes(v)}（${(100 * v / total).toFixed(1)}%）`)))) : null,
    h("div", { class: "cols" }, nodeDetail(kind, g, g.nodes.find((n) => n.id === sel), onSel),
      h("div", { class: "panel" }, h("h4", {}, "数据资产清单"),
        h("div", { class: "tbl-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["名称", "类型", "存储", "大小", "条数", "版本", "更新", "状态"].map((c) => h("th", {}, c)))),
          h("tbody", {}, rows))))));
}

/* ── 页面 ─────────────────────────────────────────────────────────────────── */
function emptyHint(kind) {
  return h("div", { class: "empty" }, kind === "pipelines"
    ? ["暂无 Pipeline。在 ", h("code", {}, "state/pipelines/<id>.json"), " 放入定义文件即可显示（格式见 README.md）。"]
    : ["暂无数据资产图。在 ", h("code", {}, "state/datasets/<id>.json"), " 放入定义文件即可显示（格式见 README.md）。"]);
}

function render() {
  if (!STATE) return;
  const scrolls = {};
  document.querySelectorAll(".graph-scroll").forEach((el) => { scrolls[el.dataset.key] = el.scrollLeft; });
  const S = STATE, sm = S.summary;
  document.title = S.title;
  const running = S.tasks.filter((t) => t.status === "running" || t.status === "queued");
  const others = S.tasks.filter((t) => !(t.status === "running" || t.status === "queued"));
  const chip = (label, n, st) => h("span", { class: `chip st-${st}` }, h("b", {}, n), label);

  const header = h("header", { class: "top" },
    h("div", { class: "top-row" }, h("h1", {}, S.title), S.subtitle ? h("span", { class: "subtitle" }, S.subtitle) : null,
      h("span", { class: "spacer" }),
      h("span", { class: "live" }, h("span", { class: `dot ${OFFLINE ? "off" : ""}` }), OFFLINE ? "连接中断，显示的是旧数据" : `实时 · 更新于 ${fmtTime(LAST_OK / 1000)}`),
      h("nav", {}, h("a", { href: "#tasks" }, "任务"), h("a", { href: "#pipelines" }, "Pipeline"), h("a", { href: "#datasets" }, "数据管理"))),
    h("div", { class: "chips" }, chip("运行中", sm.running, "running"), chip("排队", sm.queued, "queued"), chip("已完成", sm.done, "done"),
      chip("失败", sm.failed, "failed"), chip("疑似停滞", sm.stale, "stale")));

  const secTasks = h("section", { id: "tasks" }, h("h2", {}, "运行中的任务", h("small", {}, "进度条每 2.5 秒刷新；超过设定时间没有心跳会标为疑似停滞")),
    running.length ? h("div", { class: "tasks" }, running.map(taskCard)) : h("div", { class: "empty" }, "当前没有运行中的任务。"));

  const secPipes = h("section", { id: "pipelines" }, h("h2", {}, "Pipeline 结构图"),
    S.pipelines.length ? S.pipelines.map(pipelineCard) : emptyHint("pipelines"));
  const secData = h("section", { id: "datasets" }, h("h2", {}, "数据管理图"),
    S.datasets.length ? S.datasets.map(datasetCard) : emptyHint("datasets"));

  const hist = h("section", { id: "history" }, h("h2", {}, "历史与其他任务", h("small", {}, `${others.length} 条`)),
    others.length ? h("div", { class: "card" }, h("div", { class: "tbl-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["任务", "状态", "进度", "耗时", "最后更新", "说明"].map((c) => h("th", {}, c)))),
      h("tbody", {}, others.slice(0, 50).map((t) => h("tr", {},
        h("td", {}, h("b", {}, t.name || t.id), " ", h("span", { class: "id" }, t.id)), h("td", {}, badge(t.status, TASK_ST)),
        h("td", { style: { minWidth: "140px" } }, bar(t.progress, t.status, { sm: true }), " ", fmtPct(t.progress)),
        h("td", { class: "num" }, fmtDur(t.elapsed)), h("td", { class: "num" }, fmtTime(t.updated_at)),
        h("td", {}, (t.message || "").slice(0, 80)))))))) : h("div", { class: "empty" }, "暂无历史任务。"));

  document.getElementById("app").replaceChildren(
    h("div", { class: "wrap" }, header, OFFLINE ? h("div", { class: "banner" }, "无法连接看板服务，正在重试…") : null, secTasks, secPipes, secData, hist));
  document.querySelectorAll(".graph-scroll").forEach((el) => { if (scrolls[el.dataset.key]) el.scrollLeft = scrolls[el.dataset.key]; });
}

async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    STATE = await r.json();
    OFFLINE = false; LAST_OK = Date.now();
  } catch (e) {
    OFFLINE = true;
  }
  render();
  setTimeout(poll, document.hidden ? POLL_MS * 4 : POLL_MS);
}
poll();
